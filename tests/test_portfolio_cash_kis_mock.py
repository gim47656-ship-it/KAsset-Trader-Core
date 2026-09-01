"""KIS cash/account surfaces are non-operational after the broker cutover."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import portfolio_cash, portfolio_holdings
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize("tool_name", ["get_cash_balance", "get_available_capital"])
async def test_public_cash_tools_reject_kis_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
    tool_name: str,
):
    cash = AsyncMock()
    capital = AsyncMock()
    monkeypatch.setattr(portfolio_holdings, "_get_cash_balance_impl", cash)
    monkeypatch.setattr(portfolio_holdings, "_get_available_capital_impl", capital)
    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools[tool_name](account_mode=account_mode)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["account_mode"] == account_mode
    cash.assert_not_awaited()
    capital.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["kis", "kis_domestic", "kis_overseas"])
async def test_public_cash_tools_reject_kis_account_filter(account: str):
    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_cash_balance"](account=account)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"


@pytest.mark.asyncio
async def test_private_cash_collector_rejects_mock_mode():
    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
