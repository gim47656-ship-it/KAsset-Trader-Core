"""Business rules for owner-scoped AI recommendation review and PAPER claims."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.models import AndroidPaperOrder
from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationAction,
    RecommendationDecision,
    RecommendationExecutionStatus,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)
from app.models.symbol_master import SymbolMaster
from app.services.ai_recommendations.repository import AIRecommendationRepository

if TYPE_CHECKING:
    from app.extensions.kasset.automation.contracts import RecommendationDraft


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

    async def create_recommendation(
        self,
        *,
        owner_user_id: str,
        draft: RecommendationDraft,
    ) -> AIRecommendation:
        try:
            owner_id = int(owner_user_id)
        except (TypeError, ValueError) as exc:
            raise RecommendationValidationError("owner_user_id_invalid") from exc
        if str(owner_id) != owner_user_id.strip() or owner_id <= 0:
            raise RecommendationValidationError("owner_user_id_invalid")
        row = AIRecommendation(
            owner_user_id=owner_id,
            action=draft.action.value,
            decision=RecommendationDecision.PENDING.value,
            market=draft.market,
            symbol=draft.symbol,
            name=draft.name,
            currency="KRW" if draft.market == "KRX" else "USD",
            headline=draft.headline,
            rationale=list(draft.rationale),
            risks=list(draft.risks),
            evidence=[dict(item) for item in draft.evidence],
            confidence=format(Decimal(draft.confidence), "f"),
            reference_price=(
                format(Decimal(draft.reference_price), "f")
                if draft.reference_price is not None
                else None
            ),
            suggested_quantity=(
                format(Decimal(draft.suggested_quantity), "f")
                if draft.suggested_quantity is not None
                else None
            ),
            source=draft.source,
            created_at=draft.created_at,
            valid_until=draft.valid_until,
            updated_at=draft.created_at,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get_recommendation(
        self,
        owner_user_id: int,
        recommendation_id: str,
    ) -> AIRecommendation:
        row = await self._repository.get(owner_user_id, recommendation_id)
        if row is None:
            raise RecommendationNotFoundError(recommendation_id)
        return row

    async def load_paper_orders(
        self,
        recommendations: list[AIRecommendation],
    ) -> dict[str, AndroidPaperOrder]:
        order_ids = [
            row.paper_order_id
            for row in recommendations
            if row.paper_order_id is not None
        ]
        if not order_ids:
            return {}
        orders = list(
            (
                await self._session.scalars(
                    select(AndroidPaperOrder).where(AndroidPaperOrder.id.in_(order_ids))
                )
            ).all()
        )
        by_id = {order.id: order for order in orders}
        return {
            row.id: by_id[row.paper_order_id]
            for row in recommendations
            if row.paper_order_id in by_id
        }

    async def load_symbol_names(
        self,
        recommendations: list[AIRecommendation],
    ) -> dict[tuple[str, str], str]:
        """이름이 비었거나 코드뿐인 기존 추천을 종목 마스터에서 한 번에 보강한다."""

        keys = tuple(
            dict.fromkeys(
                (str(row.market), str(row.symbol))
                for row in recommendations
                if not (row.name or "").strip()
                or (row.name or "").strip() == str(row.symbol).strip()
            )
        )
        if not keys:
            return {}
        rows = (
            await self._session.execute(
                select(
                    SymbolMaster.market,
                    SymbolMaster.symbol,
                    SymbolMaster.name,
                ).where(
                    tuple_(
                        SymbolMaster.market,
                        SymbolMaster.symbol,
                    ).in_(keys)
                )
            )
        ).all()
        return {
            (market, symbol): name.strip()
            for market, symbol, name in rows
            if name and name.strip() and name.strip() != symbol
        }

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

    async def authorize_next_for_auto_execution(
        self,
        owner_user_id: int,
        *,
        now: datetime,
    ) -> AIRecommendation | None:
        decided_at = self._normalized_now(now)
        row = await self._repository.next_pending_actionable(owner_user_id, decided_at)
        if row is None:
            return None
        self._validate_approval(row, now=decided_at)
        resolved = await self._repository.resolve_pending(
            owner_user_id,
            recommendation_id=row.id,
            decision=RecommendationDecision.APPROVED,
            decided_at=decided_at,
        )
        if resolved is None:
            await self._session.rollback()
            return None
        await self._session.commit()
        return resolved

    async def claim_for_paper_execution(
        self,
        owner_user_id: int,
        now: datetime,
        *,
        recommendation_id: str | None = None,
        automation_only: bool = False,
    ) -> AIRecommendation | None:
        claimed_at = self._normalized_now(now)
        row = await self._repository.claim_for_paper_execution(
            owner_user_id,
            claimed_at,
            recommendation_id=recommendation_id,
            automation_only=automation_only,
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
