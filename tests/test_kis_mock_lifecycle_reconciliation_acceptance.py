"""KIS mock lifecycle surface removal acceptance."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_kis_mock_order_and_reconcile_surface_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    forbidden = AsyncMock(side_effect=AssertionError("legacy KIS path called"))
    monkeypatch.setattr(
        orders_registration.order_execution, "_place_order_impl", forbidden
    )
    tools = build_tools()

    assert "kis_mock_reconciliation_run" not in tools
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        quantity=10,
        price=100.0,
        account_mode="kis_mock",
        dry_run=False,
    )

    assert result == {
        "success": False,
        "error": "provider kis is not operational",
        "account_mode": "kis_mock",
        "symbol": "005930",
    }
    forbidden.assert_not_awaited()
