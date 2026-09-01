"""Build immutable KAsset portfolio evidence from DB-backed daily candles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException
from typing import Any, cast

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.benchmark_relative_strength import (
    benchmark_symbol_for_exchange,
)
from app.extensions.kasset.automation.candidate_ranker import (
    CandidateKey,
    CandidateMetadata,
)
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    CandidateBenchmarkSeries,
    PortfolioBacktestConfig,
    PortfolioBacktestDiagnostics,
    PortfolioBacktestResult,
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardResult,
    run_portfolio_diagnostics,
    run_walk_forward,
)
from app.extensions.kasset.automation.strategy_artifact import (
    BACKTEST_CANDIDATES_PER_MARKET,
    BACKTEST_HISTORY_BARS,
    PROMOTION_EVIDENCE_SCHEMA_VERSION,
    StrategyArtifactManifest,
    current_strategy_artifact,
)
from app.extensions.kasset.automation.strategy_promotion import (
    FORWARD_PAPER_TRACK,
    HISTORICAL_PIT_TRACK,
    PROMOTION_TRACKS,
    PromotionMetrics,
    PromotionThresholds,
    PromotionTrack,
    evaluate_thresholds,
    promotion_thresholds_for_track,
)
from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchPromotionCandidate,
    ResearchStrategyExperiment,
)
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    PromotionLinkRequest,
    StrategyExperimentIdentity,
)
from app.services.daily_candles.readiness import (
    REQUIRED_BENCHMARK_BARS,
    DailyCandlesReadiness,
    DailyCandlesReadinessService,
    MarketReadiness,
)
from app.services.research_canonical_hash import canonical_sha256
from app.services.strategy_experiment_registry import (
    link_promotion_candidate,
    record_trial,
    register_experiment,
)

_FALLBACK_SOURCES = frozenset({"toss", "toss_fallback", "yahoo", "yahoo_fallback"})
_HEX64 = frozenset("0123456789abcdef")

#: 트랙 어휘와 임계 프로필은 ``strategy_promotion``이 유일한 출처다. 여기서는
#: 근거 payload가 선언한 트랙을 읽어 그 프로필로만 평가한다.


class PromotionEvidenceBuildError(ValueError):
    """The DB-backed source cannot produce promotion-grade evidence."""


@dataclass(frozen=True, slots=True)
class PortfolioEvidenceSource:
    track: PromotionTrack
    as_of: datetime
    readiness: DailyCandlesReadiness
    candidates: tuple[CandidateMetadata, ...]
    bars_by_candidate: Mapping[CandidateKey, tuple[PriceBar, ...]]
    benchmark_bars_by_market: Mapping[str, tuple[PriceBar, ...]]
    benchmark_bars_by_candidate: Mapping[CandidateKey, CandidateBenchmarkSeries]
    selected_universe: tuple[Mapping[str, object], ...]
    dataset_content_hash: str
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True, slots=True)
class PromotionEvidenceBuildResult:
    experiment: ResearchStrategyExperiment
    run: ResearchBacktestRun
    candidate: ResearchPromotionCandidate
    metrics: PromotionMetrics
    raw_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Storage:
    universe_table: str
    candle_table: str
    name_expression: str
    candidate_market: str


_STORAGE: dict[str, _Storage] = {
    "kr": _Storage(
        universe_table="kr_symbol_universe",
        candle_table="kr_candles_1d",
        name_expression="u.name",
        candidate_market="KR",
    ),
    "us": _Storage(
        universe_table="us_symbol_universe",
        candle_table="us_candles_1d",
        name_expression="COALESCE(NULLIF(u.name_kr, ''), NULLIF(u.name_en, ''))",
        candidate_market="US",
    ),
}


def _universe_query(storage: _Storage):
    return text(
        f"""
        SELECT
            m.symbol,
            m.rank AS member_rank,
            m.member_kind,
            m.market_cap,
            cohort.cohort_id,
            cohort.selection_method AS cohort_method,
            cohort.selection_date AS cohort_selection_date,
            cohort.effective_date AS cohort_effective_date,
            cohort.evidence_scope AS cohort_evidence_scope,
            {storage.name_expression} AS name,
            u.is_active,
            u.listing_status,
            u.list_date,
            u.delist_date,
            u.exchange,
            COUNT(DISTINCT candles.time) FILTER (
                WHERE candles.time <= :as_of
            ) AS bar_count,
            STRING_AGG(
                DISTINCT candles.source, ',' ORDER BY candles.source
            ) FILTER (WHERE candles.time <= :as_of) AS sources
        FROM public.kasset_research_cohort_members AS m
        JOIN public.kasset_research_cohorts AS cohort
          ON cohort.cohort_id = m.cohort_id
        LEFT JOIN public.{storage.universe_table} AS u ON u.symbol = m.symbol
        LEFT JOIN public.{storage.candle_table} AS candles
          ON candles.symbol = m.symbol
        WHERE m.cohort_id = :cohort_id
          AND m.member_kind = 'active'
        GROUP BY
            m.symbol,
            m.rank,
            m.member_kind,
            m.market_cap,
            cohort.cohort_id,
            cohort.selection_method,
            cohort.selection_date,
            cohort.effective_date,
            cohort.evidence_scope,
            {storage.name_expression},
            u.is_active,
            u.listing_status,
            u.list_date,
            u.delist_date,
            u.exchange
        ORDER BY
            m.rank,
            CASE m.member_kind WHEN 'active' THEN 0 ELSE 1 END,
            m.symbol
        """
    )


def _candle_query(storage: _Storage):
    return text(
        f"""
        SELECT symbol, time, open, high, low, close, volume, source
        FROM (
            SELECT
                symbol,
                time,
                open,
                high,
                low,
                close,
                volume,
                source,
                ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY time DESC) AS row_number
            FROM public.{storage.candle_table}
            WHERE symbol IN :symbols AND time <= :as_of
        ) AS bounded
        WHERE row_number <= :history_bars
        ORDER BY symbol, time
        """
    ).bindparams(bindparam("symbols", expanding=True))


def _benchmark_query(storage: _Storage):
    return text(
        f"""
        SELECT time, open, high, low, close, volume, source
        FROM public.{storage.candle_table}
        WHERE symbol = :symbol AND time <= :as_of
        ORDER BY time DESC
        LIMIT :history_bars
        """
    )


async def load_portfolio_evidence_source(
    db: AsyncSession,
    *,
    as_of: datetime | None = None,
    cohort_ids: Mapping[str, str] | None = None,
    track: str = FORWARD_PAPER_TRACK,
) -> PortfolioEvidenceSource:
    """Read readiness denominator and exact rank-bounded active-core evidence."""

    resolved_track = _require_track(track)
    measured_at = _aware_utc(as_of or datetime.now(UTC))
    readiness = await DailyCandlesReadinessService(db).measure(
        as_of=measured_at,
        cohort_ids=cast(Any, cohort_ids),
    )
    _require_readiness(readiness, track=resolved_track)

    candidates: list[CandidateMetadata] = []
    selected_evidence: list[Mapping[str, object]] = []
    bars_by_candidate: dict[CandidateKey, tuple[PriceBar, ...]] = {}
    benchmarks: dict[str, tuple[PriceBar, ...]] = {}
    candidate_benchmarks: dict[CandidateKey, CandidateBenchmarkSeries] = {}

    for market_name in ("kr", "us"):
        storage = _STORAGE[market_name]
        market_readiness = readiness.for_market(cast(Any, market_name))
        cohort = market_readiness.cohort
        if cohort is None:
            raise PromotionEvidenceBuildError(f"{market_name}:cohort_not_found")
        universe_result = await db.execute(
            _universe_query(storage),
            {"cohort_id": cohort.cohort_id, "as_of": measured_at},
        )
        universe_rows = tuple(
            cast(Sequence[Mapping[str, object]], universe_result.mappings().all())
        )
        if len(universe_rows) != cohort.active_member_count:
            raise PromotionEvidenceBuildError(
                f"{market_name}:active_core_query_mismatch"
            )
        selected = _select_universe_rows(universe_rows)
        if not selected:
            raise PromotionEvidenceBuildError(f"{market_name}:selected_universe_empty")

        symbols = [str(row["symbol"]).strip().upper() for row in selected]
        candle_result = await db.execute(
            _candle_query(storage),
            {
                "symbols": symbols,
                "as_of": measured_at,
                "history_bars": BACKTEST_HISTORY_BARS,
            },
        )
        candle_rows = cast(
            Sequence[Mapping[str, object]], candle_result.mappings().all()
        )
        grouped: dict[str, list[PriceBar]] = {symbol: [] for symbol in symbols}
        source_by_symbol: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        for row in candle_rows:
            symbol = str(row["symbol"]).strip().upper()
            if symbol not in grouped:
                raise PromotionEvidenceBuildError(
                    f"{market_name}:{symbol}:non_cohort_candle"
                )
            grouped[symbol].append(_price_bar(row))
            source = str(row.get("source") or "").strip()
            if source:
                source_by_symbol[symbol].add(source)

        benchmark_symbol_by_candidate: dict[CandidateKey, str] = {}
        for row in selected:
            symbol = str(row["symbol"]).strip().upper()
            bars = tuple(grouped[symbol])
            if not bars:
                raise PromotionEvidenceBuildError(
                    f"{market_name}:{symbol}:daily_candles_missing"
                )
            sources = tuple(sorted(source_by_symbol[symbol]))
            if not sources:
                raise PromotionEvidenceBuildError(
                    f"{market_name}:{symbol}:candle_source_missing"
                )
            exchange = str(row.get("exchange") or "").strip().upper()
            benchmark_symbol = (
                benchmark_symbol_for_exchange(exchange)
                if market_name == "kr"
                else "SPY"
            )
            if benchmark_symbol is None:
                raise PromotionEvidenceBuildError(
                    f"{market_name}:{symbol}:benchmark_mapping_missing"
                )
            metadata = CandidateMetadata(
                symbol=symbol,
                market=cast(Any, storage.candidate_market),
                sources=sources,
                name=(str(row.get("name") or "").strip() or None),
            )
            candidates.append(metadata)
            bars_by_candidate[metadata.key] = bars
            benchmark_symbol_by_candidate[metadata.key] = benchmark_symbol
            selected_evidence.append(
                {
                    "market": storage.candidate_market,
                    "symbol": symbol,
                    "cohortId": str(row["cohort_id"]),
                    "cohortMethod": str(row["cohort_method"]),
                    "cohortSelectionDate": _date_text(
                        cast(date, row["cohort_selection_date"])
                    ),
                    "cohortEffectiveDate": _date_text(
                        cast(date, row["cohort_effective_date"])
                    ),
                    "cohortEvidenceScope": str(row["cohort_evidence_scope"]),
                    "memberRank": int(row["member_rank"]),
                    "memberKind": str(row["member_kind"]),
                    "marketCap": str(row["market_cap"]),
                    "isActive": bool(row.get("is_active")),
                    "listingStatus": str(row.get("listing_status") or "") or None,
                    "listDate": _date_text(cast(date | None, row.get("list_date"))),
                    "delistDate": _date_text(cast(date | None, row.get("delist_date"))),
                    "exchange": exchange or None,
                    "benchmarkSymbol": benchmark_symbol,
                    "storedBarCount": int(row.get("bar_count") or 0),
                    "loadedBarCount": len(bars),
                    "sources": list(sources),
                }
            )

        benchmark_bars_by_symbol: dict[str, tuple[PriceBar, ...]] = {}
        required_benchmark_symbols = {
            market_readiness.benchmark.symbol,
            *benchmark_symbol_by_candidate.values(),
        }
        for benchmark_symbol in sorted(required_benchmark_symbols):
            benchmark_result = await db.execute(
                _benchmark_query(storage),
                {
                    "symbol": benchmark_symbol,
                    "as_of": measured_at,
                    "history_bars": BACKTEST_HISTORY_BARS,
                },
            )
            benchmark_rows = list(
                cast(
                    Sequence[Mapping[str, object]],
                    benchmark_result.mappings().all(),
                )
            )
            benchmark_rows.reverse()
            benchmark_bars = tuple(_price_bar(row) for row in benchmark_rows)
            if len(benchmark_bars) < REQUIRED_BENCHMARK_BARS:
                raise PromotionEvidenceBuildError(
                    f"{market_name}:{benchmark_symbol}:benchmark_window_insufficient"
                )
            benchmark_bars_by_symbol[benchmark_symbol] = benchmark_bars
        benchmarks[storage.candidate_market] = benchmark_bars_by_symbol[
            market_readiness.benchmark.symbol
        ]
        for candidate_key, benchmark_symbol in benchmark_symbol_by_candidate.items():
            candidate_benchmarks[candidate_key] = CandidateBenchmarkSeries(
                benchmark_symbol=benchmark_symbol,
                bars=benchmark_bars_by_symbol[benchmark_symbol],
            )

    all_timestamps = tuple(
        bar.timestamp for bars in bars_by_candidate.values() for bar in bars
    )
    if not all_timestamps:
        raise PromotionEvidenceBuildError("daily_candles_missing")
    period_start = min(all_timestamps)
    period_end = max(all_timestamps)
    _require_readiness(
        readiness,
        track=resolved_track,
        period_start=period_start.date(),
    )
    content_hash = _dataset_content_hash(
        candidates=tuple(candidates),
        bars_by_candidate=bars_by_candidate,
        benchmarks=benchmarks,
        candidate_benchmarks=candidate_benchmarks,
        selected_universe=tuple(selected_evidence),
    )
    return PortfolioEvidenceSource(
        track=resolved_track,
        as_of=measured_at,
        readiness=readiness,
        candidates=tuple(candidates),
        bars_by_candidate=bars_by_candidate,
        benchmark_bars_by_market=benchmarks,
        benchmark_bars_by_candidate=candidate_benchmarks,
        selected_universe=tuple(selected_evidence),
        dataset_content_hash=content_hash,
        period_start=period_start,
        period_end=period_end,
    )


async def build_and_store_portfolio_evidence(
    db: AsyncSession,
    *,
    as_of: datetime | None = None,
    cohort_ids: Mapping[str, str] | None = None,
    track: str = FORWARD_PAPER_TRACK,
) -> PromotionEvidenceBuildResult:
    """Run deterministic engines and persist experiment -> run -> candidate."""

    artifact = current_strategy_artifact()
    source = await load_portfolio_evidence_source(
        db,
        as_of=as_of,
        cohort_ids=cohort_ids,
        track=track,
    )
    config = PortfolioBacktestConfig(
        strategy_key=artifact.strategy_key,
        strategy_version=artifact.strategy_version,
    )
    walk_config = WalkForwardConfig()
    universe_evidence = _engine_universe_evidence(source)
    try:
        diagnostics = run_portfolio_diagnostics(
            source.candidates,
            source.bars_by_candidate,
            config=config,
            benchmark_bars_by_market=cast(Any, source.benchmark_bars_by_market),
            benchmark_bars_by_candidate=source.benchmark_bars_by_candidate,
            universe_evidence=universe_evidence,
        )
        walk_forward = run_walk_forward(
            source.candidates,
            source.bars_by_candidate,
            config=config,
            walk_forward=walk_config,
            benchmark_bars_by_market=cast(Any, source.benchmark_bars_by_market),
            benchmark_bars_by_candidate=source.benchmark_bars_by_candidate,
            universe_evidence=universe_evidence,
        )
    except (ArithmeticError, ValueError) as exc:
        raise PromotionEvidenceBuildError(
            f"backtest_evidence_unavailable:{exc}"
        ) from exc

    metrics = derive_promotion_metrics(
        diagnostics,
        walk_forward,
        source.readiness,
        track=source.track,
    )
    threshold_profile = promotion_thresholds_for_track(source.track)
    thresholds = _thresholds_snapshot(threshold_profile)
    evaluation = evaluate_thresholds(metrics, threshold_profile)
    raw_payload = build_promotion_raw_payload(
        artifact=artifact,
        source=source,
        config=config,
        walk_config=walk_config,
        diagnostics=diagnostics,
        walk_forward=walk_forward,
        metrics=metrics,
        thresholds=thresholds,
    )
    payload_hash = canonical_sha256(raw_payload)
    identity = _experiment_identity(
        artifact=artifact,
        source=source,
        raw_payload=raw_payload,
        thresholds=thresholds,
    )

    try:
        experiment = await register_experiment(db, identity)
        run = await record_trial(
            db,
            experiment_id=experiment.experiment_id,
            request=BacktestTrialRequest(
                status="completed",
                strategy_name=artifact.strategy_key,
                timeframe="1d",
                runner="kasset_portfolio_evidence_v1",
                seed=0,
                information_cutoff=source.as_of,
                gate_artifact_hash=artifact.fingerprint,
                idempotency_key=f"kasset-evidence:{payload_hash}",
                exchange="KR+US",
                market="equities",
                timerange=(
                    f"{_timestamp(source.period_start)}/{_timestamp(source.period_end)}"
                ),
                started_at=source.as_of,
                ended_at=source.as_of,
                total_trades=metrics.trade_count,
                max_drawdown=metrics.max_drawdown,
                win_rate=metrics.win_rate,
                expectancy=metrics.expectancy,
                total_return=metrics.total_return,
                artifact_hash=payload_hash,
                raw_payload=dict(raw_payload),
            ),
        )
        _verify_recorded_run(run, raw_payload, artifact)
        existing_candidate = await db.scalar(
            select(ResearchPromotionCandidate).where(
                ResearchPromotionCandidate.backtest_run_id == run.id
            )
        )
        status = "eligible" if evaluation.passed else "non_promotable"
        reason_code = (
            "thresholds_passed"
            if evaluation.passed
            else f"threshold_failed:{evaluation.failed_metrics[0]}"
        )
        if existing_candidate is None:
            candidate = await link_promotion_candidate(
                db,
                backtest_run_id=run.id,
                request=PromotionLinkRequest(
                    expected_experiment_id=experiment.experiment_id,
                    expected_config_hash=experiment.frozen_config_hash,
                    expected_data_hash=experiment.dataset_manifest_hash,
                    status=status,
                    reason_code=reason_code,
                    thresholds=thresholds,
                    metrics=metrics.as_snapshot(),
                ),
            )
        else:
            candidate = existing_candidate
            _verify_recorded_candidate(
                candidate,
                experiment=experiment,
                status=status,
                reason_code=reason_code,
                thresholds=thresholds,
                metrics=metrics,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return PromotionEvidenceBuildResult(
        experiment=experiment,
        run=run,
        candidate=candidate,
        metrics=metrics,
        raw_payload=raw_payload,
    )


def derive_promotion_metrics(
    diagnostics: PortfolioBacktestDiagnostics,
    walk_forward: WalkForwardResult,
    readiness: DailyCandlesReadiness,
    *,
    track: str = FORWARD_PAPER_TRACK,
) -> PromotionMetrics:
    """Derive the only accepted promotion metric snapshot from engine results."""

    resolved_track = _require_track(track)
    _require_readiness(readiness, track=resolved_track)
    baseline = diagnostics.baseline
    _require_benchmark_window_coverage(baseline, readiness)
    if baseline.excess_return is None:
        raise PromotionEvidenceBuildError("benchmark_excess_return_missing")
    folds = walk_forward.folds
    if not folds:
        raise PromotionEvidenceBuildError("walk_forward_folds_missing")
    for fold in folds:
        _require_benchmark_window_coverage(fold.test_result, readiness)
    hashes = (
        diagnostics.determinism_hash,
        baseline.determinism_hash,
        walk_forward.determinism_hash,
        *(fold.test_result.determinism_hash for fold in folds),
    )
    if any(not _is_hash(value) for value in hashes):
        raise PromotionEvidenceBuildError("determinism_hash_missing")
    gross_profit = sum(
        (trade.net_pnl for trade in baseline.trades if trade.net_pnl > 0),
        start=Decimal("0"),
    )
    gross_loss = -sum(
        (trade.net_pnl for trade in baseline.trades if trade.net_pnl < 0),
        start=Decimal("0"),
    )
    if not diagnostics.cost_stress:
        raise PromotionEvidenceBuildError("cost_stress_scenarios_missing")
    return PromotionMetrics(
        total_return=baseline.total_return,
        max_drawdown=baseline.max_drawdown,
        win_rate=baseline.win_rate,
        expectancy=baseline.expectancy,
        excess_return=baseline.excess_return,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        # 비용을 1x/2x/3x로 물린 시나리오 중 최악의 총수익률. 승격 조건이
        # "비용을 넉넉히 물려도 성과가 남는가"를 실제로 검사하게 한다.
        cost_stressed_total_return=min(
            item.total_return for item in diagnostics.cost_stress
        ),
        total_costs=baseline.fees_paid + baseline.slippage_cost,
        trade_count=baseline.trade_count,
        walk_forward_folds=len(folds),
        walk_forward_passed_folds=sum(_fold_passed(fold) for fold in folds),
        data_quality_evidence=readiness.daily_history_ready,
        # 생존 편향 없음은 point-in-time 멤버십과 상장폐지 멤버가 실제로 증명될
        # 때만 참이다. forward 코호트에서는 거짓이며, 그 사실을 그대로 지표에
        # 실어 승격 임계 검사가 스스로 막게 한다.
        survivorship_evidence=bool(
            readiness.markets
            and all(
                item.point_in_time_available and item.includes_delisted
                for item in readiness.markets
            )
        ),
        deterministic=True,
        backtest_hashes=hashes,
    )


def _require_benchmark_window_coverage(
    result: PortfolioBacktestResult,
    readiness: DailyCandlesReadiness,
) -> None:
    if not result.equity_curve:
        raise PromotionEvidenceBuildError("backtest_equity_curve_missing")
    expected_markets = {item.market.upper() for item in readiness.markets}
    actual_markets = tuple(item.market for item in result.benchmark_by_market)
    if (
        len(actual_markets) != len(set(actual_markets))
        or set(actual_markets) != expected_markets
    ):
        raise PromotionEvidenceBuildError("benchmark_market_mismatch")
    record_start = result.equity_curve[0].timestamp
    record_end = result.equity_curve[-1].timestamp
    if any(
        item.start_at > record_start or item.end_at < record_end
        for item in result.benchmark_by_market
    ):
        raise PromotionEvidenceBuildError("benchmark_window_mismatch")


def build_promotion_raw_payload(
    *,
    artifact: StrategyArtifactManifest,
    source: PortfolioEvidenceSource,
    config: PortfolioBacktestConfig,
    walk_config: WalkForwardConfig,
    diagnostics: PortfolioBacktestDiagnostics,
    walk_forward: WalkForwardResult,
    metrics: PromotionMetrics,
    thresholds: Mapping[str, object],
) -> dict[str, object]:
    markets = {
        item.market: _readiness_market_payload(item)
        for item in source.readiness.markets
    }
    cost_stress = [
        {
            "multiplier": item.multiplier,
            "totalReturn": str(item.total_return),
            "maxDrawdown": str(item.max_drawdown),
            "tradeCount": item.trade_count,
            "determinismHash": item.determinism_hash,
        }
        for item in diagnostics.cost_stress
    ]
    if tuple(item["multiplier"] for item in cost_stress) != (1, 2, 3):
        raise PromotionEvidenceBuildError("cost_stress_scenarios_incomplete")
    folds = [
        {
            "foldIndex": fold.fold_index,
            "trainStartAt": _timestamp(fold.train_start_at),
            "trainEndAt": _timestamp(fold.train_end_at),
            "testStartAt": _timestamp(fold.test_start_at),
            "testEndAt": _timestamp(fold.test_end_at),
            "passed": _fold_passed(fold),
            "train": _backtest_summary(fold.train_result),
            "test": _backtest_summary(fold.test_result),
        }
        for fold in walk_forward.folds
    ]
    return {
        "schemaVersion": PROMOTION_EVIDENCE_SCHEMA_VERSION,
        "promotionTrack": source.track,
        "strategy": {
            "key": artifact.strategy_key,
            "version": artifact.strategy_version,
            "artifactFingerprint": artifact.fingerprint,
            "artifactSchemaVersion": artifact.schema_version,
            "sourceCommit": artifact.source_commit,
        },
        "data": {
            "asOf": _timestamp(source.as_of),
            "period": {
                "startAt": _timestamp(source.period_start),
                "endAt": _timestamp(source.period_end),
            },
            "datasetContentHash": source.dataset_content_hash,
            "requiredHistoryBars": source.readiness.required_history_bars,
            "cohorts": {market: item["cohort"] for market, item in markets.items()},
            "universeCounts": {
                market: item["totalSymbolCount"] for market, item in markets.items()
            },
            "eligible252Counts": {
                market: item["eligibleSymbolCount"] for market, item in markets.items()
            },
            "selectedCounts": {
                market: sum(
                    entry["market"] == market.upper()
                    for entry in source.selected_universe
                )
                for market in ("kr", "us")
            },
            "selectedUniverse": [dict(item) for item in source.selected_universe],
        },
        "readiness": {
            "dailyHistoryReady": source.readiness.daily_history_ready,
            "promotionReady": source.readiness.promotion_ready,
            "historicalEvidenceReady": (source.readiness.historical_evidence_ready),
            "dailyHistoryBlockers": list(source.readiness.daily_history_blockers),
            "blockers": list(source.readiness.blockers),
            "historicalEvidenceBlockers": list(
                source.readiness.historical_evidence_blockers
            ),
            "unresolvedEvidence": list(source.readiness.unresolved_evidence),
            "reasons": list(source.readiness.reasons),
            "markets": markets,
        },
        "benchmarks": {
            market: dict(payload["benchmark"]) for market, payload in markets.items()
        },
        "validation": {
            "eligibleNonzero": source.readiness.eligible_symbol_count > 0,
            "fallbackOnly": any(
                item.fallback_only for item in source.readiness.markets
            ),
            "pointInTimeProven": all(
                item.point_in_time_available for item in source.readiness.markets
            ),
            "delistedIncluded": all(
                item.includes_delisted for item in source.readiness.markets
            ),
            "corporateActionLedgerProven": all(
                item.corporate_action_status == "clear"
                for item in source.readiness.markets
            ),
            "benchmarkProven": all(
                item.benchmark.status == "available"
                for item in source.readiness.markets
            ),
        },
        "portfolioDiagnostics": {
            "config": _json_config(config),
            "costSlippage": {
                "KR": {
                    "feeRate": str(config.kr_cost.fee_rate),
                    "slippageRate": str(config.kr_cost.slippage_rate),
                },
                "US": {
                    "feeRate": str(config.us_cost.fee_rate),
                    "slippageRate": str(config.us_cost.slippage_rate),
                },
            },
            "baseline": _backtest_summary(diagnostics.baseline),
            "costStress": cost_stress,
            "oneBarDelay": {
                "additionalBars": 1,
                "result": _backtest_summary(diagnostics.delayed_execution),
            },
            "symbolRemoval": [
                {
                    "removedMarket": item.removed_market,
                    "removedSymbol": item.removed_symbol,
                    "totalReturn": str(item.total_return),
                    "excessReturn": (
                        str(item.excess_return)
                        if item.excess_return is not None
                        else None
                    ),
                    "determinismHash": item.determinism_hash,
                }
                for item in diagnostics.symbol_removal
            ],
            "periodPerformance": [
                _performance_slice(item) for item in diagnostics.period_performance
            ],
            "regimePerformance": [
                _performance_slice(item) for item in diagnostics.regime_performance
            ],
            "turnover": str(diagnostics.turnover_ratio),
            "evidence": [_backtest_evidence(item) for item in diagnostics.evidence],
            "determinismHash": diagnostics.determinism_hash,
        },
        "walkForward": {
            "config": {
                "trainBars": walk_config.train_bars,
                "testBars": walk_config.test_bars,
                "stepBars": walk_config.step_bars,
            },
            "foldCount": len(folds),
            "passedFoldCount": sum(bool(item["passed"]) for item in folds),
            "meanTestReturn": str(walk_forward.mean_test_return),
            "meanTestExcessReturn": (
                str(walk_forward.mean_test_excess_return)
                if walk_forward.mean_test_excess_return is not None
                else None
            ),
            "folds": folds,
            "evidence": [_backtest_evidence(item) for item in walk_forward.evidence],
            "determinismHash": walk_forward.determinism_hash,
        },
        "derivedPromotionMetrics": metrics.as_snapshot(),
        "promotionThresholds": dict(thresholds),
        "determinism": {
            "datasetContentHash": source.dataset_content_hash,
            "diagnosticsHash": diagnostics.determinism_hash,
            "baselineHash": diagnostics.baseline.determinism_hash,
            "walkForwardHash": walk_forward.determinism_hash,
            "foldTestHashes": [
                fold.test_result.determinism_hash for fold in walk_forward.folds
            ],
        },
    }


def _require_track(track: str) -> PromotionTrack:
    if track not in PROMOTION_TRACKS:
        raise PromotionEvidenceBuildError(f"promotion_track_invalid:{track}")
    return cast(PromotionTrack, track)


def _require_readiness(
    readiness: DailyCandlesReadiness,
    *,
    track: PromotionTrack,
    period_start: date | None = None,
) -> None:
    """Fail closed on the evidence the requested track actually claims.

    Both tracks require the full obtainable daily-history evidence. Only the
    historical track additionally requires point-in-time membership, delisted
    survivors, the corporate-action ledger and a primary-provider price series;
    on the forward track those facts stay recorded as unresolved evidence.
    """
    if not readiness.promotion_ready:
        detail = ",".join(readiness.blockers) or "readiness_not_ready"
        raise PromotionEvidenceBuildError(detail)
    if not readiness.daily_history_ready:
        raise PromotionEvidenceBuildError("daily_history_not_ready")
    if readiness.eligible_symbol_count <= 0:
        raise PromotionEvidenceBuildError("eligible_symbols_zero")
    if track == HISTORICAL_PIT_TRACK and not readiness.historical_evidence_ready:
        detail = (
            ",".join(readiness.historical_evidence_blockers)
            or "historical_evidence_not_ready"
        )
        raise PromotionEvidenceBuildError(detail)
    for market in readiness.markets:
        cohort = market.cohort
        if cohort is None:
            raise PromotionEvidenceBuildError(f"{market.market}:cohort_not_found")
        if cohort.method != "latest_market_cap":
            raise PromotionEvidenceBuildError(f"{market.market}:cohort_method_invalid")
        if (
            market.total_symbol_count <= 0
            or market.eligible_symbol_count != market.total_symbol_count
        ):
            raise PromotionEvidenceBuildError(
                f"{market.market}:cohort_members_not_ready"
            )
        if market.benchmark.status != "available":
            raise PromotionEvidenceBuildError(f"{market.market}:benchmark_unavailable")
        if not market.benchmark.sources:
            raise PromotionEvidenceBuildError(
                f"{market.market}:benchmark_source_missing"
            )
        if track != HISTORICAL_PIT_TRACK:
            continue
        if cohort.evidence_scope != "historical_pit":
            raise PromotionEvidenceBuildError(
                f"{market.market}:cohort_not_historical_pit"
            )
        if (
            market.evaluated_window_start is None
            or market.evaluated_window_start < cohort.effective_date
        ):
            raise PromotionEvidenceBuildError(
                f"{market.market}:cohort_window_predates_effective_date"
            )
        if period_start is not None and period_start < cohort.effective_date:
            raise PromotionEvidenceBuildError(
                f"{market.market}:cohort_window_predates_effective_date"
            )
        if market.fallback_only:
            raise PromotionEvidenceBuildError(f"{market.market}:fallback_only")
        if not market.point_in_time_available:
            raise PromotionEvidenceBuildError(
                f"{market.market}:point_in_time_unavailable"
            )
        if market.list_date_covered_symbol_count != market.total_symbol_count:
            raise PromotionEvidenceBuildError(
                f"{market.market}:list_date_coverage_incomplete"
            )
        if market.members_listed_after_cohort_start:
            raise PromotionEvidenceBuildError(
                f"{market.market}:member_listed_after_cohort_start"
            )
        if market.delist_date_covered_inactive_count != market.inactive_symbol_count:
            raise PromotionEvidenceBuildError(
                f"{market.market}:delist_date_coverage_incomplete"
            )
        if not market.includes_delisted:
            raise PromotionEvidenceBuildError(
                f"{market.market}:delisted_members_absent"
            )
        if market.corporate_action_status != "clear":
            raise PromotionEvidenceBuildError(
                f"{market.market}:corporate_action_unknown"
            )
        if set(market.benchmark.sources) <= _FALLBACK_SOURCES:
            raise PromotionEvidenceBuildError(
                f"{market.market}:benchmark_fallback_only"
            )


def _select_universe_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    active_core = [row for row in rows if str(row.get("member_kind") or "") == "active"]
    ordered = sorted(
        active_core,
        key=lambda row: (
            int(row.get("member_rank") or 0),
            str(row.get("symbol") or ""),
        ),
    )
    return tuple(ordered[:BACKTEST_CANDIDATES_PER_MARKET])


def _engine_universe_evidence(source: PortfolioEvidenceSource):
    from app.extensions.kasset.automation.portfolio_backtest import UniverseEvidence

    return UniverseEvidence(
        source="durable_research_cohort",
        point_in_time_membership=all(
            item.point_in_time_available for item in source.readiness.markets
        ),
        includes_delisted=all(
            item.includes_delisted for item in source.readiness.markets
        ),
        as_of=source.as_of,
        notes=(
            "Immutable cohort membership, member rank, and effective date verified",
        ),
    )


def _experiment_identity(
    *,
    artifact: StrategyArtifactManifest,
    source: PortfolioEvidenceSource,
    raw_payload: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> StrategyExperimentIdentity:
    strategy_config = artifact.effective_config
    return StrategyExperimentIdentity(
        strategy_key=artifact.strategy_key,
        strategy_version=artifact.strategy_version,
        hypothesis="DB-backed KR/US daily portfolio diagnostics for PAPER promotion",
        strategy={
            "key": artifact.strategy_key,
            "version": artifact.strategy_version,
            "registry": strategy_config["strategyRegistry"],
        },
        code={"files": [item.as_evidence() for item in artifact.code_files]},
        params={
            "candidateRanker": strategy_config["candidateRanker"],
            "portfolioBacktest": strategy_config["portfolioBacktest"],
            "walkForward": strategy_config["walkForward"],
            "positionSizer": strategy_config["positionSizer"],
            "positionManager": strategy_config["positionManager"],
            "regimeWeights": strategy_config["regimeWeights"],
        },
        dataset_manifest={
            "contentHash": source.dataset_content_hash,
            "asOf": _timestamp(source.as_of),
            "periodStart": _timestamp(source.period_start),
            "periodEnd": _timestamp(source.period_end),
            "selectedUniverse": [dict(item) for item in source.selected_universe],
        },
        universe=cast(Mapping[str, object], raw_payload["data"]),
        pit=cast(Mapping[str, object], raw_payload["validation"]),
        frozen_config={
            "artifactFingerprint": artifact.fingerprint,
            "artifactSchemaVersion": artifact.schema_version,
            "effectiveConfig": artifact.effective_config,
            "promotionEvidenceSchemaVersion": PROMOTION_EVIDENCE_SCHEMA_VERSION,
        },
        policy={"promotionThresholds": dict(thresholds)},
        benchmark=cast(Mapping[str, object], raw_payload["benchmarks"]),
        cost=cast(
            Mapping[str, object],
            cast(Mapping[str, object], raw_payload["portfolioDiagnostics"])[
                "costSlippage"
            ],
        ),
        mdd={"maximum": thresholds["maxDrawdown"]},
    )


def _verify_recorded_run(
    run: ResearchBacktestRun,
    raw_payload: Mapping[str, object],
    artifact: StrategyArtifactManifest,
) -> None:
    expected_hash = canonical_sha256(raw_payload)
    if (
        run.trial_status != "completed"
        or run.artifact_hash != expected_hash
        or run.gate_artifact_hash != artifact.fingerprint
        or type(run.raw_payload) is not dict
        or canonical_sha256(run.raw_payload) != expected_hash
    ):
        raise PromotionEvidenceBuildError("stored_run_hash_mismatch")


def _verify_recorded_candidate(
    candidate: ResearchPromotionCandidate,
    *,
    experiment: ResearchStrategyExperiment,
    status: str,
    reason_code: str,
    thresholds: Mapping[str, object],
    metrics: PromotionMetrics,
) -> None:
    if (
        candidate.experiment_id != experiment.experiment_id
        or candidate.run_config_hash != experiment.frozen_config_hash
        or candidate.run_data_hash != experiment.dataset_manifest_hash
        or candidate.status != status
        or candidate.reason_code != reason_code
        or canonical_sha256(candidate.thresholds) != canonical_sha256(thresholds)
        or canonical_sha256(candidate.metrics)
        != canonical_sha256(metrics.as_snapshot())
    ):
        raise PromotionEvidenceBuildError("stored_candidate_hash_mismatch")


def _dataset_content_hash(
    *,
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    benchmarks: Mapping[str, Sequence[PriceBar]],
    candidate_benchmarks: (
        Mapping[CandidateKey, CandidateBenchmarkSeries] | None
    ) = None,
    selected_universe: Sequence[Mapping[str, object]],
) -> str:
    # The enclosing cohort ID also covers forced readiness-only members. Keep it
    # in stored evidence, but exclude it from the active-core sample fingerprint.
    return canonical_sha256(
        {
            "selectedUniverse": tuple(
                {key: value for key, value in item.items() if key != "cohortId"}
                for item in selected_universe
            ),
            "candidates": tuple(
                {
                    "market": item.market,
                    "symbol": item.symbol,
                    "sources": item.sources,
                    "name": item.name,
                }
                for item in candidates
            ),
            "bars": {
                f"{market}:{symbol}": tuple(_bar_hash_input(bar) for bar in bars)
                for (market, symbol), bars in sorted(bars_by_candidate.items())
            },
            "benchmarks": {
                market: tuple(_bar_hash_input(bar) for bar in bars)
                for market, bars in sorted(benchmarks.items())
            },
            "candidateBenchmarks": {
                f"{market}:{symbol}": {
                    "benchmarkSymbol": series.benchmark_symbol,
                    "bars": tuple(_bar_hash_input(bar) for bar in series.bars),
                }
                for (market, symbol), series in sorted(
                    (candidate_benchmarks or {}).items()
                )
            },
        }
    )


def _price_bar(row: Mapping[str, object]) -> PriceBar:
    timestamp = cast(datetime, row["time"])
    return PriceBar(
        timestamp=_aware_utc(timestamp),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
    )


def _bar_hash_input(bar: PriceBar) -> tuple[object, ...]:
    return (
        bar.timestamp,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )


def _fold_passed(fold: WalkForwardFold) -> bool:
    result = fold.test_result
    return bool(
        result.trade_count > 0
        and result.total_return >= 0
        and result.excess_return is not None
        and result.excess_return >= 0
        and _is_hash(result.determinism_hash)
    )


def _is_hash(value: object) -> bool:
    return bool(isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64)


def _readiness_market_payload(item: MarketReadiness) -> dict[str, object]:
    benchmark_sources = list(item.benchmark.sources)
    cohort = item.cohort
    cohort_payload: dict[str, object] | None = None
    if cohort is not None:
        cohort_payload = {
            "cohortId": cohort.cohort_id,
            "method": cohort.method,
            "selectionAsOf": _timestamp(cohort.selection_as_of),
            "selectionDate": cohort.selection_date.isoformat(),
            "effectiveDate": cohort.effective_date.isoformat(),
            "requestedSize": cohort.requested_size,
            "activeMemberCount": cohort.active_member_count,
            "valuationSnapshotDate": cohort.valuation_snapshot_date.isoformat(),
            "valuationSnapshotSource": cohort.valuation_snapshot_source,
            "evidenceScope": cohort.evidence_scope,
        }
    return {
        "cohort": cohort_payload,
        "evaluatedWindowStart": _date_text(item.evaluated_window_start),
        "evaluatedWindowEnd": _date_text(item.evaluated_window_end),
        "latestCompletedSession": _date_text(item.latest_completed_session),
        "ingestLagSessionCount": item.ingest_lag_session_count,
        "unevidencedSessionCount": item.unevidenced_session_count,
        "unevidencedSessions": [
            session_day.isoformat() for session_day in item.unevidenced_sessions
        ],
        "totalSymbolCount": item.total_symbol_count,
        "cohortActiveMemberCount": item.cohort_active_member_count,
        "forcedMemberCount": item.forced_member_count,
        "benchmarkMemberCount": item.benchmark_member_count,
        "activeSymbolCount": item.active_symbol_count,
        "inactiveSymbolCount": item.inactive_symbol_count,
        "symbolsWithAtLeast252Bars": item.symbols_with_at_least_252_bars,
        "eligibleSymbolCount": item.eligible_symbol_count,
        "priceAdjustmentStatus": item.price_adjustment_status,
        "corporateActionStatus": item.corporate_action_status,
        "corporateActionCoveredSymbolCount": (
            item.corporate_action_covered_symbol_count
        ),
        "adjustmentCoveredSymbolCount": item.adjustment_covered_symbol_count,
        "listDateCoveredSymbolCount": item.list_date_covered_symbol_count,
        "membersListedAfterCohortStart": item.members_listed_after_cohort_start,
        "delistDateCoveredInactiveCount": item.delist_date_covered_inactive_count,
        "pointInTimeAvailable": item.point_in_time_available,
        "includesDelisted": item.includes_delisted,
        "delistedSymbolCount": item.delisted_symbol_count,
        "delistedWithCandlesCount": item.delisted_with_candles_count,
        "fallbackOnly": item.fallback_only,
        "dailyHistoryReady": item.daily_history_ready,
        "promotionReady": item.promotion_ready,
        "historicalEvidenceReady": item.historical_evidence_ready,
        "historicalEvidenceBlockers": list(item.historical_evidence_blockers),
        "unresolvedEvidence": list(item.unresolved_evidence),
        "benchmark": {
            "symbol": item.benchmark.symbol,
            "source": item.benchmark.source,
            "sources": benchmark_sources,
            "coverage": {
                "startAt": _timestamp_optional(item.benchmark.start),
                "endAt": _timestamp_optional(item.benchmark.end),
                "barCount": item.benchmark.count,
            },
            "status": item.benchmark.status,
            "fallbackOnly": bool(
                benchmark_sources and set(benchmark_sources) <= _FALLBACK_SOURCES
            ),
        },
        "dailyHistoryBlockers": list(item.daily_history_blockers),
        "blockers": list(item.blockers),
        "reasons": list(item.reasons),
    }


def _backtest_summary(result: PortfolioBacktestResult) -> dict[str, object]:
    return {
        "strategyKey": result.strategy_key,
        "strategyVersion": result.strategy_version,
        "initialCash": str(result.initial_cash),
        "finalCash": str(result.final_cash),
        "finalEquity": str(result.final_equity),
        "totalReturn": str(result.total_return),
        "benchmarkReturn": (
            str(result.benchmark_return)
            if result.benchmark_return is not None
            else None
        ),
        "excessReturn": (
            str(result.excess_return) if result.excess_return is not None else None
        ),
        "maxDrawdown": str(result.max_drawdown),
        "tradeCount": result.trade_count,
        "winRate": str(result.win_rate),
        "expectancy": str(result.expectancy),
        # 비용 차감 후 profit factor를 저장 payload만으로 재구성할 수 있게 한다.
        "grossProfit": str(
            sum(
                (trade.net_pnl for trade in result.trades if trade.net_pnl > 0),
                start=Decimal("0"),
            )
        ),
        "grossLoss": str(
            -sum(
                (trade.net_pnl for trade in result.trades if trade.net_pnl < 0),
                start=Decimal("0"),
            )
        ),
        "feesPaid": str(result.fees_paid),
        "slippageCost": str(result.slippage_cost),
        "openPositionCount": len(result.open_positions),
        "benchmarkMarkets": [item.market for item in result.benchmark_by_market],
        "recordStartAt": (
            _timestamp(result.equity_curve[0].timestamp)
            if result.equity_curve
            else None
        ),
        "recordEndAt": (
            _timestamp(result.equity_curve[-1].timestamp)
            if result.equity_curve
            else None
        ),
        "benchmarkWindows": [
            {
                "market": item.market,
                "startAt": _timestamp(item.start_at),
                "endAt": _timestamp(item.end_at),
            }
            for item in result.benchmark_by_market
        ],
        "evidence": [_backtest_evidence(item) for item in result.evidence],
        "determinismHash": result.determinism_hash,
    }


def _performance_slice(item: Any) -> dict[str, object]:
    return {
        "label": item.label,
        "startAt": _timestamp(item.start_at),
        "endAt": _timestamp(item.end_at),
        "totalReturn": str(item.total_return),
        "tradeCount": item.trade_count,
        "winRate": str(item.win_rate),
        "netPnl": str(item.net_pnl),
    }


def _backtest_evidence(item: Any) -> dict[str, str]:
    return {"code": item.code, "value": item.value, "detail": item.detail}


def _thresholds_snapshot(thresholds: PromotionThresholds) -> dict[str, object]:
    return {
        "minTotalReturn": str(thresholds.min_total_return),
        "maxDrawdown": str(thresholds.max_drawdown),
        "minWinRate": str(thresholds.min_win_rate),
        "minExpectancy": str(thresholds.min_expectancy),
        "minExcessReturn": str(thresholds.min_excess_return),
        "minProfitFactor": str(thresholds.min_profit_factor),
        "minCostStressedTotalReturn": str(thresholds.min_cost_stressed_total_return),
        "minTradeCount": thresholds.min_trade_count,
        "minWalkForwardFolds": thresholds.min_walk_forward_folds,
        "minWalkForwardPassRate": str(thresholds.min_walk_forward_pass_rate),
        "requireDataQualityEvidence": thresholds.require_data_quality_evidence,
        "requireSurvivorshipEvidence": thresholds.require_survivorship_evidence,
        "requireDeterministic": thresholds.require_deterministic,
    }


def _json_config(config: PortfolioBacktestConfig) -> dict[str, object]:
    return {
        "initialCash": str(config.initial_cash),
        "maxPositions": config.max_positions,
        "candidateTopN": config.candidate_top_n,
        "riskPerTradeRate": str(config.risk_per_trade_rate),
        "maxSymbolAllocation": str(config.max_symbol_allocation),
        "executionDelayBars": config.execution_delay_bars,
    }


def derive_metrics_from_stored_payload(raw: object) -> PromotionMetrics:
    """Validate a persisted evidence payload and derive its metric snapshot."""

    payload = _required_mapping(raw, "raw_payload")
    if payload.get("schemaVersion") != PROMOTION_EVIDENCE_SCHEMA_VERSION:
        raise PromotionEvidenceBuildError("evidence_schema_version_mismatch")
    declared_track = payload.get("promotionTrack")
    if not isinstance(declared_track, str) or declared_track not in PROMOTION_TRACKS:
        raise PromotionEvidenceBuildError("promotion_track_invalid")
    track = cast(PromotionTrack, declared_track)
    historical = track == HISTORICAL_PIT_TRACK
    # The threshold profile is derived from the track, never trusted from the
    # payload: a stored payload cannot smuggle in a laxer profile.
    expected_thresholds = _thresholds_snapshot(promotion_thresholds_for_track(track))
    if canonical_sha256(payload.get("promotionThresholds")) != canonical_sha256(
        expected_thresholds
    ):
        raise PromotionEvidenceBuildError("promotion_thresholds_track_mismatch")
    strategy = _required_mapping(payload.get("strategy"), "strategy")
    if not all(
        isinstance(strategy.get(field), str) and str(strategy[field]).strip()
        for field in (
            "key",
            "version",
            "artifactFingerprint",
            "artifactSchemaVersion",
            "sourceCommit",
        )
    ):
        raise PromotionEvidenceBuildError("strategy_identity_incomplete")
    if not _is_hash(strategy["artifactFingerprint"]):
        raise PromotionEvidenceBuildError("strategy_artifact_fingerprint_invalid")
    if not _is_source_commit(strategy["sourceCommit"]):
        raise PromotionEvidenceBuildError("strategy_source_commit_invalid")

    data = _required_mapping(payload.get("data"), "data")
    eligible = _required_mapping(data.get("eligible252Counts"), "eligible252Counts")
    selected = _required_mapping(data.get("selectedCounts"), "selectedCounts")
    cohorts = _required_mapping(data.get("cohorts"), "cohorts")
    period = _required_mapping(data.get("period"), "period")
    period_start = _required_timestamp(period, "startAt").date()
    if any(_required_int(eligible, market) <= 0 for market in ("kr", "us")):
        raise PromotionEvidenceBuildError("eligible_symbols_zero")
    if any(_required_int(selected, market) <= 0 for market in ("kr", "us")):
        raise PromotionEvidenceBuildError("selected_universe_empty")
    if not _is_hash(data.get("datasetContentHash")):
        raise PromotionEvidenceBuildError("dataset_content_hash_invalid")
    selected_universe = _required_sequence(
        data.get("selectedUniverse"), "selectedUniverse"
    )
    if not selected_universe or any(
        not isinstance(item, Mapping) for item in selected_universe
    ):
        raise PromotionEvidenceBuildError("selected_universe_invalid")
    selected_rows = tuple(
        cast(Mapping[str, object], item) for item in selected_universe
    )
    for market in ("kr", "us"):
        cohort = _required_mapping(cohorts.get(market), f"cohort.{market}")
        cohort_id = cohort.get("cohortId")
        selection_date = _required_date(cohort, "selectionDate")
        effective_date = _required_date(cohort, "effectiveDate")
        expected_scope = "historical_pit" if historical else "forward_paper"
        if (
            not isinstance(cohort_id, str)
            or not cohort_id.strip()
            or cohort.get("method") != "latest_market_cap"
            or cohort.get("evidenceScope") != expected_scope
        ):
            raise PromotionEvidenceBuildError(f"{market}:cohort_identity_invalid")
        if historical and period_start < effective_date:
            raise PromotionEvidenceBuildError(
                f"{market}:cohort_window_predates_effective_date"
            )
        market_rows = tuple(
            item
            for item in selected_rows
            if str(item.get("market", "")).casefold() == market
        )
        if len(market_rows) != _required_int(selected, market):
            raise PromotionEvidenceBuildError(
                f"{market}:selected_universe_count_mismatch"
            )
        if any(
            row.get("cohortId") != cohort_id
            or row.get("cohortMethod") != "latest_market_cap"
            or row.get("cohortSelectionDate") != selection_date.isoformat()
            or row.get("cohortEffectiveDate") != effective_date.isoformat()
            or row.get("cohortEvidenceScope") != expected_scope
            or row.get("memberKind") not in {"active", "forced"}
            or _required_int(row, "memberRank") <= 0
            for row in market_rows
        ):
            raise PromotionEvidenceBuildError(
                f"{market}:selected_cohort_identity_invalid"
            )
        member_keys = {
            (str(row["memberKind"]), _required_int(row, "memberRank"))
            for row in market_rows
        }
        if len(member_keys) != len(market_rows):
            raise PromotionEvidenceBuildError(
                f"{market}:selected_member_rank_duplicate"
            )
        source_values = tuple(
            source
            for row in market_rows
            for source in _required_sequence(
                row.get("sources"), "selectedUniverse.sources"
            )
        )
        if (
            not source_values
            or any(
                not isinstance(source, str) or not source.strip()
                for source in source_values
            )
            or (
                historical
                and {cast(str, source).strip() for source in source_values}
                <= _FALLBACK_SOURCES
            )
            or any(_required_int(row, "loadedBarCount") <= 0 for row in market_rows)
        ):
            raise PromotionEvidenceBuildError(
                f"{market}:selected_data_evidence_invalid"
            )

    readiness = _required_mapping(payload.get("readiness"), "readiness")
    if (
        readiness.get("dailyHistoryReady") is not True
        or readiness.get("promotionReady") is not True
    ):
        raise PromotionEvidenceBuildError("readiness_not_ready")
    if _required_sequence(
        readiness.get("dailyHistoryBlockers"), "readiness.dailyHistoryBlockers"
    ):
        raise PromotionEvidenceBuildError("daily_history_blocked")
    if _required_sequence(readiness.get("blockers"), "readiness.blockers"):
        raise PromotionEvidenceBuildError("readiness_blocked")
    # The forward track must still declare the historical facts it could not
    # prove; a payload that silently drops the key is not replayable evidence.
    unresolved = _required_sequence(
        readiness.get("unresolvedEvidence"), "readiness.unresolvedEvidence"
    )
    if historical:
        if readiness.get("historicalEvidenceReady") is not True:
            raise PromotionEvidenceBuildError("historical_evidence_not_ready")
        if _required_sequence(
            readiness.get("historicalEvidenceBlockers"),
            "readiness.historicalEvidenceBlockers",
        ):
            raise PromotionEvidenceBuildError("historical_evidence_blocked")
        if unresolved:
            raise PromotionEvidenceBuildError("historical_evidence_unresolved")
    validation = _required_mapping(payload.get("validation"), "validation")
    required_validation: dict[str, bool] = {
        "eligibleNonzero": True,
        "benchmarkProven": True,
    }
    if historical:
        required_validation.update(
            {
                "fallbackOnly": False,
                "pointInTimeProven": True,
                "delistedIncluded": True,
                "corporateActionLedgerProven": True,
            }
        )
    if any(
        validation.get(key) is not expected
        for key, expected in required_validation.items()
    ):
        raise PromotionEvidenceBuildError("required_validation_evidence_missing")

    benchmarks = _required_mapping(payload.get("benchmarks"), "benchmarks")
    for market in ("kr", "us"):
        benchmark = _required_mapping(benchmarks.get(market), f"benchmark.{market}")
        sources = _required_sequence(
            benchmark.get("sources"), f"benchmark.{market}.sources"
        )
        if (
            benchmark.get("status") != "available"
            or not sources
            or not all(isinstance(source, str) and source.strip() for source in sources)
            or (
                historical
                and (
                    benchmark.get("fallbackOnly") is not False
                    or set(cast(Sequence[str], sources)) <= _FALLBACK_SOURCES
                )
            )
        ):
            raise PromotionEvidenceBuildError(f"{market}:benchmark_evidence_invalid")
        coverage = _required_mapping(
            benchmark.get("coverage"), f"benchmark.{market}.coverage"
        )
        if _required_int(coverage, "barCount") < REQUIRED_BENCHMARK_BARS:
            raise PromotionEvidenceBuildError(
                f"{market}:benchmark_coverage_insufficient"
            )

    diagnostics = _required_mapping(
        payload.get("portfolioDiagnostics"), "portfolioDiagnostics"
    )
    baseline = _required_mapping(diagnostics.get("baseline"), "baseline")
    if (
        baseline.get("strategyKey") != strategy["key"]
        or baseline.get("strategyVersion") != strategy["version"]
    ):
        raise PromotionEvidenceBuildError("baseline_strategy_identity_mismatch")
    _require_stored_benchmark_window_coverage(baseline, selected)
    cost_slippage = _required_mapping(diagnostics.get("costSlippage"), "costSlippage")
    for market in ("KR", "US"):
        cost = _required_mapping(cost_slippage.get(market), f"costSlippage.{market}")
        _required_decimal(cost, "feeRate")
        _required_decimal(cost, "slippageRate")
    cost_stress = _required_sequence(diagnostics.get("costStress"), "costStress")
    multipliers = tuple(
        _required_int(_required_mapping(item, "costStress.item"), "multiplier")
        for item in cost_stress
    )
    if multipliers != (1, 2, 3):
        raise PromotionEvidenceBuildError("cost_stress_scenarios_incomplete")
    one_bar_delay = _required_mapping(diagnostics.get("oneBarDelay"), "oneBarDelay")
    if _required_int(one_bar_delay, "additionalBars") != 1:
        raise PromotionEvidenceBuildError("one_bar_delay_missing")
    _required_mapping(one_bar_delay.get("result"), "oneBarDelay.result")
    symbol_removal = _required_sequence(
        diagnostics.get("symbolRemoval"), "symbolRemoval"
    )
    if (
        sum(_required_int(selected, market) for market in ("kr", "us")) > 1
        and not symbol_removal
    ):
        raise PromotionEvidenceBuildError("symbol_removal_evidence_missing")
    _required_sequence(diagnostics.get("periodPerformance"), "periodPerformance")
    _required_sequence(diagnostics.get("regimePerformance"), "regimePerformance")
    _required_decimal(diagnostics, "turnover")

    walk = _required_mapping(payload.get("walkForward"), "walkForward")
    folds = _required_sequence(walk.get("folds"), "walkForward.folds")
    if not folds or _required_int(walk, "foldCount") != len(folds):
        raise PromotionEvidenceBuildError("walk_forward_folds_missing")
    passed_folds = 0
    fold_hashes: list[str] = []
    for item in folds:
        fold = _required_mapping(item, "walkForward.fold")
        test = _required_mapping(fold.get("test"), "walkForward.fold.test")
        _require_stored_benchmark_window_coverage(test, selected)
        passed = _stored_fold_passed(test)
        if fold.get("passed") is not passed:
            raise PromotionEvidenceBuildError("walk_forward_pass_mismatch")
        passed_folds += int(passed)
        test_hash = test.get("determinismHash")
        if not _is_hash(test_hash):
            raise PromotionEvidenceBuildError("walk_forward_hash_invalid")
        fold_hashes.append(cast(str, test_hash))
    if _required_int(walk, "passedFoldCount") != passed_folds:
        raise PromotionEvidenceBuildError("walk_forward_pass_count_mismatch")

    determinism = _required_mapping(payload.get("determinism"), "determinism")
    diagnostics_hash = diagnostics.get("determinismHash")
    baseline_hash = baseline.get("determinismHash")
    walk_hash = walk.get("determinismHash")
    expected_hash_fields = {
        "datasetContentHash": data.get("datasetContentHash"),
        "diagnosticsHash": diagnostics_hash,
        "baselineHash": baseline_hash,
        "walkForwardHash": walk_hash,
    }
    if any(
        not _is_hash(value) or determinism.get(key) != value
        for key, value in expected_hash_fields.items()
    ):
        raise PromotionEvidenceBuildError("determinism_hash_mismatch")
    stored_fold_hashes = _required_sequence(
        determinism.get("foldTestHashes"), "determinism.foldTestHashes"
    )
    if tuple(stored_fold_hashes) != tuple(fold_hashes):
        raise PromotionEvidenceBuildError("fold_determinism_hash_mismatch")

    excess_return = _required_decimal(baseline, "excessReturn")
    stored_cost_stress = _required_sequence(diagnostics.get("costStress"), "costStress")
    cost_stressed_returns = [
        _required_decimal(_required_mapping(item, "costStress.item"), "totalReturn")
        for item in stored_cost_stress
    ]
    if not cost_stressed_returns:
        raise PromotionEvidenceBuildError("cost_stress_scenarios_missing")
    metrics = PromotionMetrics(
        total_return=_required_decimal(baseline, "totalReturn"),
        max_drawdown=_required_decimal(baseline, "maxDrawdown"),
        win_rate=_required_decimal(baseline, "winRate"),
        expectancy=_required_decimal(baseline, "expectancy"),
        excess_return=excess_return,
        gross_profit=_required_decimal(baseline, "grossProfit"),
        gross_loss=_required_decimal(baseline, "grossLoss"),
        cost_stressed_total_return=min(cost_stressed_returns),
        total_costs=(
            _required_decimal(baseline, "feesPaid")
            + _required_decimal(baseline, "slippageCost")
        ),
        trade_count=_required_int(baseline, "tradeCount"),
        walk_forward_folds=len(folds),
        walk_forward_passed_folds=passed_folds,
        # Mirror the build-time derivation exactly: the replayed snapshot may
        # never assert evidence the persisted validation block does not carry.
        data_quality_evidence=readiness.get("dailyHistoryReady") is True,
        survivorship_evidence=bool(
            validation.get("pointInTimeProven") is True
            and validation.get("delistedIncluded") is True
        ),
        deterministic=True,
        backtest_hashes=(
            cast(str, diagnostics_hash),
            cast(str, baseline_hash),
            cast(str, walk_hash),
            *fold_hashes,
        ),
    )
    stored_metrics = _required_mapping(
        payload.get("derivedPromotionMetrics"), "derivedPromotionMetrics"
    )
    if canonical_sha256(stored_metrics) != canonical_sha256(metrics.as_snapshot()):
        raise PromotionEvidenceBuildError("derived_metrics_mismatch")
    return metrics


def _require_stored_benchmark_window_coverage(
    baseline: Mapping[str, object],
    selected: Mapping[str, object],
) -> None:
    expected_markets = {
        market.upper() for market in ("kr", "us") if _required_int(selected, market) > 0
    }
    raw_markets = _required_sequence(
        baseline.get("benchmarkMarkets"), "baseline.benchmarkMarkets"
    )
    if any(not isinstance(market, str) or not market.strip() for market in raw_markets):
        raise PromotionEvidenceBuildError("benchmark_market_mismatch")
    actual_markets = tuple(cast(str, market).strip().upper() for market in raw_markets)
    if (
        len(actual_markets) != len(set(actual_markets))
        or set(actual_markets) != expected_markets
    ):
        raise PromotionEvidenceBuildError("benchmark_market_mismatch")

    record_start = _required_timestamp(baseline, "recordStartAt")
    record_end = _required_timestamp(baseline, "recordEndAt")
    if record_start > record_end:
        raise PromotionEvidenceBuildError("benchmark_window_mismatch")
    raw_windows = _required_sequence(
        baseline.get("benchmarkWindows"), "baseline.benchmarkWindows"
    )
    windows: dict[str, tuple[datetime, datetime]] = {}
    for raw_window in raw_windows:
        window = _required_mapping(raw_window, "baseline.benchmarkWindows.item")
        market_value = window.get("market")
        if not isinstance(market_value, str) or not market_value.strip():
            raise PromotionEvidenceBuildError("benchmark_market_mismatch")
        market = market_value.strip().upper()
        if market in windows:
            raise PromotionEvidenceBuildError("benchmark_market_mismatch")
        window_start = _required_timestamp(window, "startAt")
        window_end = _required_timestamp(window, "endAt")
        windows[market] = (window_start, window_end)
    if set(windows) != expected_markets:
        raise PromotionEvidenceBuildError("benchmark_market_mismatch")
    if any(
        window_start > record_start or window_end < record_end
        for window_start, window_end in windows.values()
    ):
        raise PromotionEvidenceBuildError("benchmark_window_mismatch")


def _stored_fold_passed(result: Mapping[str, object]) -> bool:
    return bool(
        _required_int(result, "tradeCount") > 0
        and _required_decimal(result, "totalReturn") >= 0
        and _required_decimal(result, "excessReturn") >= 0
        and _is_hash(result.get("determinismHash"))
    )


def _required_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PromotionEvidenceBuildError(f"{field}_missing")
    return cast(Mapping[str, object], value)


def _required_sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PromotionEvidenceBuildError(f"{field}_missing")
    return cast(Sequence[object], value)


def _is_source_commit(value: object) -> bool:
    return bool(
        isinstance(value, str) and len(value) in {40, 64} and set(value) <= _HEX64
    )


def _required_int(value: Mapping[str, object], field: str) -> int:
    raw = value.get(field)
    if type(raw) is not int or raw < 0:
        raise PromotionEvidenceBuildError(f"{field}_invalid")
    return raw


def _required_decimal(value: Mapping[str, object], field: str) -> Decimal:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise PromotionEvidenceBuildError(f"{field}_invalid")
    try:
        parsed = Decimal(raw)
    except DecimalException as exc:
        raise PromotionEvidenceBuildError(f"{field}_invalid") from exc
    if not parsed.is_finite():
        raise PromotionEvidenceBuildError(f"{field}_invalid")
    return parsed


def _required_date(value: Mapping[str, object], field: str) -> date:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise PromotionEvidenceBuildError(f"{field}_invalid")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise PromotionEvidenceBuildError(f"{field}_invalid") from exc


def _required_timestamp(value: Mapping[str, object], field: str) -> datetime:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise PromotionEvidenceBuildError(f"{field}_invalid")
    normalized = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PromotionEvidenceBuildError(f"{field}_invalid") from exc
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PromotionEvidenceBuildError("timestamp_timezone_missing")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _timestamp_optional(value: datetime | None) -> str | None:
    return _timestamp(value) if value is not None else None


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "FORWARD_PAPER_TRACK",
    "HISTORICAL_PIT_TRACK",
    "PortfolioEvidenceSource",
    "PromotionEvidenceBuildError",
    "PromotionEvidenceBuildResult",
    "PromotionTrack",
    "build_and_store_portfolio_evidence",
    "build_promotion_raw_payload",
    "derive_metrics_from_stored_payload",
    "derive_promotion_metrics",
    "load_portfolio_evidence_source",
]
