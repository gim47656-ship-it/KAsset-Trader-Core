"""Focused contracts for deterministic KAsset PAPER position sizing."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    AITradingUsage,
)
from app.extensions.kasset.automation.position_sizing import (
    PositionSizeCapCode,
    PositionSizingConfig,
    PositionSizingInput,
    PositionSizingZeroCode,
    calculate_position_size,
)
from app.extensions.kasset.automation.regime import MarketRegime

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
_PRICE_AS_OF = _NOW - timedelta(hours=1)


def _buy_input(**changes: object) -> PositionSizingInput:
    base = PositionSizingInput(
        action="BUY",
        market="KRX",
        entry_price=Decimal("100"),
        price_as_of=_PRICE_AS_OF,
        evaluated_at=_NOW,
        operating_budget=Decimal("100000"),
        budget_used=Decimal("0"),
        max_symbol_allocation=Decimal("1"),
        current_symbol_invested=Decimal("0"),
        risk_per_trade_rate=Decimal("0.01"),
        regime=MarketRegime.BULL,
        strategy_stop=Decimal("90"),
        strategy_atr=Decimal("5"),
        average_volume=Decimal("1000000"),
        average_turnover=Decimal("100000000"),
    )
    return replace(base, **changes)


def _reason_codes(request: PositionSizingInput) -> set[PositionSizingZeroCode]:
    return {reason.code for reason in calculate_position_size(request).zero_reasons}


def test_buy_sizes_one_r_from_strategy_stop() -> None:
    result = calculate_position_size(_buy_input())

    assert result.quantity == Decimal("100")
    assert result.risk_budget == Decimal("1000.00")
    assert result.risk_per_unit == Decimal("10")
    assert result.limiting_caps == (PositionSizeCapCode.RISK_BUDGET,)
    assert result.actionable is True
    assert result.zero_reasons == ()
    assert result.as_evidence()["strategyStop"] == "90"
    assert result.as_evidence()["strategyAtr"] == "5"
    assert result.as_evidence()["riskBudget"] == "1000.00"


def test_buy_uses_smallest_owner_symbol_and_liquidity_cap() -> None:
    config = PositionSizingConfig(
        max_average_volume_participation=Decimal("0.02"),
        max_average_turnover_participation=Decimal("0.01"),
    )
    result = calculate_position_size(
        _buy_input(
            strategy_stop=Decimal("99"),
            risk_per_trade_rate=Decimal("0.10"),
            max_symbol_allocation=Decimal("0.20"),
            current_symbol_invested=Decimal("15000"),
            budget_used=Decimal("99000"),
            average_volume=Decimal("500"),
            average_turnover=Decimal("30000"),
        ),
        config=config,
    )

    caps = {cap.code: cap.quantity for cap in result.caps}
    assert caps == {
        PositionSizeCapCode.RISK_BUDGET: Decimal("10000.00"),
        PositionSizeCapCode.SYMBOL_ALLOCATION: Decimal("50.00"),
        PositionSizeCapCode.OWNER_BUDGET: Decimal("10"),
        PositionSizeCapCode.AVERAGE_VOLUME: Decimal("10.00"),
        PositionSizeCapCode.AVERAGE_TURNOVER: Decimal("3.00"),
    }
    assert result.quantity == Decimal("3")
    assert result.limiting_caps == (PositionSizeCapCode.AVERAGE_TURNOVER,)


@pytest.mark.parametrize(
    ("risk_rate", "regime", "expected"),
    [
        (Decimal("0.005"), MarketRegime.BULL, Decimal("50")),
        (Decimal("0.01"), MarketRegime.BULL, Decimal("100")),
        (Decimal("0.01"), MarketRegime.BEAR, Decimal("0")),
        (Decimal("0.01"), MarketRegime.SIDEWAYS, Decimal("75")),
        (Decimal("0.01"), MarketRegime.VOLATILE, Decimal("50")),
    ],
)
def test_risk_rate_and_regime_scale_one_r_budget(
    risk_rate: Decimal,
    regime: MarketRegime,
    expected: Decimal,
) -> None:
    result = calculate_position_size(
        _buy_input(risk_per_trade_rate=risk_rate, regime=regime)
    )

    assert result.quantity == expected
    assert result.regime == regime.name
    if regime == MarketRegime.BEAR:
        assert _reason_codes(_buy_input(regime=regime)) == {
            PositionSizingZeroCode.REGIME_BLOCKED
        }


@pytest.mark.parametrize(
    ("raw_regime", "canonical"),
    [
        ("BULL", "BULL"),
        ("TRENDING_UP", "BULL"),
        ("SIDEWAYS", "SIDEWAYS"),
        ("RANGING", "SIDEWAYS"),
        ("BEAR", "BEAR"),
        ("TRENDING_DOWN", "BEAR"),
    ],
)
def test_regime_aliases_emit_canonical_evidence(
    raw_regime: str,
    canonical: str,
) -> None:
    result = calculate_position_size(_buy_input(regime=raw_regime))

    assert result.regime == canonical


@pytest.mark.parametrize(
    ("market", "budget", "expected"),
    [
        ("KRX", Decimal("3999"), Decimal("3")),
        ("US", Decimal("3141.59"), Decimal("3.1415")),
    ],
)
def test_market_lot_rounding_always_rounds_down(
    market: str,
    budget: Decimal,
    expected: Decimal,
) -> None:
    result = calculate_position_size(
        _buy_input(
            market=market,
            operating_budget=budget,
            risk_per_trade_rate=Decimal("0.01"),
        )
    )

    assert result.quantity == expected
    assert result.quantity <= result.unrounded_quantity


def test_sell_needs_no_atr_and_is_capped_to_actual_paper_holding() -> None:
    result = calculate_position_size(
        PositionSizingInput(
            action="SELL",
            market="US",
            entry_price=Decimal("25"),
            price_as_of=_PRICE_AS_OF,
            evaluated_at=_NOW,
            current_holding_quantity=Decimal("2.34567"),
            strategy_quantity=Decimal("10"),
        )
    )

    assert result.quantity == Decimal("2.3456")
    assert result.unrounded_quantity == Decimal("2.34567")
    assert result.limiting_caps == (PositionSizeCapCode.PAPER_HOLDING,)
    assert result.actionable is True


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"strategy_stop": None}, PositionSizingZeroCode.MISSING_STOP),
        ({"strategy_stop": Decimal("100")}, PositionSizingZeroCode.INVERTED_STOP),
        ({"strategy_atr": None}, PositionSizingZeroCode.MISSING_ATR),
        ({"strategy_atr": Decimal("0")}, PositionSizingZeroCode.NONPOSITIVE_ATR),
        (
            {"average_volume": None},
            PositionSizingZeroCode.MISSING_LIQUIDITY,
        ),
        (
            {"average_turnover": None},
            PositionSizingZeroCode.MISSING_LIQUIDITY,
        ),
        (
            {"price_as_of": _NOW - timedelta(days=5)},
            PositionSizingZeroCode.STALE_PRICE,
        ),
        (
            {"price_as_of": _NOW + timedelta(seconds=1)},
            PositionSizingZeroCode.FUTURE_PRICE,
        ),
        ({"entry_price": Decimal("NaN")}, PositionSizingZeroCode.INVALID_PRICE),
        ({"operating_budget": Decimal("0")}, PositionSizingZeroCode.ZERO_BUDGET),
        ({"budget_used": Decimal("100000")}, PositionSizingZeroCode.ZERO_BUDGET),
        (
            {"current_symbol_invested": Decimal("100000")},
            PositionSizingZeroCode.ZERO_SYMBOL_ALLOCATION,
        ),
    ],
)
def test_invalid_buy_inputs_fail_closed_with_structured_zero_reason(
    changes: dict[str, object],
    reason: PositionSizingZeroCode,
) -> None:
    result = calculate_position_size(_buy_input(**changes))

    assert result.quantity == Decimal("0")
    assert result.actionable is False
    assert reason in {item.code for item in result.zero_reasons}
    evidence = result.as_evidence()
    assert reason.value in {item["code"] for item in evidence["zeroReasons"]}


def test_nonfinite_sell_quantity_fails_closed() -> None:
    request = PositionSizingInput(
        action="SELL",
        market="US",
        entry_price=Decimal("25"),
        price_as_of=_PRICE_AS_OF,
        evaluated_at=_NOW,
        current_holding_quantity=Decimal("2"),
        strategy_quantity=Decimal("Infinity"),
    )

    assert _reason_codes(request) == {PositionSizingZeroCode.NONFINITE_QUANTITY}


def test_ai_fields_cannot_enter_or_override_the_sizing_contract() -> None:
    request = _buy_input()
    trusted = {item.name: getattr(request, item.name) for item in fields(request)}

    with pytest.raises(TypeError):
        PositionSizingInput(  # type: ignore[call-arg]
            **trusted,
            ai_quantity=Decimal("999999"),
        )
    with pytest.raises(TypeError):
        PositionSizingInput(  # type: ignore[call-arg]
            **trusted,
            ai_stop=Decimal("99.99"),
        )


class _NoPositionDb:
    async def scalar(self, _statement: object) -> None:
        return None


class _HeldPositionDb:
    def __init__(self) -> None:
        self._values = iter(
            (
                91,
                SimpleNamespace(
                    quantity=Decimal("2.34567"),
                    total_invested=Decimal("50"),
                ),
            )
        )

    async def scalar(self, _statement: object) -> object:
        return next(self._values)


@pytest.mark.asyncio
async def test_portfolio_plan_delegates_buy_quantity_to_atr_sizer() -> None:
    limits = AITradingLimits(
        risk_level=4,
        operating_budget=Decimal("100000"),
    )
    plan = await AITradingPolicyService().portfolio_plan(
        _NoPositionDb(),  # type: ignore[arg-type]
        42,
        action="BUY",
        market="KRX",
        symbol="005930",
        reference_price=Decimal("100"),
        limits=limits,
        usage=AITradingUsage(),
        strategy_stop=Decimal("90"),
        strategy_atr=Decimal("5"),
        price_as_of=_PRICE_AS_OF,
        evaluated_at=_NOW,
        regime=MarketRegime.BULL,
        average_volume=Decimal("1000000"),
        average_turnover=Decimal("100000000"),
    )

    assert plan.target_quantity == Decimal("100")
    assert plan.position_sizing is not None
    assert plan.position_sizing.risk_budget == Decimal("1000.00")
    assert "limitingCaps=RISK_BUDGET" in plan.note
    sizing_evidence = plan.as_evidence()["positionSizing"]
    assert isinstance(sizing_evidence, dict)
    assert sizing_evidence["quantity"] == "100"


@pytest.mark.asyncio
async def test_portfolio_plan_sell_uses_actual_paper_holding_without_atr() -> None:
    plan = await AITradingPolicyService().portfolio_plan(
        _HeldPositionDb(),  # type: ignore[arg-type]
        42,
        action="SELL",
        market="US",
        symbol="AAPL",
        reference_price=Decimal("25"),
        limits=AITradingLimits(
            risk_level=2,
            operating_budget=Decimal("100000"),
            currency="USD",
        ),
        usage=AITradingUsage(budget_used=Decimal("50")),
        price_as_of=_PRICE_AS_OF,
        evaluated_at=_NOW,
        strategy_quantity=Decimal("10"),
    )

    assert plan.target_quantity == Decimal("2.3456")
    assert plan.position_sizing is not None
    assert plan.position_sizing.limiting_caps == (PositionSizeCapCode.PAPER_HOLDING,)
