from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.daily_candles.yahoo_us_fallback import (
    fetch_us_daily_yahoo_fallback,
)


class TestFetchUsDailyYahooFallback:
    @pytest.mark.asyncio
    async def test_returns_canonical_rows_with_adj_close(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=3, freq="B"),
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "adj_close": [99.0, 100.0, float("nan")],
                "volume": [1000, 1100, 1200],
            }
        )
        fetcher = AsyncMock(return_value=sample)
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=fetcher,
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="ILLIQUIDETF", n=3)
        fetcher.assert_awaited_once_with(
            ticker="ILLIQUIDETF",
            days=4,
            period="day",
            use_cache=False,
        )
        assert len(rows) == 3
        assert rows[0].adj_close == pytest.approx(99.0)
        assert rows[-1].adj_close is None
        assert all(r.symbol == "ILLIQUIDETF" for r in rows)

    @pytest.mark.asyncio
    async def test_handles_missing_adj_close_column(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=2, freq="B"),
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1000, 1100],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="X", n=2)
        assert all(r.adj_close is None for r in rows)

    @pytest.mark.asyncio
    async def test_preserves_zero_ohlc_for_readiness_validation(self):
        sample = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-05-01"]),
                "open": [0.0],
                "high": [101.0],
                "low": [0.0],
                "close": [100.0],
                "adj_close": [99.0],
                "volume": [1000],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="X", n=1)

        assert rows[0].open == 0.0
        assert rows[0].low == 0.0

    @pytest.mark.asyncio
    async def test_recovers_exact_completed_terminal_row_from_validated_metadata(self):
        sample = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
                "open": [100.0, 101.0],
                "high": [101.0, 103.0],
                "low": [99.0, 100.0],
                "close": [100.0, float("nan")],
                "adj_close": [99.5, float("nan")],
                "volume": [1000, 1200],
            }
        )
        metadata = {
            "regularMarketPrice": 102.0,
            "previousClose": 100.0,
            "regularMarketDayLow": 100.0,
            "regularMarketDayHigh": 103.0,
            "currentTradingPeriod": {
                "regular": {
                    "end": int(datetime(2026, 8, 28, 20, 0, tzinfo=UTC).timestamp())
                }
            },
        }
        metadata_fetcher = AsyncMock(return_value=metadata)
        with (
            patch(
                "app.services.brokers.yahoo.client.fetch_ohlcv",
                new=AsyncMock(return_value=sample),
            ),
            patch(
                "app.services.brokers.yahoo.client.fetch_history_metadata",
                new=metadata_fetcher,
            ),
        ):
            rows = await fetch_us_daily_yahoo_fallback(
                symbol="AMD",
                n=2,
                now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            )

        assert len(rows) == 2
        assert rows[-1].time_utc.date().isoformat() == "2026-08-28"
        assert rows[-1].close == pytest.approx(102.0)
        assert rows[-1].adj_close == pytest.approx(102.0)
        metadata_fetcher.assert_awaited_once_with("AMD")

    @pytest.mark.asyncio
    async def test_rejects_terminal_row_when_metadata_session_end_mismatches(self):
        sample = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
                "open": [100.0, 101.0],
                "high": [101.0, 103.0],
                "low": [99.0, 100.0],
                "close": [100.0, float("nan")],
                "adj_close": [99.5, float("nan")],
                "volume": [1000, 1200],
            }
        )
        metadata = {
            "regularMarketPrice": 102.0,
            "previousClose": 100.0,
            "regularMarketDayLow": 100.0,
            "regularMarketDayHigh": 103.0,
            "currentTradingPeriod": {
                "regular": {
                    "end": int(datetime(2026, 8, 28, 19, 0, tzinfo=UTC).timestamp())
                }
            },
        }
        with (
            patch(
                "app.services.brokers.yahoo.client.fetch_ohlcv",
                new=AsyncMock(return_value=sample),
            ),
            patch(
                "app.services.brokers.yahoo.client.fetch_history_metadata",
                new=AsyncMock(return_value=metadata),
            ),
        ):
            rows = await fetch_us_daily_yahoo_fallback(
                symbol="AMD",
                n=2,
                now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            )

        assert len(rows) == 1
        assert rows[-1].time_utc.date().isoformat() == "2026-08-27"

    @pytest.mark.asyncio
    async def test_recovers_ex_dividend_row_and_normalizes_metadata_ohlc_bounds(self):
        sample = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
                "open": [755_000.0, 754_105.75],
                "high": [760_000.0, 759_544.9375],
                "low": [750_000.0, 754_553.875],
                "close": [758_000.0, float("nan")],
                "adj_close": [754_000.0, float("nan")],
                "volume": [8_641_700, 13_950_121],
            }
        )
        metadata = {
            "regularMarketPrice": 757_985.0,
            "previousClose": 754_000.0,
            # Yahoo metadata rounds large prices more coarsely than history.
            "regularMarketDayLow": 754_553.9,
            "regularMarketDayHigh": 759_544.9,
            "currentTradingPeriod": {
                "regular": {
                    "end": int(datetime(2026, 8, 28, 20, 0, tzinfo=UTC).timestamp())
                }
            },
        }
        with (
            patch(
                "app.services.brokers.yahoo.client.fetch_ohlcv",
                new=AsyncMock(return_value=sample),
            ),
            patch(
                "app.services.brokers.yahoo.client.fetch_history_metadata",
                new=AsyncMock(return_value=metadata),
            ),
        ):
            rows = await fetch_us_daily_yahoo_fallback(
                symbol="NEE",
                n=2,
                now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            )

        assert len(rows) == 2
        assert rows[-1].open == pytest.approx(754_105.75)
        assert rows[-1].high == pytest.approx(759_544.9375)
        assert rows[-1].low == pytest.approx(754_105.75)
        assert rows[-1].close == pytest.approx(757_985.0)
        assert rows[-1].adj_close == pytest.approx(757_985.0)
