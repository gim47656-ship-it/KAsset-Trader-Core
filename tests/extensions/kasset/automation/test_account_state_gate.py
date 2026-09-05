"""PAPER 계좌 High-Watermark 활성 관문의 focused 계약."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.automation import account_state_gate as gate_module
from app.extensions.kasset.automation.account_state_gate import (
    ACCOUNT_STATE_SCHEMA_VERSION,
    STAGED_REDUCTION_MULTIPLIER,
    AccountState,
    AccountStateEvaluation,
    AccountStateGate,
    AccountStateThresholds,
    account_state_from_shadow,
    evaluate_account_state_gate,
)
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
    def __init__(self) -> None:
        self.commits = 0

    async def scalar(self, _statement: object) -> object:
        return SimpleNamespace(
            id=11,
            cash_krw=Decimal("1000"),
            cash_usd=Decimal("100"),
        )

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


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

    async def evaluate(
        _db: object,
        valuation: ShadowEquityValuation,
        *,
        thresholds: object,
    ):
        valuations.append(valuation)
        return evaluate_shadow_high_watermark(valuation, thresholds=thresholds)

    monkeypatch.setattr(
        gate_module,
        "PaperTradingService",
        lambda _db: SimpleNamespace(get_positions=AsyncMock(return_value=positions)),
    )
    monkeypatch.setattr(
        gate_module,
        "evaluate_and_persist_shadow_high_watermark",
        evaluate,
    )

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
    assert db.commits == 2
    assert snapshot.for_market("KRX").state is AccountState.NORMAL
    assert snapshot.for_market("US").state is AccountState.NORMAL


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
