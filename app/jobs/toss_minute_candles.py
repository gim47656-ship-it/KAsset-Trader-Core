from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.market_events.session_calendar import is_trading_session
from app.services.research_candles.toss_minute_repository import (
    TOSS_MINUTE_BATCH_SIZE,
    TossMinuteCandleRepository,
)
from app.services.research_candles.toss_minute_source import (
    TossMinuteCandleRow,
    TossMinuteCandleSource,
)

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class _MinuteRepository(Protocol):
    async def active_symbol_count(self) -> int: ...

    async def active_symbol_batch(
        self, *, total: int, offset: int, limit: int
    ) -> list[str]: ...

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int: ...


class _MinuteSource(Protocol):
    async def fetch(
        self, *, symbol: str, retrieved_at: datetime, batch_id: str
    ) -> list[TossMinuteCandleRow]: ...

    async def close(self) -> None: ...


def _as_kst(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def _is_collection_window(now_kst: datetime) -> bool:
    clock = now_kst.time().replace(tzinfo=None)
    return time(8, 0) <= clock <= time(20, 0)


def _batch_offset(*, now: datetime, total: int, batch_size: int) -> int:
    if total <= 0:
        return 0
    minute_slot = int(now.astimezone(UTC).timestamp()) // 60
    return (minute_slot * batch_size) % total


def _batch_id(*, now: datetime, offset: int) -> str:
    minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    return f"toss-1m-{minute:%Y%m%dT%H%MZ}-{offset:06d}"


async def _collect_batch(
    *,
    repository: _MinuteRepository,
    source: _MinuteSource,
    now: datetime,
    batch_size: int,
) -> dict[str, Any]:
    total = await repository.active_symbol_count()
    if total == 0:
        return {
            "status": "noop",
            "reason": "active_universe_empty",
            "symbols_total": 0,
            "symbols_selected": 0,
            "rows_upserted": 0,
        }

    offset = _batch_offset(now=now, total=total, batch_size=batch_size)
    symbols = await repository.active_symbol_batch(
        total=total,
        offset=offset,
        limit=batch_size,
    )
    batch_id = _batch_id(now=now, offset=offset)
    rows: list[TossMinuteCandleRow] = []
    failed_symbols: dict[str, str] = {}
    empty_symbols = 0
    for symbol in symbols:
        try:
            fetched = await source.fetch(
                symbol=symbol,
                retrieved_at=now,
                batch_id=batch_id,
            )
        except Exception as exc:  # isolate one provider/symbol failure from the batch
            failed_symbols[symbol] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Toss minute fetch failed symbol=%s batch_id=%s: %s",
                symbol,
                batch_id,
                exc,
                exc_info=True,
            )
            continue
        if not fetched:
            empty_symbols += 1
        rows.extend(fetched)

    rows_upserted = await repository.upsert(rows)
    succeeded = len(symbols) - len(failed_symbols)
    if failed_symbols and succeeded:
        status = "partial"
    elif failed_symbols:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "batch_id": batch_id,
        "batch_offset": offset,
        "batch_limit": batch_size,
        "symbols_total": total,
        "symbols_selected": len(symbols),
        "symbols_succeeded": succeeded,
        "symbols_failed": len(failed_symbols),
        "failed_symbols": failed_symbols,
        "empty_symbols": empty_symbols,
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
