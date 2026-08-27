"""Contract tests for the OpenAI-format API tier and its fallback chain."""

from __future__ import annotations

import json

import httpx
import pytest

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


def _completion(payload: dict[str, object]) -> dict[str, object]:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


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
async def test_provider_parses_a_wire_format_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        [
            httpx.Response(
                200,
                json=_completion(
                    {
                        "summary": "Momentum is improving.",
                        "signal": "buy",
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

    sent = json.loads(transport.requests[0].content)
    assert sent["model"] == "test-model"
    assert sent["response_format"] == {"type": "json_object"}
    assert transport.requests[0].headers["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize("status", [401, 402, 403, 408, 429, 500, 503])
@pytest.mark.asyncio
async def test_availability_failures_raise_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    transport = _Transport([httpx.Response(status, json={"error": "nope"})])
    _patch_transport(monkeypatch, {"https://example.test": transport})

    with pytest.raises(AiProviderUnavailable):
        await OpenAiCompatibleProvider(_profile()).run_skill(_REQUEST)


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
    """A reachable provider with broken output must fail loudly, not fall back."""
    transport = _Transport(
        [
            httpx.Response(200, json=_completion({"signal": "BUY"})),
            httpx.Response(200, json=_completion({"summary": "unused fallback"})),
        ]
    )
    _patch_transport(monkeypatch, {"https://example.test": transport})
    chain = ChainedApiProvider(
        [
            OpenAiCompatibleProvider(_profile("primary-api")),
            OpenAiCompatibleProvider(_profile("openrouter")),
        ]
    )

    with pytest.raises(ValueError, match="missing a summary"):
        await chain.run_skill(_REQUEST)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_chain_falls_back_only_on_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Transport([httpx.Response(429, json={"error": "quota"})])
    fallback = _Transport(
        [httpx.Response(200, json=_completion({"summary": "from openrouter"}))]
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
    primary = _Transport([httpx.Response(429, json={})])
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
    assert "HTTP 429" in message
    assert "ConnectError" in message


def test_factory_skips_unconfigured_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_API_MODEL", "")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL", "")
    assert build_api_provider_chain() is None


def test_factory_orders_primary_before_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("k1"))
    monkeypatch.setattr(settings, "KASSET_AI_API_MODEL", "deepseek-chat")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr("k2"))
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL", "google/gemini-flash")

    chain = build_api_provider_chain()

    assert chain is not None
    names = [provider.name for provider in chain._providers]
    assert names == ["primary-api", "openrouter"]
