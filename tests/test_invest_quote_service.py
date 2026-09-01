from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import invest_quote_service as module
from app.services.invest_quote_service import InvestQuoteService
from app.services.us_symbol_universe_service import USSymbolInactiveError


class _TossClient:
    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices
        self.calls: list[list[str]] = []

    async def prices(self, symbols: list[str] | tuple[str, ...]):
        self.calls.append(list(symbols))
        return [
            SimpleNamespace(symbol=symbol, last_price=self._prices[symbol])
            for symbol in symbols
            if symbol in self._prices
        ]


@pytest.mark.asyncio
async def test_kr_prices_resolve_toss_then_snapshot() -> None:
    toss = _TossClient({"005930": 71000.0})
    service = InvestQuoteService(AsyncMock(), toss_client=toss)
    service._snapshot_latest = AsyncMock(  # type: ignore[method-assign]
        return_value={"000660": 180000.0}
    )

    result = await service.fetch_kr_prices(["005930", "000660"])

    assert result == {"005930": 71000.0, "000660": 180000.0}
    assert toss.calls == [["005930", "000660"]]
    service._snapshot_latest.assert_awaited_once_with("kr", ["000660"])


@pytest.mark.asyncio
async def test_us_prices_query_toss_only_for_active_symbols(monkeypatch) -> None:
    async def exchange(symbol: str, _db) -> str:
        if symbol == "DELISTED":
            raise USSymbolInactiveError(symbol)
        return "NASD"

    monkeypatch.setattr(module, "get_us_exchange_by_symbol", exchange)
    toss = _TossClient({"AAPL": 200.0, "DELISTED": 99.0})
    service = InvestQuoteService(AsyncMock(), toss_client=toss)
    service._snapshot_latest = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await service.fetch_us_prices(["AAPL", "DELISTED"])

    assert result == {"AAPL": 200.0, "DELISTED": None}
    assert toss.calls == [["AAPL"]]


@pytest.mark.asyncio
async def test_no_toss_client_uses_snapshot_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "toss_api_enabled", False)
    service = InvestQuoteService(AsyncMock())
    service._snapshot_latest = AsyncMock(return_value={"005930": 70000.0})  # type: ignore[method-assign]

    assert await service.fetch_kr_prices(["005930"]) == {"005930": 70000.0}
