"""Production wiring for owner-scoped PAPER recommendation automation.

The pure consumer (``PaperAutomationConsumer``) speaks the string-owner
protocol from ``contracts``; Core persistence speaks integer ``users.id``.
This module owns that translation plus the scheduler-facing entrypoint so a
TaskIQ (or any other) scheduler can run one bounded automation sweep.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.paper_schemas import OrderRequest, RiskAssessment
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.automation.consumer import PaperAutomationConsumer
from app.extensions.kasset.automation.contracts import (
    OwnerExecutionPolicy,
    PaperExecutionClaim,
    PaperExecutionOutcome,
)
from app.models.ai_recommendations import AIRecommendation, RecommendationDecision
from app.services.ai_recommendations.service import AIRecommendationService


class RuntimeStateSafetyGate:
    """Owner execution policy sourced from persisted runtime state.

    Any engaged kill switch (global or owner) reports as the global switch:
    the consumer only needs to know that execution is forbidden right now.
    Opt-in is the deliberate operator-level ``AI_PAPER_AUTO_EXECUTION_ENABLED``
    flag; there is no per-owner automation opt-in in Phase 1.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy:
        owner_id = int(owner_user_id)
        state = await runtime_state.get(self._db, owner_id)
        global_state = await runtime_state.get_global(self._db)
        return OwnerExecutionPolicy(
            owner_user_id=owner_user_id,
            paper_automation_enabled=settings.AI_PAPER_AUTO_EXECUTION_ENABLED,
            global_kill_switch_enabled=(
                global_state.kill_switch_enabled or state.kill_switch_enabled
            ),
            trading_mode=cast(Any, str(state.trading_mode).strip().upper()),
        )


class OwnerScopedRecommendationService:
    """String-owner facade over the integer-owner recommendation service."""

    def __init__(self, db: AsyncSession) -> None:
        self._service = AIRecommendationService(db)

    async def claim_for_paper_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> PaperExecutionClaim | None:
        row = await self._service.claim_for_paper_execution(int(owner_user_id), now)
        if row is None:
            return None
        return PaperExecutionClaim(
            id=row.id,
            owner_user_id=str(row.owner_user_id),
            decision=row.decision,
            action=row.action,
            market=row.market,
            symbol=row.symbol,
            suggested_quantity=row.suggested_quantity,
            valid_until=row.valid_until,
        )

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        paper_order_id: str,
        now: datetime,
    ) -> None:
        await self._service.complete_paper_execution(
            int(owner_user_id),
            recommendation_id,
            paper_order_id,
            now,
        )

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        error: str,
        now: datetime,
    ) -> None:
        await self._service.fail_paper_execution(
            int(owner_user_id),
            recommendation_id,
            error,
            now,
        )


class OwnerScopedPaperOrders:
    """String-owner facade over the shared PAPER order facade."""

    async def preview(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> RiskAssessment:
        return await paper_orders.preview(db, int(owner_user_id), request)

    async def submit(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> tuple[object, bool]:
        return await paper_orders.submit(db, int(owner_user_id), request)


async def _claimable_owner_ids(db: AsyncSession, now: datetime) -> list[int]:
    rows = await db.execute(
        select(AIRecommendation.owner_user_id)
        .distinct()
        .where(
            AIRecommendation.decision == RecommendationDecision.APPROVED,
            AIRecommendation.paper_execution_status.is_(None),
            AIRecommendation.valid_until > now,
        )
        .order_by(AIRecommendation.owner_user_id)
    )
    return [int(owner_id) for (owner_id,) in rows.all()]


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


async def run_paper_automation_once(
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run one bounded automation sweep: at most one execution per owner.

    Fail-closed by default: with ``AI_PAPER_AUTO_EXECUTION_ENABLED`` false the
    sweep reports itself disabled without touching the database. One owner's
    failure never aborts the other owners' sweeps.
    """

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    if not settings.AI_PAPER_AUTO_EXECUTION_ENABLED:
        return {"enabled": False, "owners": 0, "outcomes": []}

    async with _session() as db:
        owner_ids = await _claimable_owner_ids(db, current)

    outcomes: list[dict[str, object]] = []
    for owner_id in owner_ids:
        try:
            async with _session() as db:
                consumer = PaperAutomationConsumer(
                    owner_user_id=str(owner_id),
                    safety_gate=RuntimeStateSafetyGate(db),
                    recommendation_service=OwnerScopedRecommendationService(db),
                    paper_orders=OwnerScopedPaperOrders(),
                    db=db,
                )
                outcome = await consumer.run_once(now=current)
        except Exception as exc:  # one owner's failure must not stop the sweep
            outcome = PaperExecutionOutcome(
                status="FAILED",
                reason=f"owner_sweep_failed:{type(exc).__name__}",
            )
        outcomes.append(
            {
                "owner_user_id": owner_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "recommendation_id": outcome.recommendation_id,
                "replayed": outcome.replayed,
            }
        )
    return {"enabled": True, "owners": len(owner_ids), "outcomes": outcomes}


__all__ = [
    "OwnerScopedPaperOrders",
    "OwnerScopedRecommendationService",
    "RuntimeStateSafetyGate",
    "run_paper_automation_once",
]
