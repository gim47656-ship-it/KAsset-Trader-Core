"""Focused contracts for market-regime weights and the shared ensemble."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.contracts import (
    Action,
    PriceBar,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.producer import compose_weighted_ensemble
from app.extensions.kasset.automation.regime import (
    MarketRegime,
    assess_market_regime,
    weights_for_regime,
)

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _bars(*, start: Decimal, daily_move: Decimal, spread: Decimal) -> list[PriceBar]:
    rows: list[PriceBar] = []
    for offset in range(20):
        close = start + daily_move * offset
        rows.append(
            PriceBar(
                timestamp=_NOW - timedelta(days=19 - offset),
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=Decimal("1000"),
            )
        )
    return rows


@pytest.mark.parametrize(
    ("bars", "expected"),
    [
        (
            _bars(start=Decimal("100"), daily_move=Decimal("1"), spread=Decimal("1")),
            MarketRegime.BULL,
        ),
        (
            _bars(start=Decimal("119"), daily_move=Decimal("-1"), spread=Decimal("1")),
            MarketRegime.BEAR,
        ),
        (
            _bars(start=Decimal("100"), daily_move=Decimal("0"), spread=Decimal("1")),
            MarketRegime.SIDEWAYS,
        ),
        (
            _bars(start=Decimal("100"), daily_move=Decimal("0.1"), spread=Decimal("5")),
            MarketRegime.VOLATILE,
        ),
    ],
)
def test_regime_classifier_selects_expected_weight_table(
    bars: list[PriceBar], expected: MarketRegime
) -> None:
    assessment = assess_market_regime({"005930": bars})

    assert assessment.regime == expected
    assert assessment.weights == weights_for_regime(expected)
    assert sum(assessment.weights.values(), Decimal("0")) == Decimal("1.00")
    assert set(assessment.weights) == set(StrategyName)


def _result(strategy: StrategyName, action: Action, confidence: str) -> StrategyResult:
    return StrategyResult(
        action=action,
        confidence=Decimal(confidence),
        entry=Decimal("100") if action != Action.HOLD else None,
        stop=Decimal("98") if action != Action.HOLD else None,
        target=Decimal("104") if action != Action.HOLD else None,
        rationale=(f"{strategy.value} vote",),
        evidence=(),
        strategy=strategy,
        version="1.0.0",
        symbol="005930",
        market="KRX",
        as_of=_NOW,
        valid_until=_NOW + timedelta(days=1),
    )


def test_dynamic_weights_change_the_action_without_reimplementing_strategies() -> None:
    results = [
        _result(StrategyName.MOMENTUM, Action.BUY, "0.8"),
        _result(StrategyName.MEAN_REVERSION, Action.SELL, "0.8"),
        _result(StrategyName.BREAKOUT, Action.BUY, "0.8"),
        _result(StrategyName.VOLATILITY_TREND, Action.SELL, "0.8"),
    ]

    bull = compose_weighted_ensemble(results, weights_for_regime(MarketRegime.BULL))
    bear = compose_weighted_ensemble(results, weights_for_regime(MarketRegime.BEAR))

    assert bull.action == Action.HOLD
    assert bear.action == Action.HOLD
    assert bull.score == Decimal("0.240000")
    assert bear.score == Decimal("-0.240000")


def test_ensemble_requires_two_directional_votes_even_with_dominant_weight() -> None:
    results = [
        _result(StrategyName.MOMENTUM, Action.BUY, "1"),
        _result(StrategyName.MEAN_REVERSION, Action.HOLD, "0"),
        _result(StrategyName.BREAKOUT, Action.HOLD, "0"),
        _result(StrategyName.VOLATILITY_TREND, Action.HOLD, "0"),
    ]
    weights = {
        StrategyName.MOMENTUM: Decimal("1"),
        StrategyName.MEAN_REVERSION: Decimal("0"),
        StrategyName.BREAKOUT: Decimal("0"),
        StrategyName.VOLATILITY_TREND: Decimal("0"),
    }

    decision = compose_weighted_ensemble(results, weights)

    assert decision.score == Decimal("1.000000")
    assert decision.action == Action.HOLD
    assert len(decision.votes) == 4
