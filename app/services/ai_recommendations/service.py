"""Business rules for owner-scoped AI recommendation review and PAPER claims."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationAction,
    RecommendationDecision,
    RecommendationExecutionStatus,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)
from app.services.ai_recommendations.repository import AIRecommendationRepository


class AIRecommendationServiceError(Exception):
    """Base class for stable recommendation API failures."""


class RecommendationNotFoundError(AIRecommendationServiceError):
    """Raised when the owner's recommendation id does not exist."""


class RecommendationStateConflictError(AIRecommendationServiceError):
    """Raised when a terminal transition conflicts with persisted state."""


class RecommendationValidationError(AIRecommendationServiceError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class AIRecommendationService:
    MAX_LIMIT = 100

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = session
        self._repository = AIRecommendationRepository(session)
        self._clock = clock

    async def list_recommendations(
        self,
        owner_user_id: int,
        *,
        status: RecommendationStatusGroup,
        limit: int = 50,
    ) -> list[AIRecommendation]:
        if status not in {"PENDING", "RESOLVED"}:
            raise RecommendationValidationError("invalid_status")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise RecommendationValidationError("invalid_limit")
        if limit < 1 or limit > self.MAX_LIMIT:
            raise RecommendationValidationError("invalid_limit")
        return await self._repository.list_by_status(
            owner_user_id,
            status=status,
            limit=limit,
        )

    async def decide(
        self,
        owner_user_id: int,
        *,
        recommendation_id: str,
        decision: TerminalRecommendationDecision,
    ) -> AIRecommendation:
        if decision not in {
            RecommendationDecision.APPROVED,
            RecommendationDecision.REJECTED,
        }:
            raise RecommendationValidationError("invalid_decision")

        row = await self._repository.get(owner_user_id, recommendation_id)
        if row is None:
            raise RecommendationNotFoundError(recommendation_id)

        if row.decision != RecommendationDecision.PENDING:
            if row.decision == decision:
                return row
            raise RecommendationStateConflictError(recommendation_id)

        now = self._normalized_now(self._clock())
        if decision == RecommendationDecision.APPROVED:
            self._validate_approval(row, now=now)

        resolved = await self._repository.resolve_pending(
            owner_user_id,
            recommendation_id=recommendation_id,
            decision=decision,
            decided_at=now,
        )
        if resolved is not None:
            await self._session.commit()
            return resolved

        await self._session.rollback()
        current = await self._repository.get(owner_user_id, recommendation_id)
        if current is None:
            raise RecommendationNotFoundError(recommendation_id)
        if current.decision == decision:
            return current
        raise RecommendationStateConflictError(recommendation_id)

    async def claim_for_paper_execution(
        self,
        owner_user_id: int,
        now: datetime,
    ) -> AIRecommendation | None:
        claimed_at = self._normalized_now(now)
        row = await self._repository.claim_for_paper_execution(
            owner_user_id,
            claimed_at,
        )
        if row is None:
            await self._session.rollback()
            return None
        row.paper_execution_status = RecommendationExecutionStatus.CLAIMED
        row.paper_execution_claimed_at = claimed_at
        row.updated_at = claimed_at
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def complete_paper_execution(
        self,
        owner_user_id: int,
        recommendation_id: str,
        paper_order_id: str,
        now: datetime,
    ) -> AIRecommendation:
        normalized_order_id = paper_order_id.strip()
        if not normalized_order_id:
            raise RecommendationValidationError("paper_order_id_required")
        completed_at = self._normalized_now(now)
        row = await self._repository.get(
            owner_user_id,
            recommendation_id,
            for_update=True,
        )
        if row is None:
            raise RecommendationNotFoundError(recommendation_id)
        if row.paper_execution_status == RecommendationExecutionStatus.SUCCEEDED:
            if row.paper_order_id == normalized_order_id:
                await self._session.commit()
                return row
            raise RecommendationStateConflictError(recommendation_id)
        if row.paper_execution_status != RecommendationExecutionStatus.CLAIMED:
            raise RecommendationStateConflictError(recommendation_id)
        row.paper_execution_status = RecommendationExecutionStatus.SUCCEEDED
        row.paper_execution_completed_at = completed_at
        row.paper_order_id = normalized_order_id
        row.paper_execution_error = None
        row.updated_at = completed_at
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def fail_paper_execution(
        self,
        owner_user_id: int,
        recommendation_id: str,
        error: str,
        now: datetime,
    ) -> AIRecommendation:
        normalized_error = error.strip()[:1000]
        if not normalized_error:
            raise RecommendationValidationError("paper_execution_error_required")
        completed_at = self._normalized_now(now)
        row = await self._repository.get(
            owner_user_id,
            recommendation_id,
            for_update=True,
        )
        if row is None:
            raise RecommendationNotFoundError(recommendation_id)
        if row.paper_execution_status == RecommendationExecutionStatus.FAILED:
            await self._session.commit()
            return row
        if row.paper_execution_status != RecommendationExecutionStatus.CLAIMED:
            raise RecommendationStateConflictError(recommendation_id)
        row.paper_execution_status = RecommendationExecutionStatus.FAILED
        row.paper_execution_completed_at = completed_at
        row.paper_order_id = None
        row.paper_execution_error = normalized_error
        row.updated_at = completed_at
        await self._session.commit()
        await self._session.refresh(row)
        return row

    @staticmethod
    def _validate_approval(row: AIRecommendation, *, now: datetime) -> None:
        if row.action not in {
            RecommendationAction.BUY,
            RecommendationAction.SELL,
        }:
            raise RecommendationValidationError("action_not_approvable")
        if not any(item.strip() for item in row.rationale if isinstance(item, str)):
            raise RecommendationValidationError("rationale_required")
        if row.valid_until is None:
            raise RecommendationValidationError("valid_until_required")
        if row.valid_until.tzinfo is None or row.valid_until.utcoffset() is None:
            raise RecommendationValidationError("valid_until_invalid")
        if row.valid_until <= now:
            raise RecommendationValidationError("recommendation_expired")

    @staticmethod
    def _normalized_now(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError(
                "recommendation clock must return a timezone-aware datetime"
            )
        return value.astimezone(UTC).replace(microsecond=0)


__all__ = [
    "AIRecommendationService",
    "AIRecommendationServiceError",
    "RecommendationNotFoundError",
    "RecommendationStateConflictError",
    "RecommendationValidationError",
]
