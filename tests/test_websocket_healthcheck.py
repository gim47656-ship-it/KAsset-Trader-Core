"""Tests for the Upbit-only websocket healthcheck."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run_healthcheck(
    heartbeat_path: str,
    *,
    stale_seconds: float = 90,
) -> subprocess.CompletedProcess[str]:
    script_path = Path(__file__).parent.parent / "scripts" / "websocket_healthcheck.py"
    full_env = os.environ.copy()
    full_env.update(
        {
            "WS_MONITOR_HEARTBEAT_PATH": heartbeat_path,
            "WS_MONITOR_HEARTBEAT_STALE_SECONDS": str(stale_seconds),
        }
    )
    full_env.pop("WS_MONITOR_EXPECT_MODE", None)
    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env=full_env,
    )


def write_heartbeat(
    path: str,
    *,
    updated_at_unix: float | None = None,
    mode: str = "upbit",
    is_running: bool = True,
    upbit_connected: bool | str = True,
) -> None:
    data = {
        "updated_at_unix": updated_at_unix
        if updated_at_unix is not None
        else time.time(),
        "mode": mode,
        "is_running": is_running,
        "upbit_connected": upbit_connected,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as file:
        json.dump(data, file)


class TestWebsocketHealthcheck:
    def test_pass_fresh_upbit_heartbeat(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path)

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 0, result.stderr
        assert "mode=upbit" in result.stdout

    def test_rejects_removed_kis_mode(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, mode="kis")

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 1
        assert "expected=upbit, actual=kis" in result.stderr

    def test_rejects_removed_both_mode(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, mode="both")

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 1
        assert "expected=upbit, actual=both" in result.stderr

    def test_fail_missing_heartbeat_file(self, tmp_path: Path) -> None:
        result = run_healthcheck(str(tmp_path / "nonexistent.json"))

        assert result.returncode == 1
        assert "not found" in result.stderr.lower()

    def test_fail_stale_heartbeat(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, updated_at_unix=time.time() - 120)

        result = run_healthcheck(heartbeat_path, stale_seconds=90)

        assert result.returncode == 1
        assert "stale" in result.stderr.lower()

    def test_fail_future_heartbeat_timestamp(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, updated_at_unix=time.time() + 120)

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 1
        assert "future" in result.stderr.lower()

    def test_pass_small_future_skew_within_tolerance(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, updated_at_unix=time.time() + 0.5)

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 0

    def test_fail_not_running(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, is_running=False)

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 1
        assert "not running" in result.stderr.lower()

    def test_fail_upbit_not_connected(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, upbit_connected=False)

        result = run_healthcheck(heartbeat_path)

        assert result.returncode == 1
        assert "upbit not connected" in result.stderr.lower()

    def test_fail_invalid_json(self, tmp_path: Path) -> None:
        heartbeat_path = tmp_path / "heartbeat.json"
        heartbeat_path.write_text("{ invalid json }")

        result = run_healthcheck(str(heartbeat_path))

        assert result.returncode == 1
        assert "parse" in result.stderr.lower()

    def test_pass_custom_stale_threshold(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(heartbeat_path, updated_at_unix=time.time() - 50)

        result = run_healthcheck(heartbeat_path, stale_seconds=60)

        assert result.returncode == 0
