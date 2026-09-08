"""Tests for intraday order review background tasks."""

from datetime import datetime

from app.tasks.intraday_order_review_tasks import (
    _is_kr_trading_hours,
    _is_us_trading_hours,
)


class TestTradingHoursCheck:
    def test_kr_trading_hours_weekday(self):
        dt = datetime(2026, 3, 16, 10, 0)
        assert _is_kr_trading_hours(dt) is True

    def test_kr_trading_hours_weekend(self):
        dt = datetime(2026, 3, 15, 10, 0)
        assert _is_kr_trading_hours(dt) is False

    def test_kr_trading_hours_before_open(self):
        dt = datetime(2026, 3, 16, 8, 0)
        assert _is_kr_trading_hours(dt) is False

    def test_us_trading_hours_late_night(self):
        # 2026-03-17 00:30 KST is Monday 11:30 ET (NYSE regular session)
        dt = datetime(2026, 3, 17, 0, 30)
        assert _is_us_trading_hours(dt) is True

    def test_us_trading_hours_early_morning(self):
        # 2026-03-17 04:00 KST is Monday 15:00 ET (NYSE regular session)
        dt = datetime(2026, 3, 17, 4, 0)
        assert _is_us_trading_hours(dt) is True

    def test_us_trading_hours_daytime(self):
        dt = datetime(2026, 3, 16, 12, 0)
        assert _is_us_trading_hours(dt) is False
