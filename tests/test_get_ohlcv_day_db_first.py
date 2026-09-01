from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.market_data import service as market_data
from app.services.us_symbol_universe_service import USSymbolInactiveError


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 11.0],
            "value": [1010.0, 1122.0],
        }
    )


@pytest.mark.asyncio
async def test_kr_day_warm_db_cache_skips_toss(monkeypatch) -> None:
    cache = AsyncMock(return_value=_frame())
    toss = AsyncMock()
    writeback = AsyncMock()
    monkeypatch.setattr(market_data, "cache_first_kr", cache)
    monkeypatch.setattr(market_data, "fetch_daily_toss_frame", toss)
    monkeypatch.setattr(market_data, "write_back_kr", writeback)

    candles = await market_data.get_ohlcv("005930", "kr", "day", count=2)

    assert [row.source for row in candles] == ["db", "db"]
    toss.assert_not_awaited()
    writeback.assert_not_awaited()


@pytest.mark.asyncio
async def test_kr_day_cache_miss_uses_toss_and_writes_toss_source(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "cache_first_kr", AsyncMock(return_value=None))
    toss = AsyncMock(return_value=_frame())
    writeback = AsyncMock()
    monkeypatch.setattr(market_data, "fetch_daily_toss_frame", toss)
    monkeypatch.setattr(market_data, "write_back_kr", writeback)

    candles = await market_data.get_ohlcv("005930", "kr", "day", count=2)

    assert [row.source for row in candles] == ["toss", "toss"]
    toss.assert_awaited_once_with(symbol="005930", count=2, end_date=None)
    writeback.assert_awaited_once()
    assert writeback.await_args.kwargs == {"symbol": "005930", "source": "toss"}


@pytest.mark.asyncio
async def test_us_day_active_gate_runs_before_cache_or_toss(monkeypatch) -> None:
    lookup = AsyncMock(side_effect=USSymbolInactiveError("AAPL"))
    cache = AsyncMock()
    toss = AsyncMock()
    monkeypatch.setattr(market_data, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data, "cache_first_us", cache)
    monkeypatch.setattr(market_data, "fetch_daily_toss_frame", toss)

    with pytest.raises(USSymbolInactiveError):
        await market_data.get_ohlcv("AAPL", "us", "day", count=2)

    cache.assert_not_awaited()
    toss.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_day_cache_miss_uses_toss_and_exchange_partition(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(market_data, "cache_first_us", AsyncMock(return_value=None))
    toss = AsyncMock(return_value=_frame())
    writeback = AsyncMock()
    monkeypatch.setattr(market_data, "fetch_daily_toss_frame", toss)
    monkeypatch.setattr(market_data, "write_back_us", writeback)

    candles = await market_data.get_ohlcv("AAPL", "us", "day", count=2)

    assert [row.source for row in candles] == ["toss", "toss"]
    writeback.assert_awaited_once()
    assert writeback.await_args.kwargs == {
        "symbol": "AAPL",
        "partition": "NASD",
        "source": "toss",
    }


@pytest.mark.asyncio
async def test_week_and_month_use_toss_daily_resampling(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    resample = AsyncMock(return_value=_frame())
    monkeypatch.setattr(market_data, "fetch_resampled_daily_toss_frame", resample)

    candles = await market_data.get_ohlcv("AAPL", "us", "week", count=2)

    assert [row.source for row in candles] == ["toss", "toss"]
    resample.assert_awaited_once_with(
        symbol="AAPL", period="week", count=2, end_date=None
    )
