from __future__ import annotations

from app.mcp_server.tooling import market_data_quotes


def test_execution_strength_tool_is_physically_unregistered() -> None:
    assert "get_execution_strength" not in market_data_quotes.MARKET_DATA_TOOL_NAMES
    assert not hasattr(market_data_quotes, "_get_execution_strength_impl")
