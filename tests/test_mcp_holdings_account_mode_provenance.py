"""Portfolio provenance and KIS cutover guards."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import account_modes, portfolio_holdings
from tests._mcp_tooling_support import DummyMCP


def test_order_routability_uses_active_brokers_only(monkeypatch):
    monkeypatch.setattr(
        account_modes.settings,
        "toss_live_order_mutations_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        portfolio_holdings.settings,
        "toss_live_order_mutations_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        portfolio_holdings.settings,
        "toss_api_enabled",
        True,
        raising=False,
    )

    assert portfolio_holdings._account_order_routable(source="toss_api") is True
    assert portfolio_holdings._account_order_routable(source="upbit_api") is True
    assert portfolio_holdings._account_order_routable(source="manual") is False
    assert (
        portfolio_holdings._account_order_routable(
            source="kis_api",
            broker="kis",
        )
        is False
    )


def test_active_provenance_labels_ignore_legacy_routing_mode():
    assert (
        portfolio_holdings._provenance_account_mode(
            broker="upbit",
            source="upbit_api",
            routing_mode="toss_live",
        )
        == "upbit_live"
    )
    assert (
        portfolio_holdings._provenance_account_mode(
            broker="toss",
            source="toss_api",
            routing_mode="toss_live",
        )
        == "toss_live"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_get_holdings_rejects_kis_mode_without_collecting(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
):
    collect = AsyncMock()
    monkeypatch.setattr(portfolio_holdings, "_get_holdings_impl", collect)
    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_holdings"](account_mode=account_mode)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["account_mode"] == account_mode
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_holdings_rejects_kis_account_filter_without_collecting(monkeypatch):
    collect = AsyncMock()
    monkeypatch.setattr(portfolio_holdings, "_get_holdings_impl", collect)
    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)

    result = await mcp.tools["get_holdings"](account="kis")

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    collect.assert_not_awaited()
