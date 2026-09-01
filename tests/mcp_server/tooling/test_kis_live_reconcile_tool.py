import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.unit
@pytest.mark.parametrize("profile", list(McpProfile))
def test_kis_live_reconcile_tool_is_not_registered(profile: McpProfile):
    mcp = DummyMCP()

    register_all_tools(mcp, profile=profile)

    assert "kis_live_reconcile_orders" not in mcp.tools
