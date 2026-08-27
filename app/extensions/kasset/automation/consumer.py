"""Claimed recommendation consumer with explicit PAPER safety gates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.extensions.kasset.api.paper_schemas import OrderRequest
from app.extensions.kasset.automation.contracts import (
    ClaimedRecommendation,
    ExecutionSafetyGate,
    OwnerExecutionPolicy,
    PaperExecutionOutcome,
    PaperOrderFacade,
    RecommendationService,
    utc_datetime,
)


class PaperAutomationConsumer:
    """Executes at most one already-claimed recommendation per invocation.

    Claim selection and completion are owned by ``RecommendationService``.
    Order mutation is available only through ``PaperOrderFacade``.  This class
    does not contain a LIVE provider path and never derives a quantity.
    """

    def __init__(
        self,
        *,
        owner_user_id: str,
        safety_gate: ExecutionSafetyGate,
        recommendation_service: RecommendationService,
        paper_orders: PaperOrderFacade,
        db: Any,
    ) -> None:
        owner = owner_user_id.strip()
        if not owner:
            raise ValueError("owner_user_id is required")
        self._owner_user_id = owner
        self._safety_gate = safety_gate
        self._recommendations = recommendation_service
        self._paper_orders = paper_orders
        self._db = db

    async def run_once(self, *, now: datetime) -> PaperExecutionOutcome:
        current = utc_datetime(now, field_name="now").replace(microsecond=0)
        try:
            policy = await self._safety_gate.get_policy(
                owner_user_id=self._owner_user_id,
                now=current,
            )
        except Exception as exc:
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason=f"safety_gate_failed:{type(exc).__name__}",
            )
        blocked = self._policy_block_reason(policy)
        if blocked is not None:
            return PaperExecutionOutcome(status="BLOCKED", reason=blocked)

        claim = await self._recommendations.claim_for_paper_execution(
            self._owner_user_id,
            current,
        )
        if claim is None:
            return PaperExecutionOutcome(status="IDLE", reason="no_eligible_claim")
        if claim.owner_user_id != self._owner_user_id:
            return PaperExecutionOutcome(
                status="FAILED",
                reason="claim_owner_mismatch",
                recommendation_id=claim.id,
            )

        invalid_reason = self._claim_block_reason(claim, now=current)
        if invalid_reason is not None:
            return await self._finish_failure(claim, current, invalid_reason)

        request = self._build_order_request(claim)
        if request is None:
            return await self._finish_failure(claim, current, "invalid_order_intent")

        try:
            assessment = await self._paper_orders.preview(
                self._db,
                self._owner_user_id,
                request,
            )
        except Exception as exc:  # stable task failure boundary
            return await self._finish_failure(
                claim,
                current,
                f"preview_failed:{type(exc).__name__}",
            )
        if assessment.decision != "APPROVED":
            return await self._finish_failure(claim, current, "risk_preview_rejected")

        # Opt-in, owner mode, and the global switch are mutable. Re-read them
        # after preview so a revoked permission cannot race into submit.
        try:
            latest_policy = await self._safety_gate.get_policy(
                owner_user_id=self._owner_user_id,
                now=current,
            )
        except Exception as exc:
            return await self._finish_failure(
                claim,
                current,
                f"safety_gate_failed:{type(exc).__name__}",
            )
        blocked = self._policy_block_reason(latest_policy)
        if blocked is not None:
            return await self._finish_failure(
                claim,
                current,
                f"safety_gate_changed:{blocked}",
            )

        try:
            envelope, replayed = await self._paper_orders.submit(
                self._db,
                self._owner_user_id,
                request,
            )
        except Exception as exc:  # stable task failure boundary
            return await self._finish_failure(
                claim,
                current,
                f"submit_failed:{type(exc).__name__}",
            )

        paper_order_id = self._paper_order_id(envelope)
        if paper_order_id is None:
            return await self._finish_failure(
                claim,
                current,
                "submit_result_missing_order_id",
            )
        try:
            await self._recommendations.complete_paper_execution(
                self._owner_user_id,
                claim.id,
                paper_order_id,
                current,
            )
        except Exception as exc:  # order side effect is already idempotently keyed
            return PaperExecutionOutcome(
                status="FAILED",
                reason=f"completion_failed:{type(exc).__name__}",
                recommendation_id=claim.id,
                replayed=replayed,
            )
        return PaperExecutionOutcome(
            status="SUBMITTED",
            reason="idempotent_replay" if replayed else "submitted",
            recommendation_id=claim.id,
            replayed=replayed,
        )

    def _policy_block_reason(self, policy: OwnerExecutionPolicy) -> str | None:
        if policy.owner_user_id != self._owner_user_id:
            return "policy_owner_mismatch"
        if policy.global_kill_switch_enabled:
            return "global_kill_switch_enabled"
        if not policy.paper_automation_enabled:
            return "owner_opt_in_disabled"
        if policy.trading_mode == "LIVE":
            return "live_mode_forbidden"
        if policy.trading_mode != "PAPER":
            return "paper_mode_required"
        return None

    @staticmethod
    def _claim_block_reason(
        claim: ClaimedRecommendation,
        *,
        now: datetime,
    ) -> str | None:
        if str(claim.decision).strip().upper() != "APPROVED":
            return "recommendation_not_approved"
        if str(claim.action).strip().upper() not in {"BUY", "SELL"}:
            return "recommendation_not_actionable"
        try:
            valid_until = utc_datetime(
                claim.valid_until,
                field_name="claim.valid_until",
            )
        except (AttributeError, TypeError, ValueError):
            return "invalid_expiry"
        if valid_until <= now:
            return "recommendation_expired"
        return None

    @staticmethod
    def _build_order_request(claim: ClaimedRecommendation) -> OrderRequest | None:
        try:
            quantity = Decimal(str(claim.suggested_quantity))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not quantity.is_finite() or quantity <= 0:
            return None
        market = str(claim.market).strip().upper()
        symbol = str(claim.symbol).strip().upper()
        if market not in {"KRX", "US"} or not symbol:
            return None
        try:
            return OrderRequest(
                clientOrderId=f"ai-rec:{claim.id}",
                broker="PAPER",
                accountId=None,
                market=market,
                symbol=symbol,
                side=str(claim.action).strip().upper(),
                orderType="MARKET",
                quantity=quantity,
            )
        except ValueError:
            return None

    @staticmethod
    def _paper_order_id(envelope: object) -> str | None:
        order = getattr(envelope, "order", None)
        order_id = getattr(order, "id", None)
        if not isinstance(order_id, str) or not order_id.strip():
            return None
        return order_id

    async def _finish_failure(
        self,
        claim: ClaimedRecommendation,
        completed_at: datetime,
        detail: str,
    ) -> PaperExecutionOutcome:
        try:
            await self._recommendations.fail_paper_execution(
                self._owner_user_id,
                claim.id,
                detail,
                completed_at,
            )
        except Exception as exc:
            return PaperExecutionOutcome(
                status="FAILED",
                reason=f"failure_completion_failed:{type(exc).__name__}",
                recommendation_id=claim.id,
            )
        return PaperExecutionOutcome(
            status="REJECTED",
            reason=detail,
            recommendation_id=claim.id,
        )


__all__ = ["PaperAutomationConsumer"]
