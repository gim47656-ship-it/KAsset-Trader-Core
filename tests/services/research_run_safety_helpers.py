"""Shared safety-test helpers for research run import boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RESEARCH_RUN_FORBIDDEN_PREFIXES = [
    "app.services.upbit",
    "app.services.brokers",
    "app.services.paper_trading_service",
    "app.services.agent_gateway",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.pending_orders_service",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.orders_registration",
    "app.tasks",
    "redis",
]

NEWS_BRIEF_FORBIDDEN_PREFIXES = [
    prefix
    for prefix in RESEARCH_RUN_FORBIDDEN_PREFIXES
    if prefix
    not in {
        "app.services.crypto_trade_cooldown_service",
        "app.services.pending_orders_service",
        "app.mcp_server.tooling.orders_history",
        "app.mcp_server.tooling.orders_registration",
    }
]


def assert_module_does_not_import_forbidden(
    module_name: str,
    forbidden_prefixes: list[str],
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = f"""
import importlib
import json
import sys

importlib.import_module({module_name!r})
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in forbidden_prefixes
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"
