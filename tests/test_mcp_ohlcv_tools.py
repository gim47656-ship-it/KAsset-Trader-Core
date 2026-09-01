from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import market_data_quotes
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": dt.date(2024, 1, 2),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )


def _minute_frame(*, aware: bool = False) -> pd.DataFrame:
    timestamp = pd.Timestamp(
        "2024-06-03T14:30:00+00:00" if aware else "2024-06-03 09:30:00"
    )
    return pd.DataFrame(
        [
            {
                "datetime": timestamp,
                "date": timestamp.date(),
                "time": timestamp.time(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 20.0,
                "value": 2010.0,
                "session": "REGULAR",
            }
        ]
    )


@pytest.mark.asyncio
async def test_crypto_ohlcv_keeps_upbit_and_caps_count(monkeypatch) -> None:
    fetch = AsyncMock(return_value=_daily_frame())
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", fetch)

    result = await market_data_quotes._get_ohlcv_impl("KRW-BTC", count=300)

    fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="day", end_date=None
    )
    assert result["source"] == "upbit"
    assert result["instrument_type"] == "crypto"
    assert result["count"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h"])
async def test_kr_intraday_uses_toss(period: str, monkeypatch) -> None:
    fetch = AsyncMock(return_value=_minute_frame())
    monkeypatch.setattr(market_data_quotes, "fetch_kr_intraday_toss_frame", fetch)

    result = await market_data_quotes._get_ohlcv_impl(
        "005930", market="kr", period=period, count=250
    )

    fetch.assert_awaited_once_with(
        symbol="005930", period=period, count=200, end_date=None
    )
    assert result["source"] == "toss"
    assert result["instrument_type"] == "equity_kr"
    assert result["period"] == period


@pytest.mark.asyncio
async def test_kr_daily_and_resampled_periods_use_toss(monkeypatch) -> None:
    daily = AsyncMock(return_value=_daily_frame())
    resampled = AsyncMock(return_value=_daily_frame())
    monkeypatch.setattr(market_data_quotes, "fetch_daily_toss_frame", daily)
    monkeypatch.setattr(
        market_data_quotes, "fetch_resampled_daily_toss_frame", resampled
    )

    day = await market_data_quotes._get_ohlcv_impl(
        "005930", market="kr", period="day", count=5
    )
    month = await market_data_quotes._get_ohlcv_impl(
        "005930", market="kr", period="month", count=5
    )

    assert day["source"] == "toss"
    assert month["source"] == "toss"
    daily.assert_awaited_once_with(symbol="005930", count=5, end_date=None)
    resampled.assert_awaited_once_with(
        symbol="005930", period="month", count=5, end_date=None
    )


@pytest.mark.asyncio
async def test_us_intraday_checks_active_symbol_before_toss(monkeypatch) -> None:
    lookup = AsyncMock(
        side_effect=USSymbolNotRegisteredError("US symbol 'AAPL' is not registered")
    )
    fetch = AsyncMock()
    monkeypatch.setattr(market_data_quotes, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data_quotes, "fetch_us_intraday_toss_frame", fetch)

    result = await market_data_quotes._get_ohlcv_impl(
        "AAPL", market="us", period="5m", count=5
    )

    fetch.assert_not_awaited()
    assert result["source"] == "toss"
    assert result["instrument_type"] == "equity_us"
    assert "not registered" in result["error"]


@pytest.mark.asyncio
async def test_us_intraday_uses_toss_et_naive_frame(
    monkeypatch,
) -> None:
    lookup = AsyncMock(return_value="NASD")
    fetch = AsyncMock(return_value=_minute_frame())
    monkeypatch.setattr(market_data_quotes, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data_quotes, "fetch_us_intraday_toss_frame", fetch)

    result = await market_data_quotes._get_ohlcv_impl(
        "AAPL",
        market="us",
        period="5m",
        count=20,
        end_date="2024-06-03",
    )

    lookup.assert_awaited_once_with("AAPL")
    fetch.assert_awaited_once_with(
        symbol="AAPL",
        period="5m",
        count=20,
        end_date=dt.datetime(2024, 6, 3, 20, 0),
    )
    assert result["source"] == "toss"
    assert result["rows"][0]["datetime"] == "2024-06-03T09:30:00"


@pytest.mark.asyncio
async def test_us_daily_uses_toss_after_active_gate(monkeypatch) -> None:
    lookup = AsyncMock(return_value="NYSE")
    fetch = AsyncMock(return_value=_daily_frame())
    monkeypatch.setattr(market_data_quotes, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data_quotes, "fetch_daily_toss_frame", fetch)

    result = await market_data_quotes._get_ohlcv_impl(
        "BRK-B", market="us", period="day", count=5
    )

    lookup.assert_awaited_once_with("BRK.B")
    fetch.assert_awaited_once_with(symbol="BRK.B", count=5, end_date=None)
    assert result["symbol"] == "BRK.B"
    assert result["source"] == "toss"


@pytest.mark.asyncio
async def test_toss_failure_is_reported_without_provider_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data_quotes,
        "fetch_daily_toss_frame",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )

    result = await market_data_quotes._get_ohlcv_impl(
        "005930", market="kr", period="day", count=5
    )

    assert result == {
        "error": "toss unavailable",
        "source": "toss",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_empty_toss_response_is_a_normal_empty_result(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data_quotes,
        "fetch_daily_toss_frame",
        AsyncMock(return_value=pd.DataFrame()),
    )

    result = await market_data_quotes._get_ohlcv_impl(
        "005930", market="kr", period="day", count=5
    )

    assert result["source"] == "toss"
    assert result["count"] == 5
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_rejects_invalid_period_before_provider(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(market_data_quotes, "fetch_daily_toss_frame", fetch)

    with pytest.raises(ValueError, match="supported only for crypto"):
        await market_data_quotes._get_ohlcv_impl(
            "005930", market="kr", period="4h", count=5
        )

    fetch.assert_not_awaited()
