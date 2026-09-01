from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.market_session import (
    completed_bar_cutoff,
    completed_daily_bars,
    current_regular_session,
    latest_completed_session,
)

# KST 12:00 on a KRX trading day: the 2026-09-01 session is in progress.
_MID_SESSION = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)


def _bar(timestamp: datetime) -> PriceBar:
    return PriceBar(
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )


def test_in_progress_session_is_not_the_completed_session() -> None:
    session = current_regular_session("KRX", _MID_SESSION)
    completed = latest_completed_session("KRX", _MID_SESSION)

    assert session is not None and completed is not None
    assert session.session_date.isoformat() == "2026-09-01"
    assert completed.session_date.isoformat() == "2026-08-31"
    assert completed_bar_cutoff("KRX", _MID_SESSION) == completed.closes_at


def test_partial_daily_bar_of_the_open_session_is_dropped_for_both_conventions() -> (
    None
):
    """KIS writes the session date at UTC midnight; Toss writes it at KST midnight.

    두 규약 모두 진행 중인 세션의 부분 일봉을 만들 수 있고, 둘 다 제외되어야 한다.
    """

    kis_partial = _bar(datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
    toss_partial = _bar(datetime(2026, 8, 31, 15, 0, tzinfo=UTC))
    kis_completed = _bar(datetime(2026, 8, 31, 0, 0, tzinfo=UTC))
    toss_completed = _bar(datetime(2026, 8, 30, 15, 0, tzinfo=UTC))

    kept = completed_daily_bars(
        [kis_partial, toss_partial, kis_completed, toss_completed],
        market="KRX",
        as_of=_MID_SESSION,
    )

    assert [bar.timestamp for bar in kept] == [
        toss_completed.timestamp,
        kis_completed.timestamp,
    ]


def test_completed_daily_bars_fail_closed_when_no_session_is_provable() -> None:
    # 달력 범위 밖이면 완료 세션을 입증할 수 없다. 그때는 모든 bar를 완료로
    # 취급하지 않고 아무 것도 남기지 않는다.
    out_of_range = datetime(1900, 1, 15, 3, 0, tzinfo=UTC)

    assert completed_bar_cutoff("KRX", out_of_range) is None
    assert (
        completed_daily_bars(
            [_bar(datetime(1900, 1, 14, 0, 0, tzinfo=UTC))],
            market="KRX",
            as_of=out_of_range,
        )
        == ()
    )


def test_future_bars_never_count_as_completed() -> None:
    far_future = _bar(_MID_SESSION + timedelta(days=1))

    assert completed_daily_bars([far_future], market="KRX", as_of=_MID_SESSION) == ()


def test_naive_timestamps_are_excluded_instead_of_guessed() -> None:
    naive = PriceBar(
        timestamp=datetime(2026, 8, 31, 0, 0),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )

    assert completed_daily_bars([naive], market="KRX", as_of=_MID_SESSION) == ()


def test_after_the_close_todays_session_becomes_completed() -> None:
    after_close = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)

    completed = latest_completed_session("KRX", after_close)

    assert completed is not None
    assert completed.session_date.isoformat() == "2026-09-01"
    assert current_regular_session("KRX", after_close) is None
