"""MCP order tool tests for get_order_history input validation."""

import pytest

from app.mcp_server.tooling import orders_history
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["filled", "cancelled"])
async def test_get_order_history_requires_symbol_for_closed_status(status):
    # filled/cancelled history is broker-symbol-keyed, so it still requires a symbol.
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required when status="):
        await tools["get_order_history"](status=status, order_id="some-id")


def test_validate_history_inputs_allows_all_status_without_symbol():
    # status='all' without a symbol resolves to the operational markets and does
    # not raise (ROB-466).
    (_symbol, _oid, _mh, _side, _days, _lim, market_types, normalized_symbol) = (
        orders_history._validate_history_inputs(
            symbol=None,
            status="all",
            order_id=None,
            market=None,
            side=None,
            days=None,
            limit=50,
        )
    )
    assert normalized_symbol is None
    assert set(market_types) == {"equity_kr", "equity_us"}


@pytest.mark.parametrize("status", ["filled", "cancelled"])
def test_validate_history_inputs_still_requires_symbol_for_closed(status):
    with pytest.raises(ValueError, match="symbol is required when status="):
        orders_history._validate_history_inputs(
            symbol=None,
            status=status,
            order_id=None,
            market=None,
            side=None,
            days=None,
            limit=50,
        )
