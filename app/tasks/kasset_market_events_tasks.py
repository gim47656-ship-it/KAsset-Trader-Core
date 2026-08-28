"""TaskIQ schedule for event-driven KAsset market analysis."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.automation.market_pipeline import MarketEventPipeline
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)


@broker.task(
    task_name="kasset_market_events.run",
    schedule=[{"cron": "*/15 * * * 1-5"}],
)
async def kasset_market_events_run() -> dict[str, object]:
    if not settings.KASSET_MARKET_EVENTS_ENABLED:
        return {
            "enabled": False,
            "owner_user_id": None,
            "scanned": 0,
            "results": [],
        }

    async with _session() as session:
        owner_ids = [
            int(owner_id)
            for owner_id in (
                await session.scalars(
                    select(User.id)
                    .where(User.role == UserRole.trader, User.is_active.is_(True))
                    .order_by(User.id)
                )
            ).all()
        ]
    if not owner_ids:
        return {
            "enabled": True,
            "owners": [],
            "scanned": 0,
            "skipped": "trader_not_found",
        }

    from app.extensions.kasset.ai.factory import build_model_router

    try:
        router = build_model_router()
    except AiProviderUnavailable:
        return {
            "enabled": True,
            "owners": [
                {
                    "ownerUserId": owner_user_id,
                    "scanned": 0,
                    "results": [],
                    "skipped": "ai_unavailable",
                }
                for owner_user_id in owner_ids
            ],
            "scanned": 0,
            "skipped": "ai_unavailable",
        }

    owners: list[dict[str, object]] = []
    total_scanned = 0
    for owner_user_id in owner_ids:
        results: list[dict[str, object]] = []
        try:
            async with _session() as session:
                watch_items = await _active_watch_items(session, owner_user_id)
                pipeline = MarketEventPipeline(session, router, _utc_now)
                for symbol, instrument_type, exchange_code in watch_items:
                    market = _scan_market(instrument_type, exchange_code)
                    if market is None:
                        continue
                    try:
                        outcome = await pipeline.run_symbol_scan(
                            owner_user_id,
                            market,
                            symbol,
                        )
                    except ValueError:
                        # One unsupported venue must not abort the remaining scans.
                        results.append(
                            {
                                "symbol": symbol,
                                "market": market,
                                "skipped": "unsupported_market",
                            }
                        )
                    except Exception as exc:
                        # Provider or data failures stay isolated to this scan.
                        results.append(
                            {
                                "symbol": symbol,
                                "market": market,
                                "skipped": "scan_failed",
                                "errorClass": type(exc).__name__,
                            }
                        )
                    else:
                        results.append({"symbol": symbol, "market": market, **outcome})
        except Exception as exc:
            owners.append(
                {
                    "ownerUserId": owner_user_id,
                    "scanned": 0,
                    "results": [],
                    "skipped": "owner_scan_failed",
                    "errorClass": type(exc).__name__,
                }
            )
            continue

        scanned = len(results)
        total_scanned += scanned
        owners.append(
            {
                "ownerUserId": owner_user_id,
                "scanned": scanned,
                "results": results,
            }
        )

    return {
        "enabled": True,
        "owners": owners,
        "scanned": total_scanned,
    }


async def _active_watch_items(
    session: AsyncSession,
    owner_user_id: int,
) -> Sequence[tuple[str, InstrumentType, str | None]]:
    rows = await session.execute(
        select(Instrument.symbol, Instrument.type, Exchange.code)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .outerjoin(Exchange, Exchange.id == Instrument.exchange_id)
        .where(
            UserWatchItem.user_id == owner_user_id,
            UserWatchItem.is_active.is_(True),
            Instrument.is_active.is_(True),
        )
        .order_by(UserWatchItem.id)
    )
    return [
        (str(symbol), instrument_type, str(exchange_code) if exchange_code else None)
        for symbol, instrument_type, exchange_code in rows.all()
    ]


def _scan_market(
    instrument_type: InstrumentType,
    exchange_code: str | None,
) -> str | None:
    if instrument_type == InstrumentType.equity_kr:
        return (exchange_code or "KRX").upper()
    if instrument_type == InstrumentType.equity_us:
        return (exchange_code or "NASD").upper()
    if instrument_type == InstrumentType.crypto:
        return "CRYPTO"
    return None


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


__all__ = ["kasset_market_events_run"]
