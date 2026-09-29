"""KRX 1분 실시간 단타 주기: 보호 청산 → 흐름 악화 청산 → KRX 전용 집행.

순서가 계약이다.

1. 기존 Position Manager 보호 평가(손절·조기 보호선·부분익절·TIME_STOP)를
   KRX 보유분에 먼저 돌린다. 손절이 이미 닿았는데 SELL이 아직 없는 상태에서
   흐름 청산이 먼저 나가는 경쟁을 막는다.
2. 그다음 NH 관찰 snapshot으로 평가익 보유분의 흐름 악화 조기 청산을 본다.
   같은 사이클에 끝나지 않은 청산이 있으면 만들지 않는다.
3. 마지막으로 ``run_paper_automation_once(markets={"KRX"})``로 KRX 추천만
   집행한다. US 추천은 이 주기에서 절대 집행하지 않는다.

NH 관찰이 없거나 Redis가 죽어도 1번 보호 청산과 3번의 SELL 집행은 그대로
돈다. 신규 KRX BUY만 실시간 관문에서 막힌다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.automation import job
from app.extensions.kasset.automation.market_session import current_regular_session
from app.extensions.kasset.automation.policy import (
    AITradingPolicyService,
    OperatingMode,
)
from app.extensions.kasset.automation.position_manager_service import (
    PaperPositionManagerService,
)
from app.extensions.kasset.automation.strategy_artifact import (
    current_strategy_artifact,
)
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PAPER_STRATEGY_VERSION,
)
from app.extensions.kasset.models import AndroidPaperAccount
from app.extensions.kasset.nhplug.tape_store import (
    RealtimeEntryGate,
    RedisTapeStore,
    TapeSnapshotReader,
)
from app.models.paper_trading import PaperPosition
from app.models.trading import InstrumentType, User, UserRole

logger = logging.getLogger(__name__)

KRX_MARKETS: Final = frozenset({"KRX"})


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


async def _auto_paper_owner_ids(db: AsyncSession, now: datetime) -> list[int]:
    owner_ids = (
        await db.scalars(
            select(User.id)
            .where(User.role == UserRole.trader, User.is_active.is_(True))
            .order_by(User.id)
        )
    ).all()
    selected: list[int] = []
    policy = AITradingPolicyService()
    for raw_owner_id in owner_ids:
        owner_id = int(raw_owner_id)
        snapshot = await policy.get_snapshot(db, owner_id, now=now, execution_limit=0)
        if snapshot.mode == OperatingMode.AUTO_PAPER and not snapshot.kill_switch:
            selected.append(owner_id)
    await db.commit()
    return selected


async def _held_krx_symbols(db: AsyncSession, owner_user_id: int) -> list[str]:
    return [
        str(symbol)
        for symbol in (
            await db.scalars(
                select(PaperPosition.symbol)
                .join(
                    AndroidPaperAccount,
                    AndroidPaperAccount.paper_account_id == PaperPosition.account_id,
                )
                .where(
                    AndroidPaperAccount.owner_user_id == owner_user_id,
                    PaperPosition.quantity > 0,
                    PaperPosition.instrument_type == InstrumentType.equity_kr,
                )
            )
        ).all()
    ]


async def run_kr_realtime_once(
    *,
    now: datetime | None = None,
    tape_reader: TapeSnapshotReader | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    execute: Callable[..., Awaitable[dict[str, object]]] | None = None,
) -> dict[str, object]:
    """1분 KRX 주기 한 번. 정규장이 아니면 DB를 건드리지 않는다."""

    current = (now or clock()).replace(microsecond=0)
    if not settings.AI_PAPER_AUTO_EXECUTION_ENABLED:
        return {"enabled": False}
    if current_regular_session("KRX", current) is None:
        return {"enabled": True, "skipped": "krx_regular_session_closed"}

    owned_store: RedisTapeStore | None = None
    reader = tape_reader
    if reader is None:
        owned_store = RedisTapeStore.from_settings()
        reader = owned_store
    try:
        async with _session() as db:
            owner_ids = await _auto_paper_owner_ids(db, current)
        fingerprint = current_strategy_artifact().fingerprint
        owners: list[dict[str, object]] = []
        for owner_id in owner_ids:
            try:
                async with _session() as db:
                    protective = await PaperPositionManagerService(
                        db,
                        now=clock(),
                        strategy_version=DEFAULT_PAPER_STRATEGY_VERSION,
                        strategy_fingerprint=fingerprint,
                    ).run_owner(owner_id, markets=KRX_MARKETS)
                    symbols = await _held_krx_symbols(db, owner_id)
                    snapshots = await reader.read_many(symbols) if symbols else {}
                    # 신선도는 snapshot을 읽은 직후 시각으로 판정한다.
                    trend = await PaperPositionManagerService(
                        db,
                        now=clock(),
                        strategy_version=DEFAULT_PAPER_STRATEGY_VERSION,
                        strategy_fingerprint=fingerprint,
                    ).run_realtime_trend_exits(owner_id, snapshots=dict(snapshots))
                owners.append(
                    {
                        "ownerUserId": owner_id,
                        "protectiveExitIds": list(protective),
                        "realtimeTrendExitIds": list(trend),
                        "observedSymbols": sorted(snapshots),
                    }
                )
            except Exception as exc:  # 한 owner 실패가 다른 owner를 막지 않는다
                logger.exception(
                    "kasset realtime KR owner cycle failed: owner_user_id=%s",
                    owner_id,
                )
                owners.append({"ownerUserId": owner_id, "error": type(exc).__name__})
        runner = execute or job.run_paper_automation_once
        execution = await runner(
            now=clock(),
            markets=KRX_MARKETS,
            realtime_gate=RealtimeEntryGate(reader, clock=clock),
        )
    finally:
        if owned_store is not None:
            await owned_store.aclose()
    result: dict[str, object] = {
        "enabled": True,
        "owners": owners,
        "execution": execution,
    }
    logger.info("kasset realtime KR cycle done: %s", result)
    return result


__all__ = ["KRX_MARKETS", "run_kr_realtime_once"]
