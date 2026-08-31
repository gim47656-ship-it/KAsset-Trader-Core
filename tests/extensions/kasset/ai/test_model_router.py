"""Behavioral tests for the event-driven Luna/Terra/Sol router."""

from __future__ import annotations

import json
from typing import Literal

import httpx
import pytest

from app.core.config import settings
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.factory import build_model_router
from app.extensions.kasset.ai.model_router import AnalysisKind, OpenAiModelRouter
from app.services.research_canonical_hash import canonical_sha256


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = list(payloads)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = self._payloads.pop(0)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(payload),
                            }
                        ],
                    }
                ]
            },
        )


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _wire_response(payload: dict[str, object]) -> httpx.Response:
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


def _patch_transports(
    monkeypatch: pytest.MonkeyPatch,
    transports: dict[str, httpx.AsyncBaseTransport],
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        base_url = str(kwargs.get("base_url", ""))
        for prefix, transport in transports.items():
            if base_url.startswith(prefix):
                kwargs["transport"] = transport
                break
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: _Transport,
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _router(*, openrouter_api_key: str | None = None) -> OpenAiModelRouter:
    return OpenAiModelRouter(
        base_url="https://example.test/v1",
        api_key="test-key",
        luna_model="test-luna",
        terra_model="test-terra",
        sol_model="test-sol",
        openrouter_base_url="https://openrouter.test/v1",
        openrouter_api_key=openrouter_api_key,
        openrouter_flash_model="test-openrouter-flash",
        openrouter_pro_model="test-openrouter-pro",
    )


def _verdict(
    *,
    action: Literal["BUY", "SELL", "HOLD", "IGNORE", "REVIEW"] = "HOLD",
    confidence: float = 0.9,
    risk: Literal["LOW", "MEDIUM", "HIGH"] = "LOW",
    escalate: bool = False,
) -> dict[str, object]:
    return {
        "action": action,
        "confidence": confidence,
        "risk": risk,
        "bullish_score": 55,
        "bearish_score": 45,
        "escalate": escalate,
        "rationale_tags": ["momentum_stable"],
    }


@pytest.mark.asyncio
async def test_high_confidence_luna_hold_stops_after_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport([_verdict()])
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.MARKET_STATE,
        {"market": "kr", "breadth": 0.52},
        correlation_id="corr-luna",
        address_instruction="사용자를 '침착한수달42님'으로 부른다.",
    )

    assert len(transport.requests) == 1
    assert result.action == "HOLD"
    assert result.tier_used == "test-luna"
    assert result.provider == "direct-api"
    assert result.tier == "luna"
    assert result.model_id == "test-luna"
    assert result.input_hash == canonical_sha256(
        {
            "kind": "market_state",
            "payload": {"market": "kr", "breadth": 0.52},
        }
    )
    assert result.kind is AnalysisKind.MARKET_STATE
    assert result.correlation_id == "corr-luna"

    body = json.loads(transport.requests[0].content)
    assert str(transport.requests[0].url) == "https://example.test/v1/responses"
    assert set(body) == {"model", "instructions", "input", "reasoning", "text"}
    assert "사용자를 '침착한수달42님'으로 부른다." in body["instructions"]
    assert body["model"] == "test-luna"
    assert body["reasoning"] == {"effort": "low"}
    assert json.loads(body["input"]) == {
        "kind": "market_state",
        "payload": {"market": "kr", "breadth": 0.52},
    }
    response_format = body["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["name"] == "kasset_tier_verdict"
    assert response_format["schema"]["additionalProperties"] is False
    assert set(response_format["schema"]["required"]) == {
        "action",
        "confidence",
        "risk",
        "bullish_score",
        "bearish_score",
        "escalate",
        "rationale_tags",
    }
    rationale_schema = response_format["schema"]["properties"]["rationale_tags"]
    assert "Short noun-like rationale tags only" in rationale_schema["description"]


@pytest.mark.asyncio
async def test_normalized_input_hash_is_stable_across_mapping_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport([_verdict(), _verdict()])
    _patch_transport(monkeypatch, transport)
    router = _router()

    first = await router.analyze(
        AnalysisKind.MARKET_STATE,
        {"market": "kr", "breadth": 0.52},
    )
    second = await router.analyze(
        AnalysisKind.MARKET_STATE,
        {"breadth": 0.52, "market": "kr"},
    )

    assert first.input_hash == second.input_hash


@pytest.mark.asyncio
async def test_low_confidence_luna_escalates_to_terra_with_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        [_verdict(confidence=0.4), _verdict(confidence=0.9, risk="MEDIUM")]
    )
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.CANDIDATE_SCAN,
        {"symbol": "005930", "rsi14": 51.2},
    )

    assert [json.loads(request.content)["model"] for request in transport.requests] == [
        "test-luna",
        "test-terra",
    ]
    assert result.tier_used == "test-terra"
    assert result.kind is AnalysisKind.CANDIDATE_SCAN
    second_body = json.loads(transport.requests[1].content)
    assert (
        second_body["instructions"]
        == json.loads(transport.requests[0].content)["instructions"]
    )
    assert json.loads(second_body["input"])["payload"] == {
        "symbol": "005930",
        "rsi14": 51.2,
        "prior": {
            "action": "HOLD",
            "confidence": 0.4,
            "risk": "LOW",
            "bullish_score": 55,
            "bearish_score": 45,
        },
    }


@pytest.mark.asyncio
async def test_terra_high_risk_escalates_to_sol_as_critical_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        [
            _verdict(action="BUY", confidence=0.9, risk="HIGH"),
            _verdict(action="IGNORE", confidence=0.93, risk="MEDIUM"),
        ]
    )
    _patch_transport(monkeypatch, transport)

    result = await _router().analyze(
        AnalysisKind.CANDIDATE_REVIEW,
        {"symbol": "AAPL"},
        correlation_id="corr-sol",
    )

    bodies = [json.loads(request.content) for request in transport.requests]
    assert [body["model"] for body in bodies] == ["test-terra", "test-sol"]
    assert bodies[0]["reasoning"] == {"effort": "medium"}
    assert bodies[1]["reasoning"] == {"effort": "high"}
    assert json.loads(bodies[1]["input"])["kind"] == "critical_review"
    assert json.loads(bodies[1]["input"])["payload"]["prior"] == {
        "action": "BUY",
        "confidence": 0.9,
        "risk": "HIGH",
        "bullish_score": 55,
        "bearish_score": 45,
    }
    assert result.tier_used == "test-sol"
    assert result.provider == "direct-api"
    assert result.tier == "sol"
    assert result.model_id == "test-sol"
    assert result.input_hash == canonical_sha256(json.loads(bodies[1]["input"]))
    assert result.kind is AnalysisKind.CRITICAL_REVIEW
    assert result.correlation_id == "corr-sol"


@pytest.mark.parametrize(
    ("kind", "expected_model", "expected_effort"),
    [
        (AnalysisKind.NEWS_TRIAGE, "test-luna", "low"),
        (AnalysisKind.MARKET_STATE, "test-luna", "low"),
        (AnalysisKind.CANDIDATE_SCAN, "test-luna", "low"),
        (AnalysisKind.CANDIDATE_REVIEW, "test-terra", "medium"),
        (AnalysisKind.TRADE_REVIEW, "test-terra", "high"),
        (AnalysisKind.CRITICAL_REVIEW, "test-sol", "high"),
    ],
)
@pytest.mark.asyncio
async def test_kind_selects_starting_model_and_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    kind: AnalysisKind,
    expected_model: str,
    expected_effort: str,
) -> None:
    transport = _Transport([_verdict()])
    _patch_transport(monkeypatch, transport)

    await _router().analyze(kind, {"sample": 1})

    body = json.loads(transport.requests[0].content)
    assert body["model"] == expected_model
    assert body["reasoning"] == {"effort": expected_effort}


@pytest.mark.asyncio
async def test_missing_api_key_factory_router_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)

    router = build_model_router()

    with pytest.raises(AiProviderUnavailable, match="not configured"):
        await router.analyze(AnalysisKind.NEWS_TRIAGE, {"headline": "sample"})


@pytest.mark.parametrize(
    ("kind", "expected_fallback_model", "expected_tier"),
    [
        (AnalysisKind.MARKET_STATE, "test-openrouter-flash", "luna"),
        (AnalysisKind.CANDIDATE_REVIEW, "test-openrouter-pro", "terra"),
        (AnalysisKind.CRITICAL_REVIEW, "test-openrouter-pro", "sol"),
    ],
)
@pytest.mark.asyncio
async def test_primary_unavailable_uses_tier_openrouter_fallback(
    monkeypatch: pytest.MonkeyPatch,
    kind: AnalysisKind,
    expected_fallback_model: str,
    expected_tier: str,
) -> None:
    primary = _ScriptedTransport([httpx.Response(503, json={"error": "busy"})])
    fallback = _ScriptedTransport([_wire_response(_verdict())])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    result = await _router(openrouter_api_key="openrouter-key").analyze(
        kind, {"sample": 1}
    )

    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1
    assert result.tier_used == expected_fallback_model
    assert result.provider == "openrouter"
    assert result.tier == expected_tier
    assert result.model_id == expected_fallback_model
    primary_body = json.loads(primary.requests[0].content)
    fallback_body = json.loads(fallback.requests[0].content)
    assert primary_body["reasoning"]["effort"] in {"low", "medium", "high"}
    assert "reasoning" not in fallback_body
    assert fallback_body["model"] == expected_fallback_model
    assert fallback_body["instructions"] == primary_body["instructions"]
    assert fallback_body["text"] == primary_body["text"]
    assert fallback.requests[0].headers["Authorization"] == "Bearer openrouter-key"


@pytest.mark.asyncio
async def test_primary_success_does_not_call_configured_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _ScriptedTransport([_wire_response(_verdict())])
    fallback = _ScriptedTransport([_wire_response(_verdict())])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    result = await _router(openrouter_api_key="openrouter-key").analyze(
        AnalysisKind.MARKET_STATE, {"sample": 1}
    )

    assert result.tier_used == "test-luna"
    assert len(primary.requests) == 1
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_both_primary_and_fallback_unavailable_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _ScriptedTransport([httpx.Response(500, json={})])
    fallback = _ScriptedTransport([httpx.Response(503, json={})])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    with pytest.raises(AiProviderUnavailable) as excinfo:
        await _router(openrouter_api_key="openrouter-key").analyze(
            AnalysisKind.MARKET_STATE, {"sample": 1}
        )

    assert "HTTP 500" in str(excinfo.value)
    assert "HTTP 503" in str(excinfo.value)
    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1


@pytest.mark.asyncio
async def test_missing_openrouter_key_does_not_attempt_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _ScriptedTransport([httpx.Response(503, json={})])
    fallback = _ScriptedTransport([_wire_response(_verdict())])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    with pytest.raises(AiProviderUnavailable, match="HTTP 503"):
        await _router().analyze(AnalysisKind.MARKET_STATE, {"sample": 1})

    assert len(primary.requests) == 1
    assert fallback.requests == []


@pytest.mark.parametrize("status", [400])
@pytest.mark.asyncio
async def test_primary_nonretryable_4xx_does_not_attempt_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    primary = _ScriptedTransport([httpx.Response(status, text="bad request")])
    fallback = _ScriptedTransport([_wire_response(_verdict())])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    with pytest.raises(ValueError, match=f"HTTP {status}"):
        await _router(openrouter_api_key="openrouter-key").analyze(
            AnalysisKind.MARKET_STATE, {"sample": 1}
        )

    assert len(primary.requests) == 1
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_primary_429_falls_back_and_reports_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _ScriptedTransport([httpx.Response(429, text="rate limited")])
    fallback = _ScriptedTransport([_wire_response(_verdict())])
    _patch_transports(
        monkeypatch,
        {
            "https://example.test": primary,
            "https://openrouter.test": fallback,
        },
    )

    result = await _router(openrouter_api_key="openrouter-key").analyze(
        AnalysisKind.MARKET_STATE, {"sample": 1}
    )

    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1
    assert result.provider == "openrouter"
    assert result.model_id == "test-openrouter-flash"
