from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation import (
    BacktestConfig,
    BreakoutStrategy,
    PriceBar,
    StrategyName,
    run_all_backtests,
    run_backtest,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _synthetic_closes() -> list[Decimal]:
    closes = [Decimal("100") + Decimal(index % 3) / 10 for index in range(30)]
    closes.extend(Decimal("110") + index for index in range(10))
    closes.extend(Decimal("90") - Decimal(index % 2) / 10 for index in range(15))
    closes.extend(Decimal("115") + index for index in range(10))
    closes.extend(Decimal("98") + Decimal(index % 4) / 10 for index in range(15))
    return closes


def _bars(closes: list[Decimal]) -> list[PriceBar]:
    return [
        PriceBar(
            timestamp=_START + timedelta(days=index),
            open=close - Decimal("0.1"),
            high=close + Decimal("0.8"),
            low=close - Decimal("0.8"),
            close=close,
            volume=Decimal("1000") + index * 10,
        )
        for index, close in enumerate(closes)
    ]


def test_four_strategies_share_costs_and_emit_bounded_metrics() -> None:
    config = BacktestConfig(
        initial_capital=Decimal("100000"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
    )

    results = run_all_backtests(
        _bars(_synthetic_closes()),
        symbol="AAPL",
        market="US",
        config=config,
    )

    assert {result.strategy for result in results} == set(StrategyName)
    assert len(results) == 4
    for result in results:
        assert result.initial_capital == Decimal("100000.000000")
        assert result.final_equity > 0
        assert result.trade_count == len(result.trades)
        assert Decimal("0") <= result.max_drawdown <= Decimal("1")
        assert Decimal("0") <= result.win_rate <= Decimal("1")
        assert result.total_return == (
            result.final_equity / result.initial_capital - Decimal("1")
        ).quantize(Decimal("0.000001"))
        assert all(trade.entry_at <= trade.exit_at for trade in result.trades)


def test_signals_before_cutoff_do_not_change_when_future_prices_change() -> None:
    common = _synthetic_closes()[:60]
    rising_future = common + [Decimal("150") + index for index in range(20)]
    falling_future = common + [Decimal("70") - index for index in range(20)]
    cutoff = _START + timedelta(days=59)
    config = BacktestConfig()

    rising = run_all_backtests(
        _bars(rising_future), symbol="AAPL", market="US", config=config
    )
    falling = run_all_backtests(
        _bars(falling_future), symbol="AAPL", market="US", config=config
    )

    for rising_result, falling_result in zip(rising, falling, strict=True):
        rising_prefix = tuple(
            signal for signal in rising_result.signals if signal.signal_at <= cutoff
        )
        falling_prefix = tuple(
            signal for signal in falling_result.signals if signal.signal_at <= cutoff
        )
        assert rising_prefix == falling_prefix


def test_fees_and_slippage_cannot_improve_identical_breakout_trades() -> None:
    bars = _bars(_synthetic_closes())
    free = run_backtest(
        BreakoutStrategy(),
        bars,
        symbol="AAPL",
        market="US",
        config=BacktestConfig(fee_rate=Decimal("0"), slippage_rate=Decimal("0")),
    )
    paid = run_backtest(
        BreakoutStrategy(),
        bars,
        symbol="AAPL",
        market="US",
        config=BacktestConfig(
            fee_rate=Decimal("0.002"),
            slippage_rate=Decimal("0.001"),
        ),
    )

    assert free.trade_count >= 1
    assert paid.trade_count == free.trade_count
    assert paid.final_equity < free.final_equity


def test_invalid_backtest_prices_fail_before_any_metric_is_emitted() -> None:
    bars = _bars(_synthetic_closes())
    bars[-1] = replace(bars[-1], close=Decimal("0"))

    with pytest.raises(ValueError, match="finite and positive"):
        run_backtest(
            BreakoutStrategy(),
            bars,
            symbol="AAPL",
            market="US",
            config=BacktestConfig(),
        )


@pytest.mark.parametrize(
    "config",
    [
        lambda: BacktestConfig(initial_capital=Decimal("0")),
        lambda: BacktestConfig(fee_rate=Decimal("NaN")),
        lambda: BacktestConfig(slippage_rate=Decimal("1")),
    ],
)
def test_invalid_cost_configuration_fails_closed(config: object) -> None:
    with pytest.raises(ValueError):
        config()  # type: ignore[operator]
