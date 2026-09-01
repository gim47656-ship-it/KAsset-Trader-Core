from __future__ import annotations

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.unit
@pytest.mark.parametrize("profile", list(McpProfile))
def test_active_profiles_register_no_kis_order_tools(profile: McpProfile):
    mcp = DummyMCP()

    register_all_tools(mcp, profile=profile)

    assert not {name for name in mcp.tools if name.startswith("kis_")}
