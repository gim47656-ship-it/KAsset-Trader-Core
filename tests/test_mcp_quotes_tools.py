from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes as tools
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from app.services.us_symbol_universe_service import USSymbolInactiveError


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [pd.Timestamp("2024-06-28T14:00:00Z")],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [10.0],
            "value": [1010.0],
        }
    )


@pytest.mark.asyncio
async def test_kr_quote_uses_toss(monkeypatch) -> None:
    point = SimpleNamespace(
        price=70000.0,
        as_of=dt.datetime.now(dt.UTC),
    )
    monkeypatch.setattr(
        tools.toss_market_data,
        "prices",
        AsyncMock(return_value={"005930": point}),
    )
    monkeypatch.setattr(
        tools, "fetch_daily_toss_frame", AsyncMock(return_value=_daily_frame())
    )
    monkeypatch.setattr(tools, "get_kr_nxt_tradability", AsyncMock(return_value={}))
    monkeypatch.setattr(tools, "kr_market_data_state", lambda: "fresh")

    result = await tools._get_quote_impl("5930", "kr")

    assert result["symbol"] == "005930"
    assert result["source"] == "toss"
    assert result["price"] == 70000.0
    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_kr_quote_stale_provider_timestamp_overrides_open_session(
    monkeypatch,
) -> None:
    point = SimpleNamespace(
        price=70000.0,
        as_of=dt.datetime(2024, 6, 28, 5, 0, tzinfo=dt.UTC),
    )
    monkeypatch.setattr(
        tools.toss_market_data,
        "prices",
        AsyncMock(return_value={"005930": point}),
    )
    monkeypatch.setattr(
        tools,
        "fetch_daily_toss_frame",
        AsyncMock(return_value=_daily_frame()),
    )
    monkeypatch.setattr(
        tools,
        "get_kr_nxt_tradability",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(tools, "kr_market_data_state", lambda: "fresh")

    result = await tools._get_quote_impl("005930", "kr")

    assert result["price_usable"] is False
    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "stale_price_asof"


@pytest.mark.asyncio
async def test_us_quote_validates_universe_before_toss(monkeypatch) -> None:
    lookup = AsyncMock(side_effect=USSymbolInactiveError("AAPL"))
    prices = AsyncMock()
    monkeypatch.setattr(tools, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(tools.toss_market_data, "prices", prices)

    result = await tools._get_quote_impl("AAPL", "us")

    assert result["success"] is False
    prices.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_quote_preserves_toss_timestamp_and_universe_venue(
    monkeypatch,
) -> None:
    as_of = dt.datetime.now(dt.UTC)
    lookup = AsyncMock(return_value="NASD")
    monkeypatch.setattr(tools, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(
        tools.toss_market_data,
        "prices",
        AsyncMock(
            return_value={
                "AAPL": SimpleNamespace(price=200.0, as_of=as_of),
            }
        ),
    )
    monkeypatch.setattr(
        tools,
        "fetch_daily_toss_frame",
        AsyncMock(return_value=_daily_frame()),
    )
    monkeypatch.setattr(tools, "us_market_session", lambda now=None: "regular")

    result = await tools._get_quote_impl("AAPL", "us")

    assert result["source"] == "toss"
    assert result["price_source"] == "toss_price"
    assert result["price_as_of"] == as_of.isoformat()
    assert result["venue"] == "NASD"
    assert result["delayed"] is True
    assert result["data_state"] == "fresh"
    lookup.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_us_daily_close_fallback_is_not_marked_fresh_during_session(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tools,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    monkeypatch.setattr(
        tools.toss_market_data,
        "prices",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        tools,
        "fetch_daily_toss_frame",
        AsyncMock(return_value=_daily_frame()),
    )
    monkeypatch.setattr(tools, "us_market_session", lambda now=None: "regular")

    result = await tools._get_quote_impl("AAPL", "us")

    assert result["price"] == 101.0
    assert result["price_source"] == "toss_daily_close"
    assert result["price_as_of"] == "2024-06-28T10:00:00-04:00"
    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "toss_daily_close_fallback"


@pytest.mark.asyncio
async def test_us_live_price_helper_rejects_daily_close_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        tools,
        "_fetch_quote_equity_us",
        AsyncMock(
            return_value={
                "price": 101.0,
                "price_source": "toss_daily_close",
                "data_state": "stale",
            }
        ),
    )

    assert await tools.fetch_us_live_last_price("AAPL") is None


def test_us_stale_toss_timestamp_overrides_open_session(monkeypatch) -> None:
    monkeypatch.setattr(tools, "us_market_session", lambda now=None: "regular")
    quote = {
        "price_source": "toss_price",
        "price_as_of": "2024-06-27T19:59:00-04:00",
    }

    result = tools._tag_us_quote_session(
        quote,
        now=dt.datetime(2024, 6, 28, 14, 0, tzinfo=dt.UTC),
    )

    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "stale_price_asof"


@pytest.mark.asyncio
async def test_us_intraday_ohlcv_uses_toss_after_active_gate(monkeypatch) -> None:
    lookup = AsyncMock(return_value="NASD")
    toss = AsyncMock(return_value=_daily_frame())
    monkeypatch.setattr(tools, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(tools, "fetch_us_intraday_toss_frame", toss)

    result = await tools._get_ohlcv_impl(
        symbol="AAPL", market="us", period="1m", count=1
    )

    assert result["source"] == "toss"
    assert result["instrument_type"] == "equity_us"
    assert len(result["rows"]) == 1
    lookup.assert_awaited_once_with("AAPL")
    toss.assert_awaited_once_with(symbol="AAPL", period="1m", count=1, end_date=None)


@pytest.mark.asyncio
async def test_us_intraday_empty_toss_response_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        tools, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        tools, "fetch_us_intraday_toss_frame", AsyncMock(return_value=pd.DataFrame())
    )

    result = await tools._get_ohlcv_impl("AAPL", 5, "5m", market="us")

    assert result["source"] == "toss"
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_us_intraday_provider_failure_is_error_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        tools, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        tools,
        "fetch_us_intraday_toss_frame",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )

    result = await tools._get_ohlcv_impl("AAPL", 5, "15m", market="us")

    assert result["success"] is False
    assert "toss unavailable" in result["error"]
    assert result["source"] == "toss"


@pytest.mark.asyncio
async def test_kr_orderbook_payload_comes_from_nh_plug_service(monkeypatch) -> None:
    snapshot = OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="nhplug",
        asks=[OrderbookLevel(70100.0, 12.0)],
        bids=[OrderbookLevel(70000.0, 15.0)],
        total_ask_qty=12.0,
        total_bid_qty=15.0,
        bid_ask_ratio=1.25,
        venue="krx",
        venue_label="KRX",
    )
    fetch = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(tools.market_data_service, "get_orderbook", fetch)

    result = await tools._get_orderbook_impl("5930", "kr")

    assert result["source"] == "nhplug"
    assert result["venue"] == "krx"
    assert "kis_market_code" not in result
    fetch.assert_awaited_once_with("005930", "kr", venue=None)


def test_kis_only_execution_strength_tool_is_not_registered() -> None:
    assert "get_execution_strength" not in tools.MARKET_DATA_TOOL_NAMES
