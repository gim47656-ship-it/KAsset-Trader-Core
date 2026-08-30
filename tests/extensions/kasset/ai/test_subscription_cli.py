"""Contract tests for the subscription-backed Codex CLI bridge."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.extensions.kasset.ai import factory
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.models import SkillRequest, SkillResult
from app.extensions.kasset.ai.subscription_cli import build_cli_invoker

_REQUEST = SkillRequest(
    skill="technical_analysis",
    instruction="Analyze only the supplied evidence.",
    symbol="005930",
    market="kr",
    context={"timeframe": "1d"},
    correlation_id="corr-cli-1",
)


def _script_command(tmp_path: Path, source: str) -> list[str]:
    script = tmp_path / "fake_subscription_cli.py"
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_cli_invoker_returns_last_valid_skill_result(tmp_path: Path) -> None:
    command = _script_command(
        tmp_path,
        """
import json
import sys

prompt = sys.stdin.read()
assert "read-only market-analysis layer" in prompt
assert '"skill":"technical_analysis"' in prompt
assert '"correlation_id"' not in prompt
print(json.dumps({"summary": "stale result", "signal": "HOLD"}))
print("diagnostic noise")
print(json.dumps({
    "summary": "Momentum is improving.",
    "signal": "BUY",
    "confidence": 0.73,
    "rationale": ["RSI recovered"],
    "metadata": {"source": "fake-cli"},
}))
""",
    )

    result = await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)

    assert isinstance(result, SkillResult)
    assert result.skill == "technical_analysis"
    assert result.provider == "subscription"
    assert result.summary == "Momentum is improving."
    assert result.signal == "BUY"
    assert result.confidence == 0.73
    assert result.rationale == ["RSI recovered"]
    assert result.metadata == {"source": "fake-cli"}
    assert result.correlation_id == "corr-cli-1"


@pytest.mark.asyncio
async def test_cli_invoker_accepts_json_code_fence(tmp_path: Path) -> None:
    command = _script_command(
        tmp_path,
        """
print("some harmless preface")
print("```json")
print('{"summary": "Fenced result", "signal": "WATCH", "rationale": []}')
print("```")
""",
    )

    result = await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)

    assert result.summary == "Fenced result"
    assert result.signal == "WATCH"


@pytest.mark.asyncio
async def test_cli_invoker_nonzero_exit_fails_closed(tmp_path: Path) -> None:
    command = _script_command(
        tmp_path,
        """
import sys

print("partial output")
print("failure details", file=sys.stderr)
raise SystemExit(7)
""",
    )

    with pytest.raises(ValueError, match="exit_code=7"):
        await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)


@pytest.mark.asyncio
async def test_cli_invoker_missing_executable_is_unavailable(tmp_path: Path) -> None:
    missing_executable = tmp_path / "missing-codex-executable"

    with pytest.raises(AiProviderUnavailable, match="could not start"):
        await build_cli_invoker([str(missing_executable)], timeout_seconds=2.0)(
            _REQUEST
        )


@pytest.mark.asyncio
async def test_cli_invoker_timeout_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False
            self.input: bytes | None = None

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            self.input = input
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    process = HangingProcess()

    async def fake_create_subprocess_exec(
        *command: str, **kwargs: Any
    ) -> HangingProcess:
        assert command == ("fake-codex", "exec")
        assert kwargs == {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(AiProviderUnavailable, match="timed out"):
        await build_cli_invoker(["fake-codex", "exec"], timeout_seconds=0.01)(_REQUEST)

    assert process.killed is True
    assert process.input is not None


@pytest.mark.asyncio
async def test_cli_invoker_without_json_fails_closed(tmp_path: Path) -> None:
    command = _script_command(tmp_path, 'print("plain text only")\n')

    with pytest.raises(ValueError, match="no JSON object"):
        await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)


@pytest.mark.asyncio
async def test_cli_invoker_schema_error_fails_closed(tmp_path: Path) -> None:
    command = _script_command(
        tmp_path,
        'print(\'{"signal": "BUY", "confidence": 0.5}\')\n',
    )

    with pytest.raises(ValueError, match="invalid SkillResult"):
        await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)


@pytest.mark.asyncio
async def test_cli_failure_does_not_expose_process_output(tmp_path: Path) -> None:
    stdout_secret = "stdout-auth-json-secret-value"
    stderr_secret = "stderr-token-secret-value"
    command = _script_command(
        tmp_path,
        f"""
import sys

print({json.dumps(stdout_secret)})
print({json.dumps(stderr_secret)}, file=sys.stderr)
raise SystemExit(9)
""",
    )

    with pytest.raises(ValueError) as excinfo:
        await build_cli_invoker(command, timeout_seconds=2.0)(_REQUEST)

    message = str(excinfo.value)
    assert "exit_code=9" in message
    assert stdout_secret not in message
    assert stderr_secret not in message


@pytest.mark.asyncio
async def test_factory_builds_configured_cli_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    async def configured_invoker(request: SkillRequest) -> SkillResult:
        return SkillResult(
            skill=request.skill,
            provider="subscription",
            summary="configured CLI",
            correlation_id=request.correlation_id,
        )

    def fake_build_cli_invoker(
        command: list[str], timeout_seconds: float
    ) -> factory.SubscriptionInvoker:
        calls.append((command, timeout_seconds))
        return configured_invoker

    monkeypatch.setattr(settings, "KASSET_AI_PROVIDER_MODE", "subscription")
    monkeypatch.setattr(
        settings,
        "KASSET_AI_SUBSCRIPTION_CMD",
        'codex exec --sandbox "read only" -',
    )
    monkeypatch.setattr(settings, "KASSET_AI_SUBSCRIPTION_TIMEOUT_SECONDS", 17.5)
    monkeypatch.setattr(factory, "build_cli_invoker", fake_build_cli_invoker)
    monkeypatch.setattr(
        factory, "build_api_provider_chain", lambda *, snapshot=None: None
    )

    result = await factory.build_ai_provider_router().run_skill(_REQUEST)

    assert result.summary == "configured CLI"
    assert calls == [(["codex", "exec", "--sandbox", "read only", "-"], 17.5)]


@pytest.mark.asyncio
async def test_factory_leaves_subscription_unconfigured_for_empty_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_builder(
        command: list[str], timeout_seconds: float
    ) -> factory.SubscriptionInvoker:
        raise AssertionError((command, timeout_seconds))

    monkeypatch.setattr(settings, "KASSET_AI_PROVIDER_MODE", "subscription")
    monkeypatch.setattr(settings, "KASSET_AI_SUBSCRIPTION_CMD", "   ")
    monkeypatch.setattr(factory, "build_cli_invoker", unexpected_builder)
    monkeypatch.setattr(
        factory, "build_api_provider_chain", lambda *, snapshot=None: None
    )

    router = factory.build_ai_provider_router()

    with pytest.raises(AiProviderUnavailable, match="bridge is not configured"):
        await router.run_skill(_REQUEST)
