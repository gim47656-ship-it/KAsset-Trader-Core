from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.extensions.kasset.api.paper_schemas import RiskAssessment
from app.extensions.kasset.automation import (
    OwnerExecutionPolicy,
    PaperAutomationConsumer,
    PaperExecutionClaim,
)

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_OWNER = "user-a"


def _policy(
    *,
    owner: str = _OWNER,
    opt_in: bool = True,
    kill: bool = False,
    mode: str = "PAPER",
) -> OwnerExecutionPolicy:
    return OwnerExecutionPolicy(
        owner_user_id=owner,
        paper_automation_enabled=opt_in,
        global_kill_switch_enabled=kill,
        trading_mode=mode,  # type: ignore[arg-type]
    )


def _claim(
    *,
    recommendation_id: str = "rec-1",
    owner: str = _OWNER,
    decision: str = "APPROVED",
    valid_until: datetime | None = None,
) -> PaperExecutionClaim:
    return PaperExecutionClaim(
        id=recommendation_id,
        owner_user_id=owner,
        decision=decision,
        action="BUY",
        market="US",
        symbol="AAPL",
        suggested_quantity="2",
        valid_until=valid_until or _NOW + timedelta(hours=1),
    )


class FakeGate:
    def __init__(self, *policies: OwnerExecutionPolicy) -> None:
        self.policies = policies
        self.calls: list[tuple[str, datetime]] = []

    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy:
        self.calls.append((owner_user_id, now))
        index = min(len(self.calls) - 1, len(self.policies) - 1)
        return self.policies[index]


class FakeRecommendations:
    def __init__(self, *claims: PaperExecutionClaim | None) -> None:
        self.claims = list(claims)
        self.claim_calls: list[tuple[str, datetime]] = []
        self.complete_calls: list[tuple[str, str, str, datetime]] = []
        self.fail_calls: list[tuple[str, str, str, datetime]] = []

    async def claim_for_paper_execution(
        self, owner_user_id: str, now: datetime
    ) -> PaperExecutionClaim | None:
        self.claim_calls.append((owner_user_id, now))
        return self.claims.pop(0) if self.claims else None

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        paper_order_id: str,
        now: datetime,
    ) -> None:
        self.complete_calls.append(
            (owner_user_id, recommendation_id, paper_order_id, now)
        )

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        error: str,
        now: datetime,
    ) -> None:
        self.fail_calls.append((owner_user_id, recommendation_id, error, now))


class FakePaperOrders:
    def __init__(self, *, risk_decision: str = "APPROVED") -> None:
        self.risk_decision = risk_decision
        self.preview_calls: list[tuple[object, str, object]] = []
        self.submit_calls: list[tuple[object, str, object]] = []

    async def preview(
        self, db: object, owner_user_id: str, request: object
    ) -> RiskAssessment:
        self.preview_calls.append((db, owner_user_id, request))
        return RiskAssessment(decision=self.risk_decision, reasons=[])

    async def submit(
        self, db: object, owner_user_id: str, request: object
    ) -> tuple[object, bool]:
        self.submit_calls.append((db, owner_user_id, request))
        return SimpleNamespace(order=SimpleNamespace(id="paper-order-1")), False


def _consumer(
    gate: FakeGate,
    recommendations: FakeRecommendations,
    paper_orders: FakePaperOrders,
) -> PaperAutomationConsumer:
    return PaperAutomationConsumer(
        owner_user_id=_OWNER,
        safety_gate=gate,
        recommendation_service=recommendations,
        paper_orders=paper_orders,
        db=object(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (_policy(opt_in=False), "owner_opt_in_disabled"),
        (_policy(kill=True), "global_kill_switch_enabled"),
        (_policy(mode="LIVE"), "live_mode_forbidden"),
        (_policy(mode="DISABLED"), "paper_mode_required"),
    ],
)
async def test_safety_gate_blocks_before_claim(
    policy: OwnerExecutionPolicy,
    reason: str,
) -> None:
    gate = FakeGate(policy)
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders()

    outcome = await _consumer(gate, recommendations, paper_orders).run_once(now=_NOW)

    assert outcome.status == "BLOCKED"
    assert outcome.reason == reason
    assert recommendations.claim_calls == []
    assert paper_orders.preview_calls == []
    assert paper_orders.submit_calls == []


@pytest.mark.asyncio
async def test_expired_claim_is_failed_once_without_preview_or_submit() -> None:
    recommendations = FakeRecommendations(
        _claim(valid_until=_NOW - timedelta(seconds=1))
    )
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "REJECTED"
    assert outcome.reason == "recommendation_expired"
    assert recommendations.fail_calls == [
        (_OWNER, "rec-1", "recommendation_expired", _NOW)
    ]
    assert paper_orders.preview_calls == []
    assert paper_orders.submit_calls == []


@pytest.mark.asyncio
async def test_risk_rejection_is_terminal_and_never_submits() -> None:
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders(risk_decision="REJECTED")

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "REJECTED"
    assert outcome.reason == "risk_preview_rejected"
    assert len(paper_orders.preview_calls) == 1
    assert paper_orders.submit_calls == []
    assert recommendations.fail_calls[0][2] == "risk_preview_rejected"


@pytest.mark.asyncio
async def test_cross_owner_claim_never_reaches_paper_facade() -> None:
    recommendations = FakeRecommendations(_claim(owner="user-b"))
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "FAILED"
    assert outcome.reason == "claim_owner_mismatch"
    assert paper_orders.preview_calls == []
    assert paper_orders.submit_calls == []
    assert recommendations.complete_calls == []
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_gate_revocation_between_preview_and_submit_is_terminal() -> None:
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders()
    gate = FakeGate(_policy(), _policy(kill=True))

    outcome = await _consumer(gate, recommendations, paper_orders).run_once(now=_NOW)

    assert outcome.status == "REJECTED"
    assert outcome.reason == "safety_gate_changed:global_kill_switch_enabled"
    assert len(paper_orders.preview_calls) == 1
    assert paper_orders.submit_calls == []
    assert recommendations.fail_calls[0][2] == outcome.reason


@pytest.mark.asyncio
async def test_duplicate_poll_submits_claim_once_with_fixed_idempotency_key() -> None:
    recommendations = FakeRecommendations(_claim(), None)
    paper_orders = FakePaperOrders()
    consumer = _consumer(FakeGate(_policy()), recommendations, paper_orders)

    first = await consumer.run_once(now=_NOW)
    second = await consumer.run_once(now=_NOW + timedelta(seconds=1))

    assert first.status == "SUBMITTED"
    assert second.status == "IDLE"
    assert len(paper_orders.preview_calls) == 1
    assert len(paper_orders.submit_calls) == 1
    _, submitted_owner, request = paper_orders.submit_calls[0]
    assert submitted_owner == _OWNER
    assert request.broker == "PAPER"  # type: ignore[attr-defined]
    assert request.client_order_id == "ai-rec:rec-1"  # type: ignore[attr-defined]
    assert recommendations.complete_calls == [(_OWNER, "rec-1", "paper-order-1", _NOW)]
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_different_owners_execute_independently_through_owner_scoped_calls() -> (
    None
):
    paper_orders = FakePaperOrders()
    recommendations_a = FakeRecommendations(_claim(recommendation_id="rec-a"))
    recommendations_b = FakeRecommendations(
        _claim(recommendation_id="rec-b", owner="user-b")
    )
    consumer_a = _consumer(FakeGate(_policy()), recommendations_a, paper_orders)
    consumer_b = PaperAutomationConsumer(
        owner_user_id="user-b",
        safety_gate=FakeGate(_policy(owner="user-b")),
        recommendation_service=recommendations_b,
        paper_orders=paper_orders,
        db=object(),
    )

    outcome_a = await consumer_a.run_once(now=_NOW)
    outcome_b = await consumer_b.run_once(now=_NOW)

    assert outcome_a.status == outcome_b.status == "SUBMITTED"
    assert [call[1] for call in paper_orders.submit_calls] == ["user-a", "user-b"]
    assert [call[2].client_order_id for call in paper_orders.submit_calls] == [
        "ai-rec:rec-a",
        "ai-rec:rec-b",
    ]


@pytest.mark.asyncio
async def test_non_approved_claim_is_failed_without_submit() -> None:
    recommendations = FakeRecommendations(_claim(decision="PENDING"))
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.reason == "recommendation_not_approved"
    assert paper_orders.submit_calls == []
    assert recommendations.fail_calls[0][2] == "recommendation_not_approved"


def test_consumer_requires_owner_and_explicit_safety_gate() -> None:
    with pytest.raises(ValueError, match="owner_user_id"):
        PaperAutomationConsumer(
            owner_user_id=" ",
            safety_gate=FakeGate(_policy()),
            recommendation_service=FakeRecommendations(),
            paper_orders=FakePaperOrders(),
            db=object(),
        )
