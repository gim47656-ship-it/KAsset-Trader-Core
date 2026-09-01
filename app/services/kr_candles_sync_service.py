from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.manual_holdings import MarketType
from app.services.candles_sync_common import (
    SyncTableConfig,
    build_cursor_sql,
    build_symbol_union,
    build_upsert_sql,
    normalize_mode,
    parse_float,
    read_cursor_utc,
)
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.market_data.toss_ohlcv import fetch_kr_intraday_toss_frame
from app.services.market_events.session_calendar import (
    is_trading_session,
    trading_sessions_in_range,
)
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_OVERLAP_MINUTES = 5
_DEFAULT_BOOTSTRAP_SESSIONS = 10
_TABLE_CFG = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
_CURSOR_SQL = build_cursor_sql(_TABLE_CFG)
_UPSERT_SQL = build_upsert_sql(_TABLE_CFG)


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    time_utc: datetime
    local_time: datetime
    symbol: str
    venue: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


def _normalize_symbol(value: object) -> str | None:
    text_value = str(value or "").strip().upper()
    if not text_value:
        return None
    if len(text_value) < 6:
        text_value = text_value.zfill(6)
    if len(text_value) == 6 and text_value.isalnum():
        return text_value
    return None


def _validate_universe_rows(
    *,
    target_symbols: set[str],
    universe_rows: list[KRSymbolUniverse],
    table_has_rows: bool,
) -> dict[str, KRSymbolUniverse]:
    if not table_has_rows:
        raise ValueError("kr_symbol_universe is empty")
    rows_by_symbol = {row.symbol: row for row in universe_rows}
    missing = sorted(target_symbols - set(rows_by_symbol))
    if missing:
        preview = ", ".join(missing[:10])
        raise ValueError(
            "KR symbol is not registered in kr_symbol_universe: "
            f"count={len(missing)} symbols=[{preview}]"
        )
    inactive = sorted(
        symbol
        for symbol in target_symbols
        if symbol in rows_by_symbol and not rows_by_symbol[symbol].is_active
    )
    if inactive:
        preview = ", ".join(inactive[:10])
        raise ValueError(
            "KR symbol is inactive in kr_symbol_universe: "
            f"count={len(inactive)} symbols=[{preview}]"
        )
    return {symbol: rows_by_symbol[symbol] for symbol in target_symbols}


def _recent_session_days(
    now_kst: datetime,
    sessions: int,
    *,
    include_today: bool,
) -> list[date]:
    lookback_days = max(90, sessions * 8)
    days = trading_sessions_in_range(
        "kr",
        now_kst.date() - timedelta(days=lookback_days),
        now_kst.date(),
    )
    if not include_today and days and days[-1] == now_kst.date():
        days = days[:-1]
    return days[-sessions:]


def _partition_for_minute(local_dt: datetime, *, nxt_eligible: bool) -> str:
    if not nxt_eligible:
        return "KRX"
    clock = local_dt.time()
    if clock < time(9, 0) or clock >= time(15, 30):
        return "NTX"
    return "KRX"


def _normalize_toss_rows(
    *,
    frame: pd.DataFrame,
    symbol: str,
    nxt_eligible: bool,
    allowed_days: set[date],
    cutoff_kst: datetime,
    now_kst: datetime,
) -> list[MinuteCandleRow]:
    if frame.empty:
        return []
    rows: dict[tuple[datetime, str], MinuteCandleRow] = {}
    for item in frame.to_dict("records"):
        raw_datetime = item.get("datetime")
        if raw_datetime is None:
            continue
        parsed = pd.to_datetime(raw_datetime, errors="coerce")
        if pd.isna(parsed):
            continue
        parsed_dt = parsed.to_pydatetime()
        local_dt = (
            parsed_dt.replace(tzinfo=_KST)
            if parsed_dt.tzinfo is None
            else parsed_dt.astimezone(_KST)
        ).replace(second=0, microsecond=0)
        if local_dt.date() not in allowed_days or local_dt < cutoff_kst:
            continue
        clock = local_dt.time()
        if nxt_eligible:
            if clock < time(8, 0) or clock >= time(20, 0):
                continue
        elif clock < time(9, 0) or clock >= time(15, 30):
            continue
        if (
            local_dt.date() == now_kst.date()
            and local_dt + timedelta(minutes=1) > now_kst
        ):
            continue

        values = [
            parse_float(item.get(key))
            for key in ("open", "high", "low", "close", "volume", "value")
        ]
        if any(value is None for value in values):
            continue
        open_value, high_value, low_value, close_value, volume_value, value_value = (
            cast(list[float], values)
        )
        venue = _partition_for_minute(local_dt, nxt_eligible=nxt_eligible)
        time_utc = local_dt.astimezone(UTC)
        rows[(time_utc, venue)] = MinuteCandleRow(
            time_utc=time_utc,
            local_time=local_dt,
            symbol=symbol,
            venue=venue,
            open=float(open_value),
            high=float(high_value),
            low=float(low_value),
            close=float(close_value),
            volume=float(volume_value),
            value=float(value_value),
        )
    return [rows[key] for key in sorted(rows)]


async def _upsert_rows(session: AsyncSession, rows: list[MinuteCandleRow]) -> int:
    if not rows:
        return 0
    payload = [
        {
            "time": row.time_utc,
            "symbol": row.symbol,
            "venue": row.venue,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "value": row.value,
        }
        for row in rows
    ]
    await session.execute(_UPSERT_SQL, payload)
    return len(payload)


async def _load_universe_context(
    session: AsyncSession,
    target_symbols: set[str],
) -> tuple[list[KRSymbolUniverse], bool]:
    has_rows_result = await session.execute(select(KRSymbolUniverse.symbol).limit(1))
    table_has_rows = has_rows_result.scalar_one_or_none() is not None
    if not target_symbols:
        return [], table_has_rows
    result = await session.execute(
        select(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(target_symbols))
    )
    return list(result.scalars().all()), table_has_rows


def _symbol_session_open(now_kst: datetime, *, nxt_eligible: bool) -> bool:
    if not is_trading_session("kr", now_kst.date()):
        return False
    clock = now_kst.time()
    if nxt_eligible:
        return time(8, 0) <= clock < time(20, 0)
    return time(9, 0) <= clock < time(15, 30)


async def _cursor_cutoff(
    session: AsyncSession,
    *,
    symbol: str,
    nxt_eligible: bool,
) -> datetime | None:
    venues = ("KRX", "NTX") if nxt_eligible else ("KRX",)
    cursors = [
        await read_cursor_utc(
            session,
            _CURSOR_SQL,
            {"symbol": symbol, "venue": venue},
        )
        for venue in venues
    ]
    if any(cursor is None for cursor in cursors):
        return None
    normalized = [
        cursor.replace(tzinfo=UTC) if cursor.tzinfo is None else cursor.astimezone(UTC)
        for cursor in cast(list[datetime], cursors)
    ]
    return min(normalized).astimezone(_KST) - timedelta(minutes=_OVERLAP_MINUTES)


async def _sync_symbol(
    *,
    session: AsyncSession,
    symbol: str,
    nxt_eligible: bool,
    mode: Literal["incremental", "backfill"],
    session_count: int,
    now_kst: datetime,
) -> tuple[int, int]:
    session_end = time(20, 0) if nxt_eligible else time(15, 30)
    if mode == "backfill":
        allowed_days = _recent_session_days(
            now_kst,
            session_count,
            include_today=now_kst.time() >= session_end,
        )
        if not allowed_days:
            return 0, 0
        cutoff_kst = datetime.combine(
            allowed_days[0],
            time(8, 0) if nxt_eligible else time(9, 0),
            tzinfo=_KST,
        )
        provider_end = datetime.combine(
            allowed_days[-1],
            session_end,
            tzinfo=_KST,
        )
    else:
        cutoff_kst = await _cursor_cutoff(
            session,
            symbol=symbol,
            nxt_eligible=nxt_eligible,
        )
        if cutoff_kst is None:
            allowed_days = _recent_session_days(
                now_kst,
                _DEFAULT_BOOTSTRAP_SESSIONS,
                include_today=True,
            )
            if not allowed_days:
                return 0, 0
            cutoff_kst = datetime.combine(
                allowed_days[0],
                time(8, 0) if nxt_eligible else time(9, 0),
                tzinfo=_KST,
            )
        else:
            allowed_days = trading_sessions_in_range(
                "kr",
                cutoff_kst.date(),
                now_kst.date(),
            )
        provider_end = now_kst

    request_count = max(
        len(allowed_days) * (720 if nxt_eligible else 390) + _OVERLAP_MINUTES,
        60,
    )
    frame = await fetch_kr_intraday_toss_frame(
        symbol=symbol,
        period="1m",
        count=request_count,
        end_date=provider_end,
    )
    rows = _normalize_toss_rows(
        frame=frame,
        symbol=symbol,
        nxt_eligible=nxt_eligible,
        allowed_days=set(allowed_days),
        cutoff_kst=cutoff_kst,
        now_kst=now_kst,
    )
    return await _upsert_rows(session, rows), 1


async def sync_kr_candles(
    *,
    mode: str,
    sessions: int = 10,
    user_id: int = 1,
    source: str = "toss",
) -> dict[str, object]:
    normalized_mode = normalize_mode(mode)
    session_count = max(int(sessions), 1)
    normalized_source = str(source or "toss").strip().lower()
    if normalized_source != "toss":
        raise ValueError("source is non-operational; source must be 'toss'")
    now_kst = datetime.now(_KST)

    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        # 전역 Toss 자격증명은 이 user_id의 단일 운영자 계좌와 같다는 배포 계약이다.
        # 다중 사용자 계좌 매핑이 생기면 이 전제를 제거하고 명시적 계정 스코프가 필요하다.
        try:
            snapshot = await fetch_toss_portfolio_snapshot(
                need_sellable=False,
                need_cash=False,
            )
            toss_positions = [
                position
                for position in snapshot.positions
                if position.instrument_type == "equity_kr"
            ]
            if snapshot.errors:
                logger.warning(
                    "Toss holdings snapshot reported errors: %s", snapshot.errors
                )
        except Exception as exc:
            logger.warning("Toss holdings scan skipped: %s", exc)
            toss_positions = []

        manual_holdings = await ManualHoldingsService(session).get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.KR,
        )
        target_symbols = build_symbol_union(
            toss_positions,
            manual_holdings,
            holdings_field="symbol",
            normalize_fn=_normalize_symbol,
        )
        if not target_symbols:
            return {
                "mode": normalized_mode,
                "sessions": session_count,
                "skipped": True,
                "reason": "no_target_symbols",
                "symbols_total": 0,
                "symbol_venues_total": 0,
                "pairs_processed": 0,
                "pairs_skipped": 0,
                "rows_upserted": 0,
                "pages_fetched": 0,
                "source": "toss",
            }

        universe_rows, table_has_rows = await _load_universe_context(
            session,
            target_symbols,
        )
        rows_by_symbol = _validate_universe_rows(
            target_symbols=target_symbols,
            universe_rows=universe_rows,
            table_has_rows=table_has_rows,
        )

        rows_upserted = 0
        requests_fetched = 0
        pairs_processed = 0
        pairs_skipped = 0
        skipped_reasons: dict[str, int] = {}
        for symbol in sorted(rows_by_symbol):
            universe = rows_by_symbol[symbol]
            if normalized_mode == "incremental" and not _symbol_session_open(
                now_kst,
                nxt_eligible=universe.nxt_eligible,
            ):
                pairs_skipped += 1
                skipped_reasons["outside_session"] = (
                    skipped_reasons.get("outside_session", 0) + 1
                )
                continue
            try:
                row_count, request_count = await _sync_symbol(
                    session=session,
                    symbol=symbol,
                    nxt_eligible=universe.nxt_eligible,
                    mode=normalized_mode,
                    session_count=session_count,
                    now_kst=now_kst,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            rows_upserted += row_count
            requests_fetched += request_count
            pairs_processed += 1

        return {
            "mode": normalized_mode,
            "sessions": session_count,
            "skipped": pairs_processed == 0,
            "skip_reasons": skipped_reasons,
            "symbols_total": len(target_symbols),
            "symbol_venues_total": sum(
                2 if row.nxt_eligible else 1 for row in rows_by_symbol.values()
            ),
            "pairs_processed": pairs_processed,
            "pairs_skipped": pairs_skipped,
            "rows_upserted": rows_upserted,
            "pages_fetched": requests_fetched,
            "source": "toss",
            "warnings": [
                "Toss 통합 캔들의 venue='KRX'/'NTX' 값은 세션별 호환 파티션이며 공급자 venue 근거가 아닙니다."
            ],
        }
    finally:
        await session.close()


__all__ = ["sync_kr_candles"]
