from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from app.services.market_data.service import ProviderUnsupportedError
from tests._mcp_tooling_support import build_tools


def _nh_snapshot(*, empty: bool = False) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="nhplug",
        asks=[] if empty else [OrderbookLevel(price=70100, quantity=123)],
        bids=[] if empty else [OrderbookLevel(price=70000, quantity=321)],
        total_ask_qty=0 if empty else 1000,
        total_bid_qty=0 if empty else 1500,
        bid_ask_ratio=None if empty else 1.5,
        venue="krx",
        venue_label="KRX",
        is_empty_book=empty,
        requires_final_recheck=empty,
        empty_reason="empty_nh_orderbook" if empty else None,
    )


@pytest.mark.asyncio
async def test_get_orderbook_returns_nh_plug_krx_payload(monkeypatch) -> None:
    from app.mcp_server.tooling import market_data_quotes

    fetch = AsyncMock(return_value=_nh_snapshot())
    monkeypatch.setattr(market_data_quotes.market_data_service, "get_orderbook", fetch)

    result = await build_tools()["get_orderbook"]("5930", market="kr")

    assert result["symbol"] == "005930"
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "nhplug"
    assert result["venue"] == "krx"
    assert result["venue_label"] == "KRX"
    assert result["asks"] == [{"price": 70100, "quantity": 123}]
    assert result["bids"] == [{"price": 70000, "quantity": 321}]
    assert result["pressure"] == "buy"
    assert result["spread"] == 100
    assert "kis_market_code" not in result
    assert "source_tr_id" not in result
    assert "source_endpoint" not in result
    assert "expected_price" not in result
    assert "expected_qty" not in result
    assert "price_as_of_source" not in result
    assert "as_of" not in result
    fetch.assert_awaited_once_with("005930", "kr", venue=None)


@pytest.mark.asyncio
async def test_empty_nh_book_requires_final_recheck(monkeypatch) -> None:
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_nh_snapshot(empty=True)),
    )

    result = await build_tools()["get_orderbook"]("005930", market="kr")

    assert result["source"] == "nhplug"
    assert result["is_empty_book"] is True
    assert result["requires_final_recheck"] is True
    assert result["empty_reason"] == "empty_nh_orderbook"


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["nxt", "unified", "통합시장"])
async def test_nxt_and_unified_orderbook_are_explicitly_unsupported(
    monkeypatch, venue: str
) -> None:
    from app.mcp_server.tooling import market_data_quotes

    fetch = AsyncMock(
        side_effect=ProviderUnsupportedError(
            "provider_unsupported: NH PLUG orderbook supports KRX only"
        )
    )
    monkeypatch.setattr(market_data_quotes.market_data_service, "get_orderbook", fetch)

    result = await build_tools()["get_orderbook"]("005930", market="kr", venue=venue)

    assert result["success"] is False
    assert result["source"] == "nhplug"
    assert "provider_unsupported" in result["error"]
    fetch.assert_awaited_once_with("005930", "kr", venue=venue)


@pytest.mark.asyncio
async def test_crypto_orderbook_remains_upbit(monkeypatch) -> None:
    from app.mcp_server.tooling import market_data_quotes

    snapshot = OrderbookSnapshot(
        symbol="KRW-BTC",
        instrument_type="crypto",
        source="upbit",
        asks=[OrderbookLevel(price=10.5, quantity=1.0)],
        bids=[OrderbookLevel(price=10.0, quantity=2.0)],
        total_ask_qty=1.0,
        total_bid_qty=2.0,
        bid_ask_ratio=2.0,
    )
    fetch = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(market_data_quotes.market_data_service, "get_orderbook", fetch)

    result = await build_tools()["get_orderbook"]("KRW-BTC", market="crypto")

    assert result["source"] == "upbit"
    assert result["instrument_type"] == "crypto"
    fetch.assert_awaited_once_with("KRW-BTC", "crypto", venue=None)
