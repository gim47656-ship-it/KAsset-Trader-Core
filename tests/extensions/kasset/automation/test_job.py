"""Production wiring contract for the PAPER automation sweep."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.core.config import settings
from app.extensions.kasset.automation import job
from app.extensions.kasset.automation.job import (
    OwnerScopedRecommendationService,
    RuntimeStateSafetyGate,
    run_paper_automation_once,
)
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    OperatingMode,
)
from app.extensions.kasset.automation.strategy_promotion import PaperApprovalDecision
from app.extensions.kasset.automation.strategy_promotion_service import (
    StrategyPromotionService,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.trading import User, UserRole
from app.tasks import TASKIQ_TASK_MODULES, kasset_paper_automation_tasks

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _approved_recommendation(owner_user_id: int) -> AIRecommendation:
    return AIRecommendation(
        id=f"rec-job-{uuid4().hex}",
        owner_user_id=owner_user_id,
        action="BUY",
        decision="APPROVED",
        market="KRX",
        symbol="005930",
        currency="KRW",
        rationale=["automation wiring"],
        risks=[],
        evidence=[],
        suggested_quantity="1",
        source="kasset-automation",
        created_at=_NOW - timedelta(minutes=5),
        valid_until=_NOW + timedelta(hours=1),
        decided_at=_NOW - timedelta(minutes=1),
        updated_at=_NOW - timedelta(minutes=1),
    )


async def _seed_owner(db_session: AsyncSession) -> tuple[int, str]:
    username = f"job-owner-{uuid4().hex}"
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("Job-owner-secret-1!"),
        role=UserRole.trader,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id, username


async def _set_auto_policy(
    db_session: AsyncSession,
    owner_user_id: int,
    *,
    kill_switch: bool = False,
) -> None:
    await AITradingPolicyService().put_snapshot(
        db_session,
        owner_user_id,
        mode=OperatingMode.AUTO_PAPER,
        limits=replace(AITradingLimits(), kill_switch=kill_switch),
        now=_NOW,
    )


async def _cleanup_owner(db_session: AsyncSession, username: str) -> None:
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.username == username))
    await db_session.commit()


def test_sweep_task_is_registered_on_the_taskiq_schedule() -> None:
    assert kasset_paper_automation_tasks in TASKIQ_TASK_MODULES
    task = kasset_paper_automation_tasks.kasset_paper_automation_run
    assert task.task_name == "kasset.paper_automation.run"


@pytest.mark.asyncio
async def test_disabled_flag_is_a_database_free_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", False)

    def _forbidden_session() -> None:
        raise AssertionError("a disabled sweep must not open a database session")

    monkeypatch.setattr(job, "AsyncSessionLocal", _forbidden_session)

    assert await run_paper_automation_once(now=_NOW) == {
        "enabled": False,
        "owners": 0,
        "outcomes": [],
    }


@pytest.mark.asyncio
async def test_enabled_sweep_is_blocked_by_the_owner_kill_switch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep discovers claimable owners but the runtime gate blocks them.

    This exercises the full production wiring (owner discovery, runtime-state
    safety gate, consumer) without needing market or PAPER account fixtures,
    and proves a blocked owner's recommendation is left unclaimed.
    """
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id, kill_switch=True)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        report = await run_paper_automation_once(now=_NOW)

        assert report["enabled"] is True
        outcomes = [
            outcome
            for outcome in report["outcomes"]  # type: ignore[union-attr]
            if outcome["owner_user_id"] == owner_id
        ]
        assert outcomes == [
            {
                "owner_user_id": owner_id,
                "status": "BLOCKED",
                "reason": "global_kill_switch_enabled",
                "recommendation_id": None,
                "replayed": False,
            }
        ]
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_one_owner_failure_does_not_abort_the_remaining_owners(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim-stage crash for one owner is recorded and the sweep continues."""
    owner_a_id, username_a = await _seed_owner(db_session)
    owner_b_id, username_b = await _seed_owner(db_session)
    try:
        db_session.add_all(
            [
                _approved_recommendation(owner_a_id),
                _approved_recommendation(owner_b_id),
            ]
        )
        await db_session.commit()
        await _set_auto_policy(db_session, owner_a_id)
        await _set_auto_policy(db_session, owner_b_id, kill_switch=True)
        # Owner B never reaches the claim stage: the kill switch blocks first.
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
        async def _approved_promotion(
            self: object,
            recommendation: AIRecommendation,
        ) -> PaperApprovalDecision:
            return PaperApprovalDecision(
                approved=True,
                strategy_key="qullamaggie_breakout_portfolio",
                version="1.0.0",
                state=None,
                metrics_hash="a" * 64,
                reason="paper_approved",
            )

        monkeypatch.setattr(
            StrategyPromotionService,
            "approval_for_recommendation",
            _approved_promotion,
        )


        async def _broken_claim(self: object, owner: str, now: datetime) -> None:
            raise RuntimeError("transient claim failure")

        monkeypatch.setattr(
            OwnerScopedRecommendationService,
            "claim_for_paper_execution",
            _broken_claim,
        )

        report = await run_paper_automation_once(now=_NOW)

        by_owner = {
            outcome["owner_user_id"]: outcome
            for outcome in report["outcomes"]  # type: ignore[union-attr]
            if outcome["owner_user_id"] in {owner_a_id, owner_b_id}
        }
        assert by_owner[owner_a_id]["status"] == "FAILED"
        assert by_owner[owner_a_id]["reason"] == "owner_sweep_failed:RuntimeError"
        assert by_owner[owner_b_id]["status"] == "BLOCKED"
        assert by_owner[owner_b_id]["reason"] == "global_kill_switch_enabled"
    finally:
        await _cleanup_owner(db_session, username_a)
        await _cleanup_owner(db_session, username_b)


@pytest.mark.asyncio
async def test_auto_paper_leaves_unpromoted_recommendation_unclaimed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        report = await run_paper_automation_once(now=_NOW)

        outcome = next(
            item
            for item in report["outcomes"]  # type: ignore[union-attr]
            if item["owner_user_id"] == owner_id
        )
        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == "strategy_promotion_required"
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_gate_and_service_adapters_bridge_integer_ownership(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        gate = RuntimeStateSafetyGate(db_session)
        policy = await gate.get_policy(owner_user_id=str(owner_id), now=_NOW)
        assert policy.owner_user_id == str(owner_id)
        assert policy.paper_automation_enabled is True
        assert policy.global_kill_switch_enabled is False
        assert policy.trading_mode == "PAPER"

        service = OwnerScopedRecommendationService(db_session)
        claim = await service.claim_for_paper_execution(str(owner_id), _NOW)
        assert claim is not None
        assert claim.id == recommendation_id
        assert claim.owner_user_id == str(owner_id)
        assert claim.decision == "APPROVED"

        await service.complete_paper_execution(
            str(owner_id),
            recommendation_id,
            "paper-order-job",
            _NOW,
        )
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored == "SUCCEEDED"
    finally:
        await _cleanup_owner(db_session, username)
