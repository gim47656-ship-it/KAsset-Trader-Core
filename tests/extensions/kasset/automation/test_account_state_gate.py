"""PAPER 계좌 High-Watermark 활성 관문의 focused 계약."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api.paper_schemas import OrderRequest
from app.extensions.kasset.automation import account_state_gate as gate_module
from app.extensions.kasset.automation import job as job_module
from app.extensions.kasset.automation.account_state_gate import (
    ACCOUNT_STATE_SCHEMA_VERSION,
    STAGED_REDUCTION_MULTIPLIER,
    AccountState,
    AccountStateEvaluation,
    AccountStateGate,
    AccountStateSnapshot,
    AccountStateThresholds,
    account_state_from_shadow,
    evaluate_account_state_gate,
)
from app.extensions.kasset.automation.job import OwnerScopedPaperOrders
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    AITradingSnapshot,
    AITradingUsage,
    OperatingMode,
)
from app.extensions.kasset.automation.shadow_high_watermark import (
    ShadowEquityValuation,
    ShadowHighWatermarkState,
    evaluate_shadow_high_watermark,
)

_NOW = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
_THRESHOLDS = AccountStateThresholds.from_risk_rates(
    daily_target_rate_pct=Decimal("0.5"),
    max_daily_loss_rate_pct=Decimal("1.0"),
)


def _shadow(
    equity: str,
    *,
    previous: ShadowHighWatermarkState | None = None,
    minutes: int = 0,
):
    moment = _NOW + timedelta(minutes=minutes)
    return evaluate_shadow_high_watermark(
        ShadowEquityValuation(
            owner_user_id=7,
            account_key="PAPER-11",
            market="KRX",
            equity=Decimal(equity),
            valuation_at=moment,
            evaluated_at=moment,
            valuation_source="paper-trading-service",
        ),
        thresholds=_THRESHOLDS.as_shadow_thresholds(),
        previous=previous,
    )


def _evaluation(state: AccountState, *, unavailable: str | None = None):
    multiplier = {
        AccountState.NORMAL: Decimal("1"),
        AccountState.STAGED_REDUCTION: STAGED_REDUCTION_MULTIPLIER,
        AccountState.EXIT_ONLY: Decimal("0"),
    }[state]
    return AccountStateEvaluation(
        market="KRX",
        state=state,
        profit_ratio=None if unavailable else Decimal("0.006"),
        peak_drawdown_ratio=None if unavailable else Decimal("0"),
        multiplier=multiplier,
        thresholds=_THRESHOLDS,
        unavailable=unavailable,
    )


def test_profit_above_half_goal_activates_staged_reduction() -> None:
    opened = _shadow("100")
    assert opened.state is not None
    active = account_state_from_shadow(
        _shadow("100.3", previous=opened.state, minutes=1),
        thresholds=_THRESHOLDS,
    )

    assert _THRESHOLDS.staged_profit_ratio == Decimal("0.0025")
    assert active.state is AccountState.STAGED_REDUCTION
    assert active.profit_ratio == Decimal("0.003")
    assert active.multiplier == Decimal("0.75")
    assert active.as_evidence()["schemaVersion"] == ACCOUNT_STATE_SCHEMA_VERSION


def test_profit_above_full_goal_is_exit_only_for_buy_but_not_sell() -> None:
    opened = _shadow("100")
    assert opened.state is not None
    active = account_state_from_shadow(
        _shadow("100.6", previous=opened.state, minutes=1),
        thresholds=_THRESHOLDS,
    )

    buy = evaluate_account_state_gate("BUY", active)
    sell = evaluate_account_state_gate("SELL", active)

    assert active.state is AccountState.EXIT_ONLY
    assert buy.passed is False
    assert buy.reason == "exit_only"
    assert sell.passed is True


def test_peak_drawdown_above_half_max_loss_activates_staged_reduction() -> None:
    opened = _shadow("100")
    assert opened.state is not None
    peak = _shadow("100.6", previous=opened.state, minutes=1)
    assert peak.state is not None
    active = account_state_from_shadow(
        _shadow("99.9964", previous=peak.state, minutes=2),
        thresholds=_THRESHOLDS,
    )

    assert active.peak_drawdown_ratio == Decimal("0.006")
    assert active.profit_ratio == Decimal("-0.000036")
    assert active.state is AccountState.STAGED_REDUCTION


class _PaperAccountDb:
    def __init__(self, *, cash_krw: Decimal = Decimal("1000")) -> None:
        self.cash_krw = cash_krw
        self.savepoint_rollbacks = 0

    async def scalar(self, _statement: object) -> object:
        return SimpleNamespace(
            id=11,
            cash_krw=self.cash_krw,
            cash_usd=Decimal("100"),
        )

    @asynccontextmanager
    async def begin_nested(self):
        try:
            yield
        except Exception:
            self.savepoint_rollbacks += 1
            raise


@pytest.mark.asyncio
async def test_owner_evaluation_keeps_krw_and_usd_books_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        {
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "evaluation_amount": Decimal("200"),
            "quote_is_stale": False,
        },
        {
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "evaluation_amount": Decimal("50"),
            "quote_is_stale": False,
        },
    ]
    valuations: list[ShadowEquityValuation] = []

    def evaluate(
        valuation: ShadowEquityValuation,
        *,
        thresholds: object,
        previous: ShadowHighWatermarkState | None,
    ):
        valuations.append(valuation)
        return evaluate_shadow_high_watermark(
            valuation,
            thresholds=thresholds,  # type: ignore[arg-type]
            previous=previous,
        )

    persister = AsyncMock()
    monkeypatch.setattr(
        gate_module,
        "PaperTradingService",
        lambda _db: SimpleNamespace(get_positions=AsyncMock(return_value=positions)),
    )
    monkeypatch.setattr(
        gate_module,
        "load_shadow_high_watermark",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(gate_module, "evaluate_shadow_high_watermark", evaluate)
    monkeypatch.setattr(gate_module, "persist_shadow_high_watermark", persister)

    db = _PaperAccountDb()
    snapshot = await AccountStateGate().evaluate_owner(
        db,  # type: ignore[arg-type]
        7,
        markets=("KRX", "US"),
        daily_target_rate_pct=Decimal("0.5"),
        max_daily_loss_rate_pct=Decimal("1.0"),
        now=_NOW,
    )

    assert [(item.market, item.equity) for item in valuations] == [
        ("KRX", Decimal("1200")),
        ("US", Decimal("150")),
    ]
    assert persister.await_count == 2
    assert snapshot.as_evidence()["representativeMarket"] in {"KRX", "US"}
    assert snapshot.for_market("KRX").state is AccountState.NORMAL
    assert snapshot.for_market("US").state is AccountState.NORMAL


@pytest.mark.asyncio
async def test_hwm_persistence_failure_keeps_exit_only_buy_blocked(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    opened = _shadow("100")
    assert opened.state is not None
    monkeypatch.setattr(
        gate_module,
        "PaperTradingService",
        lambda _db: SimpleNamespace(get_positions=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(
        gate_module,
        "load_shadow_high_watermark",
        AsyncMock(return_value=opened.state),
    )
    monkeypatch.setattr(
        gate_module,
        "persist_shadow_high_watermark",
        AsyncMock(side_effect=RuntimeError("hwm store offline")),
    )
    db = _PaperAccountDb(cash_krw=Decimal("100.6"))

    snapshot = await AccountStateGate().evaluate_owner(
        db,  # type: ignore[arg-type]
        7,
        markets=("KRX",),
        daily_target_rate_pct=Decimal("0.5"),
        max_daily_loss_rate_pct=Decimal("1.0"),
        now=_NOW + timedelta(minutes=1),
    )

    evaluation = snapshot.for_market("KRX")
    buy = evaluate_account_state_gate("BUY", evaluation)
    assert evaluation.state is AccountState.EXIT_ONLY
    assert evaluation.persist_failed == "hwm store offline"
    assert buy.passed is False
    assert buy.reason == "exit_only"
    assert buy.evidence["persistFailed"] == "hwm store offline"
    assert db.savepoint_rollbacks == 1
    assert "account state persistence failed" in caplog.text


class _FailedValuationDb:
    async def scalar(self, _statement: object) -> object:
        raise RuntimeError("valuation source offline")


@pytest.mark.asyncio
async def test_valuation_lookup_failure_is_normal_unavailable_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = await AccountStateGate().evaluate_owner(
        _FailedValuationDb(),  # type: ignore[arg-type]
        7,
        markets=("KRX",),
        daily_target_rate_pct=Decimal("0.5"),
        max_daily_loss_rate_pct=Decimal("1.0"),
        now=_NOW,
    )

    evaluation = snapshot.for_market("KRX")
    assert evaluation.state is AccountState.NORMAL
    assert evaluation.multiplier == Decimal("1")
    assert evaluation.unavailable == "valuation source offline"
    assert evaluate_account_state_gate("BUY", evaluation).passed is True
    assert "account state valuation unavailable" in caplog.text


class _EmptyRiskDb:
    async def scalar(self, _statement: object) -> None:
        return None


class _HeldSellRiskDb:
    def __init__(self) -> None:
        self._values = iter(
            (
                11,
                SimpleNamespace(
                    total_invested=Decimal("100"),
                    quantity=Decimal("2"),
                ),
                0,
            )
        )

    async def scalar(self, _statement: object) -> object:
        return next(self._values)


def _snapshot() -> AITradingSnapshot:
    limits = AITradingLimits()
    usage = AITradingUsage()
    return AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=limits,
        usage=usage,
        usage_by_currency={"KRW": usage, "USD": usage},
        kill_switch=False,
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_policy_account_state_priority_blocks_exit_only_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AITradingPolicyService()
    monkeypatch.setattr(service, "get_snapshot", AsyncMock(return_value=_snapshot()))

    result = await service.evaluate_hard_risk(
        _EmptyRiskDb(),  # type: ignore[arg-type]
        7,
        action="BUY",
        market="KRX",
        symbol="005930",
        quantity=Decimal("1"),
        reference_price=Decimal("100"),
        ai_confidence=Decimal("1"),
        now=_NOW,
        account_state=_evaluation(AccountState.EXIT_ONLY),
    )

    rules = [check.rule for check in result.checks]
    account_check = next(
        check for check in result.checks if check.rule == "ACCOUNT_STATE"
    )
    assert rules[:3] == ["DAILY_MAX_LOSS", "ACCOUNT_STATE", "LOSS_STREAK"]
    assert account_check.passed is False
    assert result.passed is False
    assert result.as_evidence()["accountState"]["state"] == "EXIT_ONLY"  # type: ignore[index]


@pytest.mark.asyncio
async def test_policy_exit_only_does_not_change_sell_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AITradingPolicyService()
    monkeypatch.setattr(service, "get_snapshot", AsyncMock(return_value=_snapshot()))

    result = await service.evaluate_hard_risk(
        _HeldSellRiskDb(),  # type: ignore[arg-type]
        7,
        action="SELL",
        market="KRX",
        symbol="005930",
        quantity=Decimal("1"),
        reference_price=Decimal("100"),
        ai_confidence=Decimal("1"),
        now=_NOW,
        account_state=_evaluation(AccountState.EXIT_ONLY),
    )

    account_check = next(
        check for check in result.checks if check.rule == "ACCOUNT_STATE"
    )
    assert account_check.passed is True
    assert result.passed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "db", "expected_passed"),
    [
        ("BUY", _EmptyRiskDb(), False),
        ("SELL", _HeldSellRiskDb(), True),
    ],
)
async def test_job_execution_rechecks_exit_only_without_changing_sell(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    db: object,
    expected_passed: bool,
) -> None:
    recommendation_service = SimpleNamespace(
        get_recommendation=AsyncMock(
            return_value=SimpleNamespace(reference_price="100", evidence=[])
        )
    )
    monkeypatch.setattr(
        job_module,
        "AIRecommendationService",
        lambda _db: recommendation_service,
    )
    policy = AITradingPolicyService()
    monkeypatch.setattr(policy, "get_snapshot", AsyncMock(return_value=_snapshot()))
    monkeypatch.setattr(job_module, "AITradingPolicyService", lambda: policy)
    account_snapshot = AccountStateSnapshot(
        thresholds=_THRESHOLDS,
        evaluations=(_evaluation(AccountState.EXIT_ONLY),),
    )
    evaluate_owner = AsyncMock(return_value=account_snapshot)
    monkeypatch.setattr(
        job_module,
        "AccountStateGate",
        lambda: SimpleNamespace(evaluate_owner=evaluate_owner),
    )
    request = OrderRequest(
        client_order_id="ai-rec:rec-account-state",
        broker="PAPER",
        market="KRX",
        symbol="005930",
        side=side,  # type: ignore[arg-type]
        order_type="MARKET",
        quantity=Decimal("1"),
    )

    result = await OwnerScopedPaperOrders(now=_NOW)._hard_risk(
        db,  # type: ignore[arg-type]
        "7",
        request,
        reference_price="100",
        base_reasons=(),
    )

    account_check = next(
        check for check in result.checks if check.rule == "ACCOUNT_STATE"
    )
    assert account_check.passed is expected_passed
    assert result.passed is expected_passed
    assert evaluate_owner.await_args.kwargs["markets"] == ("KRX",)


@pytest.mark.asyncio
async def test_job_execution_account_state_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        job_module,
        "AIRecommendationService",
        lambda _db: SimpleNamespace(
            get_recommendation=AsyncMock(
                return_value=SimpleNamespace(reference_price="100", evidence=[])
            )
        ),
    )
    policy = AITradingPolicyService()
    monkeypatch.setattr(policy, "get_snapshot", AsyncMock(return_value=_snapshot()))
    monkeypatch.setattr(job_module, "AITradingPolicyService", lambda: policy)
    monkeypatch.setattr(
        job_module,
        "AccountStateGate",
        lambda: SimpleNamespace(
            evaluate_owner=AsyncMock(side_effect=RuntimeError("valuation offline"))
        ),
    )
    request = OrderRequest(
        client_order_id="ai-rec:rec-account-state-unavailable",
        broker="PAPER",
        market="KRX",
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
    )

    result = await OwnerScopedPaperOrders(now=_NOW)._hard_risk(
        _EmptyRiskDb(),  # type: ignore[arg-type]
        "7",
        request,
        reference_price="100",
        base_reasons=(),
    )

    account_check = next(
        check for check in result.checks if check.rule == "ACCOUNT_STATE"
    )
    assert account_check.passed is True
    assert result.passed is True
    assert "execution ACCOUNT_STATE unavailable; gate passes" in caplog.text
