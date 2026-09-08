import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.brokers",
    "app.services.manual_holdings_service",
    "app.services.upbit",
    "app.services.market_data",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.paper_order_handler",
    "app.tasks",
]


@pytest.mark.unit
def test_orchestrator_service_forbidden_imports():
    project_root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        "-c",
        "import app.services.research_run_decision_session_service; import sys; print('\\n'.join(sys.modules.keys()))",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        cwd=project_root,
        env=env,
    )
    loaded = result.stdout.splitlines()
    # Sanity check: ensure we actually loaded modules
    assert len(loaded) > 100, "Subprocess did not load expected modules"
    violations = [
        mod for mod in loaded if any(mod.startswith(p) for p in FORBIDDEN_PREFIXES)
    ]
    assert not violations, f"Forbidden imports detected: {violations}"
