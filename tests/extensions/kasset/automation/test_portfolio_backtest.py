from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import Action, PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    CONSERVATIVE_COST_PROFILE,
    LIVE_MATCHED_COST_PROFILE,
    CandidateBenchmarkSeries,
    EquityPoint,
    MarketExecutionCost,
    PortfolioBacktestConfig,
    SignalStatus,
    UniverseEvidence,
    WalkForwardConfig,
    _annualization_periods_per_year,
    _risk_adjusted_metrics,
    _stable_hash,
    run_portfolio_backtest,
    run_portfolio_diagnostics,
    run_walk_forward,
)
from app.extensions.kasset.automation.promotion_evidence import (
    _STORAGE,
    PortfolioEvidenceSource,
    PromotionEvidenceBuildError,
    _dataset_content_hash,
    _experiment_identity,
    _require_readiness,
    _require_stored_benchmark_window_coverage,
    _select_universe_rows,
    _thresholds_snapshot,
    _universe_query,
    build_promotion_raw_payload,
    derive_metrics_from_stored_payload,
    derive_promotion_metrics,
)
from app.extensions.kasset.automation.strategy_artifact import (
    PROMOTION_EVIDENCE_SCHEMA_VERSION,
    StrategyArtifactManifest,
)
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PROMOTION_THRESHOLDS,
    FORWARD_PAPER_TRACK,
    HISTORICAL_PIT_TRACK,
    PromotionMetrics,
    promotion_thresholds_for_track,
)
from app.services.daily_candles.readiness import (
    BenchmarkCoverage,
    CohortEvidence,
    DailyCandlesReadiness,
    MarketReadiness,
    SymbolReadinessExclusion,
)
from app.services.paper_trading_service import FEE_RATES
from app.services.research_canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)
from scripts.kasset_bias_audit import (
    format_bias_audit_table,
    run_bias_audit,
)
from scripts.kasset_bias_audit import (
    parse_args as parse_bias_audit_args,
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


def _trend_benchmark(
    bars: tuple[PriceBar, ...],
    *,
    end_multiplier: Decimal,
) -> tuple[PriceBar, ...]:
    denominator = Decimal(max(len(bars) - 1, 1))
    return tuple(
        PriceBar(
            timestamp=bar.timestamp,
            open=(price := Decimal("100"))
            * (Decimal("1") + (end_multiplier - Decimal("1")) * index / denominator),
            high=price
            * (Decimal("1") + (end_multiplier - Decimal("1")) * index / denominator)
            + Decimal("1"),
            low=price
            * (Decimal("1") + (end_multiplier - Decimal("1")) * index / denominator)
            - Decimal("1"),
            close=price
            * (Decimal("1") + (end_multiplier - Decimal("1")) * index / denominator),
            volume=bar.volume,
        )
        for index, bar in enumerate(bars)
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


def _run(bars: tuple[PriceBar, ...], *, costs: bool = True):
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
    assert result.max_drawdown == max(point.drawdown for point in result.equity_curve)
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


def test_live_matched_cost_profile_tracks_paper_fee_source() -> None:
    kr = LIVE_MATCHED_COST_PROFILE["KR"]
    us = LIVE_MATCHED_COST_PROFILE["US"]
    assert CONSERVATIVE_COST_PROFILE["KR"] == MarketExecutionCost(
        fee_rate=Decimal("0.0015"),
        slippage_rate=Decimal("0.0010"),
    )
    assert CONSERVATIVE_COST_PROFILE["US"] == MarketExecutionCost(
        fee_rate=Decimal("0.0010"),
        slippage_rate=Decimal("0.0005"),
    )

    assert FEE_RATES["equity_kr"] == {
        "buy": 0.00015,
        "sell": 0.00015,
        "tax_sell": 0.0018,
    }
    assert kr.fee_rate == Decimal(str(FEE_RATES["equity_kr"]["buy"]))
    assert kr.fee_rate == Decimal(str(FEE_RATES["equity_kr"]["sell"]))
    assert kr.sell_tax_rate == Decimal(str(FEE_RATES["equity_kr"]["tax_sell"]))
    assert kr.fee_rate * 2 + kr.sell_tax_rate == Decimal("0.00210")

    assert FEE_RATES["equity_us"] == {
        "buy": 0.0007,
        "sell": 0.0007,
        "min_fee_usd": 1.0,
    }
    assert us.fee_rate == Decimal(str(FEE_RATES["equity_us"]["buy"]))
    assert us.fee_rate == Decimal(str(FEE_RATES["equity_us"]["sell"]))
    assert us.fee_rate * 2 == Decimal("0.0014")
    assert us.min_fee_absolute == Decimal("1.0")
    assert kr.slippage_rate == us.slippage_rate == Decimal("0")


def test_new_execution_cost_fields_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="sell_tax_rate"):
        MarketExecutionCost(
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            sell_tax_rate=Decimal("1"),
        )
    with pytest.raises(ValueError, match="min_fee_absolute"):
        MarketExecutionCost(
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            min_fee_absolute=Decimal("-0.01"),
        )


def test_sell_tax_is_separate_and_applies_only_after_a_sell() -> None:
    candidate = _candidate()
    tax_cost = MarketExecutionCost(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
        sell_tax_rate=Decimal("0.0018"),
    )
    config = replace(
        _config(costs=False),
        kr_cost=tax_cost,
        us_cost=tax_cost,
    )
    buy_only_bars = _bars(count=257)
    buy_only = run_portfolio_backtest(
        (candidate,),
        {candidate.key: buy_only_bars},
        config=config,
    )
    completed = run_portfolio_backtest(
        (candidate,),
        {candidate.key: _bars()},
        config=config,
    )

    assert buy_only.open_positions
    assert buy_only.taxes_paid == Decimal("0E-8")
    assert completed.trade_count >= 1
    assert completed.fees_paid == Decimal("0E-8")
    assert completed.taxes_paid > 0


def test_minimum_fee_and_none_slippage_are_applied_per_fill() -> None:
    candidate = _candidate()
    cost = MarketExecutionCost(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0.05"),
        min_fee_absolute=Decimal("1"),
    )
    result = run_portfolio_backtest(
        (candidate,),
        {candidate.key: _bars()},
        config=replace(
            _config(costs=False),
            kr_cost=cost,
            us_cost=cost,
            slippage_mode="none",
        ),
    )
    executed = [
        signal for signal in result.signals if signal.status == SignalStatus.EXECUTED
    ]

    assert executed
    assert result.slippage_cost == Decimal("0E-8")
    assert all(signal.fill_price == signal.reference_open for signal in executed)
    assert all(signal.fee >= Decimal("1") for signal in executed)


def test_signal_close_fill_is_explicitly_marked_as_lookahead_prone() -> None:
    bars = _bars()
    candidate = _candidate()
    result = run_portfolio_backtest(
        (candidate,),
        {candidate.key: bars},
        config=replace(_config(), entry_fill="signal_close"),
    )
    first_buy = next(
        signal
        for signal in result.signals
        if signal.status == SignalStatus.EXECUTED and signal.action == Action.BUY
    )

    assert first_buy.signal_at == bars[255].timestamp
    assert first_buy.execution_at == first_buy.signal_at
    assert first_buy.reference_open == bars[255].close.quantize(Decimal("0.00000001"))
    assert {item.code for item in result.evidence} >= {
        "ENTRY_FILL",
        "LOOKAHEAD_RISK",
    }
    assert "NO_LOOKAHEAD_BOUNDARY" not in {item.code for item in result.evidence}


def test_signal_close_future_changes_do_not_rewrite_prior_paths() -> None:
    base = list(_bars(count=300))
    cutoff_index = 270
    cutoff = base[cutoff_index].timestamp
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
    candidate = _candidate()
    config = replace(_config(), entry_fill="signal_close")

    rising = run_portfolio_backtest(
        (candidate,),
        {candidate.key: tuple(base)},
        config=config,
    )
    falling = run_portfolio_backtest(
        (candidate,),
        {candidate.key: tuple(falling_list)},
        config=config,
    )

    assert tuple(signal for signal in rising.signals if signal.signal_at <= cutoff) == (
        tuple(signal for signal in falling.signals if signal.signal_at <= cutoff)
    )
    assert tuple(
        point for point in rising.equity_curve if point.timestamp <= cutoff
    ) == (tuple(point for point in falling.equity_curve if point.timestamp <= cutoff))


def test_risk_adjusted_metrics_use_the_observed_equity_grid() -> None:
    equity_curve = tuple(
        EquityPoint(
            timestamp=_START + timedelta(days=days),
            cash=equity,
            market_value=Decimal("0"),
            equity=equity,
            drawdown=drawdown,
        )
        for days, equity, drawdown in (
            (0, Decimal("100"), Decimal("0")),
            (120, Decimal("110"), Decimal("0")),
            (240, Decimal("99"), Decimal("0.10")),
            (365, Decimal("120"), Decimal("0")),
        )
    )

    sharpe, calmar = _risk_adjusted_metrics(
        equity_curve,
        max_drawdown=Decimal("0.10"),
    )

    # 수익률은 1/10, -1/10, 7/33이고 평균은 7/99,
    # 표본분산은 8167/326700이다. 365일에 3구간이므로
    # Sharpe = (7/99)/sqrt(8167/326700)*sqrt(3) = 70/sqrt(8167).
    assert sharpe == Decimal("0.77458086")
    # Calmar = ((120/100) ** (3/3) - 1) / 0.10.
    assert calmar == Decimal("2.00000000")


def test_calendar_padding_correction_is_sqrt_365_over_252() -> None:
    seconds_per_year = 365 * 86400
    trading_grid = tuple(
        _START + timedelta(seconds=round(index * seconds_per_year / 252))
        for index in range(253)
    )
    padded_calendar_grid = tuple(_START + timedelta(days=index) for index in range(366))

    trading_periods = _annualization_periods_per_year(trading_grid)
    calendar_periods = _annualization_periods_per_year(padded_calendar_grid)

    assert trading_periods is not None
    assert calendar_periods is not None
    returns = tuple(
        Decimal("0.001") if index % 2 else Decimal("-0.0002") for index in range(252)
    )
    mean = sum(returns, start=Decimal("0")) / Decimal(len(returns))
    variance = sum(
        ((value - mean) ** 2 for value in returns),
        start=Decimal("0"),
    ) / Decimal(len(returns) - 1)
    unannualized_sharpe = mean / variance.sqrt()
    trading_sharpe = unannualized_sharpe * trading_periods.sqrt()
    padded_calendar_sharpe = unannualized_sharpe * calendar_periods.sqrt()

    correction = padded_calendar_sharpe / trading_sharpe
    expected = (Decimal(365) / Decimal(252)).sqrt()
    assert abs(correction - expected) < Decimal("0.000001")


def test_bias_audit_produces_four_cumulative_rows_and_footnote() -> None:
    bars = _bars()
    candidate = _candidate()

    rows = run_bias_audit(
        (candidate,),
        {candidate.key: bars},
        base_config=_config(),
        benchmark_bars_by_market={"US": _benchmark(bars)},
    )
    table = format_bias_audit_table(rows)

    assert tuple(row.scenario for row in rows) == (
        "baseline",
        "cost_profile",
        "slippage_mode",
        "entry_fill",
    )
    assert len(rows) == 4
    assert "position sizing이 running equity를 따라가므로 경로 의존적" in table
    assert (
        sum(
            line.startswith(
                ("baseline ", "cost_profile ", "slippage_mode ", "entry_fill ")
            )
            for line in table.splitlines()
        )
        == 4
    )


def test_bias_audit_help_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_bias_audit_args(["--help"])

    assert raised.value.code == 0
    assert "cumulative bias audit" in capsys.readouterr().out


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
    excluded = replace(
        result,
        baseline=replace(
            result.baseline,
            sharpe=Decimal("999"),
            calmar=Decimal("999"),
            taxes_paid=Decimal("999"),
        ),
        delayed_execution=replace(
            result.delayed_execution,
            sharpe=Decimal("-999"),
            calmar=Decimal("-999"),
            taxes_paid=Decimal("-999"),
        ),
    )
    assert _stable_hash(excluded) == result.determinism_hash


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
        (item.removed_market, item.removed_symbol) for item in result.symbol_removal
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
            signal.signal_at >= fold.train_end_at for signal in fold.test_result.signals
        )
        assert all(
            signal.execution_at is None or signal.execution_at >= fold.test_start_at
            for signal in fold.test_result.signals
        )
    assert result.determinism_hash
    first_fold = result.folds[0]
    excluded = replace(
        result,
        folds=(
            replace(
                first_fold,
                train_result=replace(
                    first_fold.train_result,
                    sharpe=Decimal("999"),
                    calmar=Decimal("999"),
                    taxes_paid=Decimal("999"),
                ),
            ),
            *result.folds[1:],
        ),
    )
    assert _stable_hash(excluded) == result.determinism_hash


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

    assert all(point.market_value >= Decimal("0") for point in result.equity_curve)
    assert len(result.open_positions) <= 1
    assert any(signal.reason == "max_positions_reached" for signal in result.signals)


def test_backtest_ranking_uses_candidate_specific_kr_benchmarks() -> None:
    kospi = _candidate("005930", "KR")
    kosdaq = _candidate("035900", "KR")
    bars = _bars(count=280, scale=Decimal("100"))
    flat = _trend_benchmark(bars, end_multiplier=Decimal("1"))
    rising = _trend_benchmark(bars, end_multiplier=Decimal("2"))
    config = PortfolioBacktestConfig(
        initial_cash=Decimal("10000000"),
        max_positions=1,
        candidate_top_n=1,
        risk_per_trade_rate=Decimal("0.02"),
        max_symbol_allocation=Decimal("0.50"),
    )

    kospi_first = run_portfolio_backtest(
        (kospi, kosdaq),
        {kospi.key: bars, kosdaq.key: bars},
        config=config,
        benchmark_bars_by_market={"KR": flat},
        benchmark_bars_by_candidate={
            kospi.key: CandidateBenchmarkSeries("KOSPI", flat),
            kosdaq.key: CandidateBenchmarkSeries("KOSDAQ", rising),
        },
    )
    kosdaq_first = run_portfolio_backtest(
        (kospi, kosdaq),
        {kospi.key: bars, kosdaq.key: bars},
        config=config,
        benchmark_bars_by_market={"KR": flat},
        benchmark_bars_by_candidate={
            kospi.key: CandidateBenchmarkSeries("KOSPI", rising),
            kosdaq.key: CandidateBenchmarkSeries("KOSDAQ", flat),
        },
    )

    assert (
        next(
            signal.symbol
            for signal in kospi_first.signals
            if signal.action == Action.BUY
        )
        == "005930"
    )
    assert (
        next(
            signal.symbol
            for signal in kosdaq_first.signals
            if signal.action == Action.BUY
        )
        == "035900"
    )


def test_reporting_benchmark_does_not_fill_partial_candidate_ranking_input() -> None:
    kospi = _candidate("005930", "KR")
    kosdaq = _candidate("035900", "KR")
    bars = _bars(count=280, scale=Decimal("100"))
    flat = _trend_benchmark(bars, end_multiplier=Decimal("1"))
    rising = _trend_benchmark(bars, end_multiplier=Decimal("2"))
    config = PortfolioBacktestConfig(
        initial_cash=Decimal("10000000"),
        max_positions=1,
        candidate_top_n=1,
        risk_per_trade_rate=Decimal("0.02"),
        max_symbol_allocation=Decimal("0.50"),
    )
    candidate_benchmarks = {
        kospi.key: CandidateBenchmarkSeries("KOSPI", rising),
    }

    with_reporting = run_portfolio_backtest(
        (kospi, kosdaq),
        {kospi.key: bars, kosdaq.key: bars},
        config=config,
        benchmark_bars_by_market={"KR": flat},
        benchmark_bars_by_candidate=candidate_benchmarks,
    )
    without_reporting = run_portfolio_backtest(
        (kospi, kosdaq),
        {kospi.key: bars, kosdaq.key: bars},
        config=config,
        benchmark_bars_by_candidate=candidate_benchmarks,
    )

    assert with_reporting.signals == without_reporting.signals
    assert [item.market for item in with_reporting.benchmark_by_market] == ["KR"]
    assert with_reporting.excess_return is not None


def _ready_market(market: str, *, track: str = HISTORICAL_PIT_TRACK) -> MarketReadiness:
    historical = track == HISTORICAL_PIT_TRACK
    # forward 코호트는 현재 시점 시가총액으로 뽑히므로 과거 시점 멤버십과
    # 상장폐지 생존자를 증명할 수 없다. 그 사실을 그대로 반영한다.
    historical_blockers = (
        ()
        if historical
        else (
            f"{market}:cohort_not_historical_pit",
            f"{market}:point_in_time_unavailable",
        )
    )
    benchmark = BenchmarkCoverage(
        market=market,  # type: ignore[arg-type]
        symbol="KOSPI" if market == "kr" else "SPY",
        start=_START,
        end=_START + timedelta(days=329),
        count=330,
        source="kis",
        sources=("kis",),
        status="available",
    )
    cohort = CohortEvidence(
        cohort_id=f"{market}-cohort",
        market=market,  # type: ignore[arg-type]
        selection_as_of=datetime(2024, 1, 2, tzinfo=UTC),
        selection_date=date(2024, 1, 2),
        effective_date=date(2024, 1, 3),
        method="latest_market_cap",
        requested_size=10,
        active_member_count=10,
        valuation_snapshot_date=date(2024, 1, 1),
        valuation_snapshot_source="naver_finance" if market == "kr" else "yahoo",
        evidence_scope=track,
    )
    return MarketReadiness(
        market=market,  # type: ignore[arg-type]
        cohort=cohort,
        evaluated_window_start=date(2024, 2, 1),
        evaluated_window_end=date(2025, 1, 1),
        latest_completed_session=date(2025, 1, 1),
        ingest_lag_session_count=0,
        unevidenced_session_count=0,
        unevidenced_sessions=(),
        total_symbol_count=10,
        cohort_active_member_count=10,
        forced_member_count=0,
        benchmark_member_count=1,
        active_symbol_count=9,
        inactive_symbol_count=1,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=10,
        eligible_symbol_count=10,
        eligible_symbols=(
            (("005930",) if market == "kr" else ("ALPHA",))
            + tuple(f"{market.upper()}-{index}" for index in range(9))
        ),
        excluded_symbols=(),
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=0,
        calendar_status="available",
        price_adjustment_status="covered",
        corporate_action_status="clear",
        corporate_action_covered_symbol_count=10,
        adjustment_covered_symbol_count=10,
        list_date_covered_symbol_count=10,
        members_listed_after_cohort_start=0,
        delist_date_covered_inactive_count=1,
        point_in_time_available=historical,
        inactive_with_candles_count=1,
        delisted_symbol_count=1,
        delisted_with_candles_count=1,
        includes_delisted=True,
        fallback_only=False,
        benchmark=benchmark,
        daily_history_ready=True,
        promotion_ready=True,
        historical_evidence_ready=historical,
        daily_history_blockers=(),
        blockers=(),
        historical_evidence_blockers=historical_blockers,
        unresolved_evidence=historical_blockers,
        reasons=historical_blockers,
    )


def _readiness(*, track: str = HISTORICAL_PIT_TRACK) -> DailyCandlesReadiness:
    markets = (_ready_market("kr", track=track), _ready_market("us", track=track))
    historical_blockers = tuple(
        code for market in markets for code in market.historical_evidence_blockers
    )
    return DailyCandlesReadiness(
        as_of=_START + timedelta(days=329),
        required_history_bars=252,
        markets=markets,
        daily_history_ready=True,
        promotion_ready=True,
        historical_evidence_ready=not historical_blockers,
        daily_history_blockers=(),
        blockers=(),
        historical_evidence_blockers=historical_blockers,
        unresolved_evidence=historical_blockers,
        reasons=historical_blockers,
        evidence_track=track,
    )


def _thresholds(*, track: str = HISTORICAL_PIT_TRACK) -> dict[str, object]:
    """트랙별로 실제 적용되는 임계 스냅샷. 손으로 다시 만들지 않는다."""

    return _thresholds_snapshot(promotion_thresholds_for_track(track))


def test_promotion_metrics_require_every_ready_market_benchmark() -> None:
    bars = _bars(count=330)
    candidate = _candidate()
    universe = UniverseEvidence(
        source="daily_candles_readiness",
        point_in_time_membership=True,
        includes_delisted=True,
        as_of=bars[-1].timestamp,
    )
    diagnostics = run_portfolio_diagnostics(
        (candidate,),
        {candidate.key: bars},
        config=_config(),
        benchmark_bars_by_market={"US": _benchmark(bars)},
        universe_evidence=universe,
    )
    walk = run_walk_forward(
        (candidate,),
        {candidate.key: bars},
        config=_config(),
        walk_forward=WalkForwardConfig(train_bars=260, test_bars=20, step_bars=20),
        benchmark_bars_by_market={"US": _benchmark(bars)},
        universe_evidence=universe,
    )

    with pytest.raises(PromotionEvidenceBuildError, match="benchmark_market_mismatch"):
        derive_promotion_metrics(
            diagnostics,
            walk,
            _readiness(),
            evidence_track=HISTORICAL_PIT_TRACK,
        )


def _stored_evidence_payload(
    *, track: str = HISTORICAL_PIT_TRACK
) -> tuple[
    dict[str, object],
    PromotionMetrics,
    PortfolioEvidenceSource,
    StrategyArtifactManifest,
]:
    bars = _bars(count=330)
    kr_bars = _bars(count=330, scale=Decimal("10"))
    candidate = _candidate()
    kr_candidate = _candidate("005930", "KR")
    candidates = (candidate, kr_candidate)
    bars_by_candidate = {
        candidate.key: bars,
        kr_candidate.key: kr_bars,
    }
    benchmarks = {
        "US": _benchmark(bars),
        "KR": _benchmark(kr_bars),
    }
    candidate_benchmarks = {
        candidate.key: CandidateBenchmarkSeries("SPY", benchmarks["US"]),
        kr_candidate.key: CandidateBenchmarkSeries("KOSPI", benchmarks["KR"]),
    }
    config = _config()
    universe = UniverseEvidence(
        source="daily_candles_readiness",
        point_in_time_membership=track == HISTORICAL_PIT_TRACK,
        includes_delisted=True,
        as_of=bars[-1].timestamp,
    )
    diagnostics = run_portfolio_diagnostics(
        candidates,
        bars_by_candidate,
        config=config,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        universe_evidence=universe,
    )
    walk_config = WalkForwardConfig(train_bars=260, test_bars=20, step_bars=20)
    walk = run_walk_forward(
        candidates,
        bars_by_candidate,
        config=config,
        walk_forward=walk_config,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        universe_evidence=universe,
    )
    readiness = _readiness(track=track)
    signal_start_at = _START if track == FORWARD_PAPER_TRACK else None
    metrics = derive_promotion_metrics(
        diagnostics,
        walk,
        readiness,
        evidence_track=track,
        signal_start_at=signal_start_at,
    )
    source = PortfolioEvidenceSource(
        evidence_track=track,
        as_of=readiness.as_of,
        readiness=readiness,
        candidates=candidates,
        bars_by_candidate=bars_by_candidate,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        selected_universe=(
            {
                "market": "KR",
                "symbol": "005930",
                "cohortId": "kr-cohort",
                "cohortMethod": "latest_market_cap",
                "cohortSelectionDate": "2024-01-02",
                "cohortEffectiveDate": "2024-01-03",
                "cohortEvidenceScope": track,
                "memberRank": 1,
                "memberKind": "active",
                "marketCap": "1000000",
                "isActive": True,
                "listingStatus": "listed",
                "exchange": "KOSPI",
                "benchmarkSymbol": "KOSPI",
                "loadedBarCount": 330,
                "sources": ["kis"],
            },
            {
                "market": "US",
                "symbol": "ALPHA",
                "cohortId": "us-cohort",
                "cohortMethod": "latest_market_cap",
                "cohortSelectionDate": "2024-01-02",
                "cohortEffectiveDate": "2024-01-03",
                "cohortEvidenceScope": track,
                "memberRank": 1,
                "memberKind": "active",
                "marketCap": "1000000",
                "isActive": True,
                "listingStatus": "listed",
                "loadedBarCount": 330,
                "exchange": "NASDAQ",
                "benchmarkSymbol": "SPY",
                "sources": ["kis"],
            },
        ),
        dataset_content_hash="d" * 64,
        period_start=bars[0].timestamp,
        period_end=bars[-1].timestamp,
        signal_start_at=signal_start_at,
    )
    artifact = StrategyArtifactManifest(
        schema_version="kasset.strategy-artifact.v1",
        strategy_key=config.strategy_key,
        strategy_version=config.strategy_version,
        fingerprint="a" * 64,
        source_commit="b" * 40,
        code_files=(),
        effective_config={
            "strategyRegistry": {},
            "candidateRanker": {},
            "portfolioBacktest": {},
            "walkForward": {},
            "positionSizer": {},
            "positionManager": {},
            "regimeWeights": {},
        },
    )
    raw = build_promotion_raw_payload(
        artifact=artifact,
        source=source,
        config=config,
        walk_config=walk_config,
        diagnostics=diagnostics,
        walk_forward=walk,
        metrics=metrics,
        thresholds=_thresholds(track=track),
    )
    assert raw["schemaVersion"] == PROMOTION_EVIDENCE_SCHEMA_VERSION
    assert raw["evidenceTrack"] == track
    return raw, metrics, source, artifact


@pytest.fixture(scope="module")
def historical_stored_evidence() -> tuple[dict[str, object], PromotionMetrics]:
    raw, metrics, _source, _artifact = _stored_evidence_payload()
    return raw, metrics


@pytest.fixture(scope="module")
def forward_stored_evidence() -> tuple[dict[str, object], PromotionMetrics]:
    raw, metrics, _source, _artifact = _stored_evidence_payload(
        track=FORWARD_PAPER_TRACK
    )
    return raw, metrics


def test_stored_portfolio_result_derives_exact_promotion_metrics(
    historical_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, expected = copy.deepcopy(historical_stored_evidence)
    diagnostics = raw["portfolioDiagnostics"]
    stored = raw["derivedPromotionMetrics"]
    thresholds = raw["promotionThresholds"]
    assert isinstance(diagnostics, dict)
    assert isinstance(stored, dict)
    assert isinstance(thresholds, dict)
    baseline = diagnostics["baseline"]
    cost_stress = diagnostics["costStress"]
    assert isinstance(baseline, dict)
    assert isinstance(cost_stress, list)

    assert Decimal(str(stored["grossProfit"])) == Decimal(str(baseline["grossProfit"]))
    assert Decimal(str(stored["grossLoss"])) == Decimal(str(baseline["grossLoss"]))
    expected_profit_factor = expected.profit_factor
    stored_profit_factor = stored["profitFactor"]
    assert (
        Decimal(str(stored_profit_factor)) == expected_profit_factor
        if expected_profit_factor is not None
        else stored_profit_factor is None
    )
    expected_total_costs = Decimal(str(baseline["feesPaid"])) + Decimal(
        str(baseline["slippageCost"])
    )
    assert Decimal(str(stored["totalCosts"])) == expected_total_costs
    cost_stressed_returns: list[Decimal] = []
    for item in cost_stress:
        assert isinstance(item, dict)
        cost_stressed_returns.append(Decimal(str(item["totalReturn"])))
    assert Decimal(str(stored["costStressedTotalReturn"])) == min(cost_stressed_returns)
    assert thresholds["minProfitFactor"] == str(
        DEFAULT_PROMOTION_THRESHOLDS.min_profit_factor
    )
    assert thresholds["minCostStressedTotalReturn"] == str(
        DEFAULT_PROMOTION_THRESHOLDS.min_cost_stressed_total_return
    )

    derived = derive_metrics_from_stored_payload(raw)

    assert derived.as_snapshot() == expected.as_snapshot()


def test_stored_payload_rejects_selected_symbol_outside_eligible_partition(
    forward_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, _ = copy.deepcopy(forward_stored_evidence)
    data = raw["data"]
    assert isinstance(data, dict)
    eligible = data["eligibleSymbols"]
    assert isinstance(eligible, dict)
    eligible["us"] = [f"US-{index}" for index in range(10)]

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="us:selected_universe_not_eligible",
    ):
        derive_metrics_from_stored_payload(raw)


def test_stored_payload_rejects_eligibility_count_mismatch(
    forward_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, _ = copy.deepcopy(forward_stored_evidence)
    data = raw["data"]
    assert isinstance(data, dict)
    eligible = data["eligibleSymbols"]
    assert isinstance(eligible, dict)
    eligible["us"] = ["ALPHA"]

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="us:eligible_symbols_count_mismatch",
    ):
        derive_metrics_from_stored_payload(raw)


def test_forward_track_payload_replays_without_historical_proof(
    forward_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    """forward 코호트 근거는 PIT/상장폐지 증명 없이도 그대로 재현된다."""

    raw, expected = copy.deepcopy(forward_stored_evidence)

    assert raw["evidenceTrack"] == FORWARD_PAPER_TRACK
    validation = raw["validation"]
    assert isinstance(validation, dict)
    assert validation["pointInTimeProven"] is False
    readiness = raw["readiness"]
    assert isinstance(readiness, dict)
    assert readiness["historicalEvidenceReady"] is False
    assert readiness["unresolvedEvidence"]
    assert expected.survivorship_evidence is False

    derived = derive_metrics_from_stored_payload(raw)

    assert derived.as_snapshot() == expected.as_snapshot()
    assert derived.survivorship_evidence is False


def test_stored_thresholds_must_match_the_declared_track(
    historical_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    """느슨한 임계 스냅샷을 historical 트랙 근거에 실어 보낼 수 없다."""

    raw, _metrics = historical_stored_evidence
    tampered = copy.deepcopy(raw)
    tampered["promotionThresholds"] = _thresholds(track=FORWARD_PAPER_TRACK)

    with pytest.raises(
        PromotionEvidenceBuildError, match="promotion_thresholds_track_mismatch"
    ):
        derive_metrics_from_stored_payload(tampered)


def test_forward_payload_cannot_claim_historical_thresholds(
    forward_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    """반대 방향도 막는다: 트랙과 임계 프로필은 항상 한 쌍이다."""

    raw, _metrics = forward_stored_evidence
    tampered = copy.deepcopy(raw)
    tampered["promotionThresholds"] = _thresholds(track=HISTORICAL_PIT_TRACK)

    with pytest.raises(
        PromotionEvidenceBuildError, match="promotion_thresholds_track_mismatch"
    ):
        derive_metrics_from_stored_payload(tampered)


def test_stored_payload_records_execution_and_cost_contract() -> None:
    raw, _metrics, _source, _artifact = _stored_evidence_payload()
    diagnostics = raw["portfolioDiagnostics"]
    assert diagnostics["config"]["entryFill"] == "next_open"
    assert diagnostics["config"]["slippageMode"] == "adverse_rate"
    expected_cost = {
        "feeRate": "0.001",
        "slippageRate": "0.0005",
        "sellTaxRate": "0",
        "minFeeAbsolute": "0",
    }
    assert diagnostics["costSlippage"]["KR"] == expected_cost
    assert diagnostics["costSlippage"]["US"] == expected_cost
    assert diagnostics["baseline"]["taxesPaid"] == "0E-8"


def test_legacy_stored_payload_defaults_missing_execution_contract_fields() -> None:
    raw, expected, _source, _artifact = _stored_evidence_payload()
    legacy = copy.deepcopy(raw)
    config = legacy["portfolioDiagnostics"]["config"]
    config.pop("entryFill")
    config.pop("slippageMode")

    derived = derive_metrics_from_stored_payload(legacy)

    assert derived.as_snapshot() == expected.as_snapshot()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("entryFill", "signal_close", "promotion_entry_fill_invalid"),
        ("slippageMode", "none", "promotion_slippage_mode_invalid"),
    ],
)
def test_stored_evidence_rejects_non_promotion_execution_contract(
    field: str,
    value: str,
    reason: str,
) -> None:
    raw, _metrics, _source, _artifact = _stored_evidence_payload()
    tampered = copy.deepcopy(raw)
    tampered["portfolioDiagnostics"]["config"][field] = value

    with pytest.raises(PromotionEvidenceBuildError, match=reason):
        derive_metrics_from_stored_payload(tampered)


def test_sell_tax_rate_partitions_experiment_identity() -> None:
    raw, _metrics, source, artifact = _stored_evidence_payload()
    changed = copy.deepcopy(raw)
    changed["portfolioDiagnostics"]["costSlippage"]["KR"]["sellTaxRate"] = "0.001"
    base_identity = _experiment_identity(
        artifact=artifact,
        source=source,
        raw_payload=raw,
        thresholds=_thresholds(),
    )
    changed_identity = _experiment_identity(
        artifact=artifact,
        source=source,
        raw_payload=changed,
        thresholds=_thresholds(),
    )
    base_hashes = compute_identity_hashes(base_identity.components())
    changed_hashes = compute_identity_hashes(changed_identity.components())

    assert base_hashes["cost_hash"] != changed_hashes["cost_hash"]
    assert derive_experiment_id(
        base_identity.strategy_key,
        base_identity.strategy_version,
        base_hashes,
    ) != derive_experiment_id(
        changed_identity.strategy_key,
        changed_identity.strategy_version,
        changed_hashes,
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("data", "eligible252Counts", "kr"), 0),
        (("validation", "fallbackOnly"), True),
        (("validation", "pointInTimeProven"), False),
        (("validation", "delistedIncluded"), False),
        (("validation", "benchmarkProven"), False),
        (("data", "cohorts", "us", "evidenceScope"), "forward_paper"),
        (("benchmarks", "us", "status"), "unavailable"),
        (("portfolioDiagnostics", "baseline", "benchmarkMarkets"), ["KR"]),
        (("portfolioDiagnostics", "baseline", "grossProfit"), None),
        (("portfolioDiagnostics", "baseline", "grossLoss"), None),
        (("portfolioDiagnostics", "baseline", "feesPaid"), None),
        (("benchmarks", "us", "fallbackOnly"), True),
        (("strategy", "sourceCommit"), "not-a-commit"),
        (("data", "selectedUniverse"), []),
        (("portfolioDiagnostics", "symbolRemoval"), []),
        (("walkForward", "folds"), []),
        (("evidenceTrack",), "forward_paper"),
        (("evidenceTrack",), "paper_live"),
        (("evidenceTrack",), None),
        (("readiness", "unresolvedEvidence"), ["us:fallback_only"]),
        (("readiness", "historicalEvidenceReady"), False),
        (("validation", "corporateActionLedgerProven"), False),
    ],
)
def test_stored_evidence_fails_closed_when_required_proof_is_missing(
    path: tuple[str, ...],
    value: object,
    historical_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, _metrics = historical_stored_evidence
    tampered = copy.deepcopy(raw)
    target = tampered
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(PromotionEvidenceBuildError):
        derive_metrics_from_stored_payload(tampered)


def test_stored_evidence_fails_closed_when_benchmark_window_is_invalid(
    historical_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, _metrics = historical_stored_evidence
    tampered = copy.deepcopy(raw)
    baseline = tampered["portfolioDiagnostics"]["baseline"]  # type: ignore[index]
    first_window = baseline["benchmarkWindows"][0]  # type: ignore[index]
    first_window["startAt"] = baseline["recordEndAt"]  # type: ignore[index]

    with pytest.raises(PromotionEvidenceBuildError, match="benchmark_window_mismatch"):
        derive_metrics_from_stored_payload(tampered)


def test_benchmark_windows_allow_unsynchronized_kr_us_sessions() -> None:
    _require_stored_benchmark_window_coverage(
        {
            "recordStartAt": "2026-08-31T00:00:00+00:00",
            "recordEndAt": "2026-09-01T00:00:00+00:00",
            "benchmarkMarkets": ["KR", "US"],
            "benchmarkWindows": [
                {
                    "market": "KR",
                    "startAt": "2026-08-31T00:00:00+00:00",
                    "endAt": "2026-09-01T00:00:00+00:00",
                },
                {
                    "market": "US",
                    "startAt": "2026-08-31T13:30:00+00:00",
                    "endAt": "2026-08-31T20:00:00+00:00",
                },
            ],
        },
        {"kr": 1, "us": 1},
    )


def test_stored_evidence_fails_closed_when_fold_benchmark_market_is_missing(
    historical_stored_evidence: tuple[dict[str, object], PromotionMetrics],
) -> None:
    raw, _metrics = historical_stored_evidence
    tampered = copy.deepcopy(raw)
    folds = tampered["walkForward"]["folds"]  # type: ignore[index]
    fold_test = folds[0]["test"]  # type: ignore[index]
    fold_test["benchmarkMarkets"] = ["KR"]  # type: ignore[index]

    with pytest.raises(PromotionEvidenceBuildError, match="benchmark_market_mismatch"):
        derive_metrics_from_stored_payload(tampered)


def test_promotion_universe_query_is_cohort_scoped_and_rank_ordered() -> None:
    sql = str(_universe_query(_STORAGE["us"]))

    assert "FROM public.kasset_research_cohort_members AS m" in sql
    assert "WHERE m.cohort_id = :cohort_id" in sql
    assert "m.member_kind = 'active'" in sql
    assert "m.member_kind IN ('active', 'forced')" not in sql
    assert "m.rank" in sql
    assert "ORDER BY u.symbol" not in sql


def test_cohort_selection_uses_exact_member_rank_without_delisted_injection() -> None:
    rows = [
        {
            "symbol": f"RANK{rank}",
            "member_rank": rank,
            "member_kind": "active",
            "is_active": True,
        }
        for rank in (6, 3, 1, 5, 2, 4, 7)
    ]
    rows.append(
        {
            "symbol": "DELISTED",
            "member_rank": 99,
            "member_kind": "active",
            "is_active": False,
            "listing_status": "delisted",
        }
    )
    rows.append(
        {
            "symbol": "FORCED",
            "member_rank": 1,
            "member_kind": "forced",
            "is_active": True,
        }
    )

    selected = _select_universe_rows(
        rows,
        eligible_symbols=tuple(f"RANK{rank}" for rank in range(1, 7)),
    )

    assert [row["symbol"] for row in selected] == [
        "RANK1",
        "RANK2",
        "RANK3",
        "RANK4",
        "RANK5",
        "RANK6",
    ]
    assert all(row["symbol"] != "DELISTED" for row in selected)
    assert all(row["symbol"] != "FORCED" for row in selected)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("cohortMethod", "different-method"),
        ("memberRank", 2),
        ("cohortEffectiveDate", "2024-02-01"),
    ],
)
def test_dataset_hash_binds_active_core_rank_and_effective_date(
    field: str,
    replacement: object,
) -> None:
    candidate = _candidate()
    bars = _bars(count=2)
    selected = {
        "market": "US",
        "symbol": candidate.symbol,
        "cohortId": "us-cohort",
        "cohortMethod": "latest_market_cap",
        "cohortSelectionDate": "2024-01-02",
        "cohortEffectiveDate": "2024-01-03",
        "cohortEvidenceScope": "historical_pit",
        "memberRank": 1,
        "memberKind": "active",
    }
    original = _dataset_content_hash(
        candidates=(candidate,),
        bars_by_candidate={candidate.key: bars},
        benchmarks={"US": _benchmark(bars)},
        selected_universe=(selected,),
    )
    changed = dict(selected)
    changed[field] = replacement

    modified = _dataset_content_hash(
        candidates=(candidate,),
        bars_by_candidate={candidate.key: bars},
        benchmarks={"US": _benchmark(bars)},
        selected_universe=(changed,),
    )

    assert modified != original


def test_dataset_hash_ignores_enclosing_cohort_id_for_same_active_core() -> None:
    candidate = _candidate()
    bars = _bars(count=2)
    selected = {
        "market": "US",
        "symbol": candidate.symbol,
        "cohortId": "cohort-before-watchlist-change",
        "cohortMethod": "latest_market_cap",
        "cohortSelectionDate": "2024-01-02",
        "cohortEffectiveDate": "2024-01-03",
        "cohortEvidenceScope": "historical_pit",
        "memberRank": 1,
        "memberKind": "active",
    }
    before = _dataset_content_hash(
        candidates=(candidate,),
        bars_by_candidate={candidate.key: bars},
        benchmarks={"US": _benchmark(bars)},
        selected_universe=(selected,),
    )
    changed_cohort = dict(selected)
    changed_cohort["cohortId"] = "cohort-after-watchlist-change"

    after = _dataset_content_hash(
        candidates=(candidate,),
        bars_by_candidate={candidate.key: bars},
        benchmarks={"US": _benchmark(bars)},
        selected_universe=(changed_cohort,),
    )

    assert after == before


def test_require_readiness_rejects_current_forward_historical_use() -> None:
    readiness = _readiness()
    us = readiness.for_market("us")
    assert us.cohort is not None
    current_forward = replace(
        us,
        cohort=replace(us.cohort, evidence_scope="forward_paper"),
        point_in_time_available=True,
        promotion_ready=True,
        blockers=(),
    )
    tampered = replace(
        readiness,
        markets=(readiness.for_market("kr"), current_forward),
        promotion_ready=True,
        blockers=(),
    )

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="us:cohort_not_historical_pit",
    ):
        _require_readiness(tampered, evidence_track="historical_pit")


def test_require_readiness_forward_track_accepts_a_forward_cohort() -> None:
    """A forward cohort must reach candidate creation on the forward track."""

    readiness = _readiness()
    forward_markets = tuple(
        replace(
            item,
            cohort=replace(
                item.cohort,  # type: ignore[arg-type]
                evidence_scope="forward_paper",
            ),
            point_in_time_available=False,
            includes_delisted=False,
            fallback_only=False,
            corporate_action_status="unknown",
            historical_evidence_ready=False,
            historical_evidence_blockers=(
                f"{item.market}:cohort_not_historical_pit",
                f"{item.market}:point_in_time_unavailable",
                f"{item.market}:delisted_members_absent",
                f"{item.market}:corporate_action_unknown",
                f"{item.market}:fallback_only",
            ),
            unresolved_evidence=(f"{item.market}:cohort_not_historical_pit",),
        )
        for item in readiness.markets
    )
    forward = replace(
        readiness,
        markets=forward_markets,
        historical_evidence_ready=False,
        historical_evidence_blockers=tuple(
            code
            for item in forward_markets
            for code in item.historical_evidence_blockers
        ),
        unresolved_evidence=tuple(
            code for item in forward_markets for code in item.unresolved_evidence
        ),
        evidence_track="forward_paper",
    )

    _require_readiness(
        forward,
        evidence_track=FORWARD_PAPER_TRACK,
        signal_start=date(2024, 1, 3),
    )

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="cohort_not_historical_pit",
    ):
        _require_readiness(
            replace(forward, evidence_track="historical_pit"),
            evidence_track="historical_pit",
        )


def test_forward_track_accepts_partial_eligible_cohort_but_historical_rejects() -> None:
    def _partial_readiness(readiness: DailyCandlesReadiness) -> DailyCandlesReadiness:
        kr = readiness.for_market("kr")
        partial_kr = replace(
            kr,
            eligible_symbol_count=9,
            eligible_symbols=kr.eligible_symbols[:-1],
            excluded_symbols=(
                SymbolReadinessExclusion(
                    symbol="KR-8",
                    reasons=("insufficient_history",),
                ),
            ),
            price_adjustment_status="incomplete",
            adjustment_covered_symbol_count=9,
        )
        return replace(
            readiness,
            markets=(partial_kr, readiness.for_market("us")),
        )

    forward = _partial_readiness(_readiness(track=FORWARD_PAPER_TRACK))
    _require_readiness(
        forward,
        evidence_track=FORWARD_PAPER_TRACK,
        signal_start=date(2024, 1, 3),
    )

    historical = _partial_readiness(_readiness(track=HISTORICAL_PIT_TRACK))
    with pytest.raises(
        PromotionEvidenceBuildError,
        match="kr:cohort_members_not_ready",
    ):
        _require_readiness(historical, evidence_track=HISTORICAL_PIT_TRACK)


def test_require_readiness_forward_track_rejects_the_wrong_cohort_scope() -> None:
    readiness = _readiness()

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="kr:cohort_scope_mismatch",
    ):
        _require_readiness(
            replace(readiness, evidence_track="forward_paper"),
            evidence_track="forward_paper",
        )


def test_require_readiness_rejects_a_whole_market_unevidenced_session() -> None:
    readiness = _readiness()
    kr = readiness.for_market("kr")
    unevidenced = replace(
        kr,
        unevidenced_session_count=1,
        unevidenced_sessions=(date(2026, 8, 31),),
    )
    tampered = replace(
        readiness,
        markets=(unevidenced, readiness.for_market("us")),
    )

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="kr:calendar_session_unevidenced",
    ):
        _require_readiness(tampered, evidence_track="historical_pit")


def test_require_readiness_rejects_an_unknown_track() -> None:
    with pytest.raises(PromotionEvidenceBuildError, match="evidence_track_invalid"):
        derive_promotion_metrics(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            _readiness(),
            evidence_track="paper_live",
        )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"list_date_covered_symbol_count": 9},
            "us:list_date_coverage_incomplete",
        ),
        (
            {"members_listed_after_cohort_start": 1},
            "us:member_listed_after_cohort_start",
        ),
        (
            {"delist_date_covered_inactive_count": 0},
            "us:delist_date_coverage_incomplete",
        ),
        (
            {"includes_delisted": False},
            "us:delisted_members_absent",
        ),
    ],
)
def test_require_readiness_rejects_missing_constituent_lifecycle_evidence(
    overrides: dict[str, object],
    expected: str,
) -> None:
    readiness = _readiness()
    tampered_market = replace(readiness.for_market("us"), **overrides)
    tampered = replace(
        readiness,
        markets=(readiness.for_market("kr"), tampered_market),
    )

    with pytest.raises(PromotionEvidenceBuildError, match=expected):
        _require_readiness(tampered, evidence_track="historical_pit")
