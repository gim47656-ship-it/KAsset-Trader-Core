"""QA CLI requires an explicit server before touching credentials."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import kasset_qa_token


@pytest.mark.parametrize(
    "args", [[], ["--seed-refresh", "test-refresh"], ["--base", ""]]
)
def test_missing_base_rejects_before_credential_access(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("KASSET_QA_BASE", raising=False)
    monkeypatch.setattr(sys, "argv", ["kasset_qa_token.py", *args])

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Missing server must not read/write credentials or send requests")

    monkeypatch.setattr(kasset_qa_token, "_load", forbidden)
    monkeypatch.setattr(kasset_qa_token, "_save", forbidden)
    monkeypatch.setattr(kasset_qa_token, "_refresh", forbidden)

    with pytest.raises(SystemExit) as exc:
        kasset_qa_token.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--base 또는 KASSET_QA_BASE" in captured.err
    assert captured.out == ""
    assert "test-refresh" not in captured.err


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_explicit_server_keeps_offline_cached_token_cli(
    source: str, tmp_path: Path
) -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800}).encode())
    token = f"test.{payload.decode().rstrip('=')}.unsigned"
    cache = tmp_path / "qa-token.json"
    original = json.dumps({"accessToken": token, "refreshToken": "test-refresh"})
    cache.write_text(original, encoding="utf-8")
    env = os.environ.copy()
    env.pop("KASSET_QA_BASE", None)
    env["KASSET_QA_TOKEN_CACHE"] = str(cache)
    command = [sys.executable, str(Path(kasset_qa_token.__file__).resolve())]
    if source == "argument":
        command.extend(["--base", "https://api.example.net/api/v1"])
    else:
        env["KASSET_QA_BASE"] = "https://api.example.net/api/v1"

    result = subprocess.run(
        command, env=env, capture_output=True, text=True, check=False, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{token}\n"
    assert result.stderr == ""
    assert cache.read_text(encoding="utf-8") == original
