from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation import (
    STRATEGIES,
    Action,
    BreakoutStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    PriceBar,
    VolatilityTrendStrategy,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(closes: list[Decimal]) -> list[PriceBar]:
    return [
        PriceBar(
            timestamp=_START + timedelta(days=index),
            open=close - Decimal("0.2"),
            high=close + Decimal("0.4"),
            low=close - Decimal("0.4"),
            close=close,
            volume=Decimal("1000") + index,
        )
        for index, close in enumerate(closes)
    ]


def _uptrend(count: int = 40) -> list[PriceBar]:
    return _bars([Decimal("100") + index for index in range(count)])


@pytest.mark.parametrize(
    "strategy",
    [
        MomentumStrategy(),
        MeanReversionStrategy(),
        BreakoutStrategy(),
        VolatilityTrendStrategy(),
    ],
)
def test_all_strategies_emit_complete_versioned_results(strategy: object) -> None:
    bars = _uptrend()

    result = strategy.evaluate(  # type: ignore[attr-defined]
        bars,
        symbol="aapl",
        market="US",
        as_of=bars[-1].timestamp,
    )

    assert result.action in {Action.BUY, Action.SELL, Action.HOLD}
    assert Decimal("0") <= result.confidence <= Decimal("1")
    assert result.entry == bars[-1].close
    assert result.strategy.value
    assert result.version == "1.0.0"
    assert result.symbol == "AAPL"
    assert result.as_of == bars[-1].timestamp
    assert result.valid_until > result.as_of
    assert result.rationale
    assert result.evidence
    if result.action == Action.BUY:
        assert result.stop < result.entry < result.target
    elif result.action == Action.SELL:
        assert result.target < result.entry < result.stop
    else:
        assert result.stop is None
        assert result.target is None


def test_momentum_and_breakout_are_actionable_on_deterministic_uptrend() -> None:
    bars = _uptrend()

    momentum = MomentumStrategy().evaluate(
        bars, symbol="AAPL", market="US", as_of=bars[-1].timestamp
    )
    breakout = BreakoutStrategy().evaluate(
        bars, symbol="AAPL", market="US", as_of=bars[-1].timestamp
    )

    assert momentum.action == Action.BUY
    assert breakout.action == Action.BUY


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_short_series_fails_closed(strategy: object) -> None:
    bars = _uptrend(5)

    result = strategy.evaluate(  # type: ignore[attr-defined]
        bars, symbol="AAPL", market="US", as_of=bars[-1].timestamp
    )

    assert result.action == Action.HOLD
    assert result.confidence == 0
    assert result.entry is None
    assert result.valid_until == result.as_of
    assert result.evidence[0].code == "INSUFFICIENT_BARS"


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda bar: replace(bar, close=Decimal("NaN")), "NON_FINITE_BAR"),
        (lambda bar: replace(bar, close=Decimal("0")), "NON_POSITIVE_PRICE"),
    ],
)
def test_malformed_prices_fail_closed(
    strategy: object,
    mutate: object,
    expected_code: str,
) -> None:
    bars = _uptrend()
    bars[-1] = mutate(bars[-1])  # type: ignore[operator]

    result = strategy.evaluate(  # type: ignore[attr-defined]
        bars, symbol="AAPL", market="US", as_of=bars[-1].timestamp
    )

    assert result.action == Action.HOLD
    assert result.confidence == 0
    assert result.entry is None
    assert result.evidence[0].code == expected_code


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_future_bar_is_rejected_instead_of_silently_ignored(strategy: object) -> None:
    bars = _uptrend()
    cutoff = bars[-2].timestamp

    result = strategy.evaluate(  # type: ignore[attr-defined]
        bars, symbol="AAPL", market="US", as_of=cutoff
    )

    assert result.action == Action.HOLD
    assert result.confidence == 0
    assert result.evidence[0].code == "FUTURE_PRICE_BAR"


def test_same_prefix_has_same_signal_when_future_tail_changes() -> None:
    prefix = _uptrend(40)
    rising_tail = _bars([Decimal("200") + index for index in range(10)])
    falling_tail = _bars([Decimal("80") - index for index in range(10)])
    rising_tail = [
        replace(bar, timestamp=prefix[-1].timestamp + timedelta(days=index + 1))
        for index, bar in enumerate(rising_tail)
    ]
    falling_tail = [
        replace(bar, timestamp=prefix[-1].timestamp + timedelta(days=index + 1))
        for index, bar in enumerate(falling_tail)
    ]

    for strategy in STRATEGIES:
        before_rise = strategy.evaluate(
            (prefix + rising_tail)[: len(prefix)],
            symbol="AAPL",
            market="US",
            as_of=prefix[-1].timestamp,
        )
        before_fall = strategy.evaluate(
            (prefix + falling_tail)[: len(prefix)],
            symbol="AAPL",
            market="US",
            as_of=prefix[-1].timestamp,
        )
        assert before_rise == before_fall
