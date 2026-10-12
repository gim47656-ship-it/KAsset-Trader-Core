from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.api.paper_schemas import OrderRequest, RiskAssessment
from app.extensions.kasset.automation import (
    OwnerExecutionPolicy,
    PaperAutomationConsumer,
    PaperExecutionClaim,
)
from app.extensions.kasset.automation.chase_guard import (
    BELOW_STRATEGY_STOP,
    CHASE_GUARD_UNAVAILABLE,
    CHASE_LIMIT_EXCEEDED,
    EntrySizing,
    entry_sizing_from_evidence,
    evaluate_chase_guard,
    strategy_stop_from_evidence,
)

D = Decimal
_NOW = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)

# 2026-10-12 사건의 009155: 계획 진입 527000 / 전략 손절 515000 / 위험예산 150000.
_SIZING = EntrySizing(
    entry_price=D("527000"),
    strategy_stop=D("515000"),
    risk_budget=D("150000"),
    lot_size=D("1"),
)


def _evidence(
    *, stop: str = "515000", risk_budget: str | None = "150000"
) -> list[dict[str, object]]:
    sizing: dict[str, object] = {
        "entryPrice": "527000",
        "strategyStop": stop,
        "lotSize": "1",
    }
    if risk_budget is not None:
        sizing["riskBudget"] = risk_budget
    return [
        {"kind": "ai_review"},
        {
            "kind": "ai_vertical_slice",
            "entryPrice": "527000",
            "stopPrice": stop,
            "portfolio": {"positionSizing": sizing},
        },
    ]


def test_evidence_reader_returns_the_sizing_the_buy_was_planned_with() -> None:
    assert entry_sizing_from_evidence(_evidence()) == _SIZING
    assert strategy_stop_from_evidence(_evidence()) == D("515000")


@pytest.mark.parametrize(
    "evidence",
    [None, [], [{"kind": "ai_vertical_slice"}], "not-a-sequence"],
)
def test_evidence_reader_reports_missing_sizing_as_none(evidence: object) -> None:
    assert entry_sizing_from_evidence(evidence) is None
    assert strategy_stop_from_evidence(evidence) is None


def test_evidence_reader_rejects_a_stop_at_or_above_entry_or_no_budget() -> None:
    assert entry_sizing_from_evidence(_evidence(stop="527000")) is None

    no_budget = _evidence(risk_budget=None)
    assert entry_sizing_from_evidence(no_budget) is None
    # 손절가만 필요한 Position Manager는 예산이 없어도 손절가를 읽는다.
    assert strategy_stop_from_evidence(no_budget) == D("515000")


def test_009155_fill_far_above_the_sized_entry_is_blocked() -> None:
    decision = evaluate_chase_guard(
        _SIZING, executable_price=D("542500"), quantity=D("11")
    )

    assert decision.allowed is False
    assert decision.reason == CHASE_LIMIT_EXCEEDED
    assert decision.quantity is None


def test_fill_within_half_r_is_allowed_with_quantity_capped_to_the_budget() -> None:
    # R=12000, 체결 530000은 +3000(<=6000). 위험/주 15000 -> 150000/15000=10주.
    decision = evaluate_chase_guard(
        _SIZING, executable_price=D("530000"), quantity=D("11")
    )

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.quantity == D("10")


def test_half_r_boundary_is_inclusive_and_quantity_never_grows() -> None:
    at_limit = evaluate_chase_guard(
        _SIZING, executable_price=D("533000"), quantity=D("3")
    )
    assert at_limit.allowed is True
    assert at_limit.quantity == D("3")

    over_limit = evaluate_chase_guard(
        _SIZING, executable_price=D("533001"), quantity=D("3")
    )
    assert over_limit.allowed is False
    assert over_limit.reason == CHASE_LIMIT_EXCEEDED


@pytest.mark.parametrize("price", ["514000", "515000"])
def test_fill_at_or_below_the_sized_stop_is_blocked(price: str) -> None:
    # 계획 손절 이하 체결은 setup이 이미 무효다(손절선이 체결가 아래에 설 수 없다).
    decision = evaluate_chase_guard(
        _SIZING, executable_price=D(price), quantity=D("11")
    )

    assert decision.allowed is False
    assert decision.reason == BELOW_STRATEGY_STOP
    assert decision.quantity is None


def test_quantity_is_floored_to_the_lot_and_zero_lots_block() -> None:
    lot_ten = EntrySizing(
        entry_price=D("100"),
        strategy_stop=D("90"),
        risk_budget=D("340"),
        lot_size=D("10"),
    )
    # 체결 102: 위험/주 12, 340/12=28.3 -> 20주.
    capped = evaluate_chase_guard(lot_ten, executable_price=D("102"), quantity=D("30"))
    assert capped.quantity == D("20")

    tiny_budget = EntrySizing(
        entry_price=D("100"),
        strategy_stop=D("90"),
        risk_budget=D("100"),
        lot_size=D("10"),
    )
    # 위험/주 12, 100/12=8.3주 -> 1 lot(10주)도 못 산다.
    blocked = evaluate_chase_guard(
        tiny_budget, executable_price=D("102"), quantity=D("10")
    )
    assert blocked.allowed is False
    assert blocked.reason == CHASE_LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("sizing", "price"),
    [(None, D("530000")), (_SIZING, None), (_SIZING, D("0")), (_SIZING, D("NaN"))],
)
def test_missing_sizing_or_price_fails_closed(
    sizing: EntrySizing | None, price: Decimal | None
) -> None:
    decision = evaluate_chase_guard(sizing, executable_price=price, quantity=D("1"))

    assert decision.allowed is False
    assert decision.reason == CHASE_GUARD_UNAVAILABLE


# --- consumer wiring -------------------------------------------------------


class _Gate:
    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy:
        return OwnerExecutionPolicy(
            owner_user_id=owner_user_id,
            paper_automation_enabled=True,
            global_kill_switch_enabled=False,
            trading_mode="PAPER",
        )


class _Recommendations:
    def __init__(self, claim: PaperExecutionClaim) -> None:
        self._claim = claim
        self.fail_calls: list[str] = []

    async def claim_for_paper_execution(
        self, owner_user_id: str, now: datetime
    ) -> PaperExecutionClaim | None:
        return self._claim

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> None:
        return None

    async def reconcile_paper_execution_completion(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> bool:
        return True

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        self.fail_calls.append(error)


class _Orders:
    def __init__(self, reference_price: str | None) -> None:
        self._reference_price = reference_price
        self.submitted: list[OrderRequest] = []

    async def get_by_client_order_id(
        self, db: object, owner_user_id: str, client_order_id: str
    ) -> object | None:
        return None

    async def reconcile(self, db: object, owner_user_id: str, order: object) -> object:
        raise AssertionError("no order exists")

    async def preview(
        self, db: object, owner_user_id: str, request: OrderRequest
    ) -> RiskAssessment:
        return RiskAssessment(
            decision="APPROVED",
            reasons=[],
            reference_price=self._reference_price,
        )

    async def submit(
        self, db: object, owner_user_id: str, request: OrderRequest
    ) -> tuple[object, bool]:
        self.submitted.append(request)
        return SimpleNamespace(order=SimpleNamespace(id="o-1", status="FILLED")), False


def _claim(evidence: list[dict[str, object]] | None) -> PaperExecutionClaim:
    return PaperExecutionClaim(
        id="rec-009155",
        owner_user_id="u",
        paper_execution_token="t",
        paper_execution_claimed_at=_NOW,
        paper_execution_lease_expires_at=_NOW + timedelta(minutes=5),
        paper_execution_attempt_count=1,
        decision="APPROVED",
        action="BUY",
        market="KRX",
        symbol="009155",
        suggested_quantity="11",
        valid_until=_NOW + timedelta(hours=1),
        evidence=tuple(evidence or ()),
    )


def _consumer(
    claim: PaperExecutionClaim,
    orders: _Orders,
    *,
    chase_guard: bool = True,
) -> tuple[PaperAutomationConsumer, _Recommendations]:
    recommendations = _Recommendations(claim)
    consumer = PaperAutomationConsumer(
        owner_user_id="u",
        safety_gate=_Gate(),
        recommendation_service=recommendations,
        paper_orders=orders,
        db=object(),
        chase_guard=chase_guard,
    )
    return consumer, recommendations


@pytest.mark.asyncio
async def test_consumer_blocks_a_chased_buy_and_never_submits() -> None:
    orders = _Orders("542500")
    consumer, recommendations = _consumer(_claim(_evidence()), orders)

    outcome = await consumer.run_once(now=_NOW)

    assert outcome.status == "BLOCKED"
    assert outcome.reason == CHASE_LIMIT_EXCEEDED
    assert outcome.recommendation_id == "rec-009155"
    assert orders.submitted == []
    assert recommendations.fail_calls == [CHASE_LIMIT_EXCEEDED]


@pytest.mark.asyncio
async def test_consumer_submits_the_budget_capped_quantity() -> None:
    orders = _Orders("530000")
    consumer, _ = _consumer(_claim(_evidence()), orders)

    outcome = await consumer.run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert [request.quantity for request in orders.submitted] == [D("10")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "price"),
    [(None, "530000"), (_evidence(), None)],
)
async def test_consumer_fails_closed_without_sizing_or_price(
    evidence: list[dict[str, object]] | None, price: str | None
) -> None:
    orders = _Orders(price)
    consumer, recommendations = _consumer(_claim(evidence), orders)

    outcome = await consumer.run_once(now=_NOW)

    assert outcome.status == "BLOCKED"
    assert outcome.reason == CHASE_GUARD_UNAVAILABLE
    assert orders.submitted == []
    assert recommendations.fail_calls == [CHASE_GUARD_UNAVAILABLE]


@pytest.mark.asyncio
async def test_manual_approval_path_is_not_chase_guarded() -> None:
    orders = _Orders("542500")
    consumer, _ = _consumer(_claim(None), orders, chase_guard=False)

    outcome = await consumer.run_once(now=_NOW)

    assert outcome.status == "SUBMITTED"
    assert [request.quantity for request in orders.submitted] == [D("11")]
