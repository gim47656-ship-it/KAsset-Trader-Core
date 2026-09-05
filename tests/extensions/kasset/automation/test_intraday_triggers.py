from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.contracts import Action, PriceBar
from app.extensions.kasset.automation.intraday_triggers import (
    DEFAULT_INTRADAY_TRIGGER_POLICY,
    INDEX_INTRADAY_UNAVAILABLE,
    INTRADAY_RELATIVE_STRENGTH,
    OPENING_RANGE_BREAKOUT,
    RELATIVE_VOLUME_5M,
    RELATIVE_VOLUME_20M,
    SAME_TIME_RELATIVE_VOLUME_5M,
    SAME_TIME_RELATIVE_VOLUME_20M,
    SESSION_VWAP_RECLAIM,
    IntradayTriggerPolicy,
    SameTimeVolumeBaseline,
    TriggerDecisionStatus,
    TriggerResult,
    TriggerStatus,
    decide_intraday_triggers,
    intraday_relative_strength,
    opening_range_breakout,
    relative_volume,
    same_time_baseline_median,
    same_time_relative_volume,
    session_vwap,
    session_vwap_reclaim,
)

_OPEN = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
_INTERVAL = timedelta(minutes=5)
_OPENING_RANGE = timedelta(minutes=15)


def _bar(
    index: int,
    *,
    open_price: str,
    high: str,
    low: str,
    close: str,
    volume: str,
) -> PriceBar:
    return PriceBar(
        timestamp=_OPEN + _INTERVAL * index,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _flat(index: int, price: str = "101", volume: str = "1000") -> PriceBar:
    return _bar(
        index,
        open_price=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
    )


def _same_time_baseline(*volumes: str) -> list[SameTimeVolumeBaseline]:
    return [
        SameTimeVolumeBaseline(
            session_date=date(2026, 8, day),
            volume=Decimal(volume),
        )
        for day, volume in enumerate(volumes, start=1)
    ]


def _long_session() -> list[PriceBar]:
    """개장 3봉으로 100~102 구간을 만들고 마지막 봉이 위로 확장하는 세션."""

    bars = [
        _bar(0, open_price="100", high="102", low="99", close="100", volume="1000"),
        _bar(1, open_price="100", high="102", low="100", close="101", volume="1000"),
        _bar(2, open_price="101", high="102", low="100", close="101", volume="1000"),
    ]
    bars.extend(_flat(index) for index in range(3, 15))
    bars.append(
        _bar(15, open_price="101", high="103", low="101", close="103", volume="5000")
    )
    return bars


def _short_session() -> list[PriceBar]:
    """같은 구조를 아래 방향으로 뒤집은 세션."""

    bars = [
        _bar(0, open_price="102", high="103", low="100", close="102", volume="1000"),
        _bar(1, open_price="102", high="102", low="100", close="101", volume="1000"),
        _bar(2, open_price="101", high="102", low="100", close="101", volume="1000"),
    ]
    bars.extend(_flat(index) for index in range(3, 15))
    bars.append(
        _bar(15, open_price="101", high="101", low="96", close="96", volume="5000")
    )
    return bars


def _triggers(bars: list[PriceBar], direction: Action) -> list[TriggerResult]:
    return [
        opening_range_breakout(
            bars,
            direction=direction,
            session_open=_OPEN,
            opening_range=_OPENING_RANGE,
            bar_interval=_INTERVAL,
            source="toss",
        ),
        session_vwap_reclaim(
            bars,
            direction=direction,
            bar_interval=_INTERVAL,
            source="toss",
        ),
        relative_volume(
            bars,
            code=RELATIVE_VOLUME_5M,
            window_bars=1,
            baseline_bars=12,
            threshold=Decimal("1.5"),
            bar_interval=_INTERVAL,
            source="toss",
        ),
        relative_volume(
            bars,
            code=RELATIVE_VOLUME_20M,
            window_bars=4,
            baseline_bars=12,
            threshold=Decimal("1.5"),
            bar_interval=_INTERVAL,
            source="toss",
        ),
    ]


@pytest.mark.parametrize(
    ("direction", "bars"),
    [(Action.BUY, _long_session()), (Action.SELL, _short_session())],
)
def test_opening_range_breakout_is_symmetric(
    direction: Action, bars: list[PriceBar]
) -> None:
    result = opening_range_breakout(
        bars,
        direction=direction,
        session_open=_OPEN,
        opening_range=_OPENING_RANGE,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.code == OPENING_RANGE_BREAKOUT
    # 관측값·임계값·출처·as_of가 모두 근거로 남아야 한다.
    assert result.value is not None
    assert result.threshold is not None
    assert result.source == "toss"
    assert result.as_of == bars[-1].timestamp + _INTERVAL


@pytest.mark.parametrize(
    ("latest_close", "expected"),
    [
        ("100.199", TriggerStatus.INACTIVE),
        ("100.200", TriggerStatus.ACTIVE),
    ],
)
def test_opening_range_breakout_requires_the_pivot_buffer(
    latest_close: str, expected: TriggerStatus
) -> None:
    bars = [
        _bar(0, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(1, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(2, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(
            3,
            open_price="100",
            high=latest_close,
            low="100",
            close=latest_close,
            volume="1000",
        ),
    ]

    result = opening_range_breakout(
        bars,
        direction=Action.BUY,
        session_open=_OPEN,
        opening_range=_OPENING_RANGE,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is expected
    assert result.threshold == "100.200000"


def test_sell_triggers_do_not_require_a_pivot_buffer() -> None:
    opening_range = [
        _bar(0, open_price="101", high="101", low="100", close="101", volume="1000"),
        _bar(1, open_price="101", high="101", low="100", close="101", volume="1000"),
        _bar(2, open_price="101", high="101", low="100", close="101", volume="1000"),
        _bar(3, open_price="101", high="101", low="100", close="100", volume="1000"),
    ]
    vwap = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="0"),
        _bar(1, open_price="100", high="100", low="100", close="100", volume="0"),
    ]

    breakout = opening_range_breakout(
        opening_range,
        direction=Action.SELL,
        session_open=_OPEN,
        opening_range=_OPENING_RANGE,
        bar_interval=_INTERVAL,
        source="toss",
    )
    reclaim = session_vwap_reclaim(
        vwap,
        direction=Action.SELL,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert breakout.status is TriggerStatus.ACTIVE
    assert breakout.threshold == "100.000000"
    assert reclaim.status is TriggerStatus.ACTIVE
    assert reclaim.threshold == "100.000000"


def test_opening_range_breakout_blocks_an_overextended_price() -> None:
    bars = [
        _bar(0, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(1, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(2, open_price="99", high="100", low="99", close="99", volume="1000"),
        _bar(
            3,
            open_price="100",
            high="102.01",
            low="100",
            close="102.01",
            volume="1000",
        ),
    ]

    result = opening_range_breakout(
        bars,
        direction=Action.BUY,
        session_open=_OPEN,
        opening_range=_OPENING_RANGE,
        bar_interval=_INTERVAL,
        source="toss",
    )
    decision = decide_intraday_triggers(
        [result],
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=20),
    )

    assert result.status is TriggerStatus.BLOCKED
    assert result.blocked_reason == "too_extended"
    assert decision.status is TriggerDecisionStatus.BLOCKED
    assert decision.blocked_reason == "too_extended"


def test_sell_triggers_ignore_extension_cap_during_five_percent_drop() -> None:
    bars = _short_session()
    bars[-1] = _bar(
        15,
        open_price="101",
        high="101",
        low="95",
        close="95",
        volume="5000",
    )
    triggers = _triggers(bars, Action.SELL)

    decision = decide_intraday_triggers(
        triggers,
        symbol="005930",
        market="KRX",
        direction=Action.SELL,
        evaluated_at=_OPEN + timedelta(minutes=90),
    )

    assert triggers[0].status is TriggerStatus.ACTIVE
    assert triggers[1].status is TriggerStatus.ACTIVE
    assert triggers[0].blocked_reason is None
    assert triggers[1].blocked_reason is None
    assert decision.status is TriggerDecisionStatus.TRIGGERED


def test_opening_range_breakout_needs_a_completed_bar_after_the_range() -> None:
    bars = [
        _bar(0, open_price="100", high="102", low="99", close="100", volume="1000"),
        _bar(1, open_price="100", high="102", low="100", close="101", volume="1000"),
        _bar(2, open_price="101", high="102", low="100", close="101", volume="1000"),
    ]

    result = opening_range_breakout(
        bars,
        direction=Action.BUY,
        session_open=_OPEN,
        opening_range=_OPENING_RANGE,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is TriggerStatus.UNAVAILABLE
    assert result.unavailable_reason == "no_completed_bar_after_opening_range"


def test_session_vwap_resets_and_is_volume_weighted() -> None:
    bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="200", high="200", low="200", close="200", volume="3"),
    ]

    curve = session_vwap(bars)

    assert curve[0] == Decimal("100")
    assert curve[1] == Decimal("175")


@pytest.mark.parametrize(
    ("direction", "bars"),
    [(Action.BUY, _long_session()), (Action.SELL, _short_session())],
)
def test_session_vwap_trigger_is_symmetric(
    direction: Action, bars: list[PriceBar]
) -> None:
    result = session_vwap_reclaim(
        bars,
        direction=direction,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.code == SESSION_VWAP_RECLAIM
    assert result.status is TriggerStatus.ACTIVE
    assert result.threshold is not None


@pytest.mark.parametrize(
    ("latest_close", "expected"),
    [
        ("100.199", TriggerStatus.INACTIVE),
        ("100.200", TriggerStatus.ACTIVE),
        ("102.010", TriggerStatus.BLOCKED),
    ],
)
def test_session_vwap_reclaim_applies_buffer_and_extension_cap(
    latest_close: str, expected: TriggerStatus
) -> None:
    bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="0"),
        _bar(
            1,
            open_price="100",
            high=latest_close,
            low="100",
            close=latest_close,
            volume="0",
        ),
    ]

    result = session_vwap_reclaim(
        bars,
        direction=Action.BUY,
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is expected
    assert result.threshold == "100.200000"
    assert result.blocked_reason == (
        "too_extended" if expected is TriggerStatus.BLOCKED else None
    )


def test_relative_volume_needs_completed_window_and_baseline() -> None:
    result = relative_volume(
        [_flat(index) for index in range(4)],
        code=RELATIVE_VOLUME_20M,
        window_bars=4,
        baseline_bars=12,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is TriggerStatus.UNAVAILABLE
    assert result.unavailable_reason == "insufficient_completed_session_bars"


def test_relative_volume_reports_ratio_against_the_baseline() -> None:
    bars = [_flat(index) for index in range(12)]
    bars.extend(_flat(index, volume="3000") for index in range(12, 16))

    result = relative_volume(
        bars,
        code=RELATIVE_VOLUME_20M,
        window_bars=4,
        baseline_bars=12,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "3.000000"
    assert result.threshold == "1.500000"


def test_relative_volume_fails_closed_on_a_zero_baseline() -> None:
    # 기준선 창(마지막 봉 직전 12봉)이 전부 거래 없이 지나간 세션.
    bars = [_flat(index, volume="0") for index in range(15)]
    bars.append(_flat(15, volume="1000"))

    result = relative_volume(
        bars,
        code=RELATIVE_VOLUME_5M,
        window_bars=1,
        baseline_bars=12,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="toss",
    )

    assert result.status is TriggerStatus.UNAVAILABLE
    assert result.unavailable_reason == "zero_baseline_volume"


def test_same_time_baseline_median_returns_none_without_samples() -> None:
    assert same_time_baseline_median([]) is None


def test_same_time_baseline_median_averages_the_middle_even_samples() -> None:
    baseline = _same_time_baseline("100", "200", "400", "500")

    assert same_time_baseline_median(baseline) == Decimal("300")


def test_same_time_baseline_median_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="baseline volume must not be negative"):
        same_time_baseline_median(_same_time_baseline("-1"))


def test_same_time_relative_volume_needs_minimum_baseline_days() -> None:
    result = same_time_relative_volume(
        [_flat(0, volume="150")],
        _same_time_baseline("100"),
        code=SAME_TIME_RELATIVE_VOLUME_5M,
        window_bars=1,
        minimum_days=2,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.available is False
    assert result.unavailable_reason == "insufficient_baseline_days"
    assert result.detail == "2 baseline days are required and 1 are available"


def test_same_time_relative_volume_needs_completed_session_bars() -> None:
    result = same_time_relative_volume(
        [_flat(index) for index in range(3)],
        _same_time_baseline("100"),
        code=SAME_TIME_RELATIVE_VOLUME_20M,
        window_bars=4,
        minimum_days=1,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.available is False
    assert result.unavailable_reason == "insufficient_completed_session_bars"


def test_same_time_relative_volume_fails_closed_on_a_zero_median() -> None:
    result = same_time_relative_volume(
        [_flat(0, volume="100")],
        _same_time_baseline("0", "0", "100"),
        code=SAME_TIME_RELATIVE_VOLUME_5M,
        window_bars=1,
        minimum_days=3,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.available is False
    assert result.unavailable_reason == "zero_baseline_volume"


def test_same_time_relative_volume_averages_the_middle_even_samples() -> None:
    result = same_time_relative_volume(
        [_flat(0, volume="600")],
        _same_time_baseline("100", "200", "400", "500"),
        code=SAME_TIME_RELATIVE_VOLUME_5M,
        window_bars=1,
        minimum_days=4,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "2.000000"
    assert "baselineMedian=300.000000" in result.detail


def test_same_time_relative_volume_median_resists_one_extreme_value() -> None:
    result = same_time_relative_volume(
        [_flat(0, volume="200")],
        _same_time_baseline("100", "100", "100", "10000"),
        code=SAME_TIME_RELATIVE_VOLUME_5M,
        window_bars=1,
        minimum_days=4,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "2.000000"


def test_same_time_relative_volume_is_active_at_the_threshold() -> None:
    result = same_time_relative_volume(
        [_flat(0, volume="150")],
        _same_time_baseline("100", "100", "100"),
        code=SAME_TIME_RELATIVE_VOLUME_5M,
        window_bars=1,
        minimum_days=3,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "1.500000"
    assert result.threshold == "1.500000"


def test_same_time_relative_volume_20m_sums_only_the_latest_four_bars() -> None:
    volumes = ("9000", "8000", "10", "20", "30", "40")
    bars = [_flat(index, volume=volume) for index, volume in enumerate(volumes)]

    result = same_time_relative_volume(
        bars,
        _same_time_baseline("50", "50", "50"),
        code=SAME_TIME_RELATIVE_VOLUME_20M,
        window_bars=4,
        minimum_days=3,
        threshold=Decimal("1.5"),
        bar_interval=_INTERVAL,
        source="research.kr_candles_1m_toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "2.000000"
    assert "window=4 bars todayVolume=100.000000" in result.detail
    assert result.as_of == bars[-1].timestamp + _INTERVAL


def test_same_time_relative_volume_rejects_duplicate_session_dates() -> None:
    duplicated_date = date(2026, 8, 1)
    baseline = [
        SameTimeVolumeBaseline(duplicated_date, Decimal("100")),
        SameTimeVolumeBaseline(duplicated_date, Decimal("200")),
    ]

    with pytest.raises(ValueError, match="duplicate same-time baseline session_date"):
        same_time_relative_volume(
            [_flat(0, volume="150")],
            baseline,
            code=SAME_TIME_RELATIVE_VOLUME_5M,
            window_bars=1,
            minimum_days=2,
            threshold=Decimal("1.5"),
            bar_interval=_INTERVAL,
            source="research.kr_candles_1m_toss",
        )


@pytest.mark.parametrize(
    ("window_bars", "minimum_days"),
    [(0, 1), (1, 0)],
)
def test_same_time_relative_volume_requires_positive_windows(
    window_bars: int,
    minimum_days: int,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        same_time_relative_volume(
            [_flat(0)],
            _same_time_baseline("100"),
            code=SAME_TIME_RELATIVE_VOLUME_5M,
            window_bars=window_bars,
            minimum_days=minimum_days,
            threshold=Decimal("1.5"),
            bar_interval=_INTERVAL,
            source="research.kr_candles_1m_toss",
        )


def test_intraday_relative_strength_uses_the_shared_completed_window() -> None:
    bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="100", high="110", low="100", close="110", volume="1"),
    ]
    index_bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="100", high="101", low="100", close="101", volume="1"),
    ]

    result = intraday_relative_strength(
        bars,
        index_bars,
        direction=Action.BUY,
        threshold=Decimal("0"),
        bar_interval=_INTERVAL,
        source="toss",
        index_source="toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "0.090000"


def test_intraday_relative_strength_sell_direction_requires_underperformance() -> None:
    bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="100", high="100", low="95", close="95", volume="1"),
    ]
    index_bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="100", high="100", low="99", close="99", volume="1"),
    ]

    result = intraday_relative_strength(
        bars,
        index_bars,
        direction=Action.SELL,
        threshold=Decimal("0.01"),
        bar_interval=_INTERVAL,
        source="toss",
        index_source="toss",
    )

    assert result.status is TriggerStatus.ACTIVE
    assert result.value == "-0.040000"
    assert result.threshold == "-0.010000"


def test_intraday_relative_strength_is_unavailable_without_index_bars() -> None:
    result = intraday_relative_strength(
        _long_session(),
        None,
        direction=Action.BUY,
        threshold=Decimal("0"),
        bar_interval=_INTERVAL,
        source="toss",
        index_source="KOSPI",
    )

    assert result.status is TriggerStatus.UNAVAILABLE
    assert result.unavailable_reason == INDEX_INTRADAY_UNAVAILABLE
    # 지수 분봉이 없다는 사실이 근거에 남아야 한다.
    assert result.as_evidence()["unavailableReason"] == INDEX_INTRADAY_UNAVAILABLE
    assert result.source == "KOSPI"


def test_intraday_relative_strength_never_falls_back_to_daily_data() -> None:
    """공유 창이 없으면 값을 만들지 않는다."""

    bars = [
        _bar(0, open_price="100", high="100", low="100", close="100", volume="1"),
        _bar(1, open_price="100", high="110", low="100", close="110", volume="1"),
    ]
    disjoint_index = [
        PriceBar(
            timestamp=_OPEN - timedelta(days=1) + _INTERVAL * index,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for index in range(2)
    ]

    result = intraday_relative_strength(
        bars,
        disjoint_index,
        direction=Action.BUY,
        threshold=Decimal("0"),
        bar_interval=_INTERVAL,
        source="toss",
        index_source="toss",
    )

    assert result.status is TriggerStatus.UNAVAILABLE
    assert result.unavailable_reason == "index_window_mismatch"


@pytest.mark.parametrize(
    ("direction", "bars"),
    [(Action.BUY, _long_session()), (Action.SELL, _short_session())],
)
def test_unavailable_index_strength_does_not_block_the_other_triggers(
    direction: Action, bars: list[PriceBar]
) -> None:
    triggers = _triggers(bars, direction)
    triggers.append(
        intraday_relative_strength(
            bars,
            None,
            direction=direction,
            threshold=Decimal("0"),
            bar_interval=_INTERVAL,
            source="toss",
            index_source="KOSPI",
        )
    )

    decision = decide_intraday_triggers(
        triggers,
        symbol="005930",
        market="KRX",
        direction=direction,
        evaluated_at=_OPEN + timedelta(minutes=90),
    )

    assert decision.status is TriggerDecisionStatus.TRIGGERED
    assert decision.blocked_reason is None
    evidence = decision.as_evidence()
    codes = {item["code"] for item in evidence["triggers"]}  # type: ignore[index]
    assert codes == {
        OPENING_RANGE_BREAKOUT,
        SESSION_VWAP_RECLAIM,
        RELATIVE_VOLUME_5M,
        RELATIVE_VOLUME_20M,
        INTRADAY_RELATIVE_STRENGTH,
    }


def test_available_index_strength_must_agree_with_the_direction() -> None:
    bars = _long_session()
    triggers = _triggers(bars, Action.BUY)
    triggers.append(
        TriggerResult(
            code=INTRADAY_RELATIVE_STRENGTH,
            status=TriggerStatus.INACTIVE,
            value="-0.010000",
            threshold="0.000000",
            source="toss",
            as_of=bars[-1].timestamp + _INTERVAL,
            detail="index outperformed the candidate",
        )
    )

    decision = decide_intraday_triggers(
        triggers,
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
    )

    assert decision.status is TriggerDecisionStatus.NOT_TRIGGERED
    assert decision.blocked_reason == f"{INTRADAY_RELATIVE_STRENGTH}_disagrees"


def test_relative_volume_alone_cannot_trigger_an_entry() -> None:
    bars = [_flat(index) for index in range(12)]
    bars.extend(_flat(index, volume="3000") for index in range(12, 16))
    triggers = _triggers(bars, Action.BUY)

    decision = decide_intraday_triggers(
        triggers,
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
    )

    assert decision.status is TriggerDecisionStatus.NOT_TRIGGERED
    assert decision.blocked_reason == "no_directional_trigger"


@pytest.mark.parametrize(
    ("session_open_price", "expected_status"),
    [
        (Decimal("104"), TriggerDecisionStatus.BLOCKED),
        (Decimal("101"), TriggerDecisionStatus.TRIGGERED),
    ],
)
def test_gap_up_no_chase_blocks_only_a_large_gap(
    session_open_price: Decimal, expected_status: TriggerDecisionStatus
) -> None:
    decision = decide_intraday_triggers(
        _triggers(_long_session(), Action.BUY),
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
        session_open_price=session_open_price,
        previous_close=Decimal("100"),
        atr_14=Decimal("1"),
    )

    assert decision.status is expected_status
    gap_evidence = decision.as_evidence()["noChase"]["gapUp"]  # type: ignore[index]
    assert gap_evidence["blocked"] is (  # type: ignore[index]
        expected_status is TriggerDecisionStatus.BLOCKED
    )
    if expected_status is TriggerDecisionStatus.BLOCKED:
        assert decision.blocked_reason == "gap_up_no_chase"


def test_missing_atr_marks_gap_check_unavailable_without_blocking() -> None:
    decision = decide_intraday_triggers(
        _triggers(_long_session(), Action.BUY),
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
        session_open_price=Decimal("104"),
        previous_close=Decimal("100"),
        atr_14=None,
    )

    assert decision.status is TriggerDecisionStatus.TRIGGERED
    no_chase = decision.as_evidence()["noChase"]  # type: ignore[index]
    assert no_chase["schemaVersion"] == "kasset.no-chase.v1"  # type: ignore[index]
    assert no_chase["gapUp"]["unavailable"] == "atr14_unavailable"  # type: ignore[index]


def test_missing_previous_close_marks_gap_check_unavailable_without_blocking() -> None:
    decision = decide_intraday_triggers(
        _triggers(_long_session(), Action.BUY),
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
        session_open_price=Decimal("104"),
        previous_close=None,
        atr_14=Decimal("1"),
    )

    assert decision.status is TriggerDecisionStatus.TRIGGERED
    no_chase = decision.as_evidence()["noChase"]  # type: ignore[index]
    assert (
        no_chase["gapUp"]["unavailable"]  # type: ignore[index]
        == "previous_close_unavailable"
    )


def test_expired_trigger_decision_becomes_not_triggered() -> None:
    evaluated_at = _OPEN + timedelta(minutes=90)
    decision = decide_intraday_triggers(
        _triggers(_long_session(), Action.BUY),
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=evaluated_at,
    )

    expired = decision.expire(evaluated_at + timedelta(minutes=30))

    assert decision.valid_until == evaluated_at + timedelta(minutes=30)
    assert expired.status is TriggerDecisionStatus.NOT_TRIGGERED
    assert expired.blocked_reason == "expired"
    assert expired.as_evidence()["validUntil"] == "2026-09-01T02:00:00Z"


def test_stale_or_partial_bars_block_every_trigger() -> None:
    decision = decide_intraday_triggers(
        (),
        symbol="005930",
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_OPEN + timedelta(minutes=90),
        blocked_reason="intraday_bars_stale",
    )

    assert decision.status is TriggerDecisionStatus.UNAVAILABLE
    assert decision.blocked_reason == "intraday_bars_stale"
    assert decision.triggered is False


def test_hold_direction_never_triggers() -> None:
    decision = decide_intraday_triggers(
        _triggers(_long_session(), Action.BUY),
        symbol="005930",
        market="KRX",
        direction=Action.HOLD,
        evaluated_at=_OPEN + timedelta(minutes=90),
    )

    assert decision.status is TriggerDecisionStatus.UNAVAILABLE
    assert decision.blocked_reason == "no_directional_setup"


def test_default_policy_names_all_four_triggers() -> None:
    policy = DEFAULT_INTRADAY_TRIGGER_POLICY

    assert policy.directional == (OPENING_RANGE_BREAKOUT, SESSION_VWAP_RECLAIM)
    assert policy.volume == (RELATIVE_VOLUME_5M, RELATIVE_VOLUME_20M)
    assert policy.directional_confirmation == (INTRADAY_RELATIVE_STRENGTH,)


def test_a_trigger_cannot_sit_in_two_policy_groups() -> None:
    with pytest.raises(ValueError, match="two policy groups"):
        IntradayTriggerPolicy(
            directional=(OPENING_RANGE_BREAKOUT,),
            volume=(OPENING_RANGE_BREAKOUT,),
        )
