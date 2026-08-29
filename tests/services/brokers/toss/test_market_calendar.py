from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.toss import market_calendar as calendar_module
from app.services.brokers.toss.market_calendar import (
    clear_toss_market_calendar_cache,
    get_toss_market_day,
    get_us_toss_session_from_toss,
    kr_nxt_session_for,
    kr_toss_session_for,
    latest_completed_regular_window,
    parse_kr_market_calendar,
    parse_us_market_calendar,
    us_toss_session_for,
)

KST = dt.timezone(dt.timedelta(hours=9))


def _session(start: str, end: str, **extra: str | None) -> dict[str, str | None]:
    return {"startTime": start, "endTime": end, **extra}


def _kr_calendar():
    return parse_kr_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "integrated": {
                    "preMarket": _session(
                        "2026-08-28T08:00:00+09:00",
                        "2026-08-28T09:00:00+09:00",
                        singlePriceAuctionStartTime="2026-08-28T08:50:00+09:00",
                    ),
                    "regularMarket": _session(
                        "2026-08-28T09:00:00+09:00",
                        "2026-08-28T15:30:00+09:00",
                        singlePriceAuctionStartTime="2026-08-28T15:20:00+09:00",
                    ),
                    "afterMarket": _session(
                        "2026-08-28T15:30:00+09:00",
                        "2026-08-28T20:00:00+09:00",
                        singlePriceAuctionEndTime="2026-08-28T15:40:00+09:00",
                    ),
                },
            },
            "previousBusinessDay": {
                "date": "2026-08-27",
                "integrated": {
                    "preMarket": None,
                    "regularMarket": _session(
                        "2026-08-27T09:00:00+09:00",
                        "2026-08-27T15:30:00+09:00",
                    ),
                    "afterMarket": None,
                },
            },
            "nextBusinessDay": {"date": "2026-08-31", "integrated": None},
        }
    )


def _us_calendar():
    return parse_us_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "dayMarket": _session(
                    "2026-08-28T09:00:00+09:00",
                    "2026-08-28T17:00:00+09:00",
                ),
                "preMarket": _session(
                    "2026-08-28T17:00:00+09:00",
                    "2026-08-28T22:30:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-08-28T22:30:00+09:00",
                    "2026-08-29T05:00:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-08-29T05:00:00+09:00",
                    "2026-08-29T08:50:00+09:00",
                ),
            },
            "previousBusinessDay": {
                "date": "2026-08-27",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": _session(
                    "2026-08-27T22:30:00+09:00",
                    "2026-08-28T05:00:00+09:00",
                ),
                "afterMarket": None,
            },
            "nextBusinessDay": {"date": "2026-08-31"},
        }
    )


def test_parse_kr_calendar_reads_nxt_after_1530() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": _session(
                    "2026-03-25T08:00:00+09:00",
                    "2026-03-25T09:00:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T08:50:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T15:20:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-03-25T15:30:00+09:00",
                    "2026-03-25T20:00:00+09:00",
                    singlePriceAuctionEndTime="2026-03-25T15:40:00+09:00",
                ),
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }

    calendar = parse_kr_market_calendar(raw)
    today = calendar.day_for(dt.date(2026, 3, 25))

    assert today is not None
    assert today.after_market is not None
    assert today.after_market.start == dt.datetime(2026, 3, 25, 15, 30, tzinfo=KST)
    assert today.after_market.end == dt.datetime(2026, 3, 25, 20, 0, tzinfo=KST)


def test_kr_nxt_session_for_partial_nxt_holiday_returns_none() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": None,
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime=None,
                ),
                "afterMarket": None,
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }
    calendar = parse_kr_market_calendar(raw)

    assert (
        kr_nxt_session_for(
            dt.datetime(2026, 3, 25, 15, 45, tzinfo=KST), calendar=calendar
        )
        is None
    )


def test_kr_toss_session_for_regular_market() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": _session(
                    "2026-03-25T08:00:00+09:00",
                    "2026-03-25T09:00:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T08:50:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T15:20:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-03-25T15:30:00+09:00",
                    "2026-03-25T20:00:00+09:00",
                    singlePriceAuctionEndTime="2026-03-25T15:40:00+09:00",
                ),
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }
    calendar = parse_kr_market_calendar(raw)

    assert (
        kr_toss_session_for(
            dt.datetime(2026, 3, 25, 9, 3, tzinfo=KST), calendar=calendar
        )
        == "regular"
    )
    assert (
        kr_nxt_session_for(
            dt.datetime(2026, 3, 25, 9, 3, tzinfo=KST), calendar=calendar
        )
        is None
    )


def test_parse_us_calendar_reads_day_market_without_persisted_session_literal() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "dayMarket": _session(
                "2026-03-25T09:00:00+09:00",
                "2026-03-25T16:50:00+09:00",
            ),
            "preMarket": _session(
                "2026-03-25T17:00:00+09:00",
                "2026-03-25T22:30:00+09:00",
            ),
            "regularMarket": _session(
                "2026-03-25T22:30:00+09:00",
                "2026-03-26T05:00:00+09:00",
            ),
            "afterMarket": _session(
                "2026-03-26T05:00:00+09:00",
                "2026-03-26T07:00:00+09:00",
            ),
        },
        "previousBusinessDay": {"date": "2026-03-24"},
        "nextBusinessDay": {"date": "2026-03-26"},
    }

    calendar = parse_us_market_calendar(raw)

    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 10, 0, tzinfo=KST), calendar=calendar
        )
        == "day"
    )
    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 18, 0, tzinfo=KST), calendar=calendar
        )
        == "pre"
    )


def test_full_holiday_null_windows_stay_closed_without_time_guessing() -> None:
    kr = parse_kr_market_calendar(
        {
            "today": {"date": "2026-08-28", "integrated": None},
            "previousBusinessDay": {
                "date": "2026-08-27",
                "integrated": None,
            },
            "nextBusinessDay": {
                "date": "2026-08-31",
                "integrated": None,
            },
        }
    )
    us = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": None,
                "afterMarket": None,
            }
        }
    )
    moment = dt.datetime(2026, 8, 28, 12, 0, tzinfo=KST)

    assert kr_toss_session_for(moment, calendar=kr) is None
    assert us_toss_session_for(moment, calendar=us) is None


@pytest.mark.asyncio
async def test_get_toss_market_day_uses_one_kst_day_cache(monkeypatch) -> None:
    clear_toss_market_calendar_cache()
    calls: list[str | None] = []

    class Client:
        async def market_calendar_kr(self, *, date: str | None = None):
            calls.append(date)
            return {
                "today": {"date": "2026-03-25", "integrated": None},
                "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
                "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
            }

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.brokers.toss.market_calendar.TossReadClient.from_settings",
        lambda: Client(),
    )

    first = await get_toss_market_day("kr", dt.date(2026, 3, 25))
    second = await get_toss_market_day("kr", dt.date(2026, 3, 25))

    assert first is second
    assert calls == ["2026-03-25"]


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (7, 59, None),
        (8, 0, "nxt_premarket"),
        (8, 1, "nxt_premarket"),
        (8, 49, "nxt_premarket"),
        (8, 50, "nxt_premarket"),
        (8, 51, "nxt_premarket"),
        (8, 59, "nxt_premarket"),
        (9, 0, "regular"),
        (9, 1, "regular"),
        (15, 19, "regular"),
        (15, 20, "regular"),
        (15, 21, "regular"),
        (15, 29, "regular"),
        (15, 30, "nxt_after"),
        (15, 31, "nxt_after"),
        (15, 39, "nxt_after"),
        (15, 40, "nxt_after"),
        (15, 41, "nxt_after"),
        (19, 59, "nxt_after"),
        (20, 0, None),
        (20, 1, None),
    ],
)
def test_kr_calendar_boundaries_include_auction_markers_in_parent_window(
    hour: int, minute: int, expected: str | None
) -> None:
    assert (
        kr_toss_session_for(
            dt.datetime(2026, 8, 28, hour, minute, tzinfo=KST),
            calendar=_kr_calendar(),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ((2026, 8, 28, 8, 59), None),
        ((2026, 8, 28, 9, 0), "day"),
        ((2026, 8, 28, 9, 1), "day"),
        ((2026, 8, 28, 16, 59), "day"),
        ((2026, 8, 28, 17, 0), "pre"),
        ((2026, 8, 28, 17, 1), "pre"),
        ((2026, 8, 28, 22, 29), "pre"),
        ((2026, 8, 28, 22, 30), "regular"),
        ((2026, 8, 28, 22, 31), "regular"),
        ((2026, 8, 29, 4, 59), "regular"),
        ((2026, 8, 29, 5, 0), "post"),
        ((2026, 8, 29, 5, 1), "post"),
        ((2026, 8, 29, 8, 49), "post"),
        ((2026, 8, 29, 8, 50), None),
        ((2026, 8, 29, 8, 51), None),
    ],
)
def test_us_calendar_boundaries_cover_all_four_provider_windows(
    moment: tuple[int, int, int, int, int], expected: str | None
) -> None:
    assert (
        us_toss_session_for(
            dt.datetime(*moment, tzinfo=KST),
            calendar=_us_calendar(),
        )
        == expected
    )


def test_overlapping_provider_windows_use_explicit_precedence() -> None:
    us = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "dayMarket": _session(
                    "2026-08-28T09:00:00+09:00",
                    "2026-08-28T23:00:00+09:00",
                ),
                "preMarket": _session(
                    "2026-08-28T17:00:00+09:00",
                    "2026-08-28T23:00:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-08-28T22:30:00+09:00",
                    "2026-08-29T05:00:00+09:00",
                ),
                "afterMarket": None,
            }
        }
    )
    kr = parse_kr_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "integrated": {
                    "preMarket": _session(
                        "2026-08-28T08:00:00+09:00",
                        "2026-08-28T10:00:00+09:00",
                    ),
                    "regularMarket": _session(
                        "2026-08-28T09:00:00+09:00",
                        "2026-08-28T15:30:00+09:00",
                    ),
                    "afterMarket": None,
                },
            }
        }
    )

    assert (
        us_toss_session_for(dt.datetime(2026, 8, 28, 22, 45, tzinfo=KST), calendar=us)
        == "regular"
    )
    assert (
        us_toss_session_for(dt.datetime(2026, 8, 28, 17, 30, tzinfo=KST), calendar=us)
        == "pre"
    )
    assert (
        kr_toss_session_for(dt.datetime(2026, 8, 28, 9, 30, tzinfo=KST), calendar=kr)
        == "regular"
    )


def test_latest_completed_regular_window_uses_calendar_not_weekday_math() -> None:
    previous = latest_completed_regular_window(
        dt.datetime(2026, 8, 28, 9, 30, tzinfo=KST),
        calendar=_us_calendar(),
    )
    current = latest_completed_regular_window(
        dt.datetime(2026, 8, 29, 7, 55, tzinfo=KST),
        calendar=_us_calendar(),
    )

    assert previous is not None
    assert previous.end == dt.datetime(2026, 8, 28, 5, 0, tzinfo=KST)
    assert current is not None
    assert current.end == dt.datetime(2026, 8, 29, 5, 0, tzinfo=KST)


def test_dst_changes_are_taken_from_calendar_timestamps() -> None:
    winter = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-01-15",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": _session(
                    "2026-01-15T23:30:00+09:00",
                    "2026-01-16T06:00:00+09:00",
                ),
                "afterMarket": None,
            }
        }
    )
    summer = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-08-28",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": _session(
                    "2026-08-28T22:30:00+09:00",
                    "2026-08-29T05:00:00+09:00",
                ),
                "afterMarket": None,
            }
        }
    )

    assert (
        us_toss_session_for(
            dt.datetime(2026, 1, 15, 23, 29, tzinfo=KST), calendar=winter
        )
        is None
    )
    assert (
        us_toss_session_for(
            dt.datetime(2026, 1, 15, 23, 30, tzinfo=KST), calendar=winter
        )
        == "regular"
    )
    assert (
        us_toss_session_for(
            dt.datetime(2026, 8, 28, 22, 30, tzinfo=KST), calendar=summer
        )
        == "regular"
    )


@pytest.mark.asyncio
async def test_us_calendar_query_uses_eastern_date_and_returns_closed_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dt.date]] = []

    async def calendar(market: str, query_date: dt.date):
        calls.append((market, query_date))
        return _us_calendar()

    monkeypatch.setattr(calendar_module, "get_toss_market_calendar", calendar)

    assert (
        await get_us_toss_session_from_toss(dt.datetime(2026, 8, 29, 7, 55, tzinfo=KST))
        == "post"
    )
    assert (
        await get_us_toss_session_from_toss(dt.datetime(2026, 8, 29, 8, 55, tzinfo=KST))
        == "closed"
    )
    assert calls == [
        ("us", dt.date(2026, 8, 28)),
        ("us", dt.date(2026, 8, 28)),
    ]
