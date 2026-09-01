from __future__ import annotations

from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_indicators
from tests._mcp_tooling_support import _patch_runtime_attr, build_tools


def _frame(rows: int = 80) -> pd.DataFrame:
    close = pd.Series([100.0 + index * 0.2 + np.sin(index) for index in range(rows)])
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=rows, freq="D").date,
            "open": close - 1.0,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": pd.Series([1000.0 + index for index in range(rows)]),
            "value": pd.Series([100000.0 + index for index in range(rows)]),
        }
    )


@pytest.mark.asyncio
async def test_kr_indicator_result_reports_toss_source(monkeypatch) -> None:
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=_frame()),
    )

    result = await build_tools()["get_indicators"](
        "005930", indicators=["rsi", "adx"], market="kr"
    )

    assert result["source"] == "toss"
    assert "rsi" in result["indicators"]
    assert "adx" in result["indicators"]


@pytest.mark.asyncio
async def test_us_indicator_result_reports_toss_source(monkeypatch) -> None:
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=_frame()),
    )
    _patch_runtime_attr(
        monkeypatch, "fetch_us_live_last_price", AsyncMock(return_value=123.0)
    )

    result = await build_tools()["get_indicators"](
        "AAPL", indicators=["obv"], market="us"
    )

    assert result["source"] == "toss"
    assert result["price"] == 123.0
    assert result["current_price_source"] == "toss_live"


@pytest.mark.asyncio
async def test_plain_alpha_symbol_requires_market(monkeypatch) -> None:
    fetch = AsyncMock()
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch)

    with pytest.raises(ValueError, match="market is required"):
        await build_tools()["get_indicators"]("ETC", indicators=["rsi"])

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_profile_kr_uses_toss_daily(monkeypatch) -> None:
    toss = AsyncMock(return_value=_frame(20))
    monkeypatch.setattr(market_data_indicators, "fetch_daily_toss_frame", toss)

    result = await market_data_indicators._fetch_ohlcv_for_volume_profile(
        "005930", "equity_kr", 20
    )

    assert len(result) == 20
    toss.assert_awaited_once_with(symbol="005930", count=20)


@pytest.mark.asyncio
async def test_volume_profile_us_validates_active_symbol_before_toss(
    monkeypatch,
) -> None:
    lookup = AsyncMock(return_value="NASD")
    toss = AsyncMock(return_value=_frame(20))
    monkeypatch.setattr(market_data_indicators, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data_indicators, "fetch_daily_toss_frame", toss)

    await market_data_indicators._fetch_ohlcv_for_volume_profile(
        "AAPL", "equity_us", 20
    )

    lookup.assert_awaited_once_with("AAPL")
    toss.assert_awaited_once_with(symbol="AAPL", count=20)


@pytest.mark.asyncio
async def test_crypto_indicator_path_remains_upbit(monkeypatch) -> None:
    cache = AsyncMock(return_value=_frame(20))
    monkeypatch.setattr(market_data_indicators, "_cache_first_crypto", cache)

    result = await market_data_indicators._fetch_ohlcv_for_indicators(
        "KRW-BTC", "crypto", count=20
    )

    assert len(result) == 20
    cache.assert_awaited_once_with(symbol="KRW-BTC", count=20, instrument_id=None)
