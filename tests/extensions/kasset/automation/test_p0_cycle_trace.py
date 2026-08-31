"""One cycle trace must join the cycle ledger, its outcome, and the recommendation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.extensions.kasset.automation.contracts import Action, RecommendationDraft
from app.extensions.kasset.automation.vertical_slice import (
    AIRecommendationVerticalSlice,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.trading import User, UserRole
from app.services.ai_recommendations.service import (
    AIRecommendationService,
    RecommendationValidationError,
)
from app.services.kasset_automation_audit import (
    build_automation_cycle_event,
    new_cycle_trace_id,
)

_NOW = datetime(2026, 8, 31, 5, 50, tzinfo=UTC)


async def _seed_owner(db_session: AsyncSession) -> tuple[int, str]:
    username = f"trace-owner-{uuid4().hex}"
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("Trace-owner-secret-1!"),
        role=UserRole.trader,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id, username


async def _cleanup_owner(db_session: AsyncSession, username: str) -> None:
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.username == username))
    await db_session.commit()


def _draft(owner_user_id: int) -> RecommendationDraft:
    return RecommendationDraft(
        owner_user_id=str(owner_user_id),
        action=Action.BUY,
        market="KRX",
        symbol="005930",
        name="삼성전자",
        headline="추적 id 확인용 추천",
        rationale=("cycle trace stamping",),
        risks=(),
        evidence=(),
        confidence=Decimal("0.90"),
        reference_price=Decimal("70000"),
        suggested_quantity=Decimal("1"),
        source="kasset-automation",
        created_at=_NOW,
        valid_until=_NOW + timedelta(hours=1),
    )


def test_new_cycle_trace_ids_are_distinct_and_prefixed() -> None:
    first = new_cycle_trace_id()
    second = new_cycle_trace_id()

    assert first.startswith("cyc-")
    assert first != second
    assert len(first) <= 64


@pytest.mark.asyncio
async def test_the_persisted_recommendation_carries_the_writer_cycle_trace(
    db_session: AsyncSession,
) -> None:
    """cycle 추적 id로 만든 서비스가 저장하는 추천에 그 값이 찍힌다."""
    owner_id, username = await _seed_owner(db_session)
    trace = new_cycle_trace_id()
    try:
        traced = await AIRecommendationService(
            db_session,
            clock=lambda: _NOW,
            cycle_trace_id=trace,
        ).create_recommendation(
            owner_user_id=str(owner_id),
            draft=_draft(owner_id),
        )
        untraced = await AIRecommendationService(
            db_session,
            clock=lambda: _NOW,
        ).create_recommendation(
            owner_user_id=str(owner_id),
            draft=_draft(owner_id),
        )

        assert traced.cycle_trace_id == trace
        assert untraced.cycle_trace_id is None
        joined = list(
            await db_session.scalars(
                select(AIRecommendation.id).where(
                    AIRecommendation.owner_user_id == owner_id,
                    AIRecommendation.cycle_trace_id == trace,
                )
            )
        )
        assert joined == [traced.id]
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(AIRecommendation).where(AIRecommendation.owner_user_id == owner_id)
        )
        await db_session.commit()
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_a_blank_cycle_trace_is_rejected_before_it_reaches_the_row(
    db_session: AsyncSession,
) -> None:
    """DB CHECK가 거부할 값을 저장 시도까지 끌고 가지 않는다."""
    with pytest.raises(RecommendationValidationError):
        AIRecommendationService(db_session, cycle_trace_id="   ")


@pytest.mark.asyncio
async def test_an_early_skip_still_reports_the_injected_cycle_trace(
    db_session: AsyncSession,
) -> None:
    """AI가 없어 후보를 못 돌린 cycle도 같은 추적 id로 원장에 남는다."""
    owner_id, username = await _seed_owner(db_session)
    trace = new_cycle_trace_id()
    try:
        result = await AIRecommendationVerticalSlice(
            db_session,
            None,
            now=_NOW,
            cycle_trace_id=trace,
        ).run_owner(owner_id)

        assert result["skipped"] == "ai_unavailable"
        assert result["cycleTraceId"] == trace

        row = build_automation_cycle_event(
            owner_user_id=owner_id,
            observed_at=_NOW,
            finished_at=_NOW + timedelta(seconds=1),
            result=result,
        )
        assert row.cycle_trace_id == trace
        assert row.status == "skipped"
        assert row.recommendation_ids == []
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_each_owner_cycle_gets_its_own_trace(
    db_session: AsyncSession,
) -> None:
    """추적 id는 owner cycle 하나의 범위다. 두 slice가 같은 값을 쓰지 않는다."""
    owner_id, username = await _seed_owner(db_session)
    try:
        first = await AIRecommendationVerticalSlice(
            db_session,
            None,
            now=_NOW,
        ).run_owner(owner_id)
        second = await AIRecommendationVerticalSlice(
            db_session,
            None,
            now=_NOW,
        ).run_owner(owner_id)

        assert first["cycleTraceId"] != second["cycleTraceId"]
        assert str(first["cycleTraceId"]).startswith("cyc-")
    finally:
        await _cleanup_owner(db_session, username)
