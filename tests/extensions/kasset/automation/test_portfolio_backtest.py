from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

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
from app.extensions.kasset.automation.promotion_evidence import (
    _STORAGE,
    PortfolioEvidenceSource,
    PromotionEvidenceBuildError,
    _dataset_content_hash,
    _require_readiness,
    _select_universe_rows,
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


def _ready_market(market: str) -> MarketReadiness:
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
        evidence_scope="historical_pit",
    )
    return MarketReadiness(
        market=market,  # type: ignore[arg-type]
        cohort=cohort,
        evaluated_window_start=date(2024, 2, 1),
        evaluated_window_end=date(2025, 1, 1),
        total_symbol_count=10,
        cohort_active_member_count=10,
        forced_member_count=0,
        benchmark_member_count=1,
        active_symbol_count=10,
        inactive_symbol_count=0,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=10,
        eligible_symbol_count=10,
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=0,
        calendar_status="available",
        corporate_action_status="clear",
        corporate_action_covered_symbol_count=10,
        adjustment_covered_symbol_count=10,
        list_date_covered_symbol_count=10,
        delist_date_covered_inactive_count=0,
        point_in_time_available=True,
        inactive_with_candles_count=0,
        delisted_symbol_count=0,
        delisted_with_candles_count=0,
        includes_delisted=False,
        fallback_only=False,
        benchmark=benchmark,
        daily_history_ready=True,
        promotion_ready=True,
        daily_history_blockers=(),
        blockers=(),
        reasons=(),
    )


def _readiness() -> DailyCandlesReadiness:
    return DailyCandlesReadiness(
        as_of=_START + timedelta(days=329),
        required_history_bars=252,
        markets=(_ready_market("kr"), _ready_market("us")),
        daily_history_ready=True,
        promotion_ready=True,
        daily_history_blockers=(),
        blockers=(),
        reasons=(),
    )


def _thresholds() -> dict[str, object]:
    value = DEFAULT_PROMOTION_THRESHOLDS
    return {
        "minTotalReturn": str(value.min_total_return),
        "maxDrawdown": str(value.max_drawdown),
        "minWinRate": str(value.min_win_rate),
        "minExpectancy": str(value.min_expectancy),
        "minExcessReturn": str(value.min_excess_return),
        "minTradeCount": value.min_trade_count,
        "minWalkForwardFolds": value.min_walk_forward_folds,
        "minWalkForwardPassRate": str(value.min_walk_forward_pass_rate),
        "requireDataQualityEvidence": value.require_data_quality_evidence,
        "requireSurvivorshipEvidence": value.require_survivorship_evidence,
        "requireDeterministic": value.require_deterministic,
    }


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
        derive_promotion_metrics(diagnostics, walk, _readiness())


def _stored_evidence_payload() -> tuple[dict[str, object], object]:
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
    config = _config()
    universe = UniverseEvidence(
        source="daily_candles_readiness",
        point_in_time_membership=True,
        includes_delisted=True,
        as_of=bars[-1].timestamp,
    )
    diagnostics = run_portfolio_diagnostics(
        candidates,
        bars_by_candidate,
        config=config,
        benchmark_bars_by_market=benchmarks,
        universe_evidence=universe,
    )
    walk_config = WalkForwardConfig(train_bars=260, test_bars=20, step_bars=20)
    walk = run_walk_forward(
        candidates,
        bars_by_candidate,
        config=config,
        walk_forward=walk_config,
        benchmark_bars_by_market=benchmarks,
        universe_evidence=universe,
    )
    readiness = _readiness()
    metrics = derive_promotion_metrics(diagnostics, walk, readiness)
    source = PortfolioEvidenceSource(
        as_of=readiness.as_of,
        readiness=readiness,
        candidates=candidates,
        bars_by_candidate=bars_by_candidate,
        benchmark_bars_by_market=benchmarks,
        selected_universe=(
            {
                "market": "KR",
                "symbol": "005930",
                "cohortId": "kr-cohort",
                "cohortMethod": "latest_market_cap",
                "cohortSelectionDate": "2024-01-02",
                "cohortEffectiveDate": "2024-01-03",
                "cohortEvidenceScope": "historical_pit",
                "memberRank": 1,
                "memberKind": "active",
                "marketCap": "1000000",
                "isActive": True,
                "listingStatus": "listed",
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
                "cohortEvidenceScope": "historical_pit",
                "memberRank": 1,
                "memberKind": "active",
                "marketCap": "1000000",
                "isActive": True,
                "listingStatus": "listed",
                "loadedBarCount": 330,
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
        thresholds=_thresholds(),
    )
    assert raw["schemaVersion"] == PROMOTION_EVIDENCE_SCHEMA_VERSION
    return raw, metrics


def test_stored_portfolio_result_derives_exact_promotion_metrics() -> None:
    raw, expected = _stored_evidence_payload()

    derived = derive_metrics_from_stored_payload(raw)

    assert derived.as_snapshot() == expected.as_snapshot()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("data", "eligible252Counts", "kr"), 0),
        (("validation", "fallbackOnly"), True),
        (("validation", "pointInTimeProven"), False),
        (("validation", "benchmarkProven"), False),
        (("data", "cohorts", "us", "evidenceScope"), "forward_paper"),
        (("benchmarks", "us", "status"), "unavailable"),
        (("portfolioDiagnostics", "baseline", "benchmarkMarkets"), ["KR"]),
        (("benchmarks", "us", "fallbackOnly"), True),
        (("strategy", "sourceCommit"), "not-a-commit"),
        (("data", "selectedUniverse"), []),
        (("portfolioDiagnostics", "symbolRemoval"), []),
        (("walkForward", "folds"), []),
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
    assert "m.member_kind IN ('active', 'forced')" in sql
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


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("cohortId", "different-cohort"),
        ("cohortMethod", "different-method"),
        ("memberRank", 2),
        ("cohortEffectiveDate", "2024-02-01"),
    ],
)
def test_dataset_hash_binds_cohort_identity_rank_and_effective_date(
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
        _require_readiness(tampered)
