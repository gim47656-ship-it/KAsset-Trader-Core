"""MCP server profile definitions.

Profiles gate which tool subsets are registered at startup.
Profile selection is driven by the MCP_PROFILE env var (default: "default").

ROB-1239: for what `route_request`'s `blocked_actions` does and does not mean
relative to this file's registration, see the canonical statement in
`app/mcp_server/tooling/route_request_registration.py`'s `route_request` tool
`description=` string.
"""

from __future__ import annotations

from enum import StrEnum


class McpProfile(StrEnum):
    DEFAULT = "default"
    CRYPTO = "crypto"
    DB_PAPER = "db-paper"
    SHADOW_REPLAY = "shadow-replay"
    ANALYSIS_READONLY = "analysis_readonly"
    ACCOUNT_READ = "account_read"
    TRADINGCODEX_EXECUTION = "tradingcodex_execution"
    # ROB-1286 — the surface a watch-fire repricing session is spawned with.
    # Closed world: exactly the proposal-only allowlist, so the session can
    # create a proposal and cannot reach any broker order tool.
    WATCH_REPRICING = "watch_repricing"


def resolve_mcp_profile(env: str | None) -> McpProfile:
    """Resolve MCP_PROFILE env value to McpProfile.

    Empty/None → DEFAULT. Invalid string → ValueError.
    """
    normalized = (env or "").strip()
    if not normalized:
        return McpProfile.DEFAULT
    try:
        return McpProfile(normalized)
    except ValueError:
        allowed = ", ".join(f'"{p}"' for p in McpProfile)
        raise ValueError(
            f"Unknown MCP_PROFILE '{normalized}'; allowed values: {allowed}"
        )


__all__ = ["McpProfile", "resolve_mcp_profile"]
