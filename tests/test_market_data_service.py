from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.domain_errors import UpstreamUnavailableError
from app.services.market_data import service as market_data
from app.services.us_symbol_universe_service import USSymbolInactiveError


def _frame(timestamp: str = "2026-02-23T15:30:00Z") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [pd.Timestamp(timestamp)],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [10.0],
            "value": [1010.0],
        }
    )


def test_us_resample_handles_mixed_dst_offsets_as_et_naive() -> None:
    from zoneinfo import ZoneInfo

    from app.services.market_data import toss_ohlcv

    frame = pd.DataFrame(
        {
            "datetime": [
                pd.Timestamp("2024-11-01T09:30:00-04:00"),
                pd.Timestamp("2024-11-04T09:30:00-05:00"),
            ],
            "date": [dt.date(2024, 11, 1), dt.date(2024, 11, 4)],
            "time": [dt.time(9, 30), dt.time(9, 30)],
            "open": [100.0, 101.0],
            "high": [100.0, 101.0],
            "low": [100.0, 101.0],
            "close": [100.0, 101.0],
            "volume": [1.0, 1.0],
            "value": [100.0, 101.0],
        }
    )

    resampled = toss_ohlcv._aggregate_minute_candles_frame(
        frame,
        5,
        include_partial=True,
    )
    normalized = toss_ohlcv._normalize_intraday_market_time(
        resampled,
        timezone=ZoneInfo("America/New_York"),
        session_start=dt.time(4, 0),
        session_end=dt.time(20, 0),
        end_date=dt.datetime(2024, 11, 4, 10, 0),
    )

    assert normalized["datetime"].tolist() == [
        pd.Timestamp("2024-11-01T09:30:00"),
        pd.Timestamp("2024-11-04T09:30:00"),
    ]


@pytest.mark.asyncio
async def test_quote_uses_toss_for_equity(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data, "fetch_daily_toss_frame", AsyncMock(return_value=_frame())
    )
    monkeypatch.setattr(
        market_data.toss_market_data,
        "prices",
        AsyncMock(return_value={"005930": SimpleNamespace(price=70000.0)}),
    )

    quote = await market_data.get_quote("5930", "kr")

    assert quote.symbol == "005930"
    assert quote.source == "toss"
    assert quote.price == 70000.0


@pytest.mark.asyncio
async def test_us_quote_validates_active_symbol_before_toss(monkeypatch) -> None:
    lookup = AsyncMock(side_effect=USSymbolInactiveError("AAPL"))
    toss = AsyncMock()
    monkeypatch.setattr(market_data, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data, "fetch_daily_toss_frame", toss)

    with pytest.raises(USSymbolInactiveError):
        await market_data.get_quote("AAPL", "us")

    toss.assert_not_awaited()


@pytest.mark.asyncio
async def test_kr_orderbook_uses_nh_plug_krx_store(monkeypatch) -> None:
    snapshot = {
        "ready": True,
        "asks": [{"price": "70100", "volume": "12"}],
        "bids": [{"price": "70000", "volume": "15"}],
        "totalAskVolume": "12",
        "totalBidVolume": "15",
        "asOf": "2026-02-23T01:30:00Z",
    }
    store = SimpleNamespace(get_snapshot=AsyncMock(return_value=snapshot))
    monkeypatch.setattr(market_data, "get_orderbook_store", lambda: store)

    result = await market_data.get_orderbook("5930", "kr")

    assert result.symbol == "005930"
    assert result.source == "nhplug"
    assert result.venue == "krx"
    assert result.asks[0].price == 70100
    assert result.bids[0].quantity == 15
    assert result.as_of is None
    assert result.price_as_of_source is None
    store.get_snapshot.assert_awaited_once_with(market="KRX", symbol="005930")


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["nxt", "unified", "통합시장"])
async def test_nxt_and_unified_orderbook_are_explicitly_unsupported(venue: str) -> None:
    with pytest.raises(
        market_data.ProviderUnsupportedError, match="provider_unsupported"
    ):
        await market_data.get_orderbook("005930", "kr", venue=venue)


@pytest.mark.asyncio
async def test_kis_only_metrics_are_explicitly_unsupported() -> None:
    with pytest.raises(
        market_data.ProviderUnsupportedError, match="provider_unsupported"
    ):
        await market_data.get_kr_volume_rank()
    with pytest.raises(
        market_data.ProviderUnsupportedError, match="provider_unsupported"
    ):
        await market_data.get_short_interest("005930")


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h"])
async def test_us_intraday_validates_symbol_and_normalizes_toss_time_to_et_naive(
    monkeypatch, period: str
) -> None:
    lookup = AsyncMock(return_value="NASD")
    toss = AsyncMock(return_value=_frame())
    monkeypatch.setattr(market_data, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(market_data, "fetch_us_intraday_toss_frame", toss)

    candles = await market_data.get_ohlcv("AAPL", "us", period, count=5)

    lookup.assert_awaited_once_with("AAPL")
    toss.assert_awaited_once_with(symbol="AAPL", period=period, count=5, end_date=None)
    assert candles[0].timestamp == dt.datetime(2026, 2, 23, 10, 30)
    assert candles[0].source == "toss"


@pytest.mark.asyncio
async def test_us_intraday_empty_normal_response_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        market_data,
        "fetch_us_intraday_toss_frame",
        AsyncMock(return_value=pd.DataFrame()),
    )

    assert await market_data.get_ohlcv("AAPL", "us", "1m", count=5) == []


@pytest.mark.asyncio
async def test_us_intraday_provider_failure_is_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        market_data,
        "fetch_us_intraday_toss_frame",
        AsyncMock(side_effect=RuntimeError("Toss unavailable")),
    )

    with pytest.raises(UpstreamUnavailableError, match="Toss unavailable"):
        await market_data.get_ohlcv("AAPL", "us", "15m", count=5)


@pytest.mark.asyncio
async def test_kr_intraday_empty_toss_response_does_not_fallback(monkeypatch) -> None:
    toss = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(market_data, "fetch_kr_intraday_toss_frame", toss)

    result = await market_data.get_ohlcv("005930", "kr", "1m", count=5)

    assert result == []
    toss.assert_awaited_once()
