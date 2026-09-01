from __future__ import annotations

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.unit
@pytest.mark.parametrize("profile", list(McpProfile))
def test_kis_mock_mirror_tool_is_not_registered(profile: McpProfile):
    mcp = DummyMCP()

    register_all_tools(mcp, profile=profile)

    assert "kis_mock_mirror_execute_report" not in mcp.tools
