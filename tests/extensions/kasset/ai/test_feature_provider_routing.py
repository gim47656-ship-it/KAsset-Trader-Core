"""Feature-specific MCP, direct API, and OpenRouter routing tests."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient
from app.extensions.kasset.ai.model_router import AnalysisKind, OpenAiModelRouter


class _DispatchTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        responses: dict[str, list[httpx.Response | Exception]],
    ) -> None:
        self.responses = {host: list(items) for host, items in responses.items()}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "mcp.test":
            if request.method == "DELETE":
                return httpx.Response(204)
            body = json.loads(request.content)
            method = body["method"]
            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "route-session"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "serverInfo": {"name": "route-mcp", "version": "1"},
                        },
                    },
                )
            assert request.headers["Mcp-Session-Id"] == "route-session"
            if method == "notifications/initialized":
                return httpx.Response(202)
            assert method == "tools/call"
            item = self.responses[host].pop(0)
            if isinstance(item, Exception):
                raise item
            try:
                payload = item.json()
            except ValueError:
                return item
            if isinstance(payload, dict) and "id" in payload:
                payload["id"] = body["id"]
            return httpx.Response(item.status_code, headers=item.headers, json=payload)

        item = self.responses[host].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _provider_attempt_requests(
    requests: list[httpx.Request],
) -> list[httpx.Request]:
    attempts: list[httpx.Request] = []
    for request in requests:
        if request.url.host != "mcp.test":
            attempts.append(request)
            continue
        if request.method != "POST":
            continue
        method = json.loads(request.content).get("method")
        if method == "tools/call":
            attempts.append(request)
    return attempts


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _verdict() -> dict[str, object]:
    return {
        "action": "HOLD",
        "confidence": 0.91,
        "risk": "LOW",
        "bullish_score": 54,
        "bearish_score": 46,
        "escalate": False,
        "rationale_tags": ["risk_controlled"],
    }


def _responses_result(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                }
            ]
        },
    )


def _mcp_result(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        },
    )


def _router(
    *,
    with_mcp: bool = True,
    api_key: str | None = "direct-key",
    openrouter_key: str | None = "openrouter-key",
) -> OpenAiModelRouter:
    mcp = (
        McpStructuredJsonClient(
            url="https://mcp.test/rpc",
            token="mcp-secret",
            tool_name="run_skill",
            timeout_seconds=9.0,
        )
        if with_mcp
        else None
    )
    return OpenAiModelRouter(
        base_url="https://direct.test/v1",
        api_key=api_key,
        luna_model="direct-luna",
        terra_model="direct-terra",
        sol_model="direct-sol",
        openrouter_base_url="https://openrouter.test/api/v1",
        openrouter_api_key=openrouter_key,
        openrouter_flash_model="z-ai/glm-5.3-flash",
        openrouter_pro_model="z-ai/glm-5.3-flash",
        mcp_client=mcp,
    )


@pytest.mark.asyncio
async def test_candidate_review_uses_mcp_before_direct_and_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport({"mcp.test": [_mcp_result(_verdict())]})
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.CANDIDATE_REVIEW,
        {"symbol": "005930"},
    )

    attempts = _provider_attempt_requests(transport.requests)
    assert [request.url.host for request in attempts] == ["mcp.test"]
    assert result.tier_used == "tool:run_skill"
    assert result.provider == "mcp"
    assert result.tier == "terra"
    assert result.model_id == "tool:run_skill"
    body = json.loads(attempts[0].content)
    assert body["params"]["name"] == "run_skill"
    assert body["params"]["arguments"]["context"] == {
        "kind": "candidate_review",
        "payload": {"symbol": "005930"},
    }


@pytest.mark.asyncio
async def test_trade_review_availability_falls_back_mcp_direct_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport(
        {
            "mcp.test": [httpx.Response(503)],
            "direct.test": [httpx.Response(503)],
            "openrouter.test": [_responses_result(_verdict())],
        }
    )
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.TRADE_REVIEW,
        {"symbol": "AAPL"},
    )

    attempts = _provider_attempt_requests(transport.requests)
    assert [request.url.host for request in attempts] == [
        "mcp.test",
        "direct.test",
        "openrouter.test",
    ]
    assert result.tier_used == "z-ai/glm-5.3-flash"
    assert result.provider == "openrouter"
    assert result.tier == "terra"
    assert result.model_id == "z-ai/glm-5.3-flash"
    assert json.loads(attempts[1].content)["model"] == "direct-terra"
    openrouter_body = json.loads(attempts[2].content)
    assert openrouter_body["model"] == "z-ai/glm-5.3-flash"
    assert "reasoning" not in openrouter_body


@pytest.mark.parametrize(
    "mcp_response",
    [
        httpx.Response(401, text="unauthorized"),
        _mcp_result({"action": "HOLD"}),
    ],
)
@pytest.mark.asyncio
async def test_mcp_auth_or_schema_failure_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mcp_response: httpx.Response,
) -> None:
    transport = _DispatchTransport({"mcp.test": [mcp_response]})
    _patch_transport(monkeypatch, transport)

    expected_error = ValueError if mcp_response.status_code == 401 else ValidationError
    with pytest.raises(expected_error):
        await _router().analyze(
            AnalysisKind.CANDIDATE_REVIEW,
            {"symbol": "005930"},
        )

    assert [
        request.url.host for request in _provider_attempt_requests(transport.requests)
    ] == ["mcp.test"]


@pytest.mark.asyncio
async def test_candidate_review_skips_unconfigured_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport({"direct.test": [_responses_result(_verdict())]})
    _patch_transport(monkeypatch, transport)

    result = await _router(with_mcp=False).analyze(
        AnalysisKind.CANDIDATE_REVIEW,
        {"symbol": "005930"},
    )

    assert [request.url.host for request in transport.requests] == ["direct.test"]
    assert result.tier_used == "direct-terra"
    assert result.provider == "direct-api"
    assert result.tier == "terra"
    assert result.model_id == "direct-terra"


@pytest.mark.asyncio
async def test_candidate_scan_does_not_use_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport({"direct.test": [_responses_result(_verdict())]})
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.CANDIDATE_SCAN,
        {"symbol": "005930"},
    )

    assert [
        request.url.host for request in _provider_attempt_requests(transport.requests)
    ] == ["direct.test"]
    assert result.tier_used == "direct-luna"


@pytest.mark.asyncio
async def test_direct_api_429_falls_back_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport(
        {
            "direct.test": [httpx.Response(429)],
            "openrouter.test": [_responses_result(_verdict())],
        }
    )
    _patch_transport(monkeypatch, transport)

    result = await _router(with_mcp=False).analyze(
        AnalysisKind.CANDIDATE_REVIEW,
        {"symbol": "005930"},
    )

    assert [request.url.host for request in transport.requests] == [
        "direct.test",
        "openrouter.test",
    ]
    assert result.provider == "openrouter"
    assert result.model_id == "z-ai/glm-5.3-flash"


@pytest.mark.asyncio
async def test_unconfigured_direct_provider_skips_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _DispatchTransport({"openrouter.test": [_responses_result(_verdict())]})
    _patch_transport(monkeypatch, transport)

    result = await _router(with_mcp=False, api_key=None).analyze(
        AnalysisKind.NEWS_TRIAGE,
        {"headline": "sample"},
    )

    assert [request.url.host for request in transport.requests] == ["openrouter.test"]
    assert result.tier_used == "z-ai/glm-5.3-flash"


@pytest.mark.asyncio
async def test_provider_and_model_attempts_are_audited_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _DispatchTransport({"mcp.test": [_mcp_result(_verdict())]})
    _patch_transport(monkeypatch, transport)

    with caplog.at_level(
        logging.INFO,
        logger="app.extensions.kasset.ai.structured_router",
    ):
        await _router().analyze(
            AnalysisKind.CANDIDATE_REVIEW,
            {"symbol": "005930"},
        )

    audit_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "provider=mcp" in audit_text
    assert "model=tool:run_skill" in audit_text
    assert "schema=kasset_tier_verdict" in audit_text
    assert "mcp-secret" not in audit_text
    assert "direct-key" not in audit_text
    assert "openrouter-key" not in audit_text
