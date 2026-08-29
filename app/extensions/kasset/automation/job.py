"""Production wiring for owner-scoped PAPER recommendation automation.

The pure consumer (``PaperAutomationConsumer``) speaks the string-owner
protocol from ``contracts``; Core persistence speaks integer ``users.id``.
This module owns that translation plus the scheduler-facing entrypoint so a
TaskIQ (or any other) scheduler can run one bounded automation sweep.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.paper_schemas import (
    OrderRequest,
    RiskAssessment,
    RiskReason,
)
from app.extensions.kasset.automation.consumer import PaperAutomationConsumer
from app.extensions.kasset.automation.contracts import (
    OwnerExecutionPolicy,
    PaperExecutionClaim,
    PaperExecutionOutcome,
)
from app.extensions.kasset.automation.policy import (
    AITradingPolicyService,
    OperatingMode,
)
from app.models.ai_recommendations import AIRecommendation, RecommendationDecision
from app.services.ai_recommendations.service import AIRecommendationService


class RuntimeStateSafetyGate:
    """Resolve the persisted operating mode again at every execution boundary."""

    def __init__(self, db: AsyncSession, *, automatic: bool = True) -> None:
        self._db = db
        self._automatic = automatic

    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy:
        snapshot = await AITradingPolicyService().get_snapshot(
            self._db,
            int(owner_user_id),
            now=now,
            execution_limit=0,
        )
        required_mode = (
            OperatingMode.AUTO_PAPER if self._automatic else OperatingMode.APPROVAL
        )
        enabled = snapshot.mode == required_mode
        if self._automatic:
            enabled = enabled and settings.AI_PAPER_AUTO_EXECUTION_ENABLED
        return OwnerExecutionPolicy(
            owner_user_id=owner_user_id,
            paper_automation_enabled=enabled,
            global_kill_switch_enabled=snapshot.kill_switch,
            trading_mode="PAPER",
        )


class OwnerScopedRecommendationService:
    """String-owner facade over the integer-owner recommendation service."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        recommendation_id: str | None = None,
    ) -> None:
        self._service = AIRecommendationService(db)
        self._recommendation_id = recommendation_id

    async def authorize_next_for_auto_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> None:
        await self._service.authorize_next_for_auto_execution(
            int(owner_user_id),
            now=now,
        )

    async def claim_for_paper_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> PaperExecutionClaim | None:
        row = await self._service.claim_for_paper_execution(
            int(owner_user_id),
            now,
            recommendation_id=self._recommendation_id,
            automation_only=True,
        )
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
    """Apply KAsset Hard Risk, then delegate only to the shared PAPER facade."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = (now or datetime.now(UTC)).replace(microsecond=0)

    async def preview(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> RiskAssessment:
        base = await paper_orders.preview(db, int(owner_user_id), request)
        hard_risk = await self._hard_risk(
            db,
            owner_user_id,
            request,
            reference_price=base.reference_price,
            base_reasons=base.reasons,
        )
        failed = [
            RiskReason(code=check.rule, message=check.detail)
            for check in hard_risk.checks
            if not check.passed
        ]
        if not hard_risk.passed and not failed:
            failed.append(
                RiskReason(
                    code="KILL_SWITCH",
                    message=hard_risk.blocked_reason or "Hard Risk 차단",
                )
            )
        return RiskAssessment(
            decision="APPROVED" if hard_risk.passed else "REJECTED",
            reasons=failed,
            estimated_amount=base.estimated_amount,
            estimated_fee=base.estimated_fee,
            reference_price=base.reference_price,
            currency=base.currency,
        )

    async def submit(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> tuple[object, bool]:
        base = await paper_orders.preview(db, int(owner_user_id), request)
        hard_risk = await self._hard_risk(
            db,
            owner_user_id,
            request,
            reference_price=base.reference_price,
            base_reasons=base.reasons,
        )
        if not hard_risk.passed:
            raise MobileApiError(
                409,
                "HARD_RISK_REJECTED",
                "Hard Risk 재검증에서 PAPER 주문이 차단되었습니다.",
                {
                    "blockedReason": hard_risk.blocked_reason,
                    "checks": [check.as_evidence() for check in hard_risk.checks],
                },
            )
        return await paper_orders.submit(db, int(owner_user_id), request)

    async def _hard_risk(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
        *,
        reference_price: str | None,
        base_reasons: Sequence[RiskReason],
    ):
        recommendation_id = _recommendation_id_from_client_order(
            request.client_order_id
        )
        recommendation = await AIRecommendationService(db).get_recommendation(
            int(owner_user_id),
            recommendation_id,
        )
        try:
            price = Decimal(
                reference_price
                if reference_price is not None
                else str(recommendation.reference_price)
            )
            confidence = Decimal(str(recommendation.confidence))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("recommendation numeric evidence is invalid") from exc
        return await AITradingPolicyService().evaluate_hard_risk(
            db,
            int(owner_user_id),
            action=request.side,
            market=request.market,
            symbol=request.symbol,
            quantity=request.quantity,
            reference_price=price,
            ai_confidence=confidence,
            now=self._now,
            base_risk_reasons=base_reasons,
        )


async def _claimable_owner_ids(db: AsyncSession, now: datetime) -> list[int]:
    rows = await db.execute(
        select(AIRecommendation.owner_user_id)
        .distinct()
        .where(
            AIRecommendation.decision.in_(
                (
                    RecommendationDecision.PENDING,
                    RecommendationDecision.APPROVED,
                )
            ),
            AIRecommendation.action.in_(("BUY", "SELL")),
            AIRecommendation.paper_execution_status.is_(None),
            AIRecommendation.valid_until > now,
            AIRecommendation.source == "kasset-automation",
        )
        .order_by(AIRecommendation.owner_user_id)
    )
    return [int(owner_id) for (owner_id,) in rows.all()]


def _recommendation_id_from_client_order(client_order_id: str | None) -> str:
    value = str(client_order_id or "")
    prefix = "ai-rec:"
    if not value.startswith(prefix) or not value[len(prefix) :].strip():
        raise ValueError("AI PAPER order requires a recommendation clientOrderId")
    return value[len(prefix) :]


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
                snapshot = await AITradingPolicyService().get_snapshot(
                    db,
                    owner_id,
                    now=current,
                    execution_limit=0,
                )
                if snapshot.mode != OperatingMode.AUTO_PAPER:
                    outcome = PaperExecutionOutcome(
                        status="BLOCKED",
                        reason="auto_paper_mode_required",
                    )
                else:
                    recommendation_service = OwnerScopedRecommendationService(db)
                    await recommendation_service.authorize_next_for_auto_execution(
                        str(owner_id),
                        current,
                    )
                    consumer = PaperAutomationConsumer(
                        owner_user_id=str(owner_id),
                        safety_gate=RuntimeStateSafetyGate(db, automatic=True),
                        recommendation_service=recommendation_service,
                        paper_orders=OwnerScopedPaperOrders(now=current),
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


async def run_approved_recommendation_once(
    owner_user_id: int,
    recommendation_id: str,
    *,
    now: datetime | None = None,
) -> PaperExecutionOutcome:
    """Synchronously execute one explicit APPROVAL decision in PAPER only."""

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    async with _session() as db:
        consumer = PaperAutomationConsumer(
            owner_user_id=str(owner_user_id),
            safety_gate=RuntimeStateSafetyGate(db, automatic=False),
            recommendation_service=OwnerScopedRecommendationService(
                db,
                recommendation_id=recommendation_id,
            ),
            paper_orders=OwnerScopedPaperOrders(now=current),
            db=db,
        )
        return await consumer.run_once(now=current)


__all__ = [
    "OwnerScopedPaperOrders",
    "OwnerScopedRecommendationService",
    "RuntimeStateSafetyGate",
    "run_paper_automation_once",
    "run_approved_recommendation_once",
]
