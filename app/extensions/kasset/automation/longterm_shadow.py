"""KRX 장기 추세·재무성장 SHADOW 2후보 판정과 코호트 성과 집계(순수 함수, DB·시계 없음).

주문·추천·승격 경로와 연결되지 않는다. 판정 입력은 호출자가 이미 완료로 확정한
세션(``signal_session``)까지의 일봉, 거래소 calendar 세션, 그 세션까지 공시된 분기
재무다. ``signal_session`` 뒤의 봉·공시는 계산에 쓰지 않는다.

후보(v1):

* ``trend_momentum`` — 공통 추세 필터를 통과한 종목을 12-1 모멘텀 내림차순 상위 N개.
* ``quality_growth_trend`` — 같은 추세 필터에 TTM 순이익·매출 성장 조건을 더하고
  같은 순위로 상위 N개.

공통 추세 필터(위에서부터 처음 어긋나는 것이 탈락 사유): 종가 > SMA200, SMA200 >
20세션 전 SMA200, SMA50 > SMA200, 12-1 모멘텀(21세션 전 종가 / 252세션 전 종가 − 1) > 0.

성과는 신호일 종가가 아니라 다음 유효 거래일 시가의 가상 진입 기준이며, 같은 세션에
뽑힌 종목들을 한 코호트로 묶어 동일가중 평균을 같은 세션 평가 종목 전체의 동일가중
평균(벤치마크)과 비교한다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from statistics import median
from typing import Literal

from app.extensions.kasset.automation.swing_shadow import (
    HorizonOutcome,
    OutcomeStatus,
    SwingBar,
    decimal_text,
    screen_bars,
)

LONGTERM_SHADOW_SCHEMA_VERSION = "kasset.longterm-shadow.v1"
LONGTERM_SHADOW_CONFIG_SCHEMA_VERSION = "kasset.longterm-shadow-config.v1"
LONGTERM_SHADOW_REPORT_SCHEMA_VERSION = "kasset.longterm-shadow-report.v1"
LONGTERM_SHADOW_MARKET: Literal["KRX"] = "KRX"

_ZERO = Decimal("0")
_ONE = Decimal("1")


class LongtermCandidate(StrEnum):
    TREND_MOMENTUM = "trend_momentum"
    QUALITY_GROWTH_TREND = "quality_growth_trend"


LONGTERM_CANDIDATES: tuple[LongtermCandidate, ...] = tuple(LongtermCandidate)


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=_ZERO) / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class LongtermShadowConfig:
    """장기 SHADOW 전용 불변 설정. 스윙 SHADOW·활성 전략 artifact와 지문을 공유하지 않는다."""

    lookback_sessions: int = 260
    min_history_sessions: int = 253
    min_close: Decimal = Decimal("1000")
    turnover_sessions: int = 20
    min_average_turnover: Decimal = Decimal("1000000000")
    max_abs_session_move: Decimal = Decimal("0.35")
    sma_long_sessions: int = 200
    sma_mid_sessions: int = 50
    sma_slope_sessions: int = 20
    momentum_skip_sessions: int = 21
    momentum_base_sessions: int = 252
    top_n: int = 20
    ttm_quarters: int = 4
    min_growth: Decimal = Decimal("0.10")
    fundamentals_max_staleness_days: int = 200
    horizons: tuple[int, ...] = (20, 60, 120)
    buy_fee_rate: Decimal = Decimal("0.00015")
    sell_fee_rate: Decimal = Decimal("0.00015")
    sell_tax_rate: Decimal = Decimal("0.0018")
    slippage_rate: Decimal = Decimal("0.001")
    min_mature_cohorts: int = 12

    def __post_init__(self) -> None:
        for name in (
            "min_close",
            "min_average_turnover",
            "max_abs_session_move",
            "min_growth",
            "buy_fee_rate",
            "sell_fee_rate",
            "sell_tax_rate",
            "slippage_rate",
        ):
            value = _decimal(getattr(self, name))
            if not value.is_finite() or value < _ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        for name in (
            "lookback_sessions",
            "min_history_sessions",
            "turnover_sessions",
            "sma_long_sessions",
            "sma_mid_sessions",
            "sma_slope_sessions",
            "momentum_skip_sessions",
            "momentum_base_sessions",
            "top_n",
            "ttm_quarters",
            "fundamentals_max_staleness_days",
            "min_mature_cohorts",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.sma_mid_sessions >= self.sma_long_sessions:
            raise ValueError("mid SMA must be shorter than long SMA")
        if self.momentum_skip_sessions >= self.momentum_base_sessions:
            raise ValueError("momentum skip must be shorter than momentum base")
        if self.min_history_sessions < self.required_history_sessions:
            raise ValueError("min_history_sessions does not cover every indicator")
        if self.lookback_sessions < max(
            self.min_history_sessions, self.turnover_sessions
        ):
            raise ValueError("lookback_sessions must cover min_history_sessions")
        if not self.horizons or any(h < 1 for h in self.horizons):
            raise ValueError("horizons must be positive trading-session counts")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be strictly increasing")

    @property
    def required_history_sessions(self) -> int:
        """지표 계산에 필요한 완료 세션 수(신호 세션 포함)."""

        return max(
            self.momentum_base_sessions + 1,
            self.sma_long_sessions + self.sma_slope_sessions,
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": LONGTERM_SHADOW_CONFIG_SCHEMA_VERSION,
            "signalSchemaVersion": LONGTERM_SHADOW_SCHEMA_VERSION,
            "market": LONGTERM_SHADOW_MARKET,
            "candidates": [item.value for item in LONGTERM_CANDIDATES],
            "lookbackSessions": self.lookback_sessions,
            "minHistorySessions": self.min_history_sessions,
            "minClose": decimal_text(self.min_close),
            "turnoverSessions": self.turnover_sessions,
            "minAverageTurnover": decimal_text(self.min_average_turnover),
            "maxAbsSessionMove": decimal_text(self.max_abs_session_move),
            "smaLongSessions": self.sma_long_sessions,
            "smaMidSessions": self.sma_mid_sessions,
            "smaSlopeSessions": self.sma_slope_sessions,
            "momentumSkipSessions": self.momentum_skip_sessions,
            "momentumBaseSessions": self.momentum_base_sessions,
            "topN": self.top_n,
            "ttmQuarters": self.ttm_quarters,
            "minGrowth": decimal_text(self.min_growth),
            "fundamentalsMaxStalenessDays": self.fundamentals_max_staleness_days,
            "horizons": list(self.horizons),
            "buyFeeRate": decimal_text(self.buy_fee_rate),
            "sellFeeRate": decimal_text(self.sell_fee_rate),
            "sellTaxRate": decimal_text(self.sell_tax_rate),
            "slippageRate": decimal_text(self.slippage_rate),
            "minMatureCohorts": self.min_mature_cohorts,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return sha256(encoded).hexdigest()


DEFAULT_LONGTERM_SHADOW_CONFIG = LongtermShadowConfig()


# ---- 추세·모멘텀 -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendMetrics:
    signal_bar: SwingBar
    sma50: Decimal
    sma200: Decimal
    sma200_prior: Decimal
    price_skip: Decimal
    price_base: Decimal
    momentum: Decimal

    def evidence(self) -> dict[str, object]:
        return {
            "close": decimal_text(self.signal_bar.close),
            "sma50": decimal_text(self.sma50),
            "sma200": decimal_text(self.sma200),
            "sma200Prior": decimal_text(self.sma200_prior),
            "priceSkip": decimal_text(self.price_skip),
            "priceBase": decimal_text(self.price_base),
            "momentum12m1": decimal_text(self.momentum),
        }


@dataclass(frozen=True, slots=True)
class LongtermSymbolEvaluation:
    symbol: str
    excluded_reason: str | None
    trend_reason: str | None = None
    metrics: TrendMetrics | None = None
    future_bars_ignored: int = 0

    @property
    def evaluated(self) -> bool:
        """대상·품질 필터를 통과해 후보 판정까지 간 종목(벤치마크 구성원)."""

        return self.excluded_reason is None


def _sma(values: Sequence[Decimal], period: int, end: int) -> Decimal:
    """``values[end - period + 1 : end + 1]`` 단순평균."""

    return _mean(values[end - period + 1 : end + 1])


def evaluate_longterm_symbol(
    bars: Sequence[SwingBar],
    *,
    symbol: str,
    signal_session: date,
    calendar_sessions: Sequence[date],
    config: LongtermShadowConfig = DEFAULT_LONGTERM_SHADOW_CONFIG,
) -> LongtermSymbolEvaluation:
    """완료 세션 ``signal_session``까지의 일봉으로 공통 추세 필터와 모멘텀을 계산한다."""

    normalized = symbol.strip().upper()
    screen = screen_bars(
        bars,
        signal_session=signal_session,
        calendar_sessions=calendar_sessions,
        config=config,
        min_history_sessions=config.min_history_sessions,
    )
    if screen.excluded_reason is not None:
        return LongtermSymbolEvaluation(
            symbol=normalized,
            excluded_reason=screen.excluded_reason,
            future_bars_ignored=screen.future_bars_ignored,
        )
    window = screen.window
    closes = [bar.close for bar in window]
    last = len(closes) - 1
    metrics = TrendMetrics(
        signal_bar=window[-1],
        sma50=_sma(closes, config.sma_mid_sessions, last),
        sma200=_sma(closes, config.sma_long_sessions, last),
        sma200_prior=_sma(
            closes, config.sma_long_sessions, last - config.sma_slope_sessions
        ),
        price_skip=closes[last - config.momentum_skip_sessions],
        price_base=closes[last - config.momentum_base_sessions],
        momentum=closes[last - config.momentum_skip_sessions]
        / closes[last - config.momentum_base_sessions]
        - _ONE,
    )
    if metrics.signal_bar.close <= metrics.sma200:
        reason: str | None = "below_sma200"
    elif metrics.sma200 <= metrics.sma200_prior:
        reason = "sma200_not_rising"
    elif metrics.sma50 <= metrics.sma200:
        reason = "sma50_not_above_sma200"
    elif metrics.momentum <= _ZERO:
        reason = "momentum_not_positive"
    else:
        reason = None
    return LongtermSymbolEvaluation(
        symbol=normalized,
        excluded_reason=None,
        trend_reason=reason,
        metrics=metrics,
        future_bars_ignored=screen.future_bars_ignored,
    )


# ---- 재무 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarterFact:
    """``financial_fundamentals_snapshots`` 분기 한 행. 단일 분기(discrete) 값만 쓴다."""

    fiscal_period: str
    period_end_date: date
    filing_date: date | None
    source: str | None
    discrete_revenue: Decimal | None
    discrete_net_income: Decimal | None


@dataclass(frozen=True, slots=True)
class FundamentalsEvaluation:
    reason: str | None
    evidence: dict[str, object]


def _month_index(day: date) -> int:
    return day.year * 12 + day.month


def evaluate_fundamentals(
    facts: Sequence[QuarterFact],
    *,
    signal_session: date,
    config: LongtermShadowConfig = DEFAULT_LONGTERM_SHADOW_CONFIG,
) -> FundamentalsEvaluation:
    """``signal_session``까지 공시된 분기만으로 TTM 순이익·매출 성장 조건을 판정한다.

    같은 ``period_end_date``가 여러 행이면 공시일이 가장 늦은 행(동률은 source)을 쓴다.
    최근 ``2 × ttm_quarters``개 분기가 3개월 간격으로 이어져야 하고 모두 단일 분기
    매출·순이익이 있어야 한다.
    """

    visible: dict[date, QuarterFact] = {}
    for fact in facts:
        if fact.filing_date is None or fact.filing_date > signal_session:
            continue
        current = visible.get(fact.period_end_date)
        if current is None or (fact.filing_date, fact.source or "") > (
            current.filing_date or date.min,
            current.source or "",
        ):
            visible[fact.period_end_date] = fact
    if not visible:
        return FundamentalsEvaluation("fundamentals_missing", {})
    needed = config.ttm_quarters * 2
    recent = sorted(visible.values(), key=lambda item: item.period_end_date)[::-1][
        :needed
    ]
    evidence: dict[str, object] = {
        "visibleQuarters": len(visible),
        "quarters": [
            {
                "fiscalPeriod": item.fiscal_period,
                "periodEnd": item.period_end_date.isoformat(),
                "filingDate": item.filing_date.isoformat()
                if item.filing_date
                else None,
                "source": item.source,
                "discreteRevenue": decimal_text(item.discrete_revenue)
                if item.discrete_revenue is not None
                else None,
                "discreteNetIncome": decimal_text(item.discrete_net_income)
                if item.discrete_net_income is not None
                else None,
            }
            for item in recent
        ],
    }
    if len(recent) < needed:
        return FundamentalsEvaluation("fundamentals_insufficient_quarters", evidence)
    if any(
        _month_index(newer.period_end_date) - _month_index(older.period_end_date) != 3
        for newer, older in zip(recent, recent[1:], strict=False)
    ):
        return FundamentalsEvaluation("fundamentals_quarter_gap", evidence)
    latest = recent[0].period_end_date
    if (signal_session - latest).days > config.fundamentals_max_staleness_days:
        return FundamentalsEvaluation("fundamentals_stale", evidence)
    if any(
        item.discrete_revenue is None or item.discrete_net_income is None
        for item in recent
    ):
        return FundamentalsEvaluation("fundamentals_discrete_missing", evidence)

    def total(items: Sequence[QuarterFact], field: str) -> Decimal:
        return sum((_decimal(getattr(item, field)) for item in items), start=_ZERO)

    ttm = recent[: config.ttm_quarters]
    prior = recent[config.ttm_quarters :]
    ttm_revenue = total(ttm, "discrete_revenue")
    prior_revenue = total(prior, "discrete_revenue")
    ttm_income = total(ttm, "discrete_net_income")
    prior_income = total(prior, "discrete_net_income")
    evidence.update(
        {
            "asOfSession": signal_session.isoformat(),
            "latestPeriodEnd": latest.isoformat(),
            "ttmRevenue": decimal_text(ttm_revenue),
            "priorTtmRevenue": decimal_text(prior_revenue),
            "ttmNetIncome": decimal_text(ttm_income),
            "priorTtmNetIncome": decimal_text(prior_income),
        }
    )
    if ttm_income <= _ZERO:
        return FundamentalsEvaluation(
            "fundamentals_ttm_net_income_not_positive", evidence
        )
    if prior_income <= _ZERO:
        return FundamentalsEvaluation(
            "fundamentals_prior_ttm_net_income_not_positive", evidence
        )
    if prior_revenue <= _ZERO:
        return FundamentalsEvaluation(
            "fundamentals_prior_ttm_revenue_not_positive", evidence
        )
    income_growth = ttm_income / prior_income - _ONE
    revenue_growth = ttm_revenue / prior_revenue - _ONE
    evidence["netIncomeGrowth"] = decimal_text(income_growth)
    evidence["revenueGrowth"] = decimal_text(revenue_growth)
    if income_growth < config.min_growth:
        return FundamentalsEvaluation(
            "fundamentals_net_income_growth_below_minimum", evidence
        )
    if revenue_growth < config.min_growth:
        return FundamentalsEvaluation(
            "fundamentals_revenue_growth_below_minimum", evidence
        )
    return FundamentalsEvaluation(None, evidence)


# ---- 코호트 선정 ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LongtermSignal:
    candidate: LongtermCandidate
    symbol: str
    signal_session: date
    rank: int
    metrics: TrendMetrics
    evidence: dict[str, object]

    @property
    def momentum(self) -> Decimal:
        return self.metrics.momentum


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    candidate: LongtermCandidate
    passed: int
    signals: tuple[LongtermSignal, ...]
    no_signal_reasons: dict[str, int]


def trend_passing_symbols(
    evaluations: Sequence[LongtermSymbolEvaluation],
) -> list[str]:
    """재무를 읽어야 하는 종목(공통 추세 필터와 모멘텀을 통과한 종목)."""

    return [
        item.symbol
        for item in evaluations
        if item.excluded_reason is None and item.trend_reason is None
    ]


def select_cohort(
    evaluations: Sequence[LongtermSymbolEvaluation],
    fundamentals: Mapping[str, Sequence[QuarterFact]],
    *,
    signal_session: date,
    config: LongtermShadowConfig = DEFAULT_LONGTERM_SHADOW_CONFIG,
) -> dict[LongtermCandidate, CandidateSelection]:
    """두 후보의 상위 ``top_n`` 종목을 정한다. 순위는 모멘텀 내림차순, 동률은 symbol 오름차순."""

    trend_reasons: Counter[str] = Counter()
    passing: list[LongtermSymbolEvaluation] = []
    for item in evaluations:
        if item.excluded_reason is not None:
            continue
        if item.trend_reason is not None:
            trend_reasons[item.trend_reason] += 1
        else:
            passing.append(item)

    def top(
        candidate: LongtermCandidate,
        eligible: list[tuple[LongtermSymbolEvaluation, dict[str, object]]],
        reasons: Counter[str],
    ) -> CandidateSelection:
        ranked = sorted(
            eligible,
            key=lambda pair: (-_metrics(pair[0]).momentum, pair[0].symbol),
        )
        selected = ranked[: config.top_n]
        if len(ranked) > len(selected):
            reasons["outside_top_n"] += len(ranked) - len(selected)
        return CandidateSelection(
            candidate=candidate,
            passed=len(ranked),
            signals=tuple(
                LongtermSignal(
                    candidate=candidate,
                    symbol=item.symbol,
                    signal_session=signal_session,
                    rank=rank,
                    metrics=_metrics(item),
                    evidence={"trend": _metrics(item).evidence(), **extra},
                )
                for rank, (item, extra) in enumerate(selected, start=1)
            ),
            no_signal_reasons=dict(sorted(reasons.items())),
        )

    trend_only = top(
        LongtermCandidate.TREND_MOMENTUM,
        [(item, {}) for item in passing],
        Counter(trend_reasons),
    )
    quality_reasons: Counter[str] = Counter(trend_reasons)
    quality: list[tuple[LongtermSymbolEvaluation, dict[str, object]]] = []
    for item in passing:
        result = evaluate_fundamentals(
            fundamentals.get(item.symbol, ()),
            signal_session=signal_session,
            config=config,
        )
        if result.reason is not None:
            quality_reasons[result.reason] += 1
        else:
            quality.append((item, {"fundamentals": result.evidence}))
    return {
        LongtermCandidate.TREND_MOMENTUM: trend_only,
        LongtermCandidate.QUALITY_GROWTH_TREND: top(
            LongtermCandidate.QUALITY_GROWTH_TREND, quality, quality_reasons
        ),
    }


def _metrics(item: LongtermSymbolEvaluation) -> TrendMetrics:
    if item.metrics is None:
        raise ValueError("evaluated symbol has no trend metrics")
    return item.metrics


# ---- 코호트 성과 집계 -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CohortSummary:
    """한 코호트(또는 벤치마크) 한 horizon의 동일가중 성과.

    ``state``: ``empty``(구성원 0), ``pending``(h번째 세션 미완료 구성원 있음),
    ``calendar_unavailable``, ``no_mature_members``, ``mature``. 통계에는 ``mature``만 쓴다.
    """

    state: str
    members: int
    status_counts: dict[str, int]
    mature_count: int
    mean_net: Decimal | None = None
    mean_gross: Decimal | None = None
    mean_mfe: Decimal | None = None
    mean_mae: Decimal | None = None


def summarize_cohort(outcomes: Sequence[HorizonOutcome]) -> CohortSummary:
    counts = {status.value: 0 for status in OutcomeStatus}
    for outcome in outcomes:
        counts[outcome.status.value] += 1
    mature = [item for item in outcomes if item.status is OutcomeStatus.MATURE]
    if not outcomes:
        state = "empty"
    elif counts[OutcomeStatus.PENDING.value]:
        state = "pending"
    elif counts[OutcomeStatus.CALENDAR_UNAVAILABLE.value]:
        state = "calendar_unavailable"
    elif not mature:
        state = "no_mature_members"
    else:
        state = "mature"
    if state != "mature":
        return CohortSummary(state, len(outcomes), counts, len(mature))
    nets = [item.net_return for item in mature if item.net_return is not None]
    grosses = [item.gross_return for item in mature if item.gross_return is not None]
    mfes = [item.mfe for item in mature if item.mfe is not None]
    maes = [item.mae for item in mature if item.mae is not None]
    return CohortSummary(
        state,
        len(outcomes),
        counts,
        len(mature),
        mean_net=_mean(nets),
        mean_gross=_mean(grosses),
        mean_mfe=_mean(mfes),
        mean_mae=_mean(maes),
    )


@dataclass(frozen=True, slots=True)
class CohortPair:
    session: date
    cohort: CohortSummary
    benchmark: CohortSummary

    @property
    def excess(self) -> Decimal | None:
        if (
            self.cohort.state == "mature"
            and self.benchmark.state == "mature"
            and self.cohort.mean_net is not None
            and self.benchmark.mean_net is not None
        ):
            return self.cohort.mean_net - self.benchmark.mean_net
        return None


def summarize_candidate_horizon(
    pairs: Sequence[CohortPair], *, min_mature_cohorts: int
) -> dict[str, object]:
    """한 후보·한 horizon의 코호트 통계. 표본 부족 표기는 연구 advisory일 뿐 gate가 아니다."""

    state_counts = Counter(pair.cohort.state for pair in pairs)
    benchmark_states = Counter(pair.benchmark.state for pair in pairs)
    usable = [pair for pair in pairs if pair.excess is not None]
    summary: dict[str, object] = {
        "cohorts": len(pairs),
        "cohortStateCounts": dict(sorted(state_counts.items())),
        "benchmarkStateCounts": dict(sorted(benchmark_states.items())),
        "matureCohorts": len(usable),
        "sampleAdvisory": (
            "insufficient_sample"
            if len(usable) < min_mature_cohorts
            else "sample_size_reached"
        ),
        "minMatureCohortsAdvisory": min_mature_cohorts,
    }
    if not usable:
        summary["stats"] = None
        return summary
    cohort_nets = [_required(pair.cohort.mean_net) for pair in usable]
    bench_nets = [_required(pair.benchmark.mean_net) for pair in usable]
    excess = [_required(pair.excess) for pair in usable]
    summary["stats"] = {
        "meanCohortNetReturn": decimal_text(_mean(cohort_nets)),
        "medianCohortNetReturn": decimal_text(Decimal(median(cohort_nets))),
        "meanBenchmarkNetReturn": decimal_text(_mean(bench_nets)),
        "meanExcessReturn": decimal_text(_mean(excess)),
        "medianExcessReturn": decimal_text(Decimal(median(excess))),
        "positiveExcessRatio": decimal_text(
            Decimal(sum(1 for value in excess if value > _ZERO)) / Decimal(len(excess))
        ),
    }
    return summary


def _required(value: Decimal | None) -> Decimal:
    if value is None:
        raise ValueError("mature cohort has no mean")
    return value


__all__ = [
    "DEFAULT_LONGTERM_SHADOW_CONFIG",
    "LONGTERM_CANDIDATES",
    "LONGTERM_SHADOW_CONFIG_SCHEMA_VERSION",
    "LONGTERM_SHADOW_MARKET",
    "LONGTERM_SHADOW_REPORT_SCHEMA_VERSION",
    "LONGTERM_SHADOW_SCHEMA_VERSION",
    "CandidateSelection",
    "CohortPair",
    "CohortSummary",
    "FundamentalsEvaluation",
    "LongtermCandidate",
    "LongtermShadowConfig",
    "LongtermSignal",
    "LongtermSymbolEvaluation",
    "QuarterFact",
    "TrendMetrics",
    "evaluate_fundamentals",
    "evaluate_longterm_symbol",
    "select_cohort",
    "summarize_candidate_horizon",
    "summarize_cohort",
    "trend_passing_symbols",
]
