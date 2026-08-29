from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import Action, PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    MarketExecutionCost,
    PortfolioBacktestConfig,
    SignalStatus,
    UniverseEvidence,
    WalkForwardConfig,
    run_portfolio_backtest,
    run_portfolio_diagnostics,
    run_walk_forward,
)

_START = datetime(2025, 1, 1, tzinfo=UTC)


def _candidate(symbol: str = "ALPHA", market: str = "US") -> CandidateMetadata:
    return CandidateMetadata(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        sources=("synthetic",),
    )


def _bars(*, count: int = 330, scale: Decimal = Decimal("1")) -> tuple[PriceBar, ...]:
    closes = [
        (Decimal("100") + Decimal(index) / Decimal("100")) * scale
        for index in range(count)
    ]
    overrides = {
        255: Decimal("112"),
        256: Decimal("114"),
        257: Decimal("118"),
        258: Decimal("119"),
        259: Decimal("105"),
        260: Decimal("104"),
        261: Decimal("103"),
    }
    for index, close in overrides.items():
        if index < count:
            closes[index] = close * scale

    output: list[PriceBar] = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        open_price = previous
        if index == 256:
            open_price = Decimal("113") * scale
        elif index == 258:
            open_price = Decimal("119") * scale
        elif index == 259:
            open_price = Decimal("110") * scale
        elif index == 260:
            open_price = Decimal("104") * scale
        high = max(open_price, close) + scale
        low = min(open_price, close) - scale
        output.append(
            PriceBar(
                timestamp=_START + timedelta(days=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000000"),
            )
        )
    return tuple(output)


def _benchmark(bars: tuple[PriceBar, ...]) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            timestamp=bar.timestamp,
            open=bar.open / Decimal("2"),
            high=bar.high / Decimal("2"),
            low=bar.low / Decimal("2"),
            close=bar.close / Decimal("2"),
            volume=bar.volume,
        )
        for bar in bars
    )


def _config(*, costs: bool = True) -> PortfolioBacktestConfig:
    market_cost = (
        MarketExecutionCost(Decimal("0.001"), Decimal("0.0005"))
        if costs
        else MarketExecutionCost(Decimal("0"), Decimal("0"))
    )
    return PortfolioBacktestConfig(
        initial_cash=Decimal("100000"),
        max_positions=1,
        candidate_top_n=1,
        risk_per_trade_rate=Decimal("0.02"),
        max_symbol_allocation=Decimal("0.50"),
        kr_cost=market_cost,
        us_cost=market_cost,
    )


def _run(
    bars: tuple[PriceBar, ...], *, costs: bool = True
):
    candidate = _candidate()
    return run_portfolio_backtest(
        (candidate,),
        {candidate.key: bars},
        config=_config(costs=costs),
        benchmark_bars_by_market={"US": _benchmark(bars)},
        universe_evidence=UniverseEvidence(
            source="synthetic_point_in_time",
            point_in_time_membership=True,
            includes_delisted=True,
            as_of=bars[-1].timestamp,
        ),
    )


def test_every_fill_uses_the_first_later_bar_open() -> None:
    bars = _bars()

    result = _run(bars)

    executed = [
        signal for signal in result.signals if signal.status == SignalStatus.EXECUTED
    ]
    assert executed
    timestamps = {bar.timestamp: index for index, bar in enumerate(bars)}
    for signal in executed:
        signal_index = timestamps[signal.signal_at]
        next_bar = bars[signal_index + 1]
        assert signal.execution_at == next_bar.timestamp
        assert signal.reference_open == next_bar.open.quantize(Decimal("0.00000001"))
        assert signal.execution_at > signal.signal_at
    first_buy = next(signal for signal in executed if signal.action == Action.BUY)
    assert first_buy.signal_at == bars[255].timestamp
    assert first_buy.execution_at == bars[256].timestamp


def test_market_costs_reduce_equity_and_are_fully_accounted() -> None:
    bars = _bars()

    free = _run(bars, costs=False)
    paid = _run(bars, costs=True)

    assert free.trade_count >= 1
    assert paid.trade_count == free.trade_count
    assert free.fees_paid == Decimal("0E-8")
    assert free.slippage_cost == Decimal("0E-8")
    assert paid.fees_paid > 0
    assert paid.slippage_cost > 0
    assert paid.final_equity < free.final_equity
    assert sum(trade.entry_fee + trade.exit_fee for trade in paid.trades) <= (
        paid.fees_paid
    )


def test_repeated_portfolio_run_is_byte_hash_deterministic() -> None:
    bars = _bars()

    first = _run(bars)
    second = _run(bars)

    assert first == second
    assert first.determinism_hash == second.determinism_hash
    assert len(first.determinism_hash) == 64
    assert {item.code for item in first.evidence} >= {
        "DATA_QUALITY",
        "SURVIVORSHIP",
        "NO_LOOKAHEAD_BOUNDARY",
    }


def test_future_bar_changes_cannot_change_prior_signals_or_equity() -> None:
    base = list(_bars(count=300))
    cutoff_index = 270
    cutoff = base[cutoff_index].timestamp
    rising = tuple(base)
    falling_list = list(base)
    for index in range(cutoff_index + 1, len(falling_list)):
        original = falling_list[index]
        close = Decimal("90") - Decimal(index - cutoff_index) / Decimal("10")
        falling_list[index] = replace(
            original,
            open=close + Decimal("0.2"),
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
        )
    falling = tuple(falling_list)

    rising_result = _run(rising)
    falling_result = _run(falling)

    assert tuple(
        signal for signal in rising_result.signals if signal.signal_at <= cutoff
    ) == tuple(
        signal for signal in falling_result.signals if signal.signal_at <= cutoff
    )
    assert tuple(
        point for point in rising_result.equity_curve if point.timestamp <= cutoff
    ) == tuple(
        point for point in falling_result.equity_curve if point.timestamp <= cutoff
    )


def test_drawdown_and_expectancy_are_derived_from_observed_paths() -> None:
    result = _run(_bars())

    assert result.trade_count == len(result.trades)
    assert result.trade_count >= 1
    assert result.max_drawdown == max(
        point.drawdown for point in result.equity_curve
    )
    expected = (
        sum((trade.net_pnl for trade in result.trades), start=Decimal("0"))
        / Decimal(result.trade_count)
    ).quantize(Decimal("0.00000001"))
    assert result.expectancy == expected
    assert Decimal("0") <= result.win_rate <= Decimal("1")
    assert result.benchmark_return is not None
    assert result.excess_return == (
        result.total_return - result.benchmark_return
    ).quantize(Decimal("0.00000001"))


def test_diagnostics_cover_cost_stress_turnover_period_regime_and_delay() -> None:
    bars = _bars()
    candidate = _candidate()

    result = run_portfolio_diagnostics(
        (candidate,),
        {candidate.key: bars},
        config=_config(),
        benchmark_bars_by_market={"US": _benchmark(bars)},
        universe_evidence=UniverseEvidence(
            source="synthetic_point_in_time",
            point_in_time_membership=True,
            includes_delisted=True,
        ),
    )

    assert [item.multiplier for item in result.cost_stress] == [1, 2, 3]
    assert result.turnover_ratio > Decimal("0")
    assert result.period_performance
    assert result.regime_performance
    assert result.delayed_execution.determinism_hash
    baseline_buy = next(
        signal
        for signal in result.baseline.signals
        if signal.action == Action.BUY and signal.status == SignalStatus.EXECUTED
    )
    delayed_buy = next(
        signal
        for signal in result.delayed_execution.signals
        if signal.action == Action.BUY and signal.status == SignalStatus.EXECUTED
    )
    assert delayed_buy.execution_at is not None
    assert baseline_buy.execution_at is not None
    assert delayed_buy.execution_at > baseline_buy.execution_at
    assert {item.code for item in result.evidence} == {
        "COST_STRESS",
        "COUNTERFACTUAL",
        "TURNOVER",
    }


def test_diagnostics_run_each_symbol_removal_counterfactual() -> None:
    alpha = _candidate("ALPHA", "US")
    beta = _candidate("BETA", "US")
    alpha_bars = _bars()
    beta_bars = _bars(scale=Decimal("2"))

    result = run_portfolio_diagnostics(
        (alpha, beta),
        {alpha.key: alpha_bars, beta.key: beta_bars},
        config=replace(_config(), candidate_top_n=2),
        benchmark_bars_by_market={"US": _benchmark(alpha_bars)},
    )

    assert {
        (item.removed_market, item.removed_symbol)
        for item in result.symbol_removal
    } == {("US", "ALPHA"), ("US", "BETA")}


def test_walk_forward_returns_separate_rolling_train_and_test_folds() -> None:
    bars = _bars(count=330)
    candidate = _candidate()

    result = run_walk_forward(
        (candidate,),
        {candidate.key: bars},
        config=_config(),
        walk_forward=WalkForwardConfig(
            train_bars=260,
            test_bars=20,
            step_bars=20,
        ),
        benchmark_bars_by_market={"US": _benchmark(bars)},
        universe_evidence=UniverseEvidence(
            source="synthetic_point_in_time",
            point_in_time_membership=True,
            includes_delisted=True,
        ),
    )

    assert len(result.folds) == 3
    for previous, current in zip(result.folds, result.folds[1:], strict=False):
        assert current.train_start_at > previous.train_start_at
    for fold in result.folds:
        assert fold.train_end_at < fold.test_start_at <= fold.test_end_at
        assert all(
            signal.signal_at >= fold.train_end_at
            for signal in fold.test_result.signals
        )
        assert all(
            signal.execution_at is None
            or signal.execution_at >= fold.test_start_at
            for signal in fold.test_result.signals
        )
    assert result.determinism_hash


def test_kr_and_us_mapping_never_exceeds_the_position_cap() -> None:
    us = _candidate("ALPHA", "US")
    kr = _candidate("005930", "KR")
    us_bars = _bars(count=280)
    kr_bars = _bars(count=280, scale=Decimal("100"))

    result = run_portfolio_backtest(
        (us, kr),
        {us.key: us_bars, kr.key: kr_bars},
        config=PortfolioBacktestConfig(
            initial_cash=Decimal("10000000"),
            max_positions=1,
            candidate_top_n=2,
            risk_per_trade_rate=Decimal("0.02"),
            max_symbol_allocation=Decimal("0.50"),
        ),
        benchmark_bars_by_market={
            "US": _benchmark(us_bars),
            "KR": _benchmark(kr_bars),
        },
    )

    assert all(
        point.market_value >= Decimal("0") for point in result.equity_curve
    )
    assert len(result.open_positions) <= 1
    assert any(
        signal.reason == "max_positions_reached" for signal in result.signals
    )
