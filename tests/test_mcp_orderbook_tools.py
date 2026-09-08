from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from tests._mcp_tooling_support import build_tools


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
    fetch.assert_awaited_once_with("KRW-BTC", "crypto")


@pytest.mark.asyncio
async def test_kr_equity_orderbook_is_no_longer_served(monkeypatch) -> None:
    from app.mcp_server.tooling import market_data_quotes

    fetch = AsyncMock()
    monkeypatch.setattr(market_data_quotes.market_data_service, "get_orderbook", fetch)

    with pytest.raises(ValueError, match="only supports the KRW crypto market"):
        await build_tools()["get_orderbook"]("005930", market="kr")

    fetch.assert_not_awaited()
