"""Contract tests for the Responses API tier and its fallback chain."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.extensions.kasset.ai.api_provider import (
    ApiProviderProfile,
    ChainedApiProvider,
    OpenAiCompatibleProvider,
)
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.factory import build_api_provider_chain
from app.extensions.kasset.ai.models import SkillRequest, SkillResult

_REQUEST = SkillRequest(
    skill="technical_analysis",
    instruction="Analyze the supplied evidence.",
    symbol="005930",
    market="kr",
    context={"timeframe": "1d"},
    correlation_id="corr-1",
)


def _profile(name: str = "primary-api") -> ApiProviderProfile:
    return ApiProviderProfile(
        name=name,
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
    )


def _response(payload: dict[str, object]) -> dict[str, object]:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(payload)}],
            }
        ]
    }


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    transports: dict[str, _Transport],
) -> None:
    """Route AsyncClient construction to a per-base-url fake transport."""

    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        base_url = str(kwargs.get("base_url", ""))
        for prefix, transport in transports.items():
            if base_url.startswith(prefix):
                kwargs["transport"] = transport
                break
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
async def test_provider_preserves_run_skill_contract_over_responses_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        [
            httpx.Response(
                200,
                json=_response(
                    {
                        "summary": "Momentum is improving.",
                        "signal": "BUY",
                        "confidence": 0.62,
                        "rationale": ["RSI recovered", ""],
                    }
                ),
            )
        ]
    )
    _patch_transport(monkeypatch, {"https://example.test": transport})

    result = await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)

    assert isinstance(result, SkillResult)
    assert result.provider == "api"
    assert result.signal == "BUY"
    assert result.confidence == 0.62
    assert result.rationale == ["RSI recovered"]
    assert result.metadata == {"provider_profile": "primary-api", "model": "test-model"}
    assert result.correlation_id == "corr-1"

    request = transport.requests[0]
    sent = json.loads(request.content)
    assert str(request.url) == "https://example.test/v1/responses"
    assert set(sent) == {"model", "instructions", "input", "reasoning", "text"}
    assert sent["model"] == "test-model"
    assert sent["reasoning"] == {"effort": "medium"}
    assert json.loads(sent["input"]) == {
        "skill": "technical_analysis",
        "symbol": "005930",
        "market": "kr",
        "instruction": "Analyze the supplied evidence.",
        "context": {"timeframe": "1d"},
    }
    assert "Analyze the supplied evidence." not in sent["instructions"]
    response_format = sent["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["name"] == "kasset_skill_result"
    assert response_format["schema"]["additionalProperties"] is False
    assert request.headers["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.asyncio
async def test_retryable_statuses_raise_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    transport = _Transport([httpx.Response(status, json={"error": "nope"})])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(AiProviderUnavailable):
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)


@pytest.mark.asyncio
async def test_rate_limit_status_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport([httpx.Response(429, json={"error": "rate limited"})])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(AiProviderUnavailable, match="HTTP 429"):
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)


@pytest.mark.parametrize("status", [400, 408])
@pytest.mark.asyncio
async def test_nonretryable_4xx_includes_redacted_response_excerpt(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    body = "invalid request; echoed credential=test-key; " + ("x" * 250)
    transport = _Transport([httpx.Response(status, text=body)])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(ValueError) as excinfo:
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)

    message = str(excinfo.value)
    assert f"HTTP {status}" in message
    assert "invalid request" in message
    assert "[REDACTED]" in message
    assert "test-key" not in message
    assert len(message.rsplit(": ", maxsplit=1)[-1]) == 200


@pytest.mark.asyncio
async def test_connection_errors_raise_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport([httpx.ConnectError("refused")])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(AiProviderUnavailable):
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)


@pytest.mark.asyncio
async def test_malformed_analysis_surfaces_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        [
            httpx.Response(200, json=_response({"signal": "BUY"})),
            httpx.Response(200, json=_response({"summary": "unused fallback"})),
        ]
    )
    _patch_transport(monkeypatch, {"https://example.test": transport})
    chain = ChainedApiProvider(
        [
            OpenAiCompatibleProvider(_profile("primary-api")),
            OpenAiCompatibleProvider(_profile("openrouter")),
        ]
    )

    with pytest.raises(ValueError, match="response shape is invalid"):
        await chain.run_skill(_REQUEST)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "summary": "sample",
                "signal": "buy",
                "confidence": 0.5,
                "rationale": [],
            },
            "unknown signal",
        ),
        (
            {
                "summary": "sample",
                "signal": "BUY",
                "confidence": "0.5",
                "rationale": [],
            },
            "invalid confidence",
        ),
        (
            {
                "summary": "sample",
                "signal": "BUY",
                "confidence": 0.5,
                "rationale": [1],
            },
            "invalid rationale",
        ),
    ],
)
@pytest.mark.asyncio
async def test_schema_type_errors_do_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    message: str,
) -> None:
    transport = _Transport(
        [
            httpx.Response(200, json=_response(payload)),
            httpx.Response(
                200,
                json=_response(
                    {
                        "summary": "unused",
                        "signal": None,
                        "confidence": None,
                        "rationale": [],
                    }
                ),
            ),
        ]
    )
    _patch_transport(monkeypatch, {"https://example.test": transport})
    chain = ChainedApiProvider(
        [
            OpenAiCompatibleProvider(_profile("primary-api")),
            OpenAiCompatibleProvider(_profile("openrouter")),
        ]
    )

    with pytest.raises(ValueError, match=message):
        await chain.run_skill(_REQUEST)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("response_payload", "message"),
    [
        (
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "no"}],
                    }
                ]
            },
            "refused",
        ),
        ({"output": []}, "empty structured output"),
    ],
)
@pytest.mark.asyncio
async def test_refusal_and_empty_output_raise_value_error(
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, object],
    message: str,
) -> None:
    transport = _Transport([httpx.Response(200, json=response_payload)])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(ValueError, match=message):
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)


@pytest.mark.asyncio
async def test_chain_falls_back_only_on_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Transport([httpx.Response(503, json={"error": "unavailable"})])
    fallback = _Transport(
        [
            httpx.Response(
                200,
                json=_response(
                    {
                        "summary": "from openrouter",
                        "signal": None,
                        "confidence": None,
                        "rationale": [],
                    }
                ),
            )
        ]
    )
    _patch_transport(
        monkeypatch,
        {"https://primary.test": primary, "https://openrouter.test": fallback},
    )
    chain = ChainedApiProvider(
        [
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="primary-api",
                    base_url="https://primary.test/v1",
                    api_key="k1",
                    model="m1",
                )
            ),
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="openrouter",
                    base_url="https://openrouter.test/v1",
                    api_key="k2",
                    model="m2",
                )
            ),
        ]
    )

    result = await chain.run_skill(_REQUEST)

    assert result.summary == "from openrouter"
    assert result.metadata["provider_profile"] == "openrouter"


@pytest.mark.asyncio
async def test_exhausted_chain_reports_every_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Transport([httpx.Response(500, json={})])
    fallback = _Transport([httpx.ConnectError("down")])
    _patch_transport(
        monkeypatch,
        {"https://primary.test": primary, "https://openrouter.test": fallback},
    )
    chain = ChainedApiProvider(
        [
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="primary-api",
                    base_url="https://primary.test/v1",
                    api_key="k1",
                    model="m1",
                )
            ),
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="openrouter",
                    base_url="https://openrouter.test/v1",
                    api_key="k2",
                    model="m2",
                )
            ),
        ]
    )

    with pytest.raises(AiProviderUnavailable) as excinfo:
        await chain.run_skill(_REQUEST)
    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "ConnectError" in message


def test_factory_skips_unconfigured_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "")
    assert build_api_provider_chain() is None


def test_factory_orders_terra_before_openrouter_pro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("k1"))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "gpt-terra")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr("k2"))
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "fallback-model")

    chain = build_api_provider_chain()

    assert chain is not None
    names = [provider.name for provider in chain._providers]
    models = [provider._profile.model for provider in chain._providers]
    assert names == ["primary-api", "openrouter"]
    assert models == ["gpt-terra", "fallback-model"]


def test_factory_skips_direct_when_terra_model_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("k1"))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr("k2"))
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "fallback-model")

    chain = build_api_provider_chain()

    assert chain is not None
    assert [provider.name for provider in chain._providers] == ["openrouter"]
