from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import Action, PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    CandidateBenchmarkSeries,
    MarketExecutionCost,
    PortfolioBacktestConfig,
    SignalStatus,
    UniverseEvidence,
    WalkForwardConfig,
    run_portfolio_backtest,
    run_portfolio_diagnostics,
    run_walk_forward,
)
from app.extensions.kasset.automation.promotion_evidence import (
    _STORAGE,
    PortfolioEvidenceSource,
    PromotionEvidenceBuildError,
    _dataset_content_hash,
    _require_readiness,
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
            track=HISTORICAL_PIT_TRACK,
        )


def _stored_evidence_payload(
    *, track: str = HISTORICAL_PIT_TRACK
) -> tuple[dict[str, object], PromotionMetrics]:
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
    metrics = derive_promotion_metrics(diagnostics, walk, readiness, track=track)
    source = PortfolioEvidenceSource(
        track=track,  # type: ignore[arg-type]
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
    )
    artifact = StrategyArtifactManifest(
        schema_version="kasset.strategy-artifact.v1",
        strategy_key=config.strategy_key,
        strategy_version=config.strategy_version,
        fingerprint="a" * 64,
        source_commit="b" * 40,
        code_files=(),
        effective_config={},
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
    assert raw["promotionTrack"] == track
    return raw, metrics


def test_stored_portfolio_result_derives_exact_promotion_metrics() -> None:
    raw, expected = _stored_evidence_payload()
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


def test_forward_track_payload_replays_without_historical_proof() -> None:
    """forward 코호트 근거는 PIT/상장폐지 증명 없이도 그대로 재현된다."""

    raw, expected = _stored_evidence_payload(track=FORWARD_PAPER_TRACK)

    assert raw["promotionTrack"] == FORWARD_PAPER_TRACK
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


def test_stored_thresholds_must_match_the_declared_track() -> None:
    """느슨한 임계 스냅샷을 historical 트랙 근거에 실어 보낼 수 없다."""

    raw, _metrics = _stored_evidence_payload()
    tampered = copy.deepcopy(raw)
    tampered["promotionThresholds"] = _thresholds(track=FORWARD_PAPER_TRACK)

    with pytest.raises(
        PromotionEvidenceBuildError, match="promotion_thresholds_track_mismatch"
    ):
        derive_metrics_from_stored_payload(tampered)


def test_forward_payload_cannot_claim_historical_thresholds() -> None:
    """반대 방향도 막는다: 트랙과 임계 프로필은 항상 한 쌍이다."""

    raw, _metrics = _stored_evidence_payload(track=FORWARD_PAPER_TRACK)
    tampered = copy.deepcopy(raw)
    tampered["promotionThresholds"] = _thresholds(track=HISTORICAL_PIT_TRACK)

    with pytest.raises(
        PromotionEvidenceBuildError, match="promotion_thresholds_track_mismatch"
    ):
        derive_metrics_from_stored_payload(tampered)


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
        (("promotionTrack",), "forward_paper"),
        (("promotionTrack",), "paper_live"),
        (("promotionTrack",), None),
        (("readiness", "unresolvedEvidence"), ["us:fallback_only"]),
        (("readiness", "historicalEvidenceReady"), False),
        (("validation", "corporateActionLedgerProven"), False),
    ],
)
def test_stored_evidence_fails_closed_when_required_proof_is_missing(
    path: tuple[str, ...],
    value: object,
) -> None:
    raw, _metrics = _stored_evidence_payload()
    tampered = copy.deepcopy(raw)
    target = tampered
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(PromotionEvidenceBuildError):
        derive_metrics_from_stored_payload(tampered)


def test_stored_evidence_fails_closed_when_benchmark_window_is_short() -> None:
    raw, _metrics = _stored_evidence_payload()
    tampered = copy.deepcopy(raw)
    baseline = tampered["portfolioDiagnostics"]["baseline"]  # type: ignore[index]
    first_window = baseline["benchmarkWindows"][0]  # type: ignore[index]
    first_window["startAt"] = baseline["recordEndAt"]  # type: ignore[index]

    with pytest.raises(PromotionEvidenceBuildError, match="benchmark_window_mismatch"):
        derive_metrics_from_stored_payload(tampered)


def test_stored_evidence_fails_closed_when_fold_benchmark_market_is_missing() -> None:
    raw, _metrics = _stored_evidence_payload()
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

    selected = _select_universe_rows(rows)

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
        _require_readiness(tampered, track="historical_pit")


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
            fallback_only=True,
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
    )

    _require_readiness(forward, track="forward_paper")

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="cohort_not_historical_pit",
    ):
        _require_readiness(forward, track="historical_pit")


def test_require_readiness_forward_track_rejects_the_wrong_cohort_scope() -> None:
    readiness = _readiness()

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="kr:cohort_not_forward_paper",
    ):
        _require_readiness(readiness, track="forward_paper")


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
        _require_readiness(tampered, track="historical_pit")


def test_require_readiness_rejects_an_unknown_track() -> None:
    with pytest.raises(PromotionEvidenceBuildError, match="promotion_track_invalid"):
        derive_promotion_metrics(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            _readiness(),
            track="paper_live",
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
        _require_readiness(tampered, track="historical_pit")
