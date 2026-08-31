"""Deterministic, provider-free breakout candidate ranking.

The ranker consumes only normalized candidate metadata and repository-backed
``PriceBar`` history.  It performs no I/O, never fills missing observations,
and rejects the complete candidate when any supplied bar is malformed,
future-dated, or stale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Literal

from app.extensions.kasset.automation.contracts import PriceBar

MarketKey = Literal["KR", "US"]
CandidateKey = tuple[MarketKey, str]

_SUPPORTED_MARKETS = frozenset({"KR", "US"})
_ZERO = Decimal("0")
_ONE = Decimal("1")
_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class CandidateRankerConfig:
    """The single immutable policy object for universe and factor ranking."""

    candidate_limit: int = 100
    minimum_candidate_target: int = 50
    strategy_review_limit: int = 12
    history_bars: int = 260
    minimum_history_bars: int = 252
    maximum_bar_age: timedelta = timedelta(days=7)
    validity: timedelta = timedelta(hours=24)
    minimum_price_kr: Decimal = Decimal("1000")
    minimum_price_us: Decimal = Decimal("2")
    minimum_turnover_kr: Decimal = Decimal("100000000")
    minimum_turnover_us: Decimal = Decimal("100000")
    momentum_20_weight: Decimal = Decimal("0.07")
    momentum_60_weight: Decimal = Decimal("0.08")
    momentum_120_weight: Decimal = Decimal("0.08")
    liquidity_weight: Decimal = Decimal("0.10")
    relative_strength_weight: Decimal = Decimal("0.15")
    high_distance_20_weight: Decimal = Decimal("0.06")
    high_distance_52_week_weight: Decimal = Decimal("0.08")
    higher_high_low_weight: Decimal = Decimal("0.08")
    atr_compression_weight: Decimal = Decimal("0.08")
    volume_contraction_weight: Decimal = Decimal("0.07")
    volume_expansion_weight: Decimal = Decimal("0.07")
    weekly_trend_weight: Decimal = Decimal("0.08")
    overextension_penalty_weight: Decimal = Decimal("0.15")
    volume_hangover_penalty_weight: Decimal = Decimal("0.10")
    abnormal_volume_ratio: Decimal = Decimal("3")
    volume_hangover_days: int = 5

    def __post_init__(self) -> None:
        if self.candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if not 1 <= self.minimum_candidate_target <= self.candidate_limit:
            raise ValueError("minimum_candidate_target must be within candidate_limit")
        if not 1 <= self.strategy_review_limit <= self.candidate_limit:
            raise ValueError("strategy_review_limit must be within candidate_limit")
        if self.history_bars < self.minimum_history_bars:
            raise ValueError("history_bars must cover minimum_history_bars")
        if self.minimum_history_bars < 252:
            raise ValueError("minimum_history_bars must cover 52 trading weeks")
        if self.maximum_bar_age <= timedelta(0) or self.validity <= timedelta(0):
            raise ValueError("ranker time windows must be positive")
        if self.volume_hangover_days < 1:
            raise ValueError("volume_hangover_days must be positive")
        positive_values = (
            self.minimum_price_kr,
            self.minimum_price_us,
            self.minimum_turnover_kr,
            self.minimum_turnover_us,
            self.abnormal_volume_ratio,
        )
        if any(not value.is_finite() or value <= 0 for value in positive_values):
            raise ValueError("ranker thresholds must be finite and positive")
        factor_weights = (
            self.liquidity_weight,
            self.relative_strength_weight,
            self.momentum_20_weight,
            self.momentum_60_weight,
            self.momentum_120_weight,
            self.high_distance_20_weight,
            self.high_distance_52_week_weight,
            self.higher_high_low_weight,
            self.atr_compression_weight,
            self.volume_contraction_weight,
            self.volume_expansion_weight,
            self.weekly_trend_weight,
        )
        if any(not value.is_finite() or value < 0 for value in factor_weights):
            raise ValueError("factor weights must be finite and non-negative")
        if sum(factor_weights, start=_ZERO) != _ONE:
            raise ValueError("factor weights must sum to one")
        penalty_weights = (
            self.overextension_penalty_weight,
            self.volume_hangover_penalty_weight,
        )
        if any(not value.is_finite() or value < 0 for value in penalty_weights):
            raise ValueError("penalty weights must be finite and non-negative")


DEFAULT_CANDIDATE_RANKER_CONFIG = CandidateRankerConfig()


@dataclass(frozen=True, slots=True)
class BenchmarkReturn:
    market: MarketKey
    return_60: Decimal
    data_as_of: datetime
    benchmark_symbol: str | None = None

    def __post_init__(self) -> None:
        market = str(self.market).strip().upper()
        if market not in _SUPPORTED_MARKETS:
            raise ValueError("benchmark market must be KR or US")
        symbol = str(self.benchmark_symbol or "").strip().upper() or None
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "return_60", _decimal(self.return_60))
        object.__setattr__(self, "benchmark_symbol", symbol)


@dataclass(frozen=True, slots=True)
class CandidateMetadata:
    symbol: str
    market: MarketKey
    sources: tuple[str, ...]
    name: str | None = None
    screener_turnover: Decimal | None = None
    screener_volume: Decimal | None = None
    is_held: bool = False
    is_watchlisted: bool = False
    eligible_for_new_buy: bool = True

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("candidate symbol is required")
        market = str(self.market).strip().upper()
        if market not in _SUPPORTED_MARKETS:
            raise ValueError("candidate market must be KR or US")
        sources = tuple(
            dict.fromkeys(value.strip() for value in self.sources if value.strip())
        )
        if not sources:
            raise ValueError("at least one candidate source is required")
        name = self.name.strip() if self.name and self.name.strip() else None
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "name", name if name != symbol else None)
        if self.screener_turnover is not None:
            object.__setattr__(
                self,
                "screener_turnover",
                _decimal(self.screener_turnover),
            )
        if self.screener_volume is not None:
            object.__setattr__(self, "screener_volume", _decimal(self.screener_volume))

    @property
    def key(self) -> CandidateKey:
        return self.market, self.symbol


@dataclass(frozen=True, slots=True)
class FactorScore:
    code: str
    raw_value: Decimal
    score: Decimal
    weight: Decimal
    contribution: Decimal

    def as_evidence(self) -> dict[str, str]:
        return {
            "rawValue": _text(self.raw_value),
            "score": _text(self.score),
            "weight": _text(self.weight),
            "contribution": _text(self.contribution),
        }


@dataclass(frozen=True, slots=True)
class PenaltyScore:
    code: str
    raw_value: Decimal
    score: Decimal
    weight: Decimal
    deduction: Decimal

    def as_evidence(self) -> dict[str, str]:
        return {
            "rawValue": _text(self.raw_value),
            "score": _text(self.score),
            "weight": _text(self.weight),
            "deduction": _text(self.deduction),
        }


@dataclass(frozen=True, slots=True)
class RankEvidence:
    code: str
    value: str
    detail: str

    def as_evidence(self) -> dict[str, str]:
        return {"code": self.code, "value": self.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CandidateRankResult:
    symbol: str
    market: MarketKey
    total_score: Decimal
    factor_scores: tuple[FactorScore, ...]
    penalties: tuple[PenaltyScore, ...]
    data_as_of: datetime | None
    valid_until: datetime | None
    exclusion_reason: str | None
    atr_14: Decimal | None
    average_volume_20: Decimal | None
    average_turnover_20: Decimal | None
    evidence: tuple[RankEvidence, ...]
    sources: tuple[str, ...]
    is_held: bool
    is_watchlisted: bool
    eligible_for_new_buy: bool
    rank_position: int | None = None
    ranked_total: int | None = None

    @property
    def key(self) -> CandidateKey:
        return self.market, self.symbol

    @property
    def included(self) -> bool:
        return self.exclusion_reason is None

    def as_evidence(self) -> dict[str, object]:
        return {
            "title": "Deterministic breakout candidate rank",
            "source": "kasset_candidate_ranker",
            "kind": "candidate_ranking",
            "symbol": self.symbol,
            "market": self.market,
            "totalScore": _text(self.total_score),
            "rankPosition": self.rank_position,
            "rankedTotal": self.ranked_total,
            "factorScores": {
                item.code: item.as_evidence() for item in self.factor_scores
            },
            "penalties": {item.code: item.as_evidence() for item in self.penalties},
            "dataAsOf": _timestamp_text(self.data_as_of),
            "validUntil": _timestamp_text(self.valid_until),
            "exclusionReason": self.exclusion_reason,
            "atr14": _optional_text(self.atr_14),
            "averageVolume20": _optional_text(self.average_volume_20),
            "averageTurnover20": _optional_text(self.average_turnover_20),
            "candidateSources": list(self.sources),
            "isHeld": self.is_held,
            "isWatchlisted": self.is_watchlisted,
            "eligibleForNewBuy": self.eligible_for_new_buy,
            "evidence": [item.as_evidence() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class CandidateRankingBatch:
    ranked: tuple[CandidateRankResult, ...]
    excluded: tuple[CandidateRankResult, ...]

    def for_strategy_review(self, limit: int) -> tuple[CandidateRankResult, ...]:
        """Return the factor top-N plus every ranked current holding."""

        if limit < 1:
            raise ValueError("strategy review limit must be positive")
        selected = list(self.ranked[:limit])
        selected_keys = {item.key for item in selected}
        selected.extend(
            item
            for item in self.ranked[limit:]
            if item.is_held and item.key not in selected_keys
        )
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class _PreparedCandidate:
    metadata: CandidateMetadata
    bars: tuple[PriceBar, ...]
    data_as_of: datetime
    valid_until: datetime
    turnover: Decimal
    atr_14: Decimal
    average_volume_20: Decimal
    average_turnover_20: Decimal
    momentum_20: Decimal
    momentum_60: Decimal
    momentum_120: Decimal
    high_distance_20: Decimal
    high_distance_52_week: Decimal
    higher_high_low: Decimal
    atr_compression: Decimal
    volume_contraction: Decimal
    volume_expansion: Decimal
    weekly_trend: Decimal
    liquidity: Decimal
    overextension_atr: Decimal
    volume_hangover: Decimal
    evidence: tuple[RankEvidence, ...]


class CandidateRanker:
    """Pure two-pass ranker with benchmark or cross-sectional strength."""

    def __init__(
        self,
        config: CandidateRankerConfig = DEFAULT_CANDIDATE_RANKER_CONFIG,
    ) -> None:
        self.config = config

    def rank(
        self,
        candidates: Sequence[CandidateMetadata],
        bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
        *,
        as_of: datetime,
        allowed_markets: frozenset[str] = _SUPPORTED_MARKETS,
        benchmark_returns_60: Mapping[str, BenchmarkReturn] | None = None,
        benchmark_returns_60_by_candidate: (
            Mapping[CandidateKey, BenchmarkReturn] | None
        ) = None,
    ) -> CandidateRankingBatch:
        current = _aware_utc(as_of, "as_of")
        allowed = normalize_allowed_markets(allowed_markets)
        benchmark = _normalized_benchmarks(
            benchmark_returns_60,
            as_of=current,
            maximum_age=self.config.maximum_bar_age,
        )
        candidate_benchmark = _normalized_candidate_benchmarks(
            benchmark_returns_60_by_candidate,
            as_of=current,
            maximum_age=self.config.maximum_bar_age,
        )
        prepared: list[_PreparedCandidate] = []
        excluded: list[CandidateRankResult] = []

        for metadata in candidates:
            if metadata.market not in allowed:
                excluded.append(
                    _excluded_result(metadata, "market_not_allowed", data_as_of=None)
                )
                continue
            bars = bars_by_candidate.get(metadata.key, ())
            candidate, reason, data_as_of = self._prepare(
                metadata,
                bars,
                as_of=current,
            )
            if reason is not None or candidate is None:
                excluded.append(
                    _excluded_result(
                        metadata,
                        reason or "invalid_data",
                        data_as_of,
                    )
                )
                continue
            prepared.append(candidate)

        cross_sectional = _cross_sectional_strength(prepared)
        ranked = [
            self._score(
                item,
                benchmark_return=(
                    candidate_benchmark.get(item.metadata.key)
                    or benchmark.get(item.metadata.market)
                ),
                cross_sectional_score=cross_sectional[item.metadata.key],
            )
            for item in prepared
        ]
        ranked.sort(key=lambda item: (-item.total_score, item.symbol, item.market))
        ranked_total = len(ranked)
        ranked = [
            replace(item, rank_position=index, ranked_total=ranked_total)
            for index, item in enumerate(ranked, start=1)
        ]
        excluded.sort(key=lambda item: (item.symbol, item.market))
        return CandidateRankingBatch(ranked=tuple(ranked), excluded=tuple(excluded))

    def _prepare(
        self,
        metadata: CandidateMetadata,
        bars: Sequence[PriceBar],
        *,
        as_of: datetime,
    ) -> tuple[_PreparedCandidate | None, str | None, datetime | None]:
        normalized: list[PriceBar] = []
        for bar in bars:
            try:
                timestamp = _aware_utc(bar.timestamp, "bar timestamp")
            except ValueError:
                return None, "invalid_bar_timestamp", None
            if timestamp > as_of:
                return None, "future_bar", timestamp
            if not _valid_ohlcv(bar):
                return None, "invalid_ohlcv", timestamp
            normalized.append(
                PriceBar(
                    timestamp=timestamp,
                    open=_decimal(bar.open),
                    high=_decimal(bar.high),
                    low=_decimal(bar.low),
                    close=_decimal(bar.close),
                    volume=_decimal(bar.volume),
                )
            )
        normalized.sort(key=lambda bar: bar.timestamp)
        timestamps = [bar.timestamp for bar in normalized]
        if len(set(timestamps)) != len(timestamps):
            return None, "duplicate_bar_timestamp", timestamps[-1]
        if len(normalized) < self.config.minimum_history_bars:
            data_as_of = normalized[-1].timestamp if normalized else None
            return None, "insufficient_history", data_as_of
        bounded = tuple(normalized[-self.config.history_bars :])
        data_as_of = bounded[-1].timestamp
        if as_of - data_as_of >= self.config.maximum_bar_age:
            return None, "stale_bar", data_as_of

        minimum_price = (
            self.config.minimum_price_kr
            if metadata.market == "KR"
            else self.config.minimum_price_us
        )
        if bounded[-1].close < minimum_price:
            return None, "minimum_price", data_as_of

        turnover = _candidate_turnover(metadata, bounded)
        if turnover is None:
            return None, "invalid_turnover", data_as_of
        minimum_turnover = (
            self.config.minimum_turnover_kr
            if metadata.market == "KR"
            else self.config.minimum_turnover_us
        )
        if turnover < minimum_turnover:
            return None, "minimum_turnover", data_as_of

        closes = tuple(bar.close for bar in bounded)
        highs = tuple(bar.high for bar in bounded)
        lows = tuple(bar.low for bar in bounded)
        volumes = tuple(bar.volume for bar in bounded)
        average_volume_20 = _mean(volumes[-20:])
        average_turnover_20 = _mean(
            tuple(
                close * volume
                for close, volume in zip(closes[-20:], volumes[-20:], strict=True)
            )
        )
        true_ranges = _true_ranges(bounded)
        momentum_20 = _return_over(closes, 20)
        momentum_60 = _return_over(closes, 60)
        momentum_120 = _return_over(closes, 120)
        high_20 = max(highs[-20:])
        high_52_week = max(highs[-252:])
        high_distance_20 = max(_ZERO, (high_20 - closes[-1]) / high_20)
        high_distance_52_week = max(
            _ZERO,
            (high_52_week - closes[-1]) / high_52_week,
        )
        recent_high = max(highs[-20:])
        prior_high = max(highs[-40:-20])
        recent_low = min(lows[-20:])
        prior_low = min(lows[-40:-20])
        higher_high_low = (Decimal("0.5") if recent_high > prior_high else _ZERO) + (
            Decimal("0.5") if recent_low > prior_low else _ZERO
        )

        recent_atr = _mean(true_ranges[-14:])
        prior_atr = _mean(true_ranges[-74:-14])
        recent_atr_ratio = recent_atr / closes[-1]
        prior_reference_close = _mean(closes[-74:-14])
        prior_atr_ratio = prior_atr / prior_reference_close
        atr_compression = _clamp_unit(
            (prior_atr_ratio - recent_atr_ratio)
            / max(prior_atr_ratio * Decimal("0.50"), Decimal("0.000001"))
        )

        expansion_days = 3
        contraction_days = 5
        setup_end = len(volumes) - expansion_days
        setup_start = setup_end - contraction_days
        baseline_start = setup_start - 20
        baseline_volume = _mean(volumes[baseline_start:setup_start])
        setup_volume = _mean(volumes[setup_start:setup_end])
        recent_volume = _mean(volumes[-expansion_days:])
        if baseline_volume <= 0:
            volume_contraction = _ZERO
            volume_expansion = _ZERO
        else:
            volume_contraction = _clamp_unit(
                (_ONE - setup_volume / baseline_volume) / Decimal("0.50")
            )
            volume_expansion = _clamp_unit(
                (recent_volume / baseline_volume - _ONE) / Decimal("1.50")
            )
        weekly_trend, weekly_count = _weekly_trend(bounded, metadata.market)
        liquidity = _clamp_unit((turnover / minimum_turnover - _ONE) / Decimal("9"))

        breakout_line = max(highs[-21:-1])
        current_atr = max(recent_atr, Decimal("0.000001"))
        overextension_atr = max(_ZERO, (closes[-1] - breakout_line) / current_atr)
        volume_hangover, abnormal_ratio = _volume_hangover(
            bounded,
            abnormal_ratio=self.config.abnormal_volume_ratio,
            hangover_days=self.config.volume_hangover_days,
        )
        valid_until = min(
            as_of + self.config.validity,
            data_as_of + self.config.maximum_bar_age,
        )
        evidence = (
            RankEvidence("history_bars", str(len(bounded)), "repository daily bars"),
            RankEvidence("turnover", _text(turnover), "screener or 20-day mean"),
            RankEvidence("high_20", _text(high_20), "20-session high"),
            RankEvidence(
                "high_52_week",
                _text(high_52_week),
                "252-session high without future bars",
            ),
            RankEvidence("atr_14", _text(recent_atr), "14-session true range mean"),
            RankEvidence(
                "average_volume_20",
                _text(average_volume_20),
                "20-session mean volume for participation sizing",
            ),
            RankEvidence(
                "average_turnover_20",
                _text(average_turnover_20),
                "20-session mean turnover for participation sizing",
            ),
            RankEvidence("weekly_bars", str(weekly_count), "calendar weekly closes"),
            RankEvidence(
                "abnormal_volume_ratio",
                _text(abnormal_ratio),
                "largest recent completed volume versus baseline",
            ),
        )
        return (
            _PreparedCandidate(
                metadata=metadata,
                bars=bounded,
                data_as_of=data_as_of,
                valid_until=valid_until,
                turnover=turnover,
                atr_14=recent_atr,
                average_volume_20=average_volume_20,
                average_turnover_20=average_turnover_20,
                momentum_20=momentum_20,
                momentum_60=momentum_60,
                momentum_120=momentum_120,
                high_distance_20=high_distance_20,
                high_distance_52_week=high_distance_52_week,
                higher_high_low=higher_high_low,
                atr_compression=atr_compression,
                volume_contraction=volume_contraction,
                volume_expansion=volume_expansion,
                weekly_trend=weekly_trend,
                liquidity=liquidity,
                overextension_atr=overextension_atr,
                volume_hangover=volume_hangover,
                evidence=evidence,
            ),
            None,
            data_as_of,
        )

    def _score(
        self,
        item: _PreparedCandidate,
        *,
        benchmark_return: BenchmarkReturn | None,
        cross_sectional_score: Decimal,
    ) -> CandidateRankResult:
        if benchmark_return is None:
            relative_strength_raw = item.momentum_60
            relative_strength_score = cross_sectional_score
            strength_source = "cross_sectional_60_session_percentile"
        else:
            relative_strength_raw = item.momentum_60 - benchmark_return.return_60
            relative_strength_score = _scaled_return(relative_strength_raw)
            strength_source = "benchmark_excess_60_session_return"

        raw_factors = (
            ("liquidity", item.turnover, item.liquidity, self.config.liquidity_weight),
            (
                "relative_strength",
                relative_strength_raw,
                relative_strength_score,
                self.config.relative_strength_weight,
            ),
            (
                "momentum_20",
                item.momentum_20,
                _scaled_return(item.momentum_20),
                self.config.momentum_20_weight,
            ),
            (
                "momentum_60",
                item.momentum_60,
                _scaled_return(item.momentum_60),
                self.config.momentum_60_weight,
            ),
            (
                "momentum_120",
                item.momentum_120,
                _scaled_return(item.momentum_120),
                self.config.momentum_120_weight,
            ),
            (
                "high_distance_20",
                item.high_distance_20,
                _clamp_unit(_ONE - item.high_distance_20 / Decimal("0.15")),
                self.config.high_distance_20_weight,
            ),
            (
                "high_distance_52_week",
                item.high_distance_52_week,
                _clamp_unit(_ONE - item.high_distance_52_week / Decimal("0.30")),
                self.config.high_distance_52_week_weight,
            ),
            (
                "higher_high_higher_low",
                item.higher_high_low,
                item.higher_high_low,
                self.config.higher_high_low_weight,
            ),
            (
                "atr_compression",
                item.atr_compression,
                item.atr_compression,
                self.config.atr_compression_weight,
            ),
            (
                "pre_breakout_volume_contraction",
                item.volume_contraction,
                item.volume_contraction,
                self.config.volume_contraction_weight,
            ),
            (
                "recent_volume_expansion",
                item.volume_expansion,
                item.volume_expansion,
                self.config.volume_expansion_weight,
            ),
            (
                "weekly_trend",
                item.weekly_trend,
                item.weekly_trend,
                self.config.weekly_trend_weight,
            ),
        )
        factors = tuple(
            FactorScore(
                code=code,
                raw_value=raw,
                score=_quantize(score),
                weight=weight,
                contribution=_quantize(score * weight),
            )
            for code, raw, score, weight in raw_factors
        )
        overextension_score = _clamp_unit(
            (item.overextension_atr - Decimal("1.5")) / Decimal("2")
        )
        penalties = (
            PenaltyScore(
                code="breakout_overextension",
                raw_value=_quantize(item.overextension_atr),
                score=_quantize(overextension_score),
                weight=self.config.overextension_penalty_weight,
                deduction=_quantize(
                    overextension_score * self.config.overextension_penalty_weight
                ),
            ),
            PenaltyScore(
                code="abnormal_volume_hangover",
                raw_value=_quantize(item.volume_hangover),
                score=_quantize(item.volume_hangover),
                weight=self.config.volume_hangover_penalty_weight,
                deduction=_quantize(
                    item.volume_hangover * self.config.volume_hangover_penalty_weight
                ),
            ),
        )
        total = sum((factor.contribution for factor in factors), start=_ZERO)
        total -= sum((penalty.deduction for penalty in penalties), start=_ZERO)
        evidence = item.evidence + (
            RankEvidence(
                "relative_strength_source",
                strength_source,
                "benchmark is preferred; cross-section is deterministic fallback",
            ),
        )
        if benchmark_return is not None:
            evidence += (
                RankEvidence(
                    "relative_strength_benchmark",
                    benchmark_return.benchmark_symbol or benchmark_return.market,
                    "benchmark identity used for 60-session excess return",
                ),
                RankEvidence(
                    "relative_strength_benchmark_return_60",
                    _text(benchmark_return.return_60),
                    "completed benchmark 60-session return",
                ),
                RankEvidence(
                    "relative_strength_benchmark_data_as_of",
                    _timestamp_text(benchmark_return.data_as_of) or "",
                    "latest completed benchmark observation",
                ),
            )
        return CandidateRankResult(
            symbol=item.metadata.symbol,
            market=item.metadata.market,
            total_score=_quantize(max(_ZERO, total)),
            factor_scores=factors,
            penalties=penalties,
            data_as_of=item.data_as_of,
            valid_until=item.valid_until,
            exclusion_reason=None,
            atr_14=item.atr_14,
            average_volume_20=item.average_volume_20,
            average_turnover_20=item.average_turnover_20,
            evidence=evidence,
            sources=item.metadata.sources,
            is_held=item.metadata.is_held,
            is_watchlisted=item.metadata.is_watchlisted,
            eligible_for_new_buy=item.metadata.eligible_for_new_buy,
        )


def normalize_allowed_markets(markets: frozenset[str]) -> frozenset[str]:
    normalized = frozenset(str(value).strip().upper() for value in markets)
    if not normalized or not normalized <= _SUPPORTED_MARKETS:
        raise ValueError("allowed_markets must be a non-empty subset of KR and US")
    return normalized


def cap_candidate_universe(
    candidates: Sequence[CandidateMetadata],
    *,
    limit: int,
) -> tuple[CandidateMetadata, ...]:
    """Apply the candidate cap without ever dropping a current PAPER holding."""

    if limit < 1:
        raise ValueError("candidate universe limit must be positive")
    selected: list[CandidateMetadata] = []
    selected_keys: set[CandidateKey] = set()
    nonheld_count = 0
    for candidate in candidates:
        if candidate.key in selected_keys:
            continue
        if not candidate.is_held:
            if nonheld_count >= limit:
                continue
            nonheld_count += 1
        selected.append(candidate)
        selected_keys.add(candidate.key)
    return tuple(selected)


def _excluded_result(
    metadata: CandidateMetadata,
    reason: str,
    data_as_of: datetime | None,
) -> CandidateRankResult:
    return CandidateRankResult(
        symbol=metadata.symbol,
        market=metadata.market,
        total_score=_ZERO,
        factor_scores=(),
        penalties=(),
        data_as_of=data_as_of,
        valid_until=None,
        exclusion_reason=reason,
        atr_14=None,
        average_volume_20=None,
        average_turnover_20=None,
        evidence=(RankEvidence("exclusion", reason, "hard filter"),),
        sources=metadata.sources,
        is_held=metadata.is_held,
        is_watchlisted=metadata.is_watchlisted,
        eligible_for_new_buy=metadata.eligible_for_new_buy,
    )


def _normalized_benchmarks(
    values: Mapping[str, BenchmarkReturn] | None,
    *,
    as_of: datetime,
    maximum_age: timedelta,
) -> dict[str, BenchmarkReturn]:
    normalized: dict[str, BenchmarkReturn] = {}
    for market, value in (values or {}).items():
        key = str(market).strip().upper()
        try:
            data_as_of = _aware_utc(value.data_as_of, "benchmark data_as_of")
        except ValueError:
            continue
        if (
            key != value.market
            or key not in _SUPPORTED_MARKETS
            or not value.return_60.is_finite()
            or data_as_of > as_of
            or as_of - data_as_of >= maximum_age
        ):
            continue
        normalized[key] = value
    return normalized


def _normalized_candidate_benchmarks(
    values: Mapping[CandidateKey, BenchmarkReturn] | None,
    *,
    as_of: datetime,
    maximum_age: timedelta,
) -> dict[CandidateKey, BenchmarkReturn]:
    normalized: dict[CandidateKey, BenchmarkReturn] = {}
    for raw_key, value in (values or {}).items():
        try:
            market, raw_symbol = raw_key
        except (TypeError, ValueError):
            continue
        key = (str(market).strip().upper(), str(raw_symbol).strip().upper())
        try:
            data_as_of = _aware_utc(value.data_as_of, "benchmark data_as_of")
        except ValueError:
            continue
        if (
            key[0] not in _SUPPORTED_MARKETS
            or not key[1]
            or value.market != key[0]
            or not value.return_60.is_finite()
            or data_as_of > as_of
            or as_of - data_as_of >= maximum_age
        ):
            continue
        normalized[key] = value
    return normalized


def _cross_sectional_strength(
    candidates: Sequence[_PreparedCandidate],
) -> dict[CandidateKey, Decimal]:
    output: dict[CandidateKey, Decimal] = {}
    for market in ("KR", "US"):
        market_candidates = [
            item for item in candidates if item.metadata.market == market
        ]
        unique_returns = sorted({item.momentum_60 for item in market_candidates})
        if len(unique_returns) <= 1:
            scores = {value: Decimal("0.5") for value in unique_returns}
        else:
            denominator = Decimal(len(unique_returns) - 1)
            scores = {
                value: Decimal(index) / denominator
                for index, value in enumerate(unique_returns)
            }
        for item in market_candidates:
            output[item.metadata.key] = scores[item.momentum_60]
    return output


def _candidate_turnover(
    metadata: CandidateMetadata,
    bars: Sequence[PriceBar],
) -> Decimal | None:
    if metadata.screener_turnover is not None:
        value = metadata.screener_turnover
        return value if value.is_finite() and value >= 0 else None
    turnovers = tuple(bar.close * bar.volume for bar in bars[-20:])
    value = _mean(turnovers)
    return value if value.is_finite() and value >= 0 else None


def _valid_ohlcv(bar: PriceBar) -> bool:
    try:
        open_price = _decimal(bar.open)
        high = _decimal(bar.high)
        low = _decimal(bar.low)
        close = _decimal(bar.close)
        volume = _decimal(bar.volume)
    except (InvalidOperation, TypeError, ValueError):
        return False
    values = (open_price, high, low, close, volume)
    if any(not value.is_finite() for value in values):
        return False
    if min(open_price, high, low, close) <= 0 or volume < 0:
        return False
    return low <= min(open_price, close) <= max(open_price, close) <= high


def _true_ranges(bars: Sequence[PriceBar]) -> tuple[Decimal, ...]:
    output: list[Decimal] = []
    previous_close: Decimal | None = None
    for bar in bars:
        if previous_close is None:
            value = bar.high - bar.low
        else:
            value = max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        output.append(value)
        previous_close = bar.close
    return tuple(output)


def _weekly_trend(
    bars: Sequence[PriceBar],
    _market: MarketKey,
) -> tuple[Decimal, int]:
    weekly: dict[tuple[int, int], Decimal] = {}
    for bar in bars:
        session_date = bar.timestamp.astimezone(UTC).date()
        iso_year, iso_week, _ = session_date.isocalendar()
        weekly[(iso_year, iso_week)] = bar.close
    closes = tuple(weekly.values())
    if len(closes) < 10:
        return _ZERO, len(closes)
    moving_average = _mean(closes[-10:])
    spread_score = _clamp_unit(
        (closes[-1] / moving_average - Decimal("0.95")) / Decimal("0.20")
    )
    return_4_week = closes[-1] / closes[-5] - _ONE if len(closes) >= 5 else _ZERO
    return_score = _clamp_unit((return_4_week + Decimal("0.10")) / Decimal("0.30"))
    return (spread_score + return_score) / Decimal("2"), len(closes)


def _volume_hangover(
    bars: Sequence[PriceBar],
    *,
    abnormal_ratio: Decimal,
    hangover_days: int,
) -> tuple[Decimal, Decimal]:
    volumes = tuple(bar.volume for bar in bars)
    baseline = _mean(volumes[-40 : -(hangover_days + 1)])
    recent_start = len(volumes) - hangover_days - 1
    recent = volumes[recent_start:-1]
    if baseline <= 0 or not recent:
        return _ZERO, _ZERO
    spike_offset, spike_volume = max(
        enumerate(recent),
        key=lambda value: (value[1], -value[0]),
    )
    ratio = spike_volume / baseline
    if ratio < abnormal_ratio:
        return _ZERO, ratio
    spike_index = recent_start + spike_offset
    current = bars[-1]
    spike = bars[spike_index]
    current_ratio = current.volume / baseline
    unsettled = current_ratio >= Decimal("1.5") or current.close < spike.high
    if not unsettled:
        return _ZERO, ratio
    ratio_score = _clamp_unit((ratio - abnormal_ratio) / abnormal_ratio)
    recency = Decimal(hangover_days - (len(bars) - 1 - spike_index) + 1)
    recency_score = _clamp_unit(recency / Decimal(hangover_days))
    return max(Decimal("0.25"), ratio_score) * recency_score, ratio


def _return_over(values: Sequence[Decimal], periods: int) -> Decimal:
    return values[-1] / values[-(periods + 1)] - _ONE


def _scaled_return(value: Decimal) -> Decimal:
    return _clamp_unit((value + Decimal("0.20")) / Decimal("0.60"))


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean requires at least one value")
    return sum(values, start=_ZERO) / Decimal(len(values))


def _clamp_unit(value: Decimal) -> Decimal:
    return min(_ONE, max(_ZERO, value))


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _text(value: Decimal) -> str:
    try:
        normalized = _quantize(value)
    except InvalidOperation:
        normalized = value
    return format(normalized, "f")


def _optional_text(value: Decimal | None) -> str | None:
    return _text(value) if value is not None else None


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC).replace(microsecond=0)


__all__ = [
    "BenchmarkReturn",
    "CandidateKey",
    "CandidateMetadata",
    "CandidateRankResult",
    "CandidateRanker",
    "CandidateRankerConfig",
    "CandidateRankingBatch",
    "DEFAULT_CANDIDATE_RANKER_CONFIG",
    "FactorScore",
    "MarketKey",
    "PenaltyScore",
    "RankEvidence",
    "cap_candidate_universe",
    "normalize_allowed_markets",
]
