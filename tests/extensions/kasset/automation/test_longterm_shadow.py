from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.extensions.kasset.automation.longterm_shadow import (
    DEFAULT_LONGTERM_SHADOW_CONFIG,
    CohortPair,
    LongtermCandidate,
    LongtermShadowConfig,
    LongtermSymbolEvaluation,
    QuarterFact,
    TrendMetrics,
    evaluate_fundamentals,
    evaluate_longterm_symbol,
    select_cohort,
    summarize_candidate_horizon,
    summarize_cohort,
)
from app.extensions.kasset.automation.longterm_shadow_service import (
    _forward_sessions,
)
from app.extensions.kasset.automation.swing_shadow import (
    HorizonOutcome,
    OutcomeStatus,
    SwingBar,
)

pytestmark = pytest.mark.unit

_D = Decimal
_VALUE = _D("5000000000")
_CONFIG = DEFAULT_LONGTERM_SHADOW_CONFIG


def _weekdays(count: int, start: date = date(2025, 1, 6)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _bars(days: list[date], closes: list[int]) -> list[SwingBar]:
    return [
        SwingBar(
            session_date=day,
            open=_D(close),
            high=_D(close + 10),
            low=_D(close - 10),
            close=_D(close),
            volume=_D(100000),
            value=_VALUE,
            source="toss",
        )
        for day, close in zip(days, closes, strict=True)
    ]


def _evaluate(closes: list[int], *, calendar: int = 260):
    days = _weekdays(calendar)
    return evaluate_longterm_symbol(
        _bars(days[-len(closes) :], closes),
        symbol="005930",
        signal_session=days[-1],
        calendar_sessions=days,
    )


def _uptrend(count: int = 260, slope: int = 10) -> list[int]:
    return [10000 + slope * i for i in range(count)]


# ---- 추세·모멘텀 -----------------------------------------------------------


def test_steady_uptrend_passes_with_12_1_momentum_from_21_and_252_sessions_back() -> (
    None
):
    result = _evaluate(_uptrend())

    assert result.excluded_reason is None
    assert result.trend_reason is None
    assert result.metrics is not None
    # 마지막 세션 index 259 기준: 21세션 전 = 238, 252세션 전 = 7.
    assert result.metrics.price_skip == _D(10000 + 10 * 238)
    assert result.metrics.price_base == _D(10000 + 10 * 7)
    assert result.metrics.momentum == _D(12380) / _D(10070) - _D(1)
    assert result.metrics.sma200 > result.metrics.sma200_prior
    assert result.metrics.sma50 > result.metrics.sma200


def test_close_equal_to_sma200_is_not_above_it() -> None:
    result = _evaluate([10000] * 260)

    assert result.trend_reason == "below_sma200"


def _slope_boundary_closes() -> list[int]:
    """SMA200(S)와 SMA200(S-20)이 정확히 같고 종가·SMA50은 SMA200 위인 계열."""

    closes = [10000] * 260
    for index in (*range(40, 60), *range(240, 260)):
        closes[index] = 12000
    return closes


def test_flat_sma200_slope_and_zero_momentum_are_rejected_at_their_boundaries() -> None:
    closes = _slope_boundary_closes()
    flat = _evaluate(closes)
    assert flat.metrics is not None
    assert flat.metrics.sma200 == flat.metrics.sma200_prior
    assert flat.trend_reason == "sma200_not_rising"

    # 마지막 종가만 1 올리면 SMA200이 오르지만 12-1 모멘텀은 정확히 0이다.
    closes[259] += 1
    zero_momentum = _evaluate(closes)
    assert zero_momentum.metrics is not None
    assert zero_momentum.metrics.sma200 > zero_momentum.metrics.sma200_prior
    assert zero_momentum.metrics.momentum == _D("0")
    assert zero_momentum.trend_reason == "momentum_not_positive"

    # 252세션 전 종가를 1 낮추면 모멘텀이 양수가 되어 통과한다.
    closes[7] -= 1
    assert _evaluate(closes).trend_reason is None


def test_sma50_not_above_sma200_is_its_own_reason() -> None:
    # SMA200은 20세션 전보다 오르고 종가는 SMA200 위지만, 최근 49세션이 낮아 SMA50이
    # SMA200 아래인 계열: 10000(0~59) → 12000(60~209) → 11000(210~258) → 13000(259).
    closes = [10000] * 60 + [12000] * 150 + [11000] * 49 + [13000]
    result = _evaluate(closes)

    assert result.metrics is not None
    assert result.metrics.signal_bar.close > result.metrics.sma200
    assert result.trend_reason == "sma50_not_above_sma200"


@pytest.mark.parametrize(
    ("bars_count", "reason"),
    [(253, None), (252, "insufficient_history")],
)
def test_history_requirement_is_253_completed_sessions(
    bars_count: int, reason: str | None
) -> None:
    result = _evaluate(_uptrend(bars_count))

    assert result.excluded_reason == reason


def test_missing_bar_inside_lookback_is_a_data_gap_not_insufficient_history() -> None:
    days = _weekdays(260)
    bars = _bars(days, _uptrend())
    del bars[100]
    result = evaluate_longterm_symbol(
        bars,
        symbol="005930",
        signal_session=days[-1],
        calendar_sessions=days,
    )

    assert result.excluded_reason == "data_gap"


def test_session_move_over_35_percent_inside_lookback_excludes_symbol() -> None:
    closes = _uptrend()
    closes[120] = int(closes[119] * 1.4)

    assert _evaluate(closes).excluded_reason == "price_discontinuity"


# ---- 재무 -------------------------------------------------------------------

_SIGNAL_SESSION = date(2026, 10, 2)
_QUARTER_ENDS = [
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
    date(2026, 6, 30),
]


def _fact(
    period_end: date,
    *,
    revenue: str | None = "100",
    net_income: str | None = "10",
    filing: date | None = None,
) -> QuarterFact:
    return QuarterFact(
        fiscal_period=f"{period_end.year}Q{(period_end.month - 1) // 3 + 1}",
        period_end_date=period_end,
        filing_date=filing or period_end + timedelta(days=45),
        source="dart",
        discrete_revenue=_D(revenue) if revenue is not None else None,
        discrete_net_income=_D(net_income) if net_income is not None else None,
    )


def _growth_facts(
    *,
    prior: tuple[str, str] = ("100", "10"),
    recent: tuple[str, str] = ("120", "12"),
) -> list[QuarterFact]:
    return [
        _fact(
            end,
            revenue=(prior if index < 4 else recent)[0],
            net_income=(prior if index < 4 else recent)[1],
        )
        for index, end in enumerate(_QUARTER_ENDS)
    ]


def _fundamentals(facts: list[QuarterFact], session: date = _SIGNAL_SESSION):
    return evaluate_fundamentals(facts, signal_session=session)


def test_two_year_ttm_growth_passes_and_records_observed_values() -> None:
    result = _fundamentals(_growth_facts())

    assert result.reason is None
    assert result.evidence["ttmNetIncome"] == "48.000000"
    assert result.evidence["priorTtmNetIncome"] == "40.000000"
    assert result.evidence["netIncomeGrowth"] == "0.200000"
    assert result.evidence["revenueGrowth"] == "0.200000"
    quarters = result.evidence["quarters"]
    assert isinstance(quarters, list)
    assert [item["fiscalPeriod"] for item in quarters][:2] == ["2026Q2", "2026Q1"]
    assert quarters[0]["filingDate"] == "2026-08-14"


def test_filing_after_signal_session_is_invisible_even_if_the_quarter_ended_earlier() -> (
    None
):
    facts = _growth_facts()
    # 2026Q2 공시가 신호 세션 다음 날이면 그 시점에는 7개 분기만 알려져 있다.
    facts[-1] = _fact(date(2026, 6, 30), filing=_SIGNAL_SESSION + timedelta(days=1))

    assert _fundamentals(facts).reason == "fundamentals_insufficient_quarters"
    assert _fundamentals(facts, _SIGNAL_SESSION + timedelta(days=1)).reason is None


def test_missing_filing_date_is_never_visible() -> None:
    facts = _growth_facts()
    facts[-1] = replace(facts[-1], filing_date=None)

    assert _fundamentals(facts).reason == "fundamentals_insufficient_quarters"
    assert _fundamentals([]).reason == "fundamentals_missing"


def test_quarter_gap_inside_the_latest_eight_is_rejected() -> None:
    facts = _growth_facts()
    del facts[3]
    facts.insert(0, _fact(date(2024, 6, 30)))

    assert len(facts) == 8
    assert _fundamentals(facts).reason == "fundamentals_quarter_gap"


def test_negative_or_zero_prior_ttm_net_income_is_rejected_despite_huge_growth() -> (
    None
):
    negative_prior = _growth_facts(prior=("100", "-5"), recent=("120", "50"))

    assert (
        _fundamentals(negative_prior).reason
        == "fundamentals_prior_ttm_net_income_not_positive"
    )
    loss_now = _growth_facts(recent=("120", "-1"))
    assert _fundamentals(loss_now).reason == "fundamentals_ttm_net_income_not_positive"


@pytest.mark.parametrize(
    ("recent", "reason"),
    [
        (("110", "11"), None),
        (("110", "10.99"), "fundamentals_net_income_growth_below_minimum"),
        (("109.99", "11"), "fundamentals_revenue_growth_below_minimum"),
    ],
)
def test_growth_threshold_is_inclusive_ten_percent(
    recent: tuple[str, str], reason: str | None
) -> None:
    assert _fundamentals(_growth_facts(recent=recent)).reason == reason


def test_missing_discrete_value_and_stale_latest_quarter_are_distinct_reasons() -> None:
    facts = _growth_facts()
    facts[2] = _fact(date(2025, 3, 31), revenue=None)
    assert _fundamentals(facts).reason == "fundamentals_discrete_missing"

    boundary = date(2026, 6, 30) + timedelta(days=200)
    assert _fundamentals(_growth_facts(), boundary).reason is None
    assert (
        _fundamentals(_growth_facts(), boundary + timedelta(days=1)).reason
        == "fundamentals_stale"
    )


def test_restated_period_uses_the_latest_filing_visible_at_the_session() -> None:
    facts = _growth_facts()
    original = facts[-1]
    # 정정 공시가 신호 세션 이후면 그 시점에 알려진 원본 값이 쓰인다.
    revised = replace(
        original,
        filing_date=_SIGNAL_SESSION + timedelta(days=5),
        discrete_net_income=_D("-100"),
    )

    assert _fundamentals([*facts, revised]).reason is None
    assert _fundamentals(
        [*facts, revised], _SIGNAL_SESSION + timedelta(days=5)
    ).reason == ("fundamentals_ttm_net_income_not_positive")


# ---- 코호트 선정 ------------------------------------------------------------


def _metrics(momentum: str) -> TrendMetrics:
    close = _D("12000")
    bar = SwingBar(
        session_date=_SIGNAL_SESSION,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=_D(1),
        value=_VALUE,
        source="toss",
    )
    return TrendMetrics(
        signal_bar=bar,
        sma50=_D("11000"),
        sma200=_D("10000"),
        sma200_prior=_D("9900"),
        price_skip=_D("12000"),
        price_base=_D("10000"),
        momentum=_D(momentum),
    )


def _passing(symbol: str, momentum: str) -> LongtermSymbolEvaluation:
    return LongtermSymbolEvaluation(
        symbol=symbol,
        excluded_reason=None,
        trend_reason=None,
        metrics=_metrics(momentum),
    )


def _select(evaluations, facts=None, config: LongtermShadowConfig = _CONFIG):
    return select_cohort(
        evaluations, facts or {}, signal_session=_SIGNAL_SESSION, config=config
    )


def test_top_twenty_orders_by_momentum_then_symbol_and_cuts_a_tie_at_the_boundary() -> (
    None
):
    evaluations = [_passing(f"{index:06d}", f"0.{90 - index}") for index in range(18)]
    # 18개의 뚜렷한 상위 뒤에 같은 모멘텀 3종목이 남은 2칸을 두고 경쟁한다.
    evaluations += [
        _passing(symbol, "0.5") for symbol in ("000300", "000200", "000250")
    ]
    evaluations.append(_passing("000500", "0.1"))
    evaluations.append(
        LongtermSymbolEvaluation(
            symbol="000999", excluded_reason=None, trend_reason="below_sma200"
        )
    )
    evaluations.append(
        LongtermSymbolEvaluation(symbol="000998", excluded_reason="data_gap")
    )

    selection = _select(evaluations)[LongtermCandidate.TREND_MOMENTUM]

    symbols = [signal.symbol for signal in selection.signals]
    assert [signal.rank for signal in selection.signals] == list(range(1, 21))
    assert symbols[:18] == [f"{index:06d}" for index in range(18)]
    assert symbols[18:] == ["000200", "000250"]
    assert selection.passed == 22
    assert selection.no_signal_reasons == {"below_sma200": 1, "outside_top_n": 2}


def test_fewer_than_top_n_passing_symbols_are_all_kept() -> None:
    selection = _select([_passing("000010", "0.2"), _passing("000020", "0.4")])[
        LongtermCandidate.TREND_MOMENTUM
    ]

    assert [(signal.symbol, signal.rank) for signal in selection.signals] == [
        ("000020", 1),
        ("000010", 2),
    ]
    assert selection.passed == 2
    assert "outside_top_n" not in selection.no_signal_reasons


def test_quality_candidate_filters_fundamentals_before_ranking_and_counts_reasons() -> (
    None
):
    evaluations = [
        _passing("000001", "0.9"),  # 재무 없음
        _passing("000002", "0.8"),  # 미래 공시뿐
        _passing("000003", "0.5"),  # 통과
        _passing("000004", "0.4"),  # 통과
    ]
    future_only = _growth_facts()
    future_only[-1] = _fact(
        date(2026, 6, 30), filing=_SIGNAL_SESSION + timedelta(days=1)
    )
    facts = {
        "000002": future_only,
        "000003": _growth_facts(),
        "000004": _growth_facts(),
    }

    result = _select(evaluations, facts)

    assert [s.symbol for s in result[LongtermCandidate.TREND_MOMENTUM].signals] == [
        "000001",
        "000002",
        "000003",
        "000004",
    ]
    quality = result[LongtermCandidate.QUALITY_GROWTH_TREND]
    assert [(s.symbol, s.rank) for s in quality.signals] == [
        ("000003", 1),
        ("000004", 2),
    ]
    assert quality.no_signal_reasons == {
        "fundamentals_insufficient_quarters": 1,
        "fundamentals_missing": 1,
    }
    fundamentals = quality.signals[0].evidence["fundamentals"]
    assert isinstance(fundamentals, dict)
    assert fundamentals["netIncomeGrowth"] == "0.200000"
    trend_evidence = result[LongtermCandidate.TREND_MOMENTUM].signals[0].evidence
    assert "fundamentals" not in trend_evidence


# ---- 코호트 성과 집계 -------------------------------------------------------


def _outcome(status: OutcomeStatus, net: str | None = None) -> HorizonOutcome:
    return HorizonOutcome(
        20,
        status,
        gross_return=_D(net) if net is not None else None,
        net_return=_D(net) if net is not None else None,
        mfe=_D("0.1") if net is not None else None,
        mae=_D("-0.05") if net is not None else None,
    )


def test_cohort_with_a_pending_member_is_immature_and_leaves_statistics() -> None:
    pending = summarize_cohort(
        [_outcome(OutcomeStatus.MATURE, "0.10"), _outcome(OutcomeStatus.PENDING)]
    )
    mature = summarize_cohort(
        [
            _outcome(OutcomeStatus.MATURE, "0.10"),
            _outcome(OutcomeStatus.MATURE, "0.20"),
            _outcome(OutcomeStatus.ENTRY_UNTRADABLE),
        ]
    )
    benchmark = summarize_cohort(
        [_outcome(OutcomeStatus.MATURE, "0.00"), _outcome(OutcomeStatus.MATURE, "0.10")]
    )

    assert pending.state == "pending"
    assert pending.mean_net is None
    assert mature.state == "mature"
    assert mature.members == 3
    assert mature.mature_count == 2
    assert mature.status_counts["entry_untradable"] == 1
    assert mature.mean_net == _D("0.15")
    assert summarize_cohort([]).state == "empty"

    summary = summarize_candidate_horizon(
        [
            CohortPair(date(2026, 9, 4), mature, benchmark),
            CohortPair(date(2026, 9, 11), pending, benchmark),
            CohortPair(date(2026, 9, 18), summarize_cohort([]), benchmark),
        ],
        min_mature_cohorts=12,
    )

    assert summary["cohorts"] == 3
    assert summary["cohortStateCounts"] == {"empty": 1, "mature": 1, "pending": 1}
    assert summary["matureCohorts"] == 1
    assert summary["sampleAdvisory"] == "insufficient_sample"
    assert summary["stats"] == {
        "meanCohortNetReturn": "0.150000",
        "medianCohortNetReturn": "0.150000",
        "meanBenchmarkNetReturn": "0.050000",
        "meanExcessReturn": "0.100000",
        "medianExcessReturn": "0.100000",
        "positiveExcessRatio": "1.000000",
    }


def test_excess_statistics_use_only_cohorts_with_a_mature_benchmark() -> None:
    benchmark = summarize_cohort([_outcome(OutcomeStatus.MATURE, "0.10")])
    unavailable = summarize_cohort([_outcome(OutcomeStatus.ENTRY_MISSING)])
    pairs = [
        CohortPair(
            date(2026, 1, 2) + timedelta(days=7 * index),
            summarize_cohort([_outcome(OutcomeStatus.MATURE, net)]),
            benchmark if net != "0.30" else unavailable,
        )
        for index, net in enumerate(["0.20", "0.05", "0.30", "0.10"])
    ]

    summary = summarize_candidate_horizon(pairs, min_mature_cohorts=3)

    assert summary["matureCohorts"] == 3
    assert summary["benchmarkStateCounts"] == {"mature": 3, "no_mature_members": 1}
    assert summary["sampleAdvisory"] == "sample_size_reached"
    stats = summary["stats"]
    assert isinstance(stats, dict)
    # 초과수익: +0.10, -0.05, 0.00 → 양수 비율 1/3.
    assert stats["meanExcessReturn"] == "0.016667"
    assert stats["medianExcessReturn"] == "0.000000"
    assert stats["positiveExcessRatio"] == "0.333333"


# ---- 설정 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"top_n": 10},
        {"min_growth": Decimal("0.15")},
        {"fundamentals_max_staleness_days": 180},
        {"sma_slope_sessions": 10},
        {"horizons": (20, 60)},
        {"min_mature_cohorts": 6},
    ],
)
def test_every_rule_value_changes_the_config_fingerprint(
    change: dict[str, Any],
) -> None:
    changed = replace(_CONFIG, **change)

    assert changed.fingerprint != _CONFIG.fingerprint
    assert len(_CONFIG.fingerprint) == 64


def test_config_rejects_history_shorter_than_the_indicators_need() -> None:
    with pytest.raises(ValueError, match="min_history_sessions"):
        LongtermShadowConfig(min_history_sessions=252)


def test_longest_horizon_sessions_are_available_right_after_a_recent_signal_session() -> (
    None
):
    # 조회 끝이 거래소 calendar 범위(현재 + 약 1년)를 넘으면 calendar가 구간 전체를 빈
    # 값으로 돌려줘 20거래일 horizon까지 calendar_unavailable이 된다. 최근 세션 기준으로
    # 가장 긴 horizon(120거래일)의 세션이 모두 나와야 한다.
    recent = date.today() - timedelta(days=3)

    forward = _forward_sessions(recent, _CONFIG.horizons[-1])

    assert len(forward) == _CONFIG.horizons[-1]
    assert forward[0] > recent
