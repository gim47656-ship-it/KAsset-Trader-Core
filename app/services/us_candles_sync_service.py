# pyright: reportMissingTypeStubs=none
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import cast
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.models.trading import Instrument, InstrumentType, UserWatchItem
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
from app.services.market_data.toss_ohlcv import fetch_us_intraday_toss_frame
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
    get_us_exchange_by_symbol,
    sync_us_symbol_universe,
)

_NY = ZoneInfo("America/New_York")
_OVERLAP_MINUTES = 5
_UNRESOLVED_SYMBOL_SKIP_REASON = "unresolved_symbol_after_refresh"
_US_UNIVERSE_EMPTY_MESSAGE = (
    "us_symbol_universe is empty. "
    "Sync required: uv run python scripts/sync_us_symbol_universe.py"
)

logger = logging.getLogger(__name__)

type TimestampLike = datetime | pd.Timestamp | str


class _RowcountResult:
    rowcount: int | None


_TABLE_CFG = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
_CURSOR_SQL = build_cursor_sql(_TABLE_CFG)
_UPSERT_SQL = build_upsert_sql(_TABLE_CFG)

_EXISTING_ROWS_SQL = text(
    """
    SELECT time, open, high, low, close, volume, value
    FROM public.us_candles_1m
    WHERE symbol = :symbol
      AND exchange = :exchange
      AND time >= :start_time
      AND time <= :end_time
    """
)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    session: pd.Timestamp
    open_utc: datetime
    close_utc: datetime
    last_minute_utc: datetime


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    time_utc: datetime
    local_time: datetime
    symbol: str
    exchange: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass(frozen=True, slots=True)
class ResolvedSymbolPairs:
    symbol_pairs: list[tuple[str, str]]
    skipped_symbols: list[str]
    lookup_refresh_attempted: bool


@lru_cache(maxsize=1)
def _get_xnys_calendar():
    return xcals.get_calendar("XNYS", side="left")


def _utc_now_floor_minute() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC)).floor("min")


def _normalize_symbol(value: object) -> str | None:
    normalized = to_db_symbol(str(value or "").strip().upper())
    return normalized or None


def _to_utc_datetime(value: datetime | pd.Timestamp | str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(UTC)
    else:
        timestamp = timestamp.tz_convert(UTC)
    return timestamp.to_pydatetime().astimezone(UTC)


def _to_local_minute(value: datetime | pd.Timestamp | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(_NY)
    else:
        timestamp = timestamp.tz_convert(_NY)
    local_dt = timestamp.to_pydatetime().astimezone(_NY)
    return local_dt.replace(second=0, microsecond=0)


async def _resolve_symbol_pairs(
    *,
    session: AsyncSession,
    target_symbols: set[str],
) -> ResolvedSymbolPairs:
    symbol_pairs: list[tuple[str, str]] = []
    skipped_symbols: list[str] = []
    lookup_refresh_attempted = False

    for symbol in sorted(target_symbols):
        try:
            exchange = await get_us_exchange_by_symbol(symbol, db=session)
        except (USSymbolNotRegisteredError, USSymbolInactiveError) as exc:
            if not lookup_refresh_attempted:
                refresh_result = await sync_us_symbol_universe(db=session)
                lookup_refresh_attempted = True
                if int(refresh_result.get("total", 0)) <= 0:
                    raise USSymbolUniverseEmptyError(
                        _US_UNIVERSE_EMPTY_MESSAGE
                    ) from exc
                await session.commit()
                try:
                    exchange = await get_us_exchange_by_symbol(symbol, db=session)
                except (USSymbolNotRegisteredError, USSymbolInactiveError) as retry_exc:
                    logger.warning(
                        "Skipping unresolved US candle sync symbol after universe refresh symbol=%s error=%s",
                        symbol,
                        retry_exc,
                    )
                    skipped_symbols.append(symbol)
                    continue
            else:
                logger.warning(
                    "Skipping unresolved US candle sync symbol after universe refresh symbol=%s error=%s",
                    symbol,
                    exc,
                )
                skipped_symbols.append(symbol)
                continue
        symbol_pairs.append((symbol, exchange))

    return ResolvedSymbolPairs(
        symbol_pairs=symbol_pairs,
        skipped_symbols=skipped_symbols,
        lookup_refresh_attempted=lookup_refresh_attempted,
    )


def _select_closed_sessions(now_utc: datetime, sessions: int) -> list[SessionWindow]:
    calendar = _get_xnys_calendar()
    count = max(int(sessions), 1)
    now_ts = pd.Timestamp(now_utc)
    last_closed = calendar.minute_to_past_session(now_ts, count=1)
    session_index = calendar.sessions_window(last_closed, -count)
    selected = list(pd.DatetimeIndex(session_index)[-count:])

    windows: list[SessionWindow] = []
    for session in selected:
        open_utc = _to_utc_datetime(calendar.session_open(session))
        close_utc = _to_utc_datetime(calendar.session_close(session))
        windows.append(
            SessionWindow(
                session=pd.Timestamp(session),
                open_utc=open_utc,
                close_utc=close_utc,
                last_minute_utc=close_utc - timedelta(minutes=1),
            )
        )
    return windows


def _compute_incremental_lower_bound(
    cursor_utc: datetime | None,
    session_open_utc: datetime,
) -> datetime:
    if cursor_utc is None:
        return session_open_utc

    normalized_cursor = (
        cursor_utc if cursor_utc.tzinfo is not None else cursor_utc.replace(tzinfo=UTC)
    )
    overlapped = normalized_cursor.astimezone(UTC) - timedelta(minutes=_OVERLAP_MINUTES)
    return max(overlapped, session_open_utc)


def _normalize_minute_page(
    *,
    frame: pd.DataFrame,
    symbol: str,
    exchange: str,
    lower_bound_utc: datetime,
    upper_bound_utc: datetime,
) -> list[MinuteCandleRow]:
    if frame.empty:
        return []

    deduped: dict[datetime, MinuteCandleRow] = {}
    for item in frame.to_dict("records"):
        local_dt = _to_local_minute(item.get("datetime"))
        if local_dt is None:
            continue

        time_utc = local_dt.astimezone(UTC)
        if time_utc < lower_bound_utc or time_utc > upper_bound_utc:
            continue

        open_value = parse_float(item.get("open"))
        high_value = parse_float(item.get("high"))
        low_value = parse_float(item.get("low"))
        close_value = parse_float(item.get("close"))
        volume_value = parse_float(item.get("volume"))
        value_value = parse_float(item.get("value"))
        if (
            open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
            or volume_value is None
            or value_value is None
        ):
            continue

        deduped[time_utc] = MinuteCandleRow(
            time_utc=time_utc,
            local_time=local_dt,
            symbol=symbol,
            exchange=exchange,
            open=float(open_value),
            high=float(high_value),
            low=float(low_value),
            close=float(close_value),
            volume=float(volume_value),
            value=float(value_value),
        )

    return [deduped[key] for key in sorted(deduped)]


async def _upsert_rows(session: AsyncSession, rows: list[MinuteCandleRow]) -> int:
    if not rows:
        return 0

    symbol = rows[0].symbol
    exchange = rows[0].exchange
    start_time = min(row.time_utc for row in rows)
    end_time = max(row.time_utc for row in rows)

    existing_result = await session.execute(
        _EXISTING_ROWS_SQL,
        {
            "symbol": symbol,
            "exchange": exchange,
            "start_time": start_time,
            "end_time": end_time,
        },
    )
    existing_rows = {
        mapping["time"]: (
            float(mapping["open"]),
            float(mapping["high"]),
            float(mapping["low"]),
            float(mapping["close"]),
            float(mapping["volume"]),
            float(mapping["value"]),
        )
        for mapping in existing_result.mappings().all()
    }

    payload = []
    for row in rows:
        current_values = (
            row.open,
            row.high,
            row.low,
            row.close,
            row.volume,
            row.value,
        )
        if existing_rows.get(row.time_utc) == current_values:
            continue

        payload.append(
            {
                "time": row.time_utc,
                "symbol": row.symbol,
                "exchange": row.exchange,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )

    if not payload:
        return 0

    result = cast(
        _RowcountResult, cast(object, await session.execute(_UPSERT_SQL, payload))
    )
    return max(int(result.rowcount or 0), 0)


async def _collect_window_rows(
    *,
    symbol: str,
    exchange: str,
    lower_bound_utc: datetime,
    upper_bound_utc: datetime,
) -> tuple[list[MinuteCandleRow], int]:
    minute_count = max(
        int((upper_bound_utc - lower_bound_utc).total_seconds() // 60) + 1,
        1,
    )
    frame = await fetch_us_intraday_toss_frame(
        symbol=symbol,
        period="1m",
        count=minute_count,
        end_date=upper_bound_utc.astimezone(_NY),
    )
    rows = _normalize_minute_page(
        frame=frame,
        symbol=symbol,
        exchange=exchange,
        lower_bound_utc=lower_bound_utc,
        upper_bound_utc=upper_bound_utc,
    )
    return rows, 1


async def sync_us_candles(
    *,
    mode: str,
    sessions: int = 10,
    user_id: int = 1,
) -> dict[str, object]:
    normalized_mode = normalize_mode(mode)
    session_count = max(int(sessions), 1)
    now_utc = _utc_now_floor_minute().to_pydatetime().astimezone(UTC)
    calendar = _get_xnys_calendar()

    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        # 전역 Toss 자격증명은 이 user_id의 단일 운영자 계좌와 같다는 배포 계약이다.
        # 다중 사용자 계좌 매핑이 생기면 명시적 계정 스코프로 교체해야 한다.
        holdings_snapshot_ok = True
        try:
            snapshot = await fetch_toss_portfolio_snapshot(
                need_sellable=False,
                need_cash=False,
            )
        except Exception as exc:
            holdings_snapshot_ok = False
            toss_positions = []
            logger.warning(
                "Toss portfolio snapshot unavailable during US candle sync error_type=%s",
                type(exc).__name__,
            )
        else:
            toss_positions = [
                position
                for position in snapshot.positions
                if position.instrument_type == "equity_us"
            ]

        manual_service = ManualHoldingsService(session)
        manual_holdings = await manual_service.get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.US,
        )
        # watchlist는 사용자별로 저장되지만 분봉 저장은 종목 단위 시계열이므로
        # 소유자 구분이 없다. 스케줄 job은 ``user_id=1``로 돌고 실제 관심종목은
        # 다른 사용자에 붙어 있어, owner 스코프를 걸면 대상이 비어 버린다.
        # 계좌 보유(``toss_positions``)와 수동 보유만 owner 스코프를 유지한다.
        watchlist_result = await session.execute(
            select(Instrument.symbol)
            .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
            .where(
                Instrument.type == InstrumentType.equity_us,
                UserWatchItem.is_active.is_(True),
            )
            .distinct()
        )
        watchlist_symbols = watchlist_result.scalars().all()
        target_symbols = build_symbol_union(
            toss_positions,
            manual_holdings,
            holdings_field="symbol",
            normalize_fn=_normalize_symbol,
        )
        for raw_symbol in watchlist_symbols:
            symbol = _normalize_symbol(raw_symbol)
            if symbol is not None:
                target_symbols.add(symbol)
        if not target_symbols:
            return {
                "mode": normalized_mode,
                "sessions": session_count,
                "holdings_snapshot_ok": holdings_snapshot_ok,
                "skipped": True,
                "reason": "no_target_symbols",
                "skip_reasons": {},
                "skipped_symbols": [],
                "lookup_refresh_attempted": False,
                "symbols_total": 0,
                "symbol_venues_total": 0,
                "pairs_processed": 0,
                "pairs_skipped": 0,
                "rows_upserted": 0,
                "pages_fetched": 0,
            }

        resolution = await _resolve_symbol_pairs(
            session=session, target_symbols=target_symbols
        )
        symbol_pairs = resolution.symbol_pairs
        skipped_symbols = resolution.skipped_symbols
        lookup_refresh_attempted = resolution.lookup_refresh_attempted
        skipped_reasons: dict[str, int] = {}
        if skipped_symbols:
            skipped_reasons[_UNRESOLVED_SYMBOL_SKIP_REASON] = len(skipped_symbols)
        pairs_total = len(symbol_pairs) + len(skipped_symbols)

        windows: list[SessionWindow]
        if normalized_mode == "incremental":
            if not calendar.is_trading_minute(pd.Timestamp(now_utc)):
                if symbol_pairs:
                    skipped_reasons["outside_trading_minute"] = len(symbol_pairs)
                return {
                    "mode": normalized_mode,
                    "sessions": session_count,
                    "holdings_snapshot_ok": holdings_snapshot_ok,
                    "skipped": True,
                    "skip_reasons": skipped_reasons,
                    "skipped_symbols": skipped_symbols,
                    "lookup_refresh_attempted": lookup_refresh_attempted,
                    "symbols_total": len(target_symbols),
                    "symbol_venues_total": pairs_total,
                    "pairs_processed": 0,
                    "pairs_skipped": pairs_total,
                    "rows_upserted": 0,
                    "pages_fetched": 0,
                }

            current_session = calendar.minute_to_session(
                pd.Timestamp(now_utc), direction="none"
            )
            session_open_utc = _to_utc_datetime(calendar.session_open(current_session))
            session_close_utc = _to_utc_datetime(
                calendar.session_close(current_session)
            )
            windows = [
                SessionWindow(
                    session=pd.Timestamp(current_session),
                    open_utc=session_open_utc,
                    close_utc=session_close_utc,
                    last_minute_utc=min(
                        session_close_utc - timedelta(minutes=1),
                        now_utc - timedelta(minutes=1),
                    ),
                )
            ]
        else:
            windows = _select_closed_sessions(now_utc, session_count)

        pairs_processed = 0
        rows_upserted = 0
        pages_fetched = 0
        pairs_skipped = len(skipped_symbols)
        failed_pairs: list[str] = []

        for symbol, exchange in symbol_pairs:
            pair_rows: list[MinuteCandleRow] = []
            pair_pages = 0
            try:
                for window in windows:
                    lower_bound_utc = window.open_utc
                    if normalized_mode == "incremental":
                        cursor_utc = await read_cursor_utc(
                            session,
                            _CURSOR_SQL,
                            {"symbol": symbol, "exchange": exchange},
                        )
                        lower_bound_utc = _compute_incremental_lower_bound(
                            cursor_utc,
                            window.open_utc,
                        )

                    if window.last_minute_utc < lower_bound_utc:
                        continue

                    rows, page_calls = await _collect_window_rows(
                        symbol=symbol,
                        exchange=exchange,
                        lower_bound_utc=lower_bound_utc,
                        upper_bound_utc=window.last_minute_utc,
                    )
                    pair_rows.extend(rows)
                    pair_pages += page_calls
            except Exception as exc:
                # 한 종목의 Toss 조회 실패(예: ``stock-not-found``)가 나머지 관심·보유
                # 종목의 분봉 적재를 막지 않도록 종목 단위로 격리한다.
                await session.rollback()
                failed_pairs.append(f"{symbol}:{exchange}")
                logger.warning(
                    "US minute candle fetch failed, continuing symbol=%s exchange=%s error=%s",
                    symbol,
                    exchange,
                    exc,
                )
                continue

            if not pair_rows and pair_pages == 0:
                pairs_skipped += 1
                continue

            try:
                rows_upserted += await _upsert_rows(session, pair_rows)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

            pairs_processed += 1
            pages_fetched += pair_pages

        if failed_pairs:
            logger.error(
                "US minute candle sync partial failure failed=%d/%d pairs=%s",
                len(failed_pairs),
                len(symbol_pairs),
                failed_pairs,
            )

        return {
            "failed_pairs": failed_pairs,
            "mode": normalized_mode,
            "sessions": session_count,
            "holdings_snapshot_ok": holdings_snapshot_ok,
            "skipped": pairs_processed == 0,
            "skip_reasons": skipped_reasons,
            "skipped_symbols": skipped_symbols,
            "lookup_refresh_attempted": lookup_refresh_attempted,
            "symbols_total": len(target_symbols),
            "symbol_venues_total": pairs_total,
            "pairs_processed": pairs_processed,
            "pairs_skipped": pairs_skipped,
            "rows_upserted": rows_upserted,
            "pages_fetched": pages_fetched,
        }
    finally:
        await session.close()


__all__ = ["sync_us_candles"]
