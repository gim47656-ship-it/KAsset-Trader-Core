from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.live_reconcile_registration import (
    LIVE_RECONCILE_TOOL_NAMES,
    register_live_reconcile_tools,
)
from tests._mcp_tooling_support import DummyMCP


def _registered_tool():
    mcp = DummyMCP()
    register_live_reconcile_tools(cast(Any, mcp))
    assert set(mcp.tools) == LIVE_RECONCILE_TOOL_NAMES
    return mcp.tools["live_reconcile_orders"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_upbit_reconcile_delegates_with_pinned_provider() -> None:
    expected = {"success": True, "counts": {"filled": 1}}
    tool = _registered_tool()

    with patch(
        "app.mcp_server.tooling.live_order_ledger.live_reconcile_orders_impl",
        new=AsyncMock(return_value=expected),
    ) as kernel:
        result = await tool(
            market="crypto",
            broker="upbit",
            symbol="KRW-BTC",
            order_id="upbit-order-id",
            dry_run=False,
            limit=25,
        )

    assert result == expected
    kernel.assert_awaited_once_with(
        market="crypto",
        broker="upbit",
        symbol="KRW-BTC",
        order_id="upbit-order-id",
        dry_run=False,
        limit=25,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_scope_defaults_to_only_supported_pair() -> None:
    expected = {"success": True, "counts": {}}
    tool = _registered_tool()

    with patch(
        "app.mcp_server.tooling.live_order_ledger.live_reconcile_orders_impl",
        new=AsyncMock(return_value=expected),
    ) as kernel:
        result = await tool(order_id="upbit-order-id")

    assert result == expected
    kernel.assert_awaited_once_with(
        market="crypto",
        broker="upbit",
        symbol=None,
        order_id="upbit-order-id",
        dry_run=True,
        limit=100,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "broker"),
    [
        ("us", "kis"),
        ("equity_us", "upbit"),
        ("kr", "toss"),
        ("crypto", "kis"),
    ],
)
async def test_non_upbit_crypto_scope_fails_closed_before_kernel(
    market: str | None,
    broker: str | None,
) -> None:
    tool = _registered_tool()

    with patch(
        "app.mcp_server.tooling.live_order_ledger.live_reconcile_orders_impl",
        new=AsyncMock(),
    ) as kernel:
        result = await tool(market=market, broker=broker, dry_run=False)

    assert result == {
        "success": False,
        "error": "provider_unsupported",
        "provider_unsupported": True,
        "market": market,
        "broker": broker,
        "detail": (
            "live_reconcile_orders supports only market='crypto' with broker='upbit'"
        ),
    }
    kernel.assert_not_awaited()
