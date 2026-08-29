"""Contract tests for the concrete HTTP JSON-RPC MCP provider."""

from __future__ import annotations

import json

import httpx
import pytest

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, tool_response: httpx.Response | Exception) -> None:
        self._tool_response = tool_response
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "test-mcp", "version": "1"},
                    },
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session-123"
        assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/call"
        if isinstance(self._tool_response, Exception):
            raise self._tool_response
        try:
            payload = self._tool_response.json()
        except ValueError:
            return httpx.Response(
                self._tool_response.status_code,
                headers=self._tool_response.headers,
                content=self._tool_response.content,
            )
        if isinstance(payload, dict) and "id" in payload:
            payload["id"] = body["id"]
        return httpx.Response(
            self._tool_response.status_code,
            headers=self._tool_response.headers,
            json=payload,
        )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _success(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "response-id",
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        },
    )


@pytest.mark.asyncio
async def test_mcp_calls_configured_tool_with_strict_schema_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(_success({"action": "HOLD"}))
    _patch_transport(monkeypatch, transport)
    client = McpStructuredJsonClient(
        url="https://mcp.example.test/analysis",
        token="secret-token",
        tool_name="review_market",
        timeout_seconds=17.5,
    )

    result = await client.request_json(
        model="tool:review_market",
        input_payload={"kind": "trade_review", "payload": {"symbol": "005930"}},
        reasoning_effort="high",
        schema_name="kasset_tier_verdict",
        schema={"type": "object", "required": ["action"]},
        additional_instructions="Address the owner by the supplied display name.",
    )

    assert result == {"action": "HOLD"}
    methods = [
        request.method
        if request.method == "DELETE"
        else json.loads(request.content)["method"]
        for request in transport.requests
    ]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "DELETE",
    ]
    initialize_body = json.loads(transport.requests[0].content)
    assert initialize_body["params"]["protocolVersion"]
    assert initialize_body["params"]["capabilities"] == {}
    assert initialize_body["params"]["clientInfo"]["name"] == "kasset-trader-core"
    initialized_request = transport.requests[1]
    assert initialized_request.headers["Mcp-Session-Id"] == "session-123"
    request = transport.requests[2]
    assert str(request.url) == "https://mcp.example.test/analysis"
    assert request.headers["Authorization"] == "Bearer secret-token"
    assert request.headers["Accept"] == "application/json, text/event-stream"
    assert request.headers["Mcp-Session-Id"] == "session-123"
    assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
    body = json.loads(request.content)
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert isinstance(body["id"], str) and body["id"]
    assert body["params"]["name"] == "review_market"
    arguments = body["params"]["arguments"]
    assert arguments["skill"] == "kasset_tier_verdict"
    assert arguments["context"] == {
        "kind": "trade_review",
        "payload": {"symbol": "005930"},
    }
    assert arguments["response_schema"] == {
        "type": "object",
        "required": ["action"],
    }
    assert arguments["reasoning_effort"] == "high"
    assert "read-only market-analysis layer" in arguments["instruction"]
    assert "supplied display name" in arguments["instruction"]
    assert "secret-token" not in request.content.decode()


@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.asyncio
async def test_mcp_retryable_http_status_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    transport = _Transport(httpx.Response(status, text="temporarily unavailable"))
    _patch_transport(monkeypatch, transport)
    client = McpStructuredJsonClient(url="https://mcp.test", token=None)

    with pytest.raises(AiProviderUnavailable, match=f"HTTP {status}"):
        await client.request_json(
            model="tool:run_skill",
            input_payload={"sample": 1},
            reasoning_effort="medium",
            schema_name="test_schema",
            schema={"type": "object"},
        )


@pytest.mark.asyncio
async def test_mcp_rate_limit_status_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(httpx.Response(429, text="rate limited"))
    _patch_transport(monkeypatch, transport)
    client = McpStructuredJsonClient(url="https://mcp.test", token=None)

    with pytest.raises(AiProviderUnavailable, match="HTTP 429"):
        await client.request_json(
            model="tool:run_skill",
            input_payload={"sample": 1},
            reasoning_effort="medium",
            schema_name="test_schema",
            schema={"type": "object"},
        )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408])
@pytest.mark.asyncio
async def test_mcp_nonretryable_and_auth_statuses_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    transport = _Transport(httpx.Response(status, text="echo secret-token"))
    _patch_transport(monkeypatch, transport)
    client = McpStructuredJsonClient(
        url="https://mcp.test",
        token="secret-token",
    )

    with pytest.raises(ValueError) as excinfo:
        await client.request_json(
            model="tool:run_skill",
            input_payload={"sample": 1},
            reasoning_effort=None,
            schema_name="test_schema",
            schema={"type": "object"},
        )

    assert f"HTTP {status}" in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"content": [{"type": "text", "text": "not-json"}]},
            },
        ),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"isError": True, "content": []},
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_mcp_malformed_refused_or_tool_error_output_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    transport = _Transport(response)
    _patch_transport(monkeypatch, transport)
    client = McpStructuredJsonClient(url="https://mcp.test", token=None)

    with pytest.raises(ValueError):
        await client.request_json(
            model="tool:run_skill",
            input_payload={"sample": 1},
            reasoning_effort="low",
            schema_name="test_schema",
            schema={"type": "object"},
        )
