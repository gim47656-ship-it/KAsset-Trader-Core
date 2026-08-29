"""Owner-scoped persistence operations for AI recommendation review."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationDecision,
    RecommendationExecutionStatus,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)


class AIRecommendationRepository:
    """Own recommendation queries, review CAS, and PAPER execution claims."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        owner_user_id: int,
        recommendation_id: str,
        *,
        for_update: bool = False,
    ) -> AIRecommendation | None:
        statement = select(AIRecommendation).where(
            AIRecommendation.owner_user_id == owner_user_id,
            AIRecommendation.id == recommendation_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return (
            await self._session.scalars(
                statement,
                execution_options={"populate_existing": True},
            )
        ).one_or_none()

    async def list_by_status(
        self,
        owner_user_id: int,
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
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
                decision_filter,
            )
            .order_by(AIRecommendation.created_at.desc(), AIRecommendation.id.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def resolve_pending(
        self,
        owner_user_id: int,
        *,
        recommendation_id: str,
        decision: TerminalRecommendationDecision,
        decided_at: datetime,
    ) -> AIRecommendation | None:
        """Resolve only this owner's still-pending row."""

        statement = (
            update(AIRecommendation)
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
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

    async def claim_for_paper_execution(
        self,
        owner_user_id: int,
        now: datetime,
        *,
        recommendation_id: str | None = None,
        automation_only: bool = False,
    ) -> AIRecommendation | None:
        statement = select(AIRecommendation).where(
            AIRecommendation.owner_user_id == owner_user_id,
            AIRecommendation.decision == RecommendationDecision.APPROVED,
            or_(
                and_(
                    AIRecommendation.paper_execution_status.is_(None),
                    AIRecommendation.valid_until > now,
                ),
                and_(
                    AIRecommendation.paper_execution_status
                    == RecommendationExecutionStatus.CLAIMED,
                    AIRecommendation.paper_execution_lease_expires_at <= now,
                ),
            ),
        )
        if automation_only:
            statement = statement.where(AIRecommendation.source == "kasset-automation")
        if recommendation_id is not None:
            statement = statement.where(AIRecommendation.id == recommendation_id)
        statement = (
            statement.order_by(
                AIRecommendation.decided_at,
                AIRecommendation.created_at,
                AIRecommendation.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def complete_paper_execution(
        self,
        owner_user_id: int,
        *,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        completed_at: datetime,
    ) -> AIRecommendation | None:
        statement = (
            update(AIRecommendation)
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
                AIRecommendation.id == recommendation_id,
                AIRecommendation.paper_execution_status
                == RecommendationExecutionStatus.CLAIMED,
                AIRecommendation.paper_execution_token == claim_token,
            )
            .values(
                paper_execution_status=RecommendationExecutionStatus.SUCCEEDED,
                paper_execution_token=None,
                paper_execution_lease_expires_at=None,
                paper_execution_completed_at=completed_at,
                paper_order_id=paper_order_id,
                paper_execution_error=None,
                updated_at=completed_at,
            )
            .returning(AIRecommendation)
        )
        return (
            await self._session.scalars(
                statement,
                execution_options={"populate_existing": True},
            )
        ).one_or_none()

    async def fail_paper_execution(
        self,
        owner_user_id: int,
        *,
        recommendation_id: str,
        claim_token: str,
        error: str,
        completed_at: datetime,
    ) -> AIRecommendation | None:
        statement = (
            update(AIRecommendation)
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
                AIRecommendation.id == recommendation_id,
                AIRecommendation.paper_execution_status
                == RecommendationExecutionStatus.CLAIMED,
                AIRecommendation.paper_execution_token == claim_token,
            )
            .values(
                paper_execution_status=RecommendationExecutionStatus.FAILED,
                paper_execution_token=None,
                paper_execution_lease_expires_at=None,
                paper_execution_completed_at=completed_at,
                paper_order_id=None,
                paper_execution_error=error,
                updated_at=completed_at,
            )
            .returning(AIRecommendation)
        )
        return (
            await self._session.scalars(
                statement,
                execution_options={"populate_existing": True},
            )
        ).one_or_none()

    async def next_pending_actionable(
        self,
        owner_user_id: int,
        now: datetime,
    ) -> AIRecommendation | None:
        statement = (
            select(AIRecommendation)
            .where(
                AIRecommendation.owner_user_id == owner_user_id,
                AIRecommendation.decision == RecommendationDecision.PENDING,
                AIRecommendation.action.in_(("BUY", "SELL")),
                AIRecommendation.valid_until > now,
                AIRecommendation.paper_execution_status.is_(None),
                AIRecommendation.source == "kasset-automation",
            )
            .order_by(AIRecommendation.created_at, AIRecommendation.id)
            .limit(1)
        )
        return (await self._session.scalars(statement)).one_or_none()


__all__ = ["AIRecommendationRepository", "RecommendationStatusGroup"]
