"""TaskIQ schedule for event-driven KAsset market analysis."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.extensions.kasset.automation.vertical_slice import (
    run_ai_recommendation_cycle_once,
)
from app.models.trading import (
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)


@broker.task(
    task_name="kasset_market_events.run",
    # Hourly at :10 during KST sessions, right after the :05 candle sync.
    # Features come from daily candles, so scanning faster than the data
    # refreshes only burns model calls (and the old UTC */15 ran overnight).
    schedule=[{"cron": "10 9-16 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def kasset_market_events_run() -> dict[str, object]:
    """Run the canonical screener→ensemble→AI recommendation producer once."""

    return await run_ai_recommendation_cycle_once()


@broker.task(
    task_name="kasset_watchlist_candles.sync",
    schedule=[{"cron": "5 9-16 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def kasset_watchlist_candles_sync() -> dict[str, object]:
    """Backfill/refresh daily candles for KR watchlist symbols via Toss.

    The event scan reads ``kr_candles_1d`` but this deployment has no KIS
    holdings-driven ingestion, so the watchlist is its own candle universe.
    Toss daily rows upsert as ``source='toss'``; the repository's conflict
    guard keeps them from overwriting KIS-sourced rows.
    """
    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        return {"enabled": False, "synced": []}

    symbols = await _watchlist_kr_symbols()
    synced: list[dict[str, object]] = []
    for symbol in symbols:
        try:
            synced.append(await _sync_watchlist_symbol(symbol))
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
            synced.append({"symbol": symbol, "error": str(exc)})
    return {"enabled": True, "synced": synced}


async def _watchlist_kr_symbols() -> list[str]:
    async with _session() as session:
        rows = await session.execute(
            select(Instrument.symbol)
            .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
            .join(User, User.id == UserWatchItem.user_id)
            .where(
                UserWatchItem.is_active.is_(True),
                Instrument.is_active.is_(True),
                Instrument.type == InstrumentType.equity_kr,
                User.role == UserRole.trader,
                User.is_active.is_(True),
            )
            .distinct()
            .order_by(Instrument.symbol)
        )
        return [str(symbol) for symbol in rows.scalars().all()]


async def _sync_watchlist_symbol(symbol: str) -> dict[str, object]:
    from app.services.daily_candles.converters import frame_to_rows
    from app.services.daily_candles.repository import (
        DailyCandlesRepository,
        MarketKey,
    )
    from app.services.market_data.toss_ohlcv import fetch_daily_toss_frame

    frame = await fetch_daily_toss_frame(symbol=symbol, count=60)
    rows = frame_to_rows(frame, symbol=symbol, partition="KRX", source="toss")
    async with _session() as session:
        repository = DailyCandlesRepository(session=session)
        upserted = await repository.upsert_rows(market=MarketKey.KR, rows=rows)
        await session.commit()
    return {"symbol": symbol, "rows": len(rows), "upserted": upserted}


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


__all__ = ["kasset_market_events_run", "kasset_watchlist_candles_sync"]
