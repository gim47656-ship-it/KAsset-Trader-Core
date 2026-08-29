from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import RiskAssessment
from app.extensions.kasset.automation import (
    OwnerExecutionPolicy,
    PaperAutomationConsumer,
    PaperExecutionClaim,
)

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_OWNER = "user-a"
_TOKEN = "claim-token-1"


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
    token: str = _TOKEN,
) -> PaperExecutionClaim:
    return PaperExecutionClaim(
        id=recommendation_id,
        owner_user_id=owner,
        paper_execution_token=token,
        paper_execution_claimed_at=_NOW,
        paper_execution_lease_expires_at=_NOW + timedelta(minutes=5),
        paper_execution_attempt_count=1,
        decision=decision,
        action="BUY",
        market="US",
        symbol="AAPL",
        suggested_quantity="2",
        valid_until=valid_until or _NOW + timedelta(hours=1),
    )


def _order(
    *,
    order_id: str = "paper-order-1",
    status: str = "FILLED",
) -> SimpleNamespace:
    return SimpleNamespace(id=order_id, status=status)


class FakeGate:
    def __init__(self, *policies: OwnerExecutionPolicy | Exception) -> None:
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
        policy = self.policies[index]
        if isinstance(policy, Exception):
            raise policy
        return policy


class FakeRecommendations:
    def __init__(
        self,
        *claims: PaperExecutionClaim | None,
        complete_error: Exception | None = None,
        reconcile_result: bool = True,
        reconcile_error: Exception | None = None,
    ) -> None:
        self.claims = list(claims)
        self.complete_error = complete_error
        self.reconcile_result = reconcile_result
        self.reconcile_error = reconcile_error
        self.claim_calls: list[tuple[str, datetime]] = []
        self.complete_calls: list[tuple[str, str, str, str, datetime]] = []
        self.reconcile_calls: list[tuple[str, str, str, str, datetime]] = []
        self.fail_calls: list[tuple[str, str, str, str, datetime]] = []

    async def claim_for_paper_execution(
        self, owner_user_id: str, now: datetime
    ) -> PaperExecutionClaim | None:
        self.claim_calls.append((owner_user_id, now))
        return self.claims.pop(0) if self.claims else None

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> None:
        self.complete_calls.append(
            (owner_user_id, recommendation_id, claim_token, paper_order_id, now)
        )
        if self.complete_error is not None:
            raise self.complete_error

    async def reconcile_paper_execution_completion(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> bool:
        self.reconcile_calls.append(
            (owner_user_id, recommendation_id, claim_token, paper_order_id, now)
        )
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_result

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        self.fail_calls.append(
            (owner_user_id, recommendation_id, claim_token, error, now)
        )


class FakePaperOrders:
    def __init__(
        self,
        *,
        risk_decision: str = "APPROVED",
        existing: SimpleNamespace | None = None,
        preview_error: Exception | None = None,
        submit_error: Exception | None = None,
        order_on_submit_error: SimpleNamespace | None = None,
        lookup_effects: list[SimpleNamespace | None | Exception] | None = None,
    ) -> None:
        self.risk_decision = risk_decision
        self.existing = existing
        self.preview_error = preview_error
        self.submit_error = submit_error
        self.order_on_submit_error = order_on_submit_error
        self.lookup_effects = list(lookup_effects or [])
        self.lookup_calls: list[tuple[object, str, str]] = []
        self.reconcile_calls: list[tuple[object, str, object]] = []
        self.preview_calls: list[tuple[object, str, object]] = []
        self.submit_calls: list[tuple[object, str, object]] = []

    async def get_by_client_order_id(
        self,
        db: object,
        owner_user_id: str,
        client_order_id: str,
    ) -> object | None:
        self.lookup_calls.append((db, owner_user_id, client_order_id))
        if self.lookup_effects:
            result = self.lookup_effects.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return self.existing

    async def reconcile(
        self,
        db: object,
        owner_user_id: str,
        order: object,
    ) -> object:
        self.reconcile_calls.append((db, owner_user_id, order))
        if getattr(order, "status", None) == "PENDING":
            order.status = "FILLED"
        return SimpleNamespace(order=order)

    async def preview(
        self, db: object, owner_user_id: str, request: object
    ) -> RiskAssessment:
        self.preview_calls.append((db, owner_user_id, request))
        if self.preview_error is not None:
            raise self.preview_error
        return RiskAssessment(decision=self.risk_decision, reasons=[])

    async def submit(
        self, db: object, owner_user_id: str, request: object
    ) -> tuple[object, bool]:
        self.submit_calls.append((db, owner_user_id, request))
        if self.submit_error is not None:
            self.existing = self.order_on_submit_error
            raise self.submit_error
        return SimpleNamespace(order=_order()), False


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
async def test_expired_reclaimed_claim_reconciles_existing_order_before_rejection() -> (
    None
):
    recommendations = FakeRecommendations(
        _claim(valid_until=_NOW - timedelta(seconds=1))
    )
    paper_orders = FakePaperOrders(existing=_order())

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert outcome.reason == "idempotent_replay"
    assert paper_orders.preview_calls == []
    assert paper_orders.submit_calls == []
    assert recommendations.complete_calls[0][2] == _TOKEN


@pytest.mark.asyncio
async def test_expired_claim_without_order_is_failed_with_token() -> None:
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
        (_OWNER, "rec-1", _TOKEN, "recommendation_expired", _NOW)
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
    assert recommendations.fail_calls[0][2:4] == (_TOKEN, "risk_preview_rejected")


@pytest.mark.asyncio
async def test_cross_owner_claim_never_reaches_paper_facade() -> None:
    recommendations = FakeRecommendations(_claim(owner="user-b"))
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "FAILED"
    assert outcome.reason == "claim_owner_mismatch"
    assert paper_orders.lookup_calls == []
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
    assert recommendations.fail_calls[0][2] == _TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "paper_orders", "gate"),
    [
        (
            "before_preview",
            FakePaperOrders(lookup_effects=[TimeoutError("lookup")]),
            FakeGate(_policy()),
        ),
        (
            "preview",
            FakePaperOrders(preview_error=TimeoutError("preview")),
            FakeGate(_policy()),
        ),
        (
            "after_preview",
            FakePaperOrders(),
            FakeGate(_policy(), TimeoutError("policy")),
        ),
        (
            "submit",
            FakePaperOrders(submit_error=TimeoutError("submit")),
            FakeGate(_policy()),
        ),
    ],
)
async def test_ambiguous_interruptions_leave_claim_nonterminal(
    stage: str,
    paper_orders: FakePaperOrders,
    gate: FakeGate,
) -> None:
    recommendations = FakeRecommendations(_claim())

    outcome = await _consumer(gate, recommendations, paper_orders).run_once(now=_NOW)

    assert outcome.status == "FAILED", stage
    assert recommendations.fail_calls == [], stage
    assert recommendations.complete_calls == [], stage


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_status", ["FILLED", "PENDING"])
async def test_submit_timeout_reconciles_persisted_order_without_resubmit(
    persisted_status: str,
) -> None:
    persisted = _order(status=persisted_status)
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders(
        submit_error=TimeoutError("submit response lost"),
        order_on_submit_error=persisted,
    )

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert outcome.replayed is True
    assert len(paper_orders.submit_calls) == 1
    assert len(paper_orders.reconcile_calls) == 1
    assert persisted.status == "FILLED"
    assert recommendations.complete_calls[0][3] == persisted.id
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_submit_timeout_without_order_leaves_claim_for_lease_retry() -> None:
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders(submit_error=TimeoutError("submit response lost"))

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.reason == "submit_ambiguous:TimeoutError"
    assert recommendations.complete_calls == []
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_reclaimed_existing_order_skips_preview_and_submit() -> None:
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders(existing=_order(status="PENDING"))

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert outcome.replayed is True
    assert paper_orders.preview_calls == []
    assert paper_orders.submit_calls == []
    assert len(paper_orders.reconcile_calls) == 1


@pytest.mark.asyncio
async def test_completion_timeout_rereads_and_converges() -> None:
    recommendations = FakeRecommendations(
        _claim(),
        complete_error=TimeoutError("commit response lost"),
        reconcile_result=True,
    )
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert outcome.reason == "completion_reconciled"
    assert recommendations.reconcile_calls == [
        (_OWNER, "rec-1", _TOKEN, "paper-order-1", _NOW)
    ]
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_deterministic_submit_rejection_is_token_checked_failure() -> None:
    recommendations = FakeRecommendations(_claim())
    paper_orders = FakePaperOrders(
        submit_error=MobileApiError(409, "HARD_RISK_REJECTED", "blocked")
    )

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.status == "REJECTED"
    assert outcome.reason == "submit_rejected:HARD_RISK_REJECTED"
    assert recommendations.fail_calls[0][2] == _TOKEN


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
    assert recommendations.complete_calls == [
        (_OWNER, "rec-1", _TOKEN, "paper-order-1", _NOW)
    ]
    assert recommendations.fail_calls == []


@pytest.mark.asyncio
async def test_non_approved_claim_is_failed_without_submit() -> None:
    recommendations = FakeRecommendations(_claim(decision="PENDING"))
    paper_orders = FakePaperOrders()

    outcome = await _consumer(
        FakeGate(_policy()), recommendations, paper_orders
    ).run_once(now=_NOW)

    assert outcome.reason == "recommendation_not_approved"
    assert paper_orders.submit_calls == []
    assert recommendations.fail_calls[0][2:4] == (
        _TOKEN,
        "recommendation_not_approved",
    )


def test_consumer_requires_owner_and_explicit_safety_gate() -> None:
    with pytest.raises(ValueError, match="owner_user_id"):
        PaperAutomationConsumer(
            owner_user_id=" ",
            safety_gate=FakeGate(_policy()),
            recommendation_service=FakeRecommendations(),
            paper_orders=FakePaperOrders(),
            db=object(),
        )
