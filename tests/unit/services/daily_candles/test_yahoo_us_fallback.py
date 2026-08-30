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
