"""ROB-464: KR market-session data_state classification."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.mcp_server.tooling import market_session


def test_returns_fresh_during_regular_session():
    now = pd.Timestamp("2026-06-08 10:00", tz="Asia/Seoul")
    assert market_session.kr_market_data_state(now) == market_session.DATA_STATE_FRESH


def test_returns_premarket_unavailable_before_open_on_trading_day():
    now = pd.Timestamp("2026-06-08 08:32", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_PREMARKET_UNAVAILABLE
    )


def test_returns_market_closed_after_session_on_trading_day():
    now = pd.Timestamp("2026-06-08 16:00", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_MARKET_CLOSED
    )


def test_returns_market_closed_on_non_session_day():
    now = pd.Timestamp("2026-06-07 08:32", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_MARKET_CLOSED
    )


def test_returns_market_closed_on_confirmed_one_off_closure():
    now = pd.Timestamp("2026-08-17 10:00", tz="Asia/Seoul")
    assert (
        market_session.kr_market_data_state(now)
        == market_session.DATA_STATE_MARKET_CLOSED
    )


def test_is_kr_session_day_true_on_trading_day():
    assert market_session.is_kr_session_day(pd.Timestamp("2026-06-08").date()) is True


def test_is_kr_session_day_false_on_non_session_day():
    assert market_session.is_kr_session_day(pd.Timestamp("2026-06-07").date()) is False


def test_previous_kr_session_skips_weekend():
    # 2026-06-13 is a Saturday → prior session is Fri 2026-06-12.
    assert (
        market_session.previous_kr_session(pd.Timestamp("2026-06-13").date())
        == pd.Timestamp("2026-06-12").date()
    )


def test_previous_kr_session_strictly_before_a_session_day():
    # 2026-06-12 (Fri) is itself a session → strictly-prior is Thu 2026-06-11.
    assert (
        market_session.previous_kr_session(pd.Timestamp("2026-06-12").date())
        == pd.Timestamp("2026-06-11").date()
    )


def test_previous_kr_session_monday_after_saturday_holiday():
    # 2026-06-06 (현충일, Memorial Day) falls on a Saturday; 2026-06-08 is the
    # Monday after. The prior session is Fri 2026-06-05, not the weekend.
    assert (
        market_session.previous_kr_session(pd.Timestamp("2026-06-08").date())
        == pd.Timestamp("2026-06-05").date()
    )


def test_previous_kr_session_after_multiday_lunar_new_year_holiday():
    # 2026-02-16/17/18 (Mon/Tue/Wed) are the Lunar New Year holiday; the first
    # session after is Thu 2026-02-19, whose prior session is Fri 2026-02-13.
    assert (
        market_session.previous_kr_session(pd.Timestamp("2026-02-19").date())
        == pd.Timestamp("2026-02-13").date()
    )


def test_us_market_session_returns_premarket_on_xnys_session_day(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 8, 0, tzinfo=dt.UTC)  # 04:00 ET

    assert market_session.us_market_session(now) == "premarket"


def test_us_market_session_returns_regular_during_xnys_regular_hours(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 15, 0, tzinfo=dt.UTC)  # 11:00 ET

    assert market_session.us_market_session(now) == "regular"


def test_us_market_session_returns_afterhours_after_regular_close(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 21, 0, tzinfo=dt.UTC)  # 17:00 ET

    assert market_session.us_market_session(now) == "afterhours"


def test_us_market_session_returns_closed_before_premarket(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 7, 59, tzinfo=dt.UTC)  # 03:59 ET

    assert market_session.us_market_session(now) == "closed"


def test_us_market_session_returns_closed_on_xnys_holiday(monkeypatch):
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: None,
    )

    now = dt.datetime(2026, 7, 4, 15, 0, tzinfo=dt.UTC)

    assert market_session.us_market_session(now) == "closed"


def test_us_market_session_honors_half_day_early_close(monkeypatch):
    open_utc = dt.datetime(2025, 11, 28, 14, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.UTC)  # 13:00 ET
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2025, 11, 28, 19, 0, tzinfo=dt.UTC)  # 14:00 ET

    assert market_session.us_market_session(now) == "afterhours"
