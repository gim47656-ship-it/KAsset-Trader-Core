from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.shadow_setups import ShadowSetupConfig
from app.extensions.kasset.automation.swing_shadow import (
    DEFAULT_SWING_SHADOW_CONFIG,
    CandidateStatus,
    OutcomeStatus,
    SwingBar,
    SwingCandidate,
    SwingShadowConfig,
    bar_timestamp,
    evaluate_outcome,
    evaluate_swing_symbol,
    summarize_horizon,
)

pytestmark = pytest.mark.unit

_D = Decimal
_VALUE = _D("5000000000")


def _weekdays(count: int, start: date = date(2026, 1, 5)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _bar(
    day: date,
    open_: object,
    high: object,
    low: object,
    close: object,
    volume: object = 100000,
    value: object | None = _VALUE,
) -> SwingBar:
    return SwingBar(
        session_date=day,
        open=_D(str(open_)),
        high=_D(str(high)),
        low=_D(str(low)),
        close=_D(str(close)),
        volume=_D(str(volume)),
        value=_D(str(value)) if value is not None else None,
        source="toss",
    )


def _evaluate(
    bars: list[SwingBar],
    days: list[date],
    *,
    week_complete: bool = True,
    config: SwingShadowConfig = DEFAULT_SWING_SHADOW_CONFIG,
    signal_session: date | None = None,
):
    session = signal_session or days[-1]
    sessions = [day for day in days if day <= session]
    return evaluate_swing_symbol(
        bars,
        symbol="005930",
        signal_session=session,
        calendar_sessions=sessions,
        week_complete=week_complete,
        evaluation_as_of=bar_timestamp(session) + timedelta(hours=6, minutes=30),
        config=config,
    )


def _candidate(evaluation, candidate: SwingCandidate):
    return next(item for item in evaluation.candidates if item.candidate is candidate)


# ---- box breakout retest -------------------------------------------------


def _box_series(days: list[date]) -> list[SwingBar]:
    bars = []
    for index, day in enumerate(days[:95]):
        close = 10100 if index % 2 else 9900
        bars.append(
            _bar(day, 10000, max(10000, close) + 150, min(10000, close) - 150, close)
        )
    bars += [
        _bar(days[95], 10300, 10900, 10250, 10800, volume=300000),
        _bar(days[96], 10750, 10800, 10500, 10550),
        _bar(days[97], 10500, 10560, 10300, 10480),
        _bar(days[98], 10480, 10520, 10420, 10450),
        _bar(days[99], 10460, 10700, 10440, 10650),
    ]
    return bars


def test_box_breakout_retest_signals_with_breakout_anchor() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_box_series(days), days), SwingCandidate.BOX_BREAKOUT_RETEST
    )

    assert result.status is CandidateStatus.SIGNAL
    assert result.signal is not None
    assert result.signal.anchor_session == days[95]
    assert result.signal.signal_session == days[99]
    assert result.signal.trigger_price == _D("10250")
    assert result.signal.stop_reference == _D("10300")
    assert result.signal.evidence["breakoutSession"] == days[95].isoformat()


@pytest.mark.parametrize(
    ("index", "replacement", "reason"),
    [
        (99, (10400, 10460, 10380, 10440), "close_not_above_previous_close"),
        (98, (10480, 10500, 9800, 9900), "closed_back_inside_box"),
        (99, (10700, 10720, 10600, 10650), "signal_bar_not_bullish"),
    ],
)
def test_box_breakout_retest_rejects_failed_support(
    index: int, replacement: tuple[int, int, int, int], reason: str
) -> None:
    days = _weekdays(100)
    bars = _box_series(days)
    bars[index] = _bar(days[index], *replacement)

    result = _candidate(_evaluate(bars, days), SwingCandidate.BOX_BREAKOUT_RETEST)

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == reason


def test_box_breakout_without_retest_does_not_signal() -> None:
    days = _weekdays(100)
    bars = _box_series(days)
    bars[96] = _bar(days[96], 10750, 10900, 10700, 10850)
    bars[97] = _bar(days[97], 10850, 10950, 10800, 10900)
    bars[98] = _bar(days[98], 10900, 10980, 10850, 10950)
    bars[99] = _bar(days[99], 10950, 11100, 10900, 11050)

    result = _candidate(_evaluate(bars, days), SwingCandidate.BOX_BREAKOUT_RETEST)

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == "no_retest_of_box_top"


def test_bars_after_signal_session_are_ignored() -> None:
    days = _weekdays(101)
    bars = _box_series(days[:100]) + [_bar(days[100], 10650, 10660, 5000, 5100)]

    with_future = _evaluate(bars, days, signal_session=days[99])
    without_future = _evaluate(bars[:-1], days, signal_session=days[99])

    assert with_future.future_bars_ignored == 1
    assert with_future.excluded_reason is None
    assert with_future.signals == without_future.signals
    assert with_future.signals


def test_missing_latest_completed_bar_is_stale_not_evaluated() -> None:
    days = _weekdays(100)
    evaluation = _evaluate(_box_series(days)[:-1], days)

    assert evaluation.excluded_reason == "stale_latest_bar"
    assert evaluation.signals == ()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda bars: bars.pop(50), "data_gap"),
        (lambda bars: bars.__delitem__(slice(0, 30)), "insufficient_history"),
        (
            lambda bars: bars.__setitem__(
                60,
                replace(
                    bars[60], open=_D("20000"), high=_D("20100"), close=_D("20000")
                ),
            ),
            "price_discontinuity",
        ),
        (
            lambda bars: [
                bars.__setitem__(i, replace(bars[i], volume=_D("0")))
                for i in (97, 98, 99)
            ],
            "halted_suspect",
        ),
        (
            lambda bars: bars.__setitem__(99, replace(bars[99], value=None)),
            "turnover_unavailable",
        ),
    ],
)
def test_symbol_exclusions_are_explicit(mutate, reason: str) -> None:
    days = _weekdays(100)
    bars = _box_series(days)
    mutate(bars)

    evaluation = _evaluate(bars, days)

    assert evaluation.excluded_reason == reason
    assert evaluation.candidates == ()


def test_liquidity_floor_excludes_low_turnover() -> None:
    days = _weekdays(100)
    bars = [replace(bar, value=_D("100000000")) for bar in _box_series(days)]

    assert _evaluate(bars, days).excluded_reason == "below_min_turnover"


# ---- weekly compression breakout ----------------------------------------


def _weekly_series(
    days: list[date], *, fresh_violation: bool = False
) -> list[SwingBar]:
    bars: list[SwingBar] = []
    for index, day in enumerate(days[:95]):
        week = index // 5
        if fresh_violation:
            if week == 18:
                bars.append(_bar(day, 10060, 10090, 10055, 10080))
            elif week in (16, 17):
                bars.append(_bar(day, 10000, 10050, 9990, 10000))
            elif week in (13, 14, 15):
                bars.append(_bar(day, 10000, 10050, 9700, 10000))
            else:
                bars.append(_bar(day, 10000, 10050, 9950, 10000))
        elif week >= 16:
            bars.append(_bar(day, 10000, 10050, 9950, 10000))
        else:
            bars.append(_bar(day, 10000, 10100, 9900, 10000))
    previous = bars[-1].close
    for offset, close in enumerate((10100, 10200, 10300, 10400, 10500)):
        bars.append(
            _bar(days[95 + offset], previous, close + 20, previous - 20, close, 300000)
        )
        previous = _D(close)
    return bars


def test_weekly_compression_breakout_signals_on_completed_week() -> None:
    days = _weekdays(100)
    assert days[-1].weekday() == 4

    result = _candidate(
        _evaluate(_weekly_series(days), days),
        SwingCandidate.WEEKLY_COMPRESSION_BREAKOUT,
    )

    assert result.status is CandidateStatus.SIGNAL
    assert result.signal is not None
    assert result.signal.anchor_session == days[99]
    assert result.signal.trigger_price == _D("10100")
    assert result.signal.evidence["weekSessions"] == [d.isoformat() for d in days[95:]]


def test_weekly_candidate_is_not_applicable_before_week_completes() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_weekly_series(days), days, week_complete=False),
        SwingCandidate.WEEKLY_COMPRESSION_BREAKOUT,
    )

    assert result.status is CandidateStatus.NOT_APPLICABLE
    assert result.reason == "week_incomplete"
    assert result.signal is None


def test_weekly_continuation_after_prior_breakout_week_is_not_counted() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_weekly_series(days, fresh_violation=True), days),
        SwingCandidate.WEEKLY_COMPRESSION_BREAKOUT,
    )

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == "not_first_breakout_week"


def test_weekly_breakout_needs_volume_expansion() -> None:
    days = _weekdays(100)
    bars = [
        replace(bar, volume=_D("100000")) if index >= 95 else bar
        for index, bar in enumerate(_weekly_series(days))
    ]

    result = _candidate(
        _evaluate(bars, days), SwingCandidate.WEEKLY_COMPRESSION_BREAKOUT
    )

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == "weekly_volume_below_ratio"


# ---- uptrend first pullback ----------------------------------------------


def _trend(days: list[date], pullbacks: set[int]) -> list[SwingBar]:
    bars: list[SwingBar] = []
    close = _D("10000")
    index = 0
    while index < len(days):
        if index in pullbacks and index + 2 < len(days):
            base = close
            bars += [
                _bar(days[index], base, base, base - 500, base - 300),
                _bar(days[index + 1], base - 300, base - 150, base - 450, base - 200),
                _bar(
                    days[index + 2],
                    base - 150,
                    base + 200,
                    base - 180,
                    base + 150,
                    150000,
                ),
            ]
            close = base + 150
            index += 3
            continue
        close += 100
        bars.append(_bar(days[index], close - 5, close + 10, close - 10, close))
        index += 1
    return bars


def test_uptrend_first_pullback_reuses_confirmed_evaluator() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_trend(days, {97}), days), SwingCandidate.UPTREND_FIRST_PULLBACK
    )

    assert result.status is CandidateStatus.SIGNAL
    assert result.signal is not None
    assert result.signal.anchor_session == days[97]
    assert result.signal.evidence["contactLabel"] == "first"
    assert result.signal.evidence["contactSessions"] == [
        days[97].isoformat(),
        days[98].isoformat(),
    ]


def test_first_pullback_signal_does_not_depend_on_shadow_feature_flag() -> None:
    days = _weekdays(100)
    bars = _trend(days, {97})
    disabled = _evaluate(bars, days)
    enabled = _evaluate(
        bars,
        days,
        config=SwingShadowConfig(
            shadow_setup_config=ShadowSetupConfig(feature_enabled=True)
        ),
    )

    first_disabled = _candidate(disabled, SwingCandidate.UPTREND_FIRST_PULLBACK)
    first_enabled = _candidate(enabled, SwingCandidate.UPTREND_FIRST_PULLBACK)
    assert first_disabled.status is CandidateStatus.SIGNAL
    assert first_enabled.status is CandidateStatus.SIGNAL
    assert first_disabled.signal.anchor_session == first_enabled.signal.anchor_session


def test_same_pullback_cycle_keeps_one_anchor_on_consecutive_days() -> None:
    days = _weekdays(101)
    bars = _trend(days[:100], {97})
    last = bars[-1].close
    bars.append(_bar(days[100], last + 10, last + 300, last, last + 250, 150000))

    day_one = _candidate(
        _evaluate(bars, days, signal_session=days[99]),
        SwingCandidate.UPTREND_FIRST_PULLBACK,
    )
    day_two = _candidate(_evaluate(bars, days), SwingCandidate.UPTREND_FIRST_PULLBACK)

    assert day_one.status is CandidateStatus.SIGNAL
    assert day_two.status is CandidateStatus.SIGNAL
    assert day_one.signal.anchor_session == day_two.signal.anchor_session == days[97]


def test_later_cycle_outside_contact_lookback_gets_its_own_anchor() -> None:
    days = _weekdays(180)
    bars = _trend(days, {87, 177})

    early = _candidate(
        _evaluate(bars, days, signal_session=days[89]),
        SwingCandidate.UPTREND_FIRST_PULLBACK,
    )
    later = _candidate(_evaluate(bars, days), SwingCandidate.UPTREND_FIRST_PULLBACK)

    assert early.signal is not None and later.signal is not None
    assert early.signal.anchor_session == days[87]
    assert later.signal.anchor_session == days[177]


@pytest.mark.parametrize(
    ("pullback_index", "status", "reason"),
    [
        # 100세션 중 마지막 80봉 창에서 접촉 lookback은 절대 index 60부터다.
        (60, CandidateStatus.NO_SIGNAL, "pullback_cluster_truncated"),
        (62, CandidateStatus.SIGNAL, None),
    ],
)
def test_cluster_at_contact_lookback_edge_is_not_reanchored(
    pullback_index: int, status: CandidateStatus, reason: str | None
) -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_trend(days, {pullback_index}), days),
        SwingCandidate.UPTREND_FIRST_PULLBACK,
    )

    assert result.status is status
    assert result.reason == reason
    if result.signal is not None:
        assert result.signal.anchor_session == days[pullback_index]


def test_second_contact_inside_lookback_is_not_first_pullback() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_trend(days, {80, 97}), days), SwingCandidate.UPTREND_FIRST_PULLBACK
    )

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == "not_first_contact"


def _downtrend_bounce(days: list[date]) -> list[SwingBar]:
    bars: list[SwingBar] = []
    for index, day in enumerate(days[:40]):
        close = 10100 + 100 * index
        bars.append(_bar(day, close - 5, close + 10, close - 10, close))
    for index, day in enumerate(days[40:96], start=1):
        close = 14000 - 50 * index
        bars.append(_bar(day, close + 5, close + 10, close - 10, close))
    bars += [
        _bar(days[96], 11200, 11450, 11150, 11380),
        _bar(days[97], 11380, 11450, 11300, 11420),
        _bar(days[98], 11420, 11460, 11350, 11440),
        _bar(days[99], 11450, 11650, 11440, 11600, 150000),
    ]
    return bars


def test_downtrend_bounce_confirmed_by_evaluator_is_not_uptrend_pullback() -> None:
    days = _weekdays(100)
    result = _candidate(
        _evaluate(_downtrend_bounce(days), days), SwingCandidate.UPTREND_FIRST_PULLBACK
    )

    assert result.status is CandidateStatus.NO_SIGNAL
    assert result.reason == "fast_sma_not_above_slow"


# ---- outcomes -------------------------------------------------------------


def _forward(count: int) -> list[date]:
    return _weekdays(count, start=date(2026, 3, 2))


def test_outcome_uses_next_session_open_and_costs() -> None:
    sessions = _forward(10)
    bars = {
        day: _bar(day, 100 + i, 104 + i, 97 + i, 101 + i)
        for i, day in enumerate(sessions)
    }

    outcome = evaluate_outcome(
        horizon=3,
        signal_close=_D("99"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )

    assert outcome.status is OutcomeStatus.MATURE
    assert outcome.entry_session == sessions[0]
    assert outcome.exit_session == sessions[2]
    assert outcome.entry_open == _D("100")
    assert outcome.exit_close == _D("103")
    assert outcome.gross_return == _D("0.030000")
    expected_net = (
        _D("103")
        * (_D(1) - _D("0.00015") - _D("0.0018") - _D("0.001"))
        / (_D("100") * (_D(1) + _D("0.00015") + _D("0.001")))
        - 1
    )
    assert outcome.net_return == expected_net.quantize(_D("0.000001"))
    assert outcome.mfe == _D("0.060000")
    assert outcome.mae == _D("-0.030000")


def test_outcome_waits_until_horizon_session_is_final() -> None:
    sessions = _forward(10)
    bars = {day: _bar(day, 100, 101, 99, 100) for day in sessions[:4]}

    outcome = evaluate_outcome(
        horizon=5,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[3],
        bars_by_date=bars,
    )

    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.net_return is None


@pytest.mark.parametrize(
    ("missing", "status"),
    [(0, OutcomeStatus.ENTRY_MISSING), (2, OutcomeStatus.BAR_MISSING)],
)
def test_missing_final_bars_are_not_success(
    missing: int, status: OutcomeStatus
) -> None:
    sessions = _forward(10)
    bars = {
        day: _bar(day, 100, 101, 99, 100)
        for index, day in enumerate(sessions)
        if index != missing
    }

    outcome = evaluate_outcome(
        horizon=3,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )

    assert outcome.status is status
    assert outcome.net_return is None


@pytest.mark.parametrize(
    ("index", "bar", "status"),
    [
        # 진입 세션 거래량 0: 그 시가에 체결될 수 없다.
        (0, (100, 100, 100, 100, 0), OutcomeStatus.ENTRY_UNTRADABLE),
        # 청산 세션 거래량 0: 그 종가에 팔 수 없다.
        (2, (100, 100, 100, 100, 0), OutcomeStatus.EXIT_UNTRADABLE),
        # DB가 보장하지 않는 0 가격·OHLC 역전은 나눗셈 전에 막는다.
        (0, (0, 101, 99, 100, 1000), OutcomeStatus.INVALID_BAR),
        (1, (100, 98, 99, 100, 1000), OutcomeStatus.INVALID_BAR),
    ],
)
def test_untradable_or_invalid_forward_bars_are_not_mature(
    index: int, bar: tuple[int, int, int, int, int], status: OutcomeStatus
) -> None:
    sessions = _forward(10)
    bars = {day: _bar(day, 100, 102, 98, 101) for day in sessions}
    open_, high, low, close, volume = bar
    bars[sessions[index]] = _bar(sessions[index], open_, high, low, close, volume)

    outcome = evaluate_outcome(
        horizon=3,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )

    assert outcome.status is status
    assert outcome.net_return is None
    assert outcome.mfe is None and outcome.mae is None


def test_zero_volume_holding_day_is_marked_not_failed() -> None:
    sessions = _forward(10)
    bars = {day: _bar(day, 100, 102, 98, 101) for day in sessions}
    bars[sessions[1]] = _bar(sessions[1], 101, 101, 101, 101, 0)

    outcome = evaluate_outcome(
        horizon=3,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )
    summary = summarize_horizon([outcome], min_mature_sample=30)

    assert outcome.status is OutcomeStatus.MATURE
    assert outcome.zero_volume_holding_sessions == 1
    assert summary["matureWithZeroVolumeHolding"] == 1


def test_outcome_flags_price_discontinuity() -> None:
    sessions = _forward(10)
    bars = {day: _bar(day, 100, 101, 99, 100) for day in sessions}
    bars[sessions[1]] = _bar(sessions[1], 50, 51, 49, 50)

    outcome = evaluate_outcome(
        horizon=3,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )

    assert outcome.status is OutcomeStatus.PRICE_DISCONTINUITY


def test_summary_separates_immature_and_marks_small_sample_advisory() -> None:
    sessions = _forward(10)
    bars = {day: _bar(day, 100, 103, 98, 102) for day in sessions}
    mature = evaluate_outcome(
        horizon=1,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=sessions[-1],
        bars_by_date=bars,
    )
    pending = evaluate_outcome(
        horizon=1,
        signal_close=_D("100"),
        forward_sessions=sessions,
        last_final_session=None,
        bars_by_date=bars,
    )

    summary = summarize_horizon([mature, pending], min_mature_sample=30)

    assert summary["signals"] == 2
    assert summary["matureCount"] == 1
    assert summary["statusCounts"]["pending"] == 1
    assert summary["sampleAdvisory"] == "insufficient_sample"
    assert summary["stats"]["netWinRate"] == "1.000000"


def test_config_fingerprint_tracks_rule_and_cost_changes() -> None:
    base = DEFAULT_SWING_SHADOW_CONFIG.fingerprint

    assert SwingShadowConfig().fingerprint == base
    assert SwingShadowConfig(box_volume_ratio=_D("2")).fingerprint != base
    assert SwingShadowConfig(slippage_rate=_D("0.002")).fingerprint != base


def test_evaluation_as_of_is_session_close_not_wall_clock() -> None:
    """주말 실제 시각을 그대로 넘기면 기존 evaluator가 stale로 거부하는 경계."""

    days = _weekdays(100)
    bars = _trend(days, {97})
    late = evaluate_swing_symbol(
        bars,
        symbol="005930",
        signal_session=days[-1],
        calendar_sessions=days,
        week_complete=True,
        evaluation_as_of=bar_timestamp(days[-1] + timedelta(days=1))
        + timedelta(hours=1),
    )

    assert (
        _candidate(late, SwingCandidate.UPTREND_FIRST_PULLBACK).reason
        == "pullback_evaluator_stale_completed_bar"
    )
