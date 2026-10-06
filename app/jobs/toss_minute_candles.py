from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.market_events.session_calendar import is_trading_session
from app.services.research_candles.toss_minute_repository import (
    TOSS_MINUTE_BATCH_SIZE,
    TossMinuteCandleRepository,
    TossMinuteCoverage,
)
from app.services.research_candles.toss_minute_source import (
    TossMinuteCandleRow,
    TossMinuteCandleSource,
    TossMinutePage,
)

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# A symbol with nothing stored inside this window counts as never collected.
# It also bounds each per-symbol index probe to the newest 7-day chunks.
TOSS_MINUTE_COVERAGE_LOOKBACK = timedelta(days=14)
# Gap fill pages back with Toss's ``before`` cursor when the latest 200 bars
# no longer reach the newest stored bar. One run therefore makes at most
# TOSS_MINUTE_BATCH_SIZE + TOSS_MINUTE_GAP_FILL_RUN_PAGES = 40 calls to the
# MARKET_DATA_CHART group (Toss allows 20 per second per client). A hole wider
# than these caps is stored as-is, reported in ``unfilled_gaps`` and left to
# ``scripts/repair_toss_minute_gaps.py``: once the newer page is stored the
# minute job can no longer see that hole.
TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES = 3
TOSS_MINUTE_GAP_FILL_RUN_PAGES = 20
# Symbols that keep returning no rows (404, empty page, repeated failure) wait
# 2, 4, 8, ... minutes after their second consecutive miss. The cap is longer
# than one rotation of the universe, so a dead symbol never takes more slots
# than a live one.
TOSS_MINUTE_RETRY_BACKOFF_CAP_MINUTES = 240
_RETRY_BACKOFF_MAX_EXPONENT = 8


class _MinuteRepository(Protocol):
    async def stalest_active_symbols(
        self, *, limit: int, since: datetime, exclude: Collection[str] = ()
    ) -> list[TossMinuteCoverage]: ...

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int: ...


class _MinuteSource(Protocol):
    async def fetch(
        self,
        *,
        symbol: str,
        retrieved_at: datetime,
        batch_id: str,
        before: str | None = None,
    ) -> TossMinutePage: ...

    async def close(self) -> None: ...


class _RetryBackoff:
    """Process-local retry schedule for symbols whose fetch produced no rows.

    The first miss is retried on the next run. A worker restart clears the
    schedule, which only costs a few early retries of dead symbols.
    """

    def __init__(self) -> None:
        self._misses: dict[str, tuple[int, datetime]] = {}

    def blocked(self, minute: datetime) -> frozenset[str]:
        return frozenset(
            symbol for symbol, (_, due) in self._misses.items() if due > minute
        )

    def record_rows(self, symbol: str) -> None:
        self._misses.pop(symbol, None)

    def record_miss(self, symbol: str, minute: datetime) -> None:
        misses = self._misses.get(symbol, (0, minute))[0] + 1
        delay_minutes = (
            0
            if misses == 1
            else min(
                2 ** min(misses - 1, _RETRY_BACKOFF_MAX_EXPONENT),
                TOSS_MINUTE_RETRY_BACKOFF_CAP_MINUTES,
            )
        )
        self._misses[symbol] = (misses, minute + timedelta(minutes=delay_minutes))


_RETRY_BACKOFF = _RetryBackoff()


@dataclass
class _CallBudget:
    gap_pages_left: int
    calls: int = 0
    gap_pages: int = 0


def _as_kst(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def _is_collection_window(now_kst: datetime) -> bool:
    clock = now_kst.time().replace(tzinfo=None)
    return time(8, 0) <= clock <= time(20, 0)


def _batch_id(minute: datetime) -> str:
    return f"toss-1m-{minute:%Y%m%dT%H%MZ}"


async def _fetch_symbol(
    *,
    source: _MinuteSource,
    coverage: TossMinuteCoverage,
    retrieved_at: datetime,
    batch_id: str,
    budget: _CallBudget,
) -> tuple[list[TossMinuteCandleRow], dict[str, str] | None]:
    """Fetch the latest page, then page back until it meets the stored bar.

    Returns the rows and, when a cap stops the walk, the hole left behind.
    """

    budget.calls += 1
    page = await source.fetch(
        symbol=coverage.symbol, retrieved_at=retrieved_at, batch_id=batch_id
    )
    rows = list(page.rows)
    stored = coverage.latest_time_utc
    oldest = page.oldest_time_utc
    pages = 0
    while (
        stored is not None
        and oldest is not None
        and oldest > stored
        and page.next_before
    ):
        if pages == TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES or budget.gap_pages_left == 0:
            reason = (
                "symbol_page_cap"
                if pages == TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES
                else "run_page_budget"
            )
            return rows, {
                "reason": reason,
                "stored_through": stored.isoformat(),
                "fetched_from": oldest.isoformat(),
            }
        pages += 1
        budget.gap_pages_left -= 1
        budget.gap_pages += 1
        budget.calls += 1
        page = await source.fetch(
            symbol=coverage.symbol,
            retrieved_at=retrieved_at,
            batch_id=batch_id,
            before=page.next_before,
        )
        rows.extend(page.rows)
        if page.oldest_time_utc is None or page.oldest_time_utc >= oldest:
            break  # Toss has nothing older; there is no hole left to fill.
        oldest = page.oldest_time_utc
    return rows, None


async def _collect_batch(
    *,
    repository: _MinuteRepository,
    source: _MinuteSource,
    now: datetime,
    batch_size: int,
    backoff: _RetryBackoff,
) -> dict[str, Any]:
    minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    blocked = backoff.blocked(minute)
    selected = await repository.stalest_active_symbols(
        limit=batch_size,
        since=minute - TOSS_MINUTE_COVERAGE_LOOKBACK,
        exclude=blocked,
    )
    if not selected:
        return {
            "status": "noop",
            "reason": "no_symbol_due",
            "symbols_selected": 0,
            "symbols_backing_off": len(blocked),
            "rows_upserted": 0,
        }

    batch_id = _batch_id(minute)
    budget = _CallBudget(gap_pages_left=TOSS_MINUTE_GAP_FILL_RUN_PAGES)
    rows: list[TossMinuteCandleRow] = []
    failed_symbols: dict[str, str] = {}
    unfilled_gaps: dict[str, dict[str, str]] = {}
    empty_symbols = 0
    for coverage in selected:
        symbol = coverage.symbol
        try:
            fetched, gap = await _fetch_symbol(
                source=source,
                coverage=coverage,
                retrieved_at=now,
                batch_id=batch_id,
                budget=budget,
            )
        except Exception as exc:  # isolate one provider/symbol failure from the batch
            failed_symbols[symbol] = f"{type(exc).__name__}: {exc}"
            backoff.record_miss(symbol, minute)
            logger.warning(
                "Toss minute fetch failed symbol=%s batch_id=%s: %s",
                symbol,
                batch_id,
                exc,
                exc_info=True,
            )
            continue
        if fetched:
            backoff.record_rows(symbol)
        else:
            empty_symbols += 1
            backoff.record_miss(symbol, minute)
        if gap is not None:
            unfilled_gaps[symbol] = gap
            logger.warning(
                "Toss minute gap left for the repair CLI symbol=%s "
                "stored_through=%s fetched_from=%s reason=%s",
                symbol,
                gap["stored_through"],
                gap["fetched_from"],
                gap["reason"],
            )
        rows.extend(fetched)

    rows_upserted = await repository.upsert(rows)
    succeeded = len(selected) - len(failed_symbols)
    if failed_symbols and succeeded:
        status = "partial"
    elif failed_symbols:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "batch_id": batch_id,
        "symbols_selected": len(selected),
        "symbols_succeeded": succeeded,
        "symbols_failed": len(failed_symbols),
        "failed_symbols": failed_symbols,
        "empty_symbols": empty_symbols,
        "symbols_backing_off": len(blocked),
        "toss_calls": budget.calls,
        "gap_fill_pages": budget.gap_pages,
        "unfilled_gaps": unfilled_gaps,
        "rows_upserted": rows_upserted,
    }


async def run_toss_minute_candle_sync(
    *,
    now: datetime | None = None,
    batch_size: int = TOSS_MINUTE_BATCH_SIZE,
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
    source_factory: Callable[[], _MinuteSource] = TossMinuteCandleSource.from_settings,
) -> dict[str, Any]:
    """Collect one bounded live batch without KIS or any fail-open calendar path."""

    tick = _as_kst(now or datetime.now(KST))
    if not is_trading_session("kr", tick.date()):
        return {
            "status": "noop",
            "reason": "non_trading_day",
            "rows_upserted": 0,
        }
    if not _is_collection_window(tick):
        return {
            "status": "noop",
            "reason": "outside_toss_session",
            "rows_upserted": 0,
        }

    bounded_size = max(1, min(int(batch_size), TOSS_MINUTE_BATCH_SIZE))
    session = cast(AsyncSession, cast(object, session_factory()))
    source: _MinuteSource | None = None
    try:
        repository = TossMinuteCandleRepository(session)
        source = source_factory()
        result = await _collect_batch(
            repository=repository,
            source=source,
            now=tick,
            batch_size=bounded_size,
            backoff=_RETRY_BACKOFF,
        )
        await session.commit()
        return result
    except Exception as exc:
        await session.rollback()
        logger.error("Toss minute sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rows_upserted": 0,
        }
    finally:
        try:
            if source is not None:
                await source.close()
        finally:
            await session.close()
