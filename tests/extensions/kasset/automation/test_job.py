"""Production wiring contract for the PAPER automation sweep."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.core.config import settings
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.api.toss_market_data import TOSS_QUOTE_SOURCE
from app.extensions.kasset.automation import job
from app.extensions.kasset.automation.contracts import PROMOTION_BYPASSED_BY_OWNER
from app.extensions.kasset.automation.job import (
    OwnerScopedRecommendationService,
    RuntimeStateSafetyGate,
    run_approved_recommendation_once,
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
from app.extensions.kasset.models import AndroidPaperAccount, AndroidPaperOrder
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperAccount
from app.models.trading import User, UserRole
from app.tasks import TASKIQ_TASK_MODULES, kasset_paper_automation_tasks

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
# 2026-08-31(월) 11:00 KST. 공용 XKRX 캘린더가 정규장으로 확정하는 시각이라
# 기준 시세 신선도 게이트가 실제로 켜진다. 기존 `_NOW`(21:00 KST)는 장 마감
# 이후라 그 게이트가 꺼진 상태를 그대로 유지한다.
_NOW_IN_SESSION = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


def _approved_recommendation(
    owner_user_id: int,
    *,
    now: datetime = _NOW,
) -> AIRecommendation:
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
        created_at=now - timedelta(minutes=5),
        valid_until=now + timedelta(hours=1),
        decided_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
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
    now: datetime = _NOW,
) -> None:
    await AITradingPolicyService().put_snapshot(
        db_session,
        owner_user_id,
        mode=OperatingMode.AUTO_PAPER,
        limits=replace(AITradingLimits(), kill_switch=kill_switch),
        now=now,
    )


async def _set_approval_policy(
    db_session: AsyncSession,
    owner_user_id: int,
    *,
    now: datetime = _NOW,
) -> None:
    await AITradingPolicyService().put_snapshot(
        db_session,
        owner_user_id,
        mode=OperatingMode.APPROVAL,
        limits=AITradingLimits(),
        now=now,
    )


async def _enable_promotion_bypass(
    db_session: AsyncSession,
    owner_user_id: int,
    *,
    now: datetime = _NOW,
) -> None:
    await AITradingPolicyService().set_promotion_bypass(
        db_session,
        owner_user_id,
        enabled=True,
        reason="모의투자 계좌 완전 자동매매 게이트 개방",
        now=now,
    )


def _krx_quote(*, source: str, as_of: datetime) -> Quote:
    """운영과 같은 생성점(`krx_quotes.build_quote`)으로 만든 KRX 기준 시세."""
    return krx_quotes.build_quote(
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        price=Decimal("70000"),
        previous_close=Decimal("69000"),
        as_of=as_of,
        source=source,
    )


def _record_quote_for_market(
    monkeypatch: pytest.MonkeyPatch,
    result: Quote | Exception,
) -> list[tuple[str, str]]:
    """`quote_for_market` 호출을 기록한다.

    게이트(`job`)와 주문 경로(`paper_orders`)가 같은 모듈 속성을 호출 시점에
    찾으므로, 호출 횟수가 곧 "주문 경로까지 갔는지"의 증거가 된다.
    """
    calls: list[tuple[str, str]] = []

    async def _fake(db: object, *, market: str, symbol: str) -> Quote:
        calls.append((market, symbol))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(krx_quotes, "quote_for_market", _fake)
    return calls


async def _promotion_bypass_snapshot(
    db_session: AsyncSession,
    owner_user_id: int,
) -> bool:
    snapshot = await AITradingPolicyService().get_snapshot(
        db_session,
        owner_user_id,
        now=_NOW,
        execution_limit=0,
    )
    return snapshot.promotion_bypass


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
                "promotion_bypass_reason": None,
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
        assert claim.paper_execution_token
        assert claim.paper_execution_claimed_at == _NOW
        assert claim.paper_execution_lease_expires_at > _NOW
        assert claim.paper_execution_attempt_count == 1

        await service.fail_paper_execution(
            str(owner_id),
            recommendation_id,
            claim.paper_execution_token,
            "adapter-test-rejection",
            _NOW,
        )
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored == "FAILED"
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_expired_claim_is_selected_for_reconciliation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    recommendation.paper_execution_status = "CLAIMED"
    recommendation.paper_execution_token = "expired-token"
    recommendation.paper_execution_claimed_at = _NOW - timedelta(minutes=10)
    recommendation.paper_execution_lease_expires_at = _NOW - timedelta(minutes=5)
    recommendation.paper_execution_attempt_count = 1

    async def _approved_promotion(
        self: object,
        candidate: AIRecommendation,
    ) -> PaperApprovalDecision:
        assert candidate.id == recommendation_id
        return PaperApprovalDecision(
            approved=True,
            strategy_key="qullamaggie_breakout_portfolio",
            version="1.0.0",
            state=None,
            metrics_hash="a" * 64,
            reason="paper_approved",
        )

    try:
        db_session.add(recommendation)
        await db_session.commit()
        monkeypatch.setattr(
            StrategyPromotionService,
            "approval_for_recommendation",
            _approved_promotion,
        )
        service = OwnerScopedRecommendationService(
            db_session,
            require_promotion=True,
        )

        selected = await service.authorize_next_for_auto_execution(
            str(owner_id),
            _NOW,
        )
        assert selected == recommendation_id
        reclaimed = await service.claim_for_paper_execution(str(owner_id), _NOW)
        assert reclaimed is not None
        assert reclaimed.id == recommendation_id
        assert reclaimed.paper_execution_token != "expired-token"
        assert reclaimed.paper_execution_attempt_count == 2
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_promotion_bypass_is_off_by_default_and_still_requires_promotion(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """override를 켜지 않으면 승격 없는 추천은 지금과 똑같이 차단된다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        assert await _promotion_bypass_snapshot(db_session, owner_id) is False

        gate = RuntimeStateSafetyGate(
            db_session,
            automatic=True,
            recommendation_id=recommendation_id,
        )
        policy = await gate.get_policy(owner_user_id=str(owner_id), now=_NOW)
        assert policy.promotion_bypassed is False
        assert policy.paper_automation_enabled is False

        report = await run_paper_automation_once(now=_NOW)

        outcome = next(
            item
            for item in report["outcomes"]  # type: ignore[union-attr]
            if item["owner_user_id"] == owner_id
        )
        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == "strategy_promotion_required"
        assert outcome["promotion_bypass_reason"] is None
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_promotion_bypass_executes_unpromoted_recommendation_with_evidence(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """override를 켜면 승격 없는 추천도 실행되고 그 사실이 결과에 남는다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id)
        await _enable_promotion_bypass(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        assert await _promotion_bypass_snapshot(db_session, owner_id) is True

        # 실행 직전 게이트도 승격 근거를 요구하지 않는다.
        gate = RuntimeStateSafetyGate(
            db_session,
            automatic=True,
            recommendation_id=recommendation_id,
        )
        policy = await gate.get_policy(owner_user_id=str(owner_id), now=_NOW)
        assert policy.promotion_bypassed is True
        assert policy.paper_automation_enabled is True

        report = await run_paper_automation_once(now=_NOW)

        outcome = next(
            item
            for item in report["outcomes"]  # type: ignore[union-attr]
            if item["owner_user_id"] == owner_id
        )
        # 승격 근거가 없는데도 후보로 선정되어 실행 단계까지 갔다.
        assert outcome["reason"] != "strategy_promotion_required"
        assert outcome["recommendation_id"] == recommendation_id
        assert outcome["promotion_bypass_reason"] == PROMOTION_BYPASSED_BY_OWNER
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored in {"CLAIMED", "FAILED", "SUCCEEDED"}
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_promotion_bypass_never_beats_the_kill_switch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id, kill_switch=True)
        await _enable_promotion_bypass(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        assert await _promotion_bypass_snapshot(db_session, owner_id) is False

        report = await run_paper_automation_once(now=_NOW)

        outcome = next(
            item
            for item in report["outcomes"]  # type: ignore[union-attr]
            if item["owner_user_id"] == owner_id
        )
        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == "global_kill_switch_enabled"
        assert outcome["promotion_bypass_reason"] is None
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_promotion_bypass_is_ignored_when_trading_mode_is_not_paper(
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
        await _enable_promotion_bypass(db_session, owner_id)
        state = await runtime_state.get(db_session, owner_id, for_update=True)
        state.trading_mode = "LIVE"
        await db_session.commit()
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        assert await _promotion_bypass_snapshot(db_session, owner_id) is False

        report = await run_paper_automation_once(now=_NOW)

        outcome = next(
            item
            for item in report["outcomes"]  # type: ignore[union-attr]
            if item["owner_user_id"] == owner_id
        )
        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == "strategy_promotion_required"
        assert outcome["promotion_bypass_reason"] is None
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_promotion_bypass_never_reaches_another_owners_recommendation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_a_id, username_a = await _seed_owner(db_session)
    owner_b_id, username_b = await _seed_owner(db_session)
    recommendation_b = _approved_recommendation(owner_b_id)
    recommendation_b_id = recommendation_b.id
    try:
        db_session.add(recommendation_b)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_a_id)
        await _enable_promotion_bypass(db_session, owner_a_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)

        # override는 소유자 scope를 넓히지 않는다.
        service = OwnerScopedRecommendationService(
            db_session,
            require_promotion=False,
        )
        selected = await service.authorize_next_for_auto_execution(
            str(owner_a_id),
            _NOW,
        )
        assert selected is None

        gate = RuntimeStateSafetyGate(
            db_session,
            automatic=True,
            recommendation_id=recommendation_b_id,
        )
        policy = await gate.get_policy(owner_user_id=str(owner_a_id), now=_NOW)
        assert policy.promotion_bypassed is True
        assert policy.paper_automation_enabled is False

        assert await service.claim_for_paper_execution(str(owner_a_id), _NOW) is None

        report = await run_paper_automation_once(now=_NOW)

        assert owner_a_id not in {
            item["owner_user_id"]
            for item in report["outcomes"]  # type: ignore[union-attr]
        }
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_b_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username_a)
        await _cleanup_owner(db_session, username_b)


async def _outcome_for_owner(
    owner_id: int,
    *,
    now: datetime,
) -> dict[str, object]:
    report = await run_paper_automation_once(now=now)
    return next(
        item
        for item in report["outcomes"]  # type: ignore[union-attr]
        if item["owner_user_id"] == owner_id
    )


async def _owner_order_count(db_session: AsyncSession, owner_id: int) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(AndroidPaperOrder)
            .where(AndroidPaperOrder.owner_user_id == owner_id)
        )
        or 0
    )


async def _cleanup_paper_wiring(db_session: AsyncSession, owner_id: int) -> None:
    """주문 경로까지 간 sweep이 만든 PAPER 주문/계좌 행을 되돌린다."""
    await db_session.rollback()
    account_ids = list(
        await db_session.scalars(
            select(AndroidPaperAccount.paper_account_id).where(
                AndroidPaperAccount.owner_user_id == owner_id
            )
        )
    )
    # `ai_recommendations.paper_order_id`가 주문 행을 참조하므로 추천을 먼저
    # 지운다.
    await db_session.execute(
        delete(AIRecommendation).where(AIRecommendation.owner_user_id == owner_id)
    )
    await db_session.execute(
        delete(AndroidPaperOrder).where(AndroidPaperOrder.owner_user_id == owner_id)
    )
    await db_session.execute(
        delete(AndroidPaperAccount).where(AndroidPaperAccount.owner_user_id == owner_id)
    )
    if account_ids:
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.id.in_(account_ids))
        )
    await db_session.commit()


@pytest.mark.asyncio
async def test_approved_decision_places_order_in_the_owner_paper_account(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPROVAL mode reaches the same owner-scoped PAPER adapter as AUTO_PAPER."""

    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id, now=_NOW_IN_SESSION)
    recommendation.confidence = "0.9"
    recommendation.reference_price = "70000"
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_approval_policy(db_session, owner_id, now=_NOW_IN_SESSION)
        monkeypatch.setattr(settings, "TRADING_ENABLED", True)
        calls = _record_quote_for_market(
            monkeypatch,
            _krx_quote(source=TOSS_QUOTE_SOURCE, as_of=_NOW_IN_SESSION),
        )

        outcome = await run_approved_recommendation_once(
            owner_id,
            recommendation_id,
            now=_NOW_IN_SESSION,
        )

        assert outcome.status == "SUBMITTED"
        assert outcome.reason == "submitted"
        assert outcome.recommendation_id == recommendation_id
        assert set(calls) == {("KRX", "005930")}
        account_ids = list(
            await db_session.scalars(
                select(AndroidPaperAccount.paper_account_id).where(
                    AndroidPaperAccount.owner_user_id == owner_id
                )
            )
        )
        order = await db_session.scalar(
            select(AndroidPaperOrder).where(
                AndroidPaperOrder.owner_user_id == owner_id,
                AndroidPaperOrder.id
                == (
                    select(AIRecommendation.paper_order_id)
                    .where(AIRecommendation.id == recommendation_id)
                    .scalar_subquery()
                ),
            )
        )
        assert len(account_ids) == 1
        assert order is not None
        assert order.paper_account_id == account_ids[0]
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored == "SUCCEEDED"
    finally:
        await _cleanup_paper_wiring(db_session, owner_id)
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_in_session_stale_reference_quote_blocks_the_unattended_sweep(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정규장 중 기준 시세가 저장 일봉으로 강등되면 주문을 만들지 않는다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id, now=_NOW_IN_SESSION)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id, now=_NOW_IN_SESSION)
        await _enable_promotion_bypass(db_session, owner_id, now=_NOW_IN_SESSION)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
        calls = _record_quote_for_market(
            monkeypatch,
            _krx_quote(
                source=krx_quotes.CANDLE_QUOTE_SOURCE,
                # 전 거래일(금요일) 종가.
                as_of=_NOW_IN_SESSION - timedelta(days=3),
            ),
        )

        outcome = await _outcome_for_owner(owner_id, now=_NOW_IN_SESSION)

        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == job.STALE_QUOTE_BLOCK_REASON
        assert outcome["recommendation_id"] == recommendation_id
        # 시세 조회가 게이트의 1회로 끝났다 = 주문 경로(preview)에 가지 않았다.
        assert calls == [("KRX", "005930")]
        assert await _owner_order_count(db_session, owner_id) == 0
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        # 차단은 claim을 태우지 않는다. 시세가 회복되면 다음 sweep이 다시 잡는다.
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_in_session_unresolvable_reference_quote_blocks_fail_closed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기준 시세를 아예 못 구하면 정규장 중에는 실행하지 않는다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id, now=_NOW_IN_SESSION)
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id, now=_NOW_IN_SESSION)
        await _enable_promotion_bypass(db_session, owner_id, now=_NOW_IN_SESSION)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
        calls = _record_quote_for_market(
            monkeypatch,
            MobileApiError(404, "NOT_FOUND", "종목 시세를 찾을 수 없습니다."),
        )

        outcome = await _outcome_for_owner(owner_id, now=_NOW_IN_SESSION)

        assert outcome["status"] == "BLOCKED"
        assert outcome["reason"] == (
            f"{job.STALE_QUOTE_UNRESOLVED_REASON}:MobileApiError"
        )
        assert calls == [("KRX", "005930")]
        assert await _owner_order_count(db_session, owner_id) == 0
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored is None
    finally:
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_in_session_fresh_reference_quote_still_places_the_order(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """장중에 실시간 시세가 살아 있으면 게이트가 과차단하지 않는다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id, now=_NOW_IN_SESSION)
    # Hard Risk가 요구하는 수치 근거. 이게 있어야 주문이 체결까지 간다.
    recommendation.confidence = "0.9"
    recommendation.reference_price = "70000"
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id, now=_NOW_IN_SESSION)
        await _enable_promotion_bypass(db_session, owner_id, now=_NOW_IN_SESSION)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
        monkeypatch.setattr(settings, "TRADING_ENABLED", True)
        calls = _record_quote_for_market(
            monkeypatch,
            _krx_quote(source=TOSS_QUOTE_SOURCE, as_of=_NOW_IN_SESSION),
        )

        outcome = await _outcome_for_owner(owner_id, now=_NOW_IN_SESSION)

        assert outcome["status"] == "SUBMITTED"
        assert outcome["reason"] == "submitted"
        assert outcome["recommendation_id"] == recommendation_id
        assert await _owner_order_count(db_session, owner_id) == 1
        # 게이트 1회 + 주문 경로 여러 회. 게이트는 주문 경로를 막지 않았다.
        assert len(calls) >= 2
        assert set(calls) == {("KRX", "005930")}
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored == "SUCCEEDED"
    finally:
        await _cleanup_paper_wiring(db_session, owner_id)
        await _cleanup_owner(db_session, username)


@pytest.mark.asyncio
async def test_after_hours_stale_reference_quote_is_not_blocked(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """장 마감 후 종가는 정상 최신값이라 같은 시세로도 차단되지 않는다."""
    owner_id, username = await _seed_owner(db_session)
    recommendation = _approved_recommendation(owner_id)
    recommendation.confidence = "0.9"
    recommendation.reference_price = "70000"
    recommendation_id = recommendation.id
    try:
        db_session.add(recommendation)
        await db_session.commit()
        await _set_auto_policy(db_session, owner_id)
        await _enable_promotion_bypass(db_session, owner_id)
        monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
        monkeypatch.setattr(settings, "TRADING_ENABLED", True)
        calls = _record_quote_for_market(
            monkeypatch,
            _krx_quote(source=krx_quotes.CANDLE_QUOTE_SOURCE, as_of=_NOW),
        )

        outcome = await _outcome_for_owner(owner_id, now=_NOW)

        # 장중이면 같은 시세로 BLOCKED가 됐을 조건인데 그대로 체결됐다.
        assert outcome["status"] == "SUBMITTED"
        assert not str(outcome["reason"]).startswith("stale_quote")
        assert outcome["recommendation_id"] == recommendation_id
        assert await _owner_order_count(db_session, owner_id) == 1
        assert set(calls) == {("KRX", "005930")}
        stored = await db_session.scalar(
            select(AIRecommendation.paper_execution_status).where(
                AIRecommendation.id == recommendation_id
            )
        )
        assert stored == "SUCCEEDED"
    finally:
        await _cleanup_paper_wiring(db_session, owner_id)
        await _cleanup_owner(db_session, username)
