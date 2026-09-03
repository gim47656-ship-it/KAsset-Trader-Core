"""PAPER 승격 증거 트랙(historical_pit / forward_paper) 계약.

기본 호출은 오늘과 동일하게 historical_pit을 요구하고, forward_paper 트랙은
코호트 effective_date 이후에만 시그널을 허용하며 PIT survivorship을 증명하지
않는다는 사실을 증거에 그대로 남긴다.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    BacktestWindow,
    CandidateBenchmarkSeries,
    MarketExecutionCost,
    PortfolioBacktestConfig,
    UniverseEvidence,
    WalkForwardConfig,
    run_portfolio_diagnostics,
    run_walk_forward,
)
from app.extensions.kasset.automation.promotion_evidence import (
    PROMOTION_EVIDENCE_TRACKS,
    PortfolioEvidenceSource,
    PromotionEvidenceBuildError,
    _forward_signal_start_at,
    _forward_walk_forward_bars,
    _require_readiness,
    build_promotion_raw_payload,
    derive_metrics_from_stored_payload,
    derive_promotion_metrics,
)
from app.extensions.kasset.automation.strategy_artifact import (
    StrategyArtifactManifest,
)
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PROMOTION_THRESHOLDS,
    evaluate_thresholds,
    promotion_thresholds_for_track,
)
from app.services.daily_candles.readiness import (
    HISTORICAL_PIT_ONLY_BLOCKERS,
    BenchmarkCoverage,
    CohortEvidence,
    DailyCandlesReadiness,
    MarketReadiness,
)

_START = datetime(2025, 1, 1, tzinfo=UTC)
_BAR_COUNT = 400
# forward 코호트 확정일: 적재 봉 index 300의 세션 날짜. 그 앞 300개 봉은 warm-up.
_FORWARD_INDEX = 300
_FORWARD_EFFECTIVE_DATE = (_START + timedelta(days=_FORWARD_INDEX)).date()
_WALK = WalkForwardConfig(train_bars=260, test_bars=20, step_bars=20)


def _candidate(symbol: str = "ALPHA", market: str = "US") -> CandidateMetadata:
    return CandidateMetadata(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        sources=("kis",),
    )


def _bars(
    *, count: int = _BAR_COUNT, scale: Decimal = Decimal("1")
) -> tuple[PriceBar, ...]:
    output: list[PriceBar] = []
    previous = Decimal("100") * scale
    for index in range(count):
        close = (Decimal("100") + Decimal(index) / Decimal("100")) * scale
        output.append(
            PriceBar(
                timestamp=_START + timedelta(days=index),
                open=previous,
                high=max(previous, close) + scale,
                low=min(previous, close) - scale,
                close=close,
                volume=Decimal("1000000"),
            )
        )
        previous = close
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


def _config() -> PortfolioBacktestConfig:
    cost = MarketExecutionCost(Decimal("0.001"), Decimal("0.0005"))
    return PortfolioBacktestConfig(
        initial_cash=Decimal("100000"),
        max_positions=1,
        candidate_top_n=1,
        risk_per_trade_rate=Decimal("0.02"),
        max_symbol_allocation=Decimal("0.50"),
        kr_cost=cost,
        us_cost=cost,
    )


def _market(market: str, *, track: str) -> MarketReadiness:
    forward = track == "forward_paper"
    historical_blockers = (
        ()
        if not forward
        else tuple(f"{market}:{code}" for code in HISTORICAL_PIT_ONLY_BLOCKERS)
    )
    benchmark = BenchmarkCoverage(
        market=market,  # type: ignore[arg-type]
        symbol="KOSPI" if market == "kr" else "SPY",
        start=_START,
        end=_START + timedelta(days=_BAR_COUNT - 1),
        count=_BAR_COUNT,
        source="kis",
        sources=("kis",),
        status="available",
    )
    cohort = CohortEvidence(
        cohort_id=f"{market}-cohort",
        market=market,  # type: ignore[arg-type]
        selection_as_of=datetime(2024, 1, 2, tzinfo=UTC),
        selection_date=date(2024, 1, 2),
        effective_date=_FORWARD_EFFECTIVE_DATE if forward else date(2024, 1, 3),
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
        # 현재 universe 기반 forward 코호트: readiness window는 확정일보다 앞선다.
        evaluated_window_start=date(2025, 2, 1),
        evaluated_window_end=date(2026, 1, 1),
        latest_completed_session=(_START + timedelta(days=_BAR_COUNT - 1)).date(),
        ingest_lag_session_count=0,
        unevidenced_session_count=0,
        unevidenced_sessions=(),
        total_symbol_count=10,
        cohort_active_member_count=10,
        forced_member_count=0,
        benchmark_member_count=1,
        active_symbol_count=10 if forward else 9,
        inactive_symbol_count=0 if forward else 1,
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
        list_date_covered_symbol_count=7 if forward else 10,
        members_listed_after_cohort_start=0,
        delist_date_covered_inactive_count=0 if forward else 1,
        point_in_time_available=not forward,
        inactive_with_candles_count=0 if forward else 1,
        delisted_symbol_count=0 if forward else 1,
        delisted_with_candles_count=0 if forward else 1,
        includes_delisted=not forward,
        fallback_only=False,
        benchmark=benchmark,
        daily_history_ready=True,
        promotion_ready=True,
        historical_evidence_ready=not forward,
        daily_history_blockers=(),
        blockers=(),
        historical_evidence_blockers=historical_blockers,
        unresolved_evidence=historical_blockers,
        reasons=(),
    )


def _readiness(track: str = "historical_pit") -> DailyCandlesReadiness:
    markets = (_market("kr", track=track), _market("us", track=track))
    historical_blockers = tuple(
        code for market in markets for code in market.historical_evidence_blockers
    )
    return DailyCandlesReadiness(
        as_of=_START + timedelta(days=_BAR_COUNT - 1),
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


def _thresholds(track: str) -> dict[str, object]:
    value = promotion_thresholds_for_track(track)
    return {
        "minTotalReturn": str(value.min_total_return),
        "maxDrawdown": str(value.max_drawdown),
        "minWinRate": str(value.min_win_rate),
        "minExpectancy": str(value.min_expectancy),
        "minExcessReturn": str(value.min_excess_return),
        "minProfitFactor": str(value.min_profit_factor),
        "minCostStressedTotalReturn": str(value.min_cost_stressed_total_return),
        "minTradeCount": value.min_trade_count,
        "minWalkForwardFolds": value.min_walk_forward_folds,
        "minWalkForwardPassRate": str(value.min_walk_forward_pass_rate),
        "requireDataQualityEvidence": value.require_data_quality_evidence,
        "requireSurvivorshipEvidence": value.require_survivorship_evidence,
        "requireDeterministic": value.require_deterministic,
    }


def _forward_signal_start() -> datetime:
    return datetime.combine(_FORWARD_EFFECTIVE_DATE, datetime.min.time(), tzinfo=UTC)


# --------------------------------------------------------------------------
# 기본 동작 불변
# --------------------------------------------------------------------------


def test_track_vocabulary_is_shared_and_default_thresholds_are_pit() -> None:
    assert PROMOTION_EVIDENCE_TRACKS == ("historical_pit", "forward_paper")
    assert (
        promotion_thresholds_for_track("historical_pit") == DEFAULT_PROMOTION_THRESHOLDS
    )


def test_default_call_still_rejects_forward_paper_cohort() -> None:
    """evidence_track 미지정 호출은 오늘과 같은 사유로 forward 코호트를 거절한다."""
    readiness = _readiness()
    us = readiness.for_market("us")
    assert us.cohort is not None
    tampered = replace(
        readiness,
        markets=(
            readiness.for_market("kr"),
            replace(us, cohort=replace(us.cohort, evidence_scope="forward_paper")),
        ),
    )

    with pytest.raises(
        PromotionEvidenceBuildError, match="us:cohort_not_historical_pit"
    ):
        _require_readiness(tampered)
    with pytest.raises(
        PromotionEvidenceBuildError, match="us:cohort_not_historical_pit"
    ):
        _require_readiness(tampered, period_start=date(2025, 1, 1))


def test_default_call_rejects_readiness_measured_under_forward_track() -> None:
    forward = _readiness("forward_paper")

    with pytest.raises(PromotionEvidenceBuildError, match="evidence_track_mismatch"):
        _require_readiness(forward)
    with pytest.raises(PromotionEvidenceBuildError, match="evidence_track_invalid"):
        _require_readiness(forward, evidence_track="bogus")


# --------------------------------------------------------------------------
# forward_paper readiness 계약
# --------------------------------------------------------------------------


def test_forward_track_accepts_signal_start_on_or_after_effective_date() -> None:
    readiness = _readiness("forward_paper")

    _require_readiness(
        readiness,
        evidence_track="forward_paper",
        signal_start=_FORWARD_EFFECTIVE_DATE,
    )
    _require_readiness(
        readiness,
        evidence_track="forward_paper",
        period_start=date(2025, 1, 1),  # warm-up 봉은 확정일 이전이어도 된다.
        signal_start=_FORWARD_EFFECTIVE_DATE + timedelta(days=5),
    )


@pytest.mark.parametrize(
    "signal_start",
    [None, _FORWARD_EFFECTIVE_DATE - timedelta(days=1), date(2025, 1, 1)],
)
def test_forward_track_rejects_signal_start_before_effective_date(
    signal_start: date | None,
) -> None:
    readiness = _readiness("forward_paper")

    with pytest.raises(
        PromotionEvidenceBuildError,
        match="kr:forward_window_predates_effective_date",
    ):
        _require_readiness(
            readiness, evidence_track="forward_paper", signal_start=signal_start
        )


def test_forward_track_rejects_historical_pit_cohort_scope() -> None:
    readiness = _readiness("forward_paper")
    kr = readiness.for_market("kr")
    assert kr.cohort is not None
    tampered = replace(
        readiness,
        markets=(
            replace(kr, cohort=replace(kr.cohort, evidence_scope="historical_pit")),
            readiness.for_market("us"),
        ),
    )

    with pytest.raises(PromotionEvidenceBuildError, match="kr:cohort_scope_mismatch"):
        _require_readiness(
            tampered,
            evidence_track="forward_paper",
            signal_start=_FORWARD_EFFECTIVE_DATE,
        )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"total_symbol_count": 0, "eligible_symbol_count": 0},
            "us:cohort_members_not_ready",
        ),
        ({"fallback_only": True}, "us:fallback_only"),
        ({"cohort": None}, "us:cohort_not_found"),
    ],
)
def test_forward_track_keeps_common_market_guards(
    overrides: dict[str, object],
    expected: str,
) -> None:
    readiness = _readiness("forward_paper")
    tampered = replace(
        readiness,
        markets=(
            readiness.for_market("kr"),
            replace(readiness.for_market("us"), **overrides),
        ),
    )

    with pytest.raises(PromotionEvidenceBuildError, match=expected):
        _require_readiness(
            tampered,
            evidence_track="forward_paper",
            signal_start=_FORWARD_EFFECTIVE_DATE,
        )


def test_forward_track_keeps_benchmark_and_readiness_flag_guards() -> None:
    readiness = _readiness("forward_paper")
    us = readiness.for_market("us")
    fallback_benchmark = replace(
        readiness,
        markets=(
            readiness.for_market("kr"),
            replace(us, benchmark=replace(us.benchmark, sources=("yahoo",))),
        ),
    )
    blocked = replace(readiness, promotion_ready=False, blockers=("us:custom_block",))
    history = replace(readiness, daily_history_ready=False)

    with pytest.raises(PromotionEvidenceBuildError, match="us:benchmark_fallback_only"):
        _require_readiness(
            fallback_benchmark,
            evidence_track="forward_paper",
            signal_start=_FORWARD_EFFECTIVE_DATE,
        )
    with pytest.raises(PromotionEvidenceBuildError, match="us:custom_block"):
        _require_readiness(
            blocked,
            evidence_track="forward_paper",
            signal_start=_FORWARD_EFFECTIVE_DATE,
        )
    with pytest.raises(PromotionEvidenceBuildError, match="daily_history_not_ready"):
        _require_readiness(
            history,
            evidence_track="forward_paper",
            signal_start=_FORWARD_EFFECTIVE_DATE,
        )


def test_forward_signal_start_is_the_later_effective_date_at_utc_midnight() -> None:
    readiness = _readiness("forward_paper")
    kr = readiness.for_market("kr")
    assert kr.cohort is not None
    later = replace(
        readiness,
        markets=(
            replace(
                kr,
                cohort=replace(
                    kr.cohort,
                    effective_date=_FORWARD_EFFECTIVE_DATE + timedelta(days=3),
                ),
            ),
            readiness.for_market("us"),
        ),
    )

    assert _forward_signal_start_at(readiness) == _forward_signal_start()
    assert _forward_signal_start_at(later) == _forward_signal_start() + timedelta(
        days=3
    )
    assert (
        _forward_signal_start_at(
            replace(
                readiness,
                markets=(replace(kr, cohort=None), readiness.for_market("us")),
            )
        )
        is None
    )


# --------------------------------------------------------------------------
# forward walk-forward 사전 슬라이스
# --------------------------------------------------------------------------


def test_forward_slice_puts_first_fold_signal_on_first_forward_bar() -> None:
    bars = _bars()
    candidate = _candidate()
    signal_start = _forward_signal_start()

    sliced = _forward_walk_forward_bars(
        {candidate.key: bars},
        signal_start_at=signal_start,
        walk_config=_WALK,
        min_folds=3,
    )

    # cut = timestamps[first_forward - (train_bars - 1)] = bars[300 - 259]
    assert sliced[candidate.key][0].timestamp == bars[_FORWARD_INDEX - 259].timestamp
    assert sliced[candidate.key][-1] == bars[-1]

    walk = run_walk_forward(
        (candidate,),
        sliced,
        config=_config(),
        walk_forward=_WALK,
        benchmark_bars_by_market={"US": _benchmark(bars)},
    )

    assert len(walk.folds) == 4
    assert walk.folds[0].train_end_at == signal_start
    assert walk.folds[0].train_start_at == bars[_FORWARD_INDEX - 259].timestamp
    assert all(fold.train_end_at >= signal_start for fold in walk.folds)


def test_forward_walk_forward_slice_fails_instead_of_lowering_fold_threshold() -> None:
    bars = _bars()
    candidate = _candidate()

    # index 380부터 forward: warm-up 259개를 남기면 279개 봉 -> fold 0개.
    with pytest.raises(
        PromotionEvidenceBuildError,
        match=r"forward_window_insufficient_bars:folds=0,min_folds=3",
    ):
        _forward_walk_forward_bars(
            {candidate.key: bars},
            signal_start_at=bars[380].timestamp,
            walk_config=_WALK,
            min_folds=3,
        )
    # index 340부터 forward: 319개 봉 -> fold 2개 (< 3).
    with pytest.raises(
        PromotionEvidenceBuildError,
        match=r"forward_window_insufficient_bars:folds=2,min_folds=3",
    ):
        _forward_walk_forward_bars(
            {candidate.key: bars},
            signal_start_at=bars[340].timestamp,
            walk_config=_WALK,
            min_folds=3,
        )
    with pytest.raises(PromotionEvidenceBuildError, match="forward_window_empty"):
        _forward_walk_forward_bars(
            {candidate.key: bars},
            signal_start_at=bars[-1].timestamp + timedelta(days=1),
            walk_config=_WALK,
            min_folds=3,
        )


# --------------------------------------------------------------------------
# forward 증거 생성 -> 저장 payload 검증 round trip
# --------------------------------------------------------------------------


def _forward_engine_results():
    bars = _bars()
    kr_bars = _bars(scale=Decimal("10"))
    candidate = _candidate()
    kr_candidate = _candidate("005930", "KR")
    candidates = (candidate, kr_candidate)
    bars_by_candidate = {candidate.key: bars, kr_candidate.key: kr_bars}
    benchmarks = {"US": _benchmark(bars), "KR": _benchmark(kr_bars)}
    candidate_benchmarks = {
        candidate.key: CandidateBenchmarkSeries("SPY", benchmarks["US"]),
        kr_candidate.key: CandidateBenchmarkSeries("KOSPI", benchmarks["KR"]),
    }
    signal_start = _forward_signal_start()
    universe = UniverseEvidence(
        source="durable_research_cohort",
        point_in_time_membership=False,
        includes_delisted=False,
        as_of=bars[-1].timestamp,
    )
    config = _config()
    diagnostics = run_portfolio_diagnostics(
        candidates,
        bars_by_candidate,
        config=config,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        universe_evidence=universe,
        window=BacktestWindow(signal_start_at=signal_start, end_at=bars[-1].timestamp),
    )
    walk = run_walk_forward(
        candidates,
        _forward_walk_forward_bars(
            bars_by_candidate,
            signal_start_at=signal_start,
            walk_config=_WALK,
            min_folds=3,
        ),
        config=config,
        walk_forward=_WALK,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        universe_evidence=universe,
    )
    return (
        candidates,
        bars_by_candidate,
        benchmarks,
        candidate_benchmarks,
        config,
        diagnostics,
        walk,
        signal_start,
    )


def _selected_row(market: str, symbol: str, benchmark_symbol: str) -> dict[str, object]:
    return {
        "market": market,
        "symbol": symbol,
        "cohortId": f"{market.lower()}-cohort",
        "cohortMethod": "latest_market_cap",
        "cohortSelectionDate": "2024-01-02",
        "cohortEffectiveDate": _FORWARD_EFFECTIVE_DATE.isoformat(),
        "cohortEvidenceScope": "forward_paper",
        "memberRank": 1,
        "memberKind": "active",
        "marketCap": "1000000",
        "isActive": True,
        "listingStatus": "listed",
        "exchange": "KOSPI" if market == "KR" else "NASDAQ",
        "benchmarkSymbol": benchmark_symbol,
        "loadedBarCount": _BAR_COUNT,
        "sources": ["kis"],
    }


def _forward_payload() -> tuple[dict[str, object], object]:
    (
        candidates,
        bars_by_candidate,
        benchmarks,
        candidate_benchmarks,
        config,
        diagnostics,
        walk,
        signal_start,
    ) = _forward_engine_results()
    readiness = _readiness("forward_paper")
    metrics = derive_promotion_metrics(
        diagnostics,
        walk,
        readiness,
        evidence_track="forward_paper",
        signal_start_at=signal_start,
    )
    bars = bars_by_candidate[candidates[0].key]
    source = PortfolioEvidenceSource(
        as_of=readiness.as_of,
        readiness=readiness,
        candidates=candidates,
        bars_by_candidate=bars_by_candidate,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        selected_universe=(
            _selected_row("KR", "005930", "KOSPI"),
            _selected_row("US", "ALPHA", "SPY"),
        ),
        dataset_content_hash="d" * 64,
        period_start=bars[0].timestamp,
        period_end=bars[-1].timestamp,
        evidence_track="forward_paper",
        signal_start_at=signal_start,
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
        walk_config=_WALK,
        diagnostics=diagnostics,
        walk_forward=walk,
        metrics=metrics,
        thresholds=_thresholds("forward_paper"),
    )
    return raw, metrics


def test_forward_metrics_lack_survivorship_and_pass_only_forward_thresholds() -> None:
    _raw, metrics = _forward_payload()

    assert metrics.survivorship_evidence is False
    assert metrics.data_quality_evidence is True
    default_checks = {
        check.metric: check.passed
        for check in evaluate_thresholds(metrics, DEFAULT_PROMOTION_THRESHOLDS).checks
    }
    forward_checks = {
        check.metric: check.passed
        for check in evaluate_thresholds(
            metrics, promotion_thresholds_for_track("forward_paper")
        ).checks
    }
    assert default_checks["survivorship_evidence"] is False
    assert forward_checks["survivorship_evidence"] is True
    # 성과 게이트 판정은 두 임계값에서 완전히 같다.
    performance = set(default_checks) - {"survivorship_evidence"}
    assert {k: default_checks[k] for k in performance} == {
        k: forward_checks[k] for k in performance
    }


def test_forward_payload_records_track_and_round_trips_stored_validation() -> None:
    raw, metrics = _forward_payload()

    assert raw["evidenceTrack"] == "forward_paper"
    validation = raw["validation"]
    assert validation["evidenceTrack"] == "forward_paper"
    assert validation["pointInTimeProven"] is False
    assert validation["delistedIncluded"] is False
    assert validation["historicalPitChecksWaived"] == list(HISTORICAL_PIT_ONLY_BLOCKERS)
    assert validation["forwardSignalStartAt"] == (
        _forward_signal_start().isoformat().replace("+00:00", "Z")
    )
    assert raw["derivedPromotionMetrics"]["survivorshipEvidence"] is False
    assert raw["promotionThresholds"]["requireSurvivorshipEvidence"] is False
    assert "tradeBootstrap" in raw
    assert "tradeBootstrap" not in raw["determinism"]
    assert set(raw["determinism"]) == {
        "datasetContentHash",
        "diagnosticsHash",
        "baselineHash",
        "walkForwardHash",
        "foldTestHashes",
    }
    baseline = raw["portfolioDiagnostics"]["baseline"]
    assert baseline["recordStartAt"] == validation["forwardSignalStartAt"]
    folds = raw["walkForward"]["folds"]
    assert folds
    assert all(fold["train"]["warmupOnly"] is True for fold in folds)

    derived = derive_metrics_from_stored_payload(raw)

    assert derived.as_snapshot() == metrics.as_snapshot()


def _rewind_record_start(summary: dict[str, object], stamp: str) -> None:
    summary["recordStartAt"] = stamp
    for window in summary["benchmarkWindows"]:  # type: ignore[union-attr]
        window["startAt"] = stamp  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda raw: raw.pop("evidenceTrack"),
            "promotion_thresholds_track_mismatch",
        ),
        # 트랙 필드를 모두 지우면 legacy historical_pit으로 해석되고 forward
        # 임계 스냅샷과 먼저 불일치해 fail-closed한다.
        (
            lambda raw: (
                raw.pop("evidenceTrack"),
                raw["validation"].pop("evidenceTrack"),
                raw["validation"].pop("forwardSignalStartAt"),
                raw["validation"].pop("historicalPitChecksWaived"),
            ),
            "promotion_thresholds_track_mismatch",
        ),
        (
            lambda raw: raw["data"]["cohorts"]["kr"].__setitem__(
                "evidenceScope", "historical_pit"
            ),
            "kr:cohort_identity_invalid",
        ),
        (
            lambda raw: raw["validation"].__setitem__("historicalPitChecksWaived", []),
            "required_validation_evidence_missing",
        ),
        (
            lambda raw: raw["validation"].__setitem__(
                "forwardSignalStartAt", "2025-01-01T00:00:00Z"
            ),
            "kr:forward_window_predates_effective_date",
        ),
        (
            lambda raw: raw["walkForward"]["folds"][0].__setitem__(
                "trainEndAt", "2025-01-01T00:00:00Z"
            ),
            "forward_window_predates_effective_date",
        ),
        # benchmark window도 같이 앞당겨 기존 coverage 검사를 통과시킨 뒤 forward
        # 가드만 남긴다.
        (
            lambda raw: _rewind_record_start(
                raw["portfolioDiagnostics"]["baseline"], "2025-01-01T00:00:00Z"
            ),
            "forward_window_predates_effective_date",
        ),
        (
            lambda raw: _rewind_record_start(
                raw["portfolioDiagnostics"]["oneBarDelay"]["result"],
                "2025-01-01T00:00:00Z",
            ),
            "forward_window_predates_effective_date",
        ),
        (
            lambda raw: raw["derivedPromotionMetrics"].__setitem__(
                "survivorshipEvidence", True
            ),
            "derived_metrics_mismatch",
        ),
    ],
)
def test_stored_forward_evidence_fails_closed_when_track_proof_is_tampered(
    mutate,
    expected: str,
) -> None:
    raw, _metrics = _forward_payload()
    tampered = copy.deepcopy(raw)
    mutate(tampered)

    with pytest.raises(PromotionEvidenceBuildError, match=expected):
        derive_metrics_from_stored_payload(tampered)


def test_forward_metrics_reject_engine_signals_before_effective_date() -> None:
    (
        candidates,
        bars_by_candidate,
        benchmarks,
        candidate_benchmarks,
        config,
        _diagnostics,
        walk,
        signal_start,
    ) = _forward_engine_results()
    unwindowed = run_portfolio_diagnostics(
        candidates,
        bars_by_candidate,
        config=config,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
    )
    unsliced_walk = run_walk_forward(
        candidates,
        bars_by_candidate,
        config=config,
        walk_forward=_WALK,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
    )
    readiness = _readiness("forward_paper")

    with pytest.raises(
        PromotionEvidenceBuildError, match="forward_window_predates_effective_date"
    ):
        derive_promotion_metrics(
            unwindowed,
            walk,
            readiness,
            evidence_track="forward_paper",
            signal_start_at=signal_start,
        )
    with pytest.raises(
        PromotionEvidenceBuildError, match="forward_signal_start_missing"
    ):
        derive_promotion_metrics(
            unwindowed, unsliced_walk, readiness, evidence_track="forward_paper"
        )


def test_historical_payload_without_track_fields_still_validates_as_pit() -> None:
    """트랙 필드가 없던 기존 저장 payload는 historical_pit으로 계속 검증된다."""
    bars = _bars()
    kr_bars = _bars(scale=Decimal("10"))
    candidate = _candidate()
    kr_candidate = _candidate("005930", "KR")
    candidates = (candidate, kr_candidate)
    bars_by_candidate = {candidate.key: bars, kr_candidate.key: kr_bars}
    benchmarks = {"US": _benchmark(bars), "KR": _benchmark(kr_bars)}
    candidate_benchmarks = {
        candidate.key: CandidateBenchmarkSeries("SPY", benchmarks["US"]),
        kr_candidate.key: CandidateBenchmarkSeries("KOSPI", benchmarks["KR"]),
    }
    config = _config()
    universe = UniverseEvidence(
        source="durable_research_cohort",
        point_in_time_membership=True,
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
    walk = run_walk_forward(
        candidates,
        bars_by_candidate,
        config=config,
        walk_forward=_WALK,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        universe_evidence=universe,
    )
    readiness = _readiness()
    metrics = derive_promotion_metrics(diagnostics, walk, readiness)
    assert metrics.survivorship_evidence is True
    rows = (
        {
            **_selected_row("KR", "005930", "KOSPI"),
            "cohortEffectiveDate": "2024-01-03",
            "cohortEvidenceScope": "historical_pit",
        },
        {
            **_selected_row("US", "ALPHA", "SPY"),
            "cohortEffectiveDate": "2024-01-03",
            "cohortEvidenceScope": "historical_pit",
        },
    )
    source = PortfolioEvidenceSource(
        as_of=readiness.as_of,
        readiness=readiness,
        candidates=candidates,
        bars_by_candidate=bars_by_candidate,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        selected_universe=rows,
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
        walk_config=_WALK,
        diagnostics=diagnostics,
        walk_forward=walk,
        metrics=metrics,
        thresholds=_thresholds("historical_pit"),
    )
    assert raw["evidenceTrack"] == "historical_pit"
    assert raw["validation"]["historicalPitChecksWaived"] == []
    assert raw["validation"]["forwardSignalStartAt"] is None

    legacy = copy.deepcopy(raw)
    legacy.pop("evidenceTrack")
    for key in ("evidenceTrack", "forwardSignalStartAt", "historicalPitChecksWaived"):
        legacy["validation"].pop(key)

    expected = metrics.as_snapshot()
    assert derive_metrics_from_stored_payload(legacy).as_snapshot() == expected
    assert derive_metrics_from_stored_payload(raw).as_snapshot() == expected

    relabeled = copy.deepcopy(raw)
    relabeled["evidenceTrack"] = "forward_paper"
    relabeled["validation"]["evidenceTrack"] = "forward_paper"
    with pytest.raises(PromotionEvidenceBuildError):
        derive_metrics_from_stored_payload(relabeled)
