"""Claimed recommendation consumer with explicit PAPER safety gates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.extensions.kasset.api.errors import MobileApiError
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
        if self._claim_lease_block_reason(claim, now=current) is not None:
            return PaperExecutionOutcome(
                status="FAILED",
                reason="invalid_claim_lease",
                recommendation_id=claim.id,
            )

        client_order_id = f"ai-rec:{claim.id}"
        found, existing_outcome = await self._reconcile_client_order(
            claim,
            current,
            client_order_id,
        )
        if found:
            assert existing_outcome is not None
            return existing_outcome

        request = self._build_order_request(claim)
        if request is None:
            return await self._finish_failure(claim, current, "invalid_order_intent")

        invalid_reason = self._claim_block_reason(claim, now=current)
        if invalid_reason is not None:
            return await self._finish_failure(claim, current, invalid_reason)

        try:
            assessment = await self._paper_orders.preview(
                self._db,
                self._owner_user_id,
                request,
            )
        except Exception as exc:
            deterministic = self._deterministic_exception_reason("preview", exc)
            if deterministic is not None:
                return await self._finish_failure(claim, current, deterministic)
            return self._retryable_outcome(
                claim,
                f"preview_ambiguous:{type(exc).__name__}",
            )
        if assessment.decision != "APPROVED":
            codes = ",".join(
                str(getattr(reason, "code", "UNKNOWN")) for reason in assessment.reasons
            )
            detail = (
                f"risk_preview_rejected:{codes}" if codes else "risk_preview_rejected"
            )
            return await self._finish_failure(claim, current, detail)

        # Opt-in, owner mode, and the global switch are mutable. Re-read them
        # after preview so a revoked permission cannot race into submit.
        try:
            latest_policy = await self._safety_gate.get_policy(
                owner_user_id=self._owner_user_id,
                now=current,
            )
        except Exception as exc:
            return self._retryable_outcome(
                claim,
                f"safety_gate_ambiguous:{type(exc).__name__}",
            )
        blocked = self._policy_block_reason(latest_policy)
        if blocked is not None:
            return await self._finish_failure(
                claim,
                current,
                f"safety_gate_changed:{blocked}",
            )

        found, existing_outcome = await self._reconcile_client_order(
            claim,
            current,
            request.client_order_id,
        )
        if found:
            assert existing_outcome is not None
            return existing_outcome

        try:
            envelope, replayed = await self._paper_orders.submit(
                self._db,
                self._owner_user_id,
                request,
            )
        except Exception as exc:
            try:
                await self._rollback_for_reconciliation()
            except Exception as rollback_exc:
                return self._retryable_outcome(
                    claim,
                    f"submit_rollback_ambiguous:{type(rollback_exc).__name__}",
                )
            found, existing_outcome = await self._reconcile_client_order(
                claim,
                current,
                request.client_order_id,
            )
            if found:
                assert existing_outcome is not None
                return existing_outcome
            deterministic = self._deterministic_exception_reason("submit", exc)
            if deterministic is not None:
                return await self._finish_failure(claim, current, deterministic)
            return self._retryable_outcome(
                claim,
                f"submit_ambiguous:{type(exc).__name__}",
            )

        return await self._finish_order_result(
            claim,
            current,
            envelope,
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
    def _claim_lease_block_reason(
        claim: ClaimedRecommendation,
        *,
        now: datetime,
    ) -> str | None:
        token = getattr(claim, "paper_execution_token", None)
        attempt_count = getattr(claim, "paper_execution_attempt_count", None)
        if not isinstance(token, str) or not token.strip():
            return "claim_token_missing"
        if not isinstance(attempt_count, int) or attempt_count < 1:
            return "claim_attempt_invalid"
        try:
            claimed_at = utc_datetime(
                claim.paper_execution_claimed_at,
                field_name="claim.paper_execution_claimed_at",
            )
            lease_expires_at = utc_datetime(
                claim.paper_execution_lease_expires_at,
                field_name="claim.paper_execution_lease_expires_at",
            )
        except (AttributeError, TypeError, ValueError):
            return "claim_lease_invalid"
        if lease_expires_at <= claimed_at or lease_expires_at <= now:
            return "claim_lease_invalid"
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
    def _paper_order(value: object) -> object:
        return getattr(value, "order", value)

    @classmethod
    def _paper_order_id(cls, value: object) -> str | None:
        order_id = getattr(cls._paper_order(value), "id", None)
        if not isinstance(order_id, str) or not order_id.strip():
            return None
        return order_id

    @classmethod
    def _paper_order_status(cls, value: object) -> str:
        return str(getattr(cls._paper_order(value), "status", "")).strip().upper()

    async def _rollback_for_reconciliation(self) -> None:
        rollback = getattr(self._db, "rollback", None)
        if rollback is not None:
            await rollback()

    async def _reconcile_client_order(
        self,
        claim: ClaimedRecommendation,
        completed_at: datetime,
        client_order_id: str,
    ) -> tuple[bool, PaperExecutionOutcome | None]:
        try:
            order = await self._paper_orders.get_by_client_order_id(
                self._db,
                self._owner_user_id,
                client_order_id,
            )
        except Exception as exc:
            return True, self._retryable_outcome(
                claim,
                f"order_lookup_ambiguous:{type(exc).__name__}",
            )
        if order is None:
            return False, None
        try:
            reconciled = await self._paper_orders.reconcile(
                self._db,
                self._owner_user_id,
                order,
            )
        except Exception as exc:
            try:
                latest = await self._paper_orders.get_by_client_order_id(
                    self._db,
                    self._owner_user_id,
                    client_order_id,
                )
            except Exception:
                latest = None
            if latest is not None and self._paper_order_status(latest) == "REJECTED":
                return True, await self._finish_failure(
                    claim,
                    completed_at,
                    "paper_order_rejected",
                )
            return True, self._retryable_outcome(
                claim,
                f"order_reconciliation_ambiguous:{type(exc).__name__}",
            )
        return True, await self._finish_order_result(
            claim,
            completed_at,
            reconciled,
            replayed=True,
        )

    async def _finish_order_result(
        self,
        claim: ClaimedRecommendation,
        completed_at: datetime,
        result: object,
        *,
        replayed: bool,
    ) -> PaperExecutionOutcome:
        status = self._paper_order_status(result)
        if status == "REJECTED":
            return await self._finish_failure(
                claim,
                completed_at,
                "paper_order_rejected",
            )
        if status != "FILLED":
            return self._retryable_outcome(
                claim,
                f"paper_order_not_final:{status or 'UNKNOWN'}",
                replayed=replayed,
            )
        paper_order_id = self._paper_order_id(result)
        if paper_order_id is None:
            return self._retryable_outcome(
                claim,
                "submit_result_missing_order_id",
                replayed=replayed,
            )
        try:
            await self._recommendations.complete_paper_execution(
                self._owner_user_id,
                claim.id,
                claim.paper_execution_token,
                paper_order_id,
                completed_at,
            )
        except Exception as exc:
            try:
                converged = (
                    await self._recommendations.reconcile_paper_execution_completion(
                        self._owner_user_id,
                        claim.id,
                        claim.paper_execution_token,
                        paper_order_id,
                        completed_at,
                    )
                )
            except Exception as reconcile_exc:
                return self._retryable_outcome(
                    claim,
                    (
                        "completion_reconciliation_ambiguous:"
                        f"{type(reconcile_exc).__name__}"
                    ),
                    replayed=replayed,
                )
            if not converged:
                return self._retryable_outcome(
                    claim,
                    f"completion_not_converged:{type(exc).__name__}",
                    replayed=replayed,
                )
            reason = "completion_reconciled"
        else:
            reason = "idempotent_replay" if replayed else "submitted"
        return PaperExecutionOutcome(
            status="SUBMITTED",
            reason=reason,
            recommendation_id=claim.id,
            replayed=replayed,
        )

    @staticmethod
    def _deterministic_exception_reason(
        stage: str,
        exc: Exception,
    ) -> str | None:
        if isinstance(exc, MobileApiError) and 400 <= exc.status_code < 500:
            return f"{stage}_rejected:{exc.code}"
        if isinstance(exc, (InvalidOperation, ValueError)):
            return f"{stage}_rejected:{type(exc).__name__}"
        return None

    @staticmethod
    def _retryable_outcome(
        claim: ClaimedRecommendation,
        reason: str,
        *,
        replayed: bool = False,
    ) -> PaperExecutionOutcome:
        return PaperExecutionOutcome(
            status="FAILED",
            reason=reason,
            recommendation_id=claim.id,
            replayed=replayed,
        )

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
                claim.paper_execution_token,
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
