"""Persistence operations for review-only AI recommendations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationDecision,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)


class AIRecommendationRepository:
    """Own the recommendation query and decision compare-and-set operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, recommendation_id: str) -> AIRecommendation | None:
        statement = select(AIRecommendation).where(
            AIRecommendation.id == recommendation_id
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def list_by_status(
        self,
        *,
        status: RecommendationStatusGroup,
        limit: int,
    ) -> list[AIRecommendation]:
        if status == "PENDING":
            decision_filter = (
                AIRecommendation.decision == RecommendationDecision.PENDING
            )
        else:
            decision_filter = AIRecommendation.decision.in_(
                (
                    RecommendationDecision.APPROVED,
                    RecommendationDecision.REJECTED,
                )
            )
        statement = (
            select(AIRecommendation)
            .where(decision_filter)
            .order_by(AIRecommendation.created_at.desc(), AIRecommendation.id.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def resolve_pending(
        self,
        *,
        recommendation_id: str,
        decision: TerminalRecommendationDecision,
        decided_at: datetime,
    ) -> AIRecommendation | None:
        """Resolve only a still-pending row and return it when this call won."""

        statement = (
            update(AIRecommendation)
            .where(
                AIRecommendation.id == recommendation_id,
                AIRecommendation.decision == RecommendationDecision.PENDING,
            )
            .values(
                decision=decision,
                decided_at=decided_at,
                updated_at=decided_at,
            )
            .returning(AIRecommendation)
        )
        result = await self._session.scalars(
            statement,
            execution_options={"populate_existing": True},
        )
        return result.one_or_none()


__all__ = ["AIRecommendationRepository", "RecommendationStatusGroup"]
