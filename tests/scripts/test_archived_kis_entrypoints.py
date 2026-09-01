from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
ARCHIVED_ENTRYPOINTS = (
    "kis_mock_us_cash_probe",
    "kis_overseas_premarket_probe",
    "kis_overseas_price_smoke",
    "kis_websocket_mock_smoke",
    "kis_mock_overseas_holdings_delta_smoke",
    "kis_mock_runner",
    "kis_mock_scalping_daemon",
    "kis_mock_scalping_ws_smoke",
    "kis_mock_fill_evidence_smoke",
    "kis_mock_holdings_delta_smoke",
    "kis_mock_open_order_probe",
    "kis_lean_once",
    "kis_live_auto_reconcile",
    "kis_mock_execution_consumer",
    "rob596_orderable_diagnostic",
    "rob278_kr_dryrun",
    "krb1_p0_liquidity_selector",
    "sync_kr_lifecycle_actions",
)


@pytest.mark.parametrize("entrypoint", ARCHIVED_ENTRYPOINTS)
def test_archived_kis_entrypoint_is_absent_from_runtime_and_fails_closed(
    entrypoint: str,
) -> None:
    assert not (SCRIPTS_DIR / f"{entrypoint}.py").exists()

    result = subprocess.run(
        [sys.executable, "-m", f"scripts._archive_kis.{entrypoint}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == (
        f"archived KIS entrypoint is disabled: {entrypoint}"
    )
