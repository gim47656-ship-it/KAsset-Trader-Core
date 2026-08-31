"""AI provider MCP sidecar 계약 테스트.

sidecar가 실제로 지켜야 하는 것만 본다.

* 노출 도구가 `run_skill` 하나뿐이고 broker/account/order 도구가 없다.
* bearer 토큰 없이는 `/mcp`를 쓸 수 없고 `/health`는 토큰이 필요 없다.
* 기존 `McpStructuredJsonClient`가 그대로 붙어 임의 `response_schema`를
  만족하는 structuredContent를 받는다.
* 실패가 가용성/계약으로 분류되고 CLI stdout/stderr가 절대 새지 않는다.
* 동시 실행 상한이 HTTP 429로 드러나고 슬롯이 반납된다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient
from app.extensions.kasset.ai_mcp_sidecar.config import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_SECONDS,
    SidecarConfig,
    SidecarConfigError,
    load_config,
    verify_command_executable,
)
from app.extensions.kasset.ai_mcp_sidecar.runner import (
    MAX_CONTEXT_BYTES,
    SidecarContractError,
    SidecarUnavailable,
    SkillRunner,
    build_invocation,
    build_prompt,
)
from app.extensions.kasset.ai_mcp_sidecar.server import (
    TOOL_NAME,
    build_http_app,
    build_server,
)

_TOKEN = "sidecar-test-token"
_BASE_URL = "http://ai-mcp.test"

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["action", "confidence"],
}

_ARGUMENTS: dict[str, Any] = {
    "skill": "kasset_tier_verdict",
    "instruction": "Analyze only the supplied evidence.",
    "context": {"kind": "trade_review", "payload": {"symbol": "005930"}},
    "response_schema": _RESPONSE_SCHEMA,
}

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_VALID_OUTPUT = '{"action": "HOLD", "confidence": 0.42}'


def _config(command: tuple[str, ...], **overrides: Any) -> SidecarConfig:
    settings: dict[str, Any] = {
        "command": command,
        "token": _TOKEN,
        "host": "127.0.0.1",
        "port": 8770,
        "path": "/mcp",
        "timeout_seconds": 20.0,
        "max_concurrency": 2,
    }
    settings.update(overrides)
    return SidecarConfig(**settings)


@pytest.fixture
def fake_cli(tmp_path: Path) -> Callable[..., tuple[str, ...]]:
    """가짜 구독 CLI 스크립트를 만들어 argv로 돌려준다."""

    def build(
        source: str, *, name: str = "fake_subscription_cli.py"
    ) -> tuple[str, ...]:
        script = tmp_path / name
        script.write_text(source, encoding="utf-8")
        return (sys.executable, str(script))

    return build


@asynccontextmanager
async def _serving(config: SidecarConfig) -> AsyncIterator[httpx.AsyncClient]:
    """sidecar ASGI app을 lifespan과 함께 띄우고 HTTP client를 준다."""

    app = build_http_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=_BASE_URL,
        ) as client:
            yield client


@asynccontextmanager
async def _provider_client(
    config: SidecarConfig,
    monkeypatch: pytest.MonkeyPatch,
    **client_kwargs: Any,
) -> AsyncIterator[McpStructuredJsonClient]:
    """운영 provider client를 sidecar ASGI app에 그대로 붙인다."""

    app = build_http_app(config)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = httpx.ASGITransport(app=app)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    async with app.router.lifespan_context(app):
        yield McpStructuredJsonClient(
            url=f"{_BASE_URL}/mcp",
            token=_TOKEN,
            tool_name=TOOL_NAME,
            **client_kwargs,
        )


def _tool_call_body(**overrides: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {"name": TOOL_NAME, "arguments": {**_ARGUMENTS, **overrides}},
    }


async def _call_tool(config: SidecarConfig, **overrides: Any) -> Any:
    async with Client(build_server(config)) as client:
        return await client.call_tool(TOOL_NAME, {**_ARGUMENTS, **overrides})


# ---------------------------------------------------------------- 도구 표면


@pytest.mark.asyncio
async def test_only_run_skill_is_exposed(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    async with Client(build_server(_config(fake_cli("")))) as client:
        tools = await client.list_tools()

    # broker/account/order 도구가 하나라도 붙으면 이 sidecar의 존재 이유가 깨진다.
    assert [tool.name for tool in tools] == [TOOL_NAME]


@pytest.mark.asyncio
async def test_run_skill_declares_the_provider_argument_contract(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    async with Client(build_server(_config(fake_cli("")))) as client:
        (tool,) = await client.list_tools()

    schema = tool.inputSchema
    assert set(schema["properties"]) == {
        "skill",
        "instruction",
        "context",
        "response_schema",
        "reasoning_effort",
    }
    assert set(schema["required"]) == {
        "skill",
        "instruction",
        "context",
        "response_schema",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reasoning_effort"]["anyOf"][0]["enum"] == [
        "low",
        "medium",
        "high",
    ]
    # 임의 response_schema 계약이므로 outputSchema는 객체라는 사실만 선언한다.
    assert tool.outputSchema == {"type": "object", "additionalProperties": True}
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True


# ------------------------------------------------------------------- 인증


@pytest.mark.asyncio
async def test_mcp_endpoint_rejects_a_request_without_a_bearer_token(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    async with _serving(_config(fake_cli(""))) as client:
        response = await client.post(
            "/mcp", json=_tool_call_body(), headers=_MCP_HEADERS
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_endpoint_rejects_a_wrong_bearer_token(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    async with _serving(_config(fake_cli(""))) as client:
        response = await client.post(
            "/mcp",
            json=_tool_call_body(),
            headers={**_MCP_HEADERS, "Authorization": "Bearer not-the-token"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_health_route_answers_without_a_token(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    async with _serving(_config(fake_cli(""))) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "kasset-ai-mcp-sidecar"


# ------------------------------------------------------- provider 왕복 계약


@pytest.mark.asyncio
async def test_provider_client_receives_schema_valid_structured_content(
    monkeypatch: pytest.MonkeyPatch,
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    command = fake_cli(
        f"""
import sys

sys.stdin.read()
print("still thinking")
print({_VALID_OUTPUT!r})
"""
    )
    config = _config(command)

    async with _provider_client(config, monkeypatch, timeout_seconds=30.0) as client:
        result = await client.request_json(
            model=f"mcp:{TOOL_NAME}",
            input_payload={"kind": "trade_review", "payload": {"symbol": "005930"}},
            reasoning_effort="high",
            schema_name="kasset_tier_verdict",
            schema=_RESPONSE_SCHEMA,
        )

    assert result == {"action": "HOLD", "confidence": 0.42}


@pytest.mark.asyncio
async def test_provider_client_fails_closed_when_the_tool_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    config = _config(fake_cli('print("no json here")\n'))

    async with _provider_client(config, monkeypatch) as client:
        # 계약 실패는 fail-closed다. AiProviderUnavailable이면 다른 provider로
        # 재시도되므로 이 구분이 깨지면 안 된다.
        with pytest.raises(ValueError) as excinfo:
            await client.request_json(
                model=f"mcp:{TOOL_NAME}",
                input_payload={"kind": "trade_review"},
                reasoning_effort=None,
                schema_name="kasset_tier_verdict",
                schema=_RESPONSE_SCHEMA,
            )

    assert not isinstance(excinfo.value, AiProviderUnavailable)


# --------------------------------------------------------------- 실패 분류


@pytest.mark.asyncio
async def test_output_without_a_json_object_is_a_contract_failure(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    config = _config(fake_cli('print("plain text only")\n'))

    with pytest.raises(ToolError, match="^contract: ") as excinfo:
        await _call_tool(config)

    assert "returned no JSON object" in str(excinfo.value)


@pytest.mark.asyncio
async def test_output_violating_the_response_schema_is_a_contract_failure(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    config = _config(fake_cli('print(\'{"action": "MAYBE", "confidence": 2}\')\n'))

    with pytest.raises(ToolError, match="^contract: ") as excinfo:
        await _call_tool(config)

    message = str(excinfo.value)
    assert "violates response_schema" in message
    # 위반 위치와 키워드만 남기고 모델 출력 본문은 넣지 않는다.
    assert "MAYBE" not in message


@pytest.mark.asyncio
async def test_non_zero_exit_is_classified_as_provider_unavailable(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    config = _config(fake_cli("raise SystemExit(9)\n"))

    with pytest.raises(ToolError, match="^provider_unavailable: ") as excinfo:
        await _call_tool(config)

    assert "exit_code=9" in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_cli_binary_is_classified_as_provider_unavailable(
    tmp_path: Path,
) -> None:
    config = _config((str(tmp_path / "missing-subscription-cli"),))

    with pytest.raises(
        ToolError, match="^provider_unavailable: subscription CLI could not start"
    ):
        await _call_tool(config)


@pytest.mark.asyncio
async def test_cli_stdout_and_stderr_never_reach_the_caller(
    fake_cli: Callable[..., tuple[str, ...]],
) -> None:
    stdout_secret = "stdout-auth-json-secret-value"
    stderr_secret = "stderr-token-secret-value"
    config = _config(
        fake_cli(
            f"""
import sys

print({stdout_secret!r})
print({stderr_secret!r}, file=sys.stderr)
raise SystemExit(9)
"""
        )
    )

    with pytest.raises(ToolError) as excinfo:
        await _call_tool(config)

    message = str(excinfo.value)
    assert stdout_secret not in message
    assert stderr_secret not in message
    assert "stdout_bytes=" in message


@pytest.mark.asyncio
async def test_timeout_kills_the_process_and_reports_unavailable(
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
        assert command == ("fake-codex", "exec", "-")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runner = SkillRunner(
        command=("fake-codex", "exec", "-"),
        timeout_seconds=0.01,
    )
    invocation = build_invocation(
        skill="kasset_tier_verdict",
        instruction="Analyze only the supplied evidence.",
        context={"kind": "trade_review"},
        response_schema=_RESPONSE_SCHEMA,
        reasoning_effort=None,
    )

    with pytest.raises(SidecarUnavailable, match="timed out"):
        await runner.run(invocation)

    assert process.killed is True
    assert process.input is not None


# --------------------------------------------------------------- 인자 한도


def test_oversized_context_is_rejected_before_the_cli_runs() -> None:
    with pytest.raises(SidecarContractError, match="exceeds the .* byte limit"):
        build_invocation(
            skill="kasset_tier_verdict",
            instruction="Analyze only the supplied evidence.",
            context={"blob": "x" * (MAX_CONTEXT_BYTES + 1)},
            response_schema=_RESPONSE_SCHEMA,
            reasoning_effort=None,
        )


@pytest.mark.parametrize(
    "response_schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "properties": {"action": {"type": "not-a-json-type"}}},
    ],
)
def test_unusable_response_schema_is_rejected(
    response_schema: dict[str, Any],
) -> None:
    with pytest.raises(SidecarContractError):
        build_invocation(
            skill="kasset_tier_verdict",
            instruction="Analyze only the supplied evidence.",
            context={"kind": "trade_review"},
            response_schema=response_schema,
            reasoning_effort=None,
        )


def test_prompt_carries_the_contract_schema_and_context() -> None:
    prompt = build_prompt(
        build_invocation(
            skill="kasset_tier_verdict",
            instruction="Analyze only the supplied evidence.",
            context={"kind": "trade_review"},
            response_schema=_RESPONSE_SCHEMA,
            reasoning_effort="high",
        )
    ).decode()

    assert "read-only market-analysis layer" in prompt
    assert "order-execution instructions" in prompt
    assert "requested_reasoning_effort:\nhigh" in prompt
    assert json.dumps(_RESPONSE_SCHEMA, separators=(",", ":")) in prompt
    assert '{"kind":"trade_review"}' in prompt


# ------------------------------------------------------------------ 동시성


@pytest.mark.asyncio
async def test_in_flight_limit_returns_429_and_releases_the_slot(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cli-started"
    release = tmp_path / "cli-release"
    script = tmp_path / "gated_cli.py"
    script.write_text(
        "import json, os, sys, time\n"
        "sys.stdin.read()\n"
        f"open({str(marker)!r}, 'w').close()\n"
        "deadline = time.monotonic() + 10\n"
        f"while not os.path.exists({str(release)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        f"print({_VALID_OUTPUT!r})\n",
        encoding="utf-8",
    )
    config = _config((sys.executable, str(script)), max_concurrency=1)
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {_TOKEN}"}

    async with _serving(config) as client:
        held = asyncio.create_task(
            client.post("/mcp", json=_tool_call_body(), headers=headers)
        )
        for _ in range(1000):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists(), "가짜 CLI가 시작되지 않아 포화 상태를 만들 수 없다"

        saturated = await client.post("/mcp", json=_tool_call_body(), headers=headers)
        # 429는 McpStructuredJsonClient가 AiProviderUnavailable로 분류하는
        # 유일한 in-band 가용성 신호다.
        assert saturated.status_code == 429
        assert saturated.headers["Retry-After"] == "1"

        release.touch()
        first = await held

    assert first.status_code == 200
    assert '"structuredContent":{"action":"HOLD","confidence":0.42}' in first.text


# -------------------------------------------------------------------- 설정


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"KASSET_AI_SIDECAR_CMD": "codex exec -"},
        {"KASSET_AI_SIDECAR_TOKEN": "t"},
        {"KASSET_AI_SIDECAR_CMD": "   ", "KASSET_AI_SIDECAR_TOKEN": "t"},
    ],
)
def test_load_config_requires_a_command_and_a_token(env: dict[str, str]) -> None:
    with pytest.raises(SidecarConfigError):
        load_config(env)


def test_load_config_applies_documented_defaults() -> None:
    config = load_config(
        {
            "KASSET_AI_SIDECAR_CMD": 'codex exec --sandbox "read only" -',
            "KASSET_AI_SIDECAR_TOKEN": " token-with-space ",
        }
    )

    assert config.command == ("codex", "exec", "--sandbox", "read only", "-")
    assert config.token == "token-with-space"
    assert config.port == 8770
    assert config.path == "/mcp"
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert config.max_concurrency == DEFAULT_MAX_CONCURRENCY


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("KASSET_AI_SIDECAR_PORT", "0"),
        ("KASSET_AI_SIDECAR_PORT", "not-a-port"),
        ("KASSET_AI_SIDECAR_PATH", "mcp"),
        ("KASSET_AI_SIDECAR_TIMEOUT_SECONDS", "0.5"),
        ("KASSET_AI_SIDECAR_TIMEOUT_SECONDS", "601"),
        ("KASSET_AI_SIDECAR_MAX_CONCURRENCY", "0"),
        ("KASSET_AI_SIDECAR_MAX_CONCURRENCY", "9"),
    ],
)
def test_load_config_rejects_out_of_range_settings(key: str, value: str) -> None:
    with pytest.raises(SidecarConfigError):
        load_config(
            {
                "KASSET_AI_SIDECAR_CMD": "codex exec -",
                "KASSET_AI_SIDECAR_TOKEN": "t",
                key: value,
            }
        )


def test_verify_command_executable_rejects_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(SidecarConfigError, match="not executable"):
        verify_command_executable((str(tmp_path / "missing-subscription-cli"),))


def test_verify_command_executable_accepts_a_real_binary() -> None:
    verify_command_executable((sys.executable, "-"))
