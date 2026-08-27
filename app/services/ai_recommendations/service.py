"""Business rules for persisted AI recommendation review."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationAction,
    RecommendationDecision,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)
from app.services.ai_recommendations.repository import AIRecommendationRepository


class AIRecommendationServiceError(Exception):
    """Base class for stable recommendation API failures."""


class RecommendationNotFoundError(AIRecommendationServiceError):
    """Raised when the recommendation id does not exist."""


class RecommendationStateConflictError(AIRecommendationServiceError):
    """Raised when a terminal recommendation receives a different decision."""


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
        return await self._repository.list_by_status(status=status, limit=limit)

    async def decide(
        self,
        *,
        recommendation_id: str,
        decision: TerminalRecommendationDecision,
    ) -> AIRecommendation:
        if decision not in {
            RecommendationDecision.APPROVED,
            RecommendationDecision.REJECTED,
        }:
            raise RecommendationValidationError("invalid_decision")

        row = await self._repository.get(recommendation_id)
        if row is None:
            raise RecommendationNotFoundError(recommendation_id)

        if row.decision != RecommendationDecision.PENDING:
            if row.decision == decision:
                return row
            raise RecommendationStateConflictError(recommendation_id)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError(
                "recommendation clock must return a timezone-aware datetime"
            )
        now = now.astimezone(UTC).replace(microsecond=0)

        if decision == RecommendationDecision.APPROVED:
            self._validate_approval(row, now=now)

        resolved = await self._repository.resolve_pending(
            recommendation_id=recommendation_id,
            decision=decision,
            decided_at=now,
        )
        if resolved is not None:
            await self._session.commit()
            return resolved

        # Another transaction resolved the row after our read. End this
        # transaction before re-reading the committed terminal value.
        await self._session.rollback()
        current = await self._repository.get(recommendation_id)
        if current is None:
            raise RecommendationNotFoundError(recommendation_id)
        if current.decision == decision:
            return current
        raise RecommendationStateConflictError(recommendation_id)

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


__all__ = [
    "AIRecommendationService",
    "AIRecommendationServiceError",
    "RecommendationNotFoundError",
    "RecommendationStateConflictError",
    "RecommendationValidationError",
]
