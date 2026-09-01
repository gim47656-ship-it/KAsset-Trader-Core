"""일봉 OHLCV의 DB-first read-through 서비스(ROB-639).

``get_ohlcv(period='day')``가 KR/US 일봉을 먼저 DB에서 읽고 Toss로
내려갈 수 있도록 ``market_data_indicators._cache_first_*``에서 분리했다.

설계:

* ``cache_is_fresh_equity``와 ``rows_to_frame`` 같은 공개 helper를
  ``market_data_indicators``와 공유해 신선도 의미가 어긋나지 않게 한다.
* ``cache_first_kr`` / ``cache_first_us``는 DB를 읽기만 하며 누락되거나
  오래된 경우 ``None``을 반환해 Toss 조회와 선택적 write-back을 허용한다.
* DB/캘린더 오류는 fail-open으로 ``None``을 반환한다. ``get_ohlcv``를
  실패시키지 않고 Toss 경로로 내리며 ``write_back_*``도 best-effort로 0을
  반환한다.

주식 캐시는 최신 DB 행이 가장 최근에 종료된 거래소 세션(KR ``XKRX``,
US ``XNYS``)을 포함해야 신선하다.

KR 장중에는 오늘 일봉이 아직 형성 중일 수 있으므로 세션 거래일의
15:35 KST 이전에는 ``cache_first_kr``가 ``None``을 반환해 Toss 현재 봉을
사용한다. 이후와 비거래일에는 DB-first 경로를 사용한다. US 공급자 경계는
자체 완료 세션 필터를 적용한다.
"""

from __future__ import annotations

import datetime
import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from app.core.timezone import KST, now_kst
from app.services.daily_candles.repository import DailyCandleRow
from app.services.kis_ohlcv_cache import KRX_DAILY_CACHE_CUTOFF
from app.services.market_events.session_calendar import (
    is_trading_session,
    previous_trading_session,
    regular_session_bounds,
)

logger = logging.getLogger(__name__)

# Canonical OHLCV column ordering produced by ``rows_to_frame``. Kept here so
# consumers (``market_data_indicators`` and ``get_ohlcv``) share one source of
# truth for the frame shape.
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


@lru_cache(maxsize=4)
def get_calendar(exchange: str):
    """Return (and cache) an ``exchange_calendars`` calendar by name."""
    return xcals.get_calendar(exchange)


def _coerce_kst(now: datetime.datetime | None) -> datetime.datetime:
    """Coerce an optional (possibly naive) datetime to a KST-aware datetime."""
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=KST)
    return current.astimezone(KST)


def _coerce_utc_timestamp(now: datetime.datetime | None) -> pd.Timestamp:
    """Coerce an optional (possibly naive) datetime to a UTC pd.Timestamp."""
    ts = (
        pd.Timestamp(now)
        if now is not None
        else pd.Timestamp(datetime.datetime.now(datetime.UTC))
    )
    if ts.tzinfo is None:
        return ts.tz_localize(datetime.UTC)
    return ts.tz_convert(datetime.UTC)


def latest_exchange_session(
    exchange: str, now: datetime.datetime | None = None
) -> datetime.date | None:
    """Return the most-recent *closed* session date for the given exchange.

    ``exchange`` accepts any calendar name supported by exchange_calendars
    (e.g., ``'XKRX'`` for KRX, ``'XNYS'`` for NYSE). ``now`` is injectable for
    tests; defaults to the real clock. Returns ``None`` if the library raises
    (rare, but defensive for early/late session edges).

    ``minute_to_past_session`` excludes an in-progress session, so during
    trading hours this returns the *previous* session.
    """
    if exchange == "XKRX":
        current_utc = _coerce_utc_timestamp(now).to_pydatetime()
        local_date = current_utc.astimezone(KST).date()
        bounds = regular_session_bounds("kr", local_date)
        if bounds is not None and current_utc >= bounds[1]:
            return local_date
        return previous_trading_session("kr", local_date)

    cal = get_calendar(exchange)
    try:
        session = cal.minute_to_past_session(_coerce_utc_timestamp(now), count=1)
    except Exception:
        return None
    return pd.Timestamp(session).date()


def kr_daily_bar_may_be_forming(now: datetime.datetime | None = None) -> bool:
    """True while today's KRX daily bar may still be forming.

    That is: today (KST) is an XKRX session day AND the current KST time is
    before the shared ``KRX_DAILY_CACHE_CUTOFF`` (15:35 — session close plus
    settling buffer, same semantics as ``kis_ohlcv_cache``).
    """
    current = _coerce_kst(now)
    return (
        is_trading_session("kr", current.date())
        and current.time() < KRX_DAILY_CACHE_CUTOFF
    )


def last_final_session_kr(now: datetime.datetime | None = None) -> datetime.date | None:
    """Most recent XKRX session whose daily bar is final (15:35 KST cutoff).

    Today counts only after ``KRX_DAILY_CACHE_CUTOFF``; otherwise the latest
    session strictly before today. Returns ``None`` on calendar failure.
    """
    current = _coerce_kst(now)
    today = current.date()
    if is_trading_session("kr", today) and current.time() >= KRX_DAILY_CACHE_CUTOFF:
        return today
    return previous_trading_session("kr", today)


def last_final_session_us(now: datetime.datetime | None = None) -> datetime.date | None:
    """Most recent *closed* XNYS session (the US daily bar is final at close)."""
    return latest_exchange_session("XNYS", now=now)


def drop_forming_daily_rows(
    frame: pd.DataFrame, *, market: str, now: datetime.datetime | None = None
) -> pd.DataFrame:
    """Drop rows whose session is not yet closed (forming intraday bars).

    Used by the write-back paths so a partial intraday bar fetched live is
    never persisted into ``kr/us_candles_1d`` as an authoritative daily row.
    ``market`` is ``'kr'`` or ``'us'``. If the last final session cannot be
    determined, the frame is returned unchanged (best-effort write-back).
    """
    if frame is None or frame.empty:
        return frame
    last_final = (
        last_final_session_kr(now) if market == "kr" else last_final_session_us(now)
    )
    if last_final is None:
        return frame
    if "date" in frame.columns:
        dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    elif "datetime" in frame.columns:
        dates = pd.to_datetime(frame["datetime"], errors="coerce").dt.date
    else:
        return frame
    mask = pd.Series(
        [d is not pd.NaT and d is not None and d <= last_final for d in dates],
        index=frame.index,
    )
    if mask.all():
        return frame
    dropped = int((~mask).sum())
    logger.debug(
        "drop_forming_daily_rows market=%s dropped=%d last_final=%s",
        market,
        dropped,
        last_final,
    )
    return frame.loc[mask]


def cache_is_fresh_equity(
    rows: list[DailyCandleRow],
    exchange: str,
    now: datetime.datetime | None = None,
) -> bool:
    """Cache is fresh if the newest row covers the latest closed exchange session.

    For KR this encodes the 15:30 KST (06:30 UTC) session close of ``XKRX`` —
    a row timestamped after the latest session's close satisfies the rule.
    ``now`` is injectable for tests; defaults to the real clock.
    """
    if not rows:
        return False
    latest_session = latest_exchange_session(exchange, now=now)
    if latest_session is None:
        return False
    latest_row = max(r.time_utc for r in rows)
    return pd.Timestamp(latest_row).date() >= latest_session


def cache_is_fresh_crypto(rows: list[DailyCandleRow]) -> bool:
    """Return True if the newest row's timestamp is within the last 24 hours."""
    if not rows:
        return False
    newest = max(r.time_utc for r in rows)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=datetime.UTC)
    return datetime.datetime.now(datetime.UTC) - newest < datetime.timedelta(hours=24)


def rows_to_frame(rows: list[DailyCandleRow]) -> pd.DataFrame:
    """Convert a list of ``DailyCandleRow`` to a canonical OHLCV DataFrame.

    Returns an empty DataFrame (with the standard column set) when ``rows``
    is empty. Output is sorted ascending by date and reset-indexed.
    """
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    records = []
    for row in rows:
        ts = row.time_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.UTC)
        records.append(
            {
                "date": ts.date(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )
    df = pd.DataFrame(records, columns=OHLCV_COLUMNS)
    return df.sort_values("date").reset_index(drop=True)


async def cache_first_kr(
    symbol: str,
    count: int,
    end: datetime.datetime | None = None,
    *,
    now: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """KR 일봉을 DB-first로 읽고 누락·오래됨·오류 시 ``None``을 반환한다.

    ``DailyCandlesRepository.fetch_recent``로 ``kr_candles_1d``의 KRX 최신
    ``count``행을 읽는다. 행 수가 충분하고 최신 행이 가장 최근 종료된 XKRX
    세션을 포함할 때만 정렬된 OHLCV를 반환하며, 아니면 Toss로 내린다.

    거래일 15:35 KST 이전에는 오늘 일봉이 형성 중일 수 있으므로 항상
    ``None``을 반환한다. 이때 Toss 경로가 오래된 DB 꼬리 대신 현재 봉을
    포함할 수 있다.

    DB/캘린더 예외는 기록 후 삼키며 호출자의 Toss 경로를 살린다.
    ``end``가 있으면 최신-N 캐시로 과거 질의를 처리할 수 없으므로 캐시를
    우회한다. ``now``는 테스트에서 주입할 수 있고 기본값은 실제 시각이다.
    """
    if end is not None:
        # 최신-N 캐시는 과거 시점 질의를 처리할 수 없다.
        return None

    try:
        if kr_daily_bar_may_be_forming(now):
            # 장중 KRX에서는 DB에 없는 오늘 형성 중 봉을 Toss에서 읽는다.
            return None

        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        partition = "KRX"
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            cached = await repo.fetch_recent(
                market=MarketKey.KR, symbol=symbol, partition=partition, count=count
            )
            if len(cached) >= count and cache_is_fresh_equity(cached, "XKRX", now=now):
                logger.debug(
                    "daily_candles cache hit market=kr symbol=%s rows=%d",
                    symbol,
                    len(cached),
                )
                return rows_to_frame(cached)
        return None
    except Exception:
        logger.warning(
            "cache_first_kr failed symbol=%s; falling back to live path",
            symbol,
            exc_info=True,
        )
        return None


async def cache_first_us(
    symbol: str,
    count: int,
    end: datetime.datetime | None = None,
    *,
    now: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """US 일봉을 DB-first로 읽고 누락·오래됨·오류 시 ``None``을 반환한다.

    ``get_us_exchange_by_symbol``로 거래소(``NASDAQ`` / ``NYSE`` /
    ``AMEX``)를 확인한 뒤 ``us_candles_1d``의 최신 ``count``행을 읽는다.
    AMEX를 포함한 미국 세션 신선도는 ``XNYS`` 캘린더로 판정한다. DB가
    부족하거나 오래됐으면 Toss로 내린다.

    DB/캘린더 예외는 기록 후 삼키며 호출자의 Toss 경로를 살린다.
    ``now``는 테스트에서 주입할 수 있고 기본값은 실제 시각이다.
    """
    if end is not None:
        return None

    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        async with AsyncSessionLocal() as session:
            try:
                partition = await get_us_exchange_by_symbol(symbol, db=session)
            except Exception:
                logger.warning(
                    "Could not resolve US exchange for symbol=%s; defaulting to NASD",
                    symbol,
                )
                # The failed lookup may have aborted the transaction; roll it
                # back before reusing this session, otherwise the next query
                # raises InFailedSQLTransactionError (poisoned session).
                await session.rollback()
                partition = "NASD"

            repo = DailyCandlesRepository(session=session)
            cached = await repo.fetch_recent(
                market=MarketKey.US, symbol=symbol, partition=partition, count=count
            )
            if len(cached) >= count and cache_is_fresh_equity(cached, "XNYS", now=now):
                logger.debug(
                    "daily_candles cache hit market=us symbol=%s rows=%d",
                    symbol,
                    len(cached),
                )
                return rows_to_frame(cached)
        return None
    except Exception:
        logger.warning(
            "cache_first_us failed symbol=%s; falling back to live path",
            symbol,
            exc_info=True,
        )
        return None


async def write_back_kr(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str = "KRX",
    source: str = "toss",
    now: datetime.datetime | None = None,
) -> int:
    """새 KR 일봉 frame을 ``kr_candles_1d``에 write-back한다.

    아직 종료되지 않은 오늘 형성 중 봉은 upsert 전에 제거해 부분 봉이
    확정 일봉으로 저장되지 않게 한다.

    best-effort 경계이므로 오류를 기록하고 삼키며 읽기 경로로 전파하지
    않는다. 저장 행 수를 반환하고 실패하거나 비어 있으면 0을 반환한다.
    """
    if frame is None or frame.empty:
        return 0
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.converters import frame_to_rows
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        frame = drop_forming_daily_rows(frame, market="kr", now=now)
        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source=source
        )
        if not repo_rows:
            return 0
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(market=MarketKey.KR, rows=repo_rows)
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_kr failed symbol=%s partition=%s", symbol, partition
        )
        return 0


async def write_back_us(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str | None = None,
    source: str = "toss",
    now: datetime.datetime | None = None,
) -> int:
    """새 US 일봉 frame을 ``us_candles_1d``에 write-back한다.

    ``get_ohlcv``의 운영 주식 공급자는 Toss뿐이므로 ``source`` 기본값은
    ``'toss'``다. ``partition``이 ``None``이면
    ``get_us_exchange_by_symbol``로 거래소를 확인하고 실패 시 ``'NASD'``를
    사용한다.

    아직 종료되지 않은 형성 중 봉은 upsert 전에 제거한다. frame에
    ``adj_close``가 없으면 기존 값을 건드리지 않아 Yahoo/Toss 일반 봉이
    ``yahoo_fallback`` 조정종가를 지우지 않게 한다.

    best-effort 경계이므로 오류를 기록하고 삼키며 실패 시 0을 반환한다.
    """
    if frame is None or frame.empty:
        return 0
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.converters import frame_to_rows
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        frame = drop_forming_daily_rows(frame, market="us", now=now)
        if frame.empty:
            return 0

        if partition is None:
            from app.services.us_symbol_universe_service import (
                get_us_exchange_by_symbol,
            )

            async with AsyncSessionLocal() as session:
                try:
                    partition = await get_us_exchange_by_symbol(symbol, db=session)
                except Exception:
                    logger.warning(
                        "write_back_us: could not resolve exchange for symbol=%s; "
                        "defaulting to NASD",
                        symbol,
                    )
                    partition = "NASD"

        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source=source
        )
        if not repo_rows:
            return 0
        update_adj_close = "adj_close" in frame.columns
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(
                market=MarketKey.US,
                rows=repo_rows,
                update_adj_close=update_adj_close,
            )
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_us failed symbol=%s partition=%s", symbol, partition
        )
        return 0


__all__ = [
    "OHLCV_COLUMNS",
    "cache_first_kr",
    "cache_first_us",
    "cache_is_fresh_crypto",
    "cache_is_fresh_equity",
    "drop_forming_daily_rows",
    "get_calendar",
    "kr_daily_bar_may_be_forming",
    "last_final_session_kr",
    "last_final_session_us",
    "latest_exchange_session",
    "rows_to_frame",
    "write_back_kr",
    "write_back_us",
]
