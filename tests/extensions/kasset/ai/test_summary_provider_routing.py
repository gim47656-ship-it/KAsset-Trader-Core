"""News and disclosure summary provider-route integration tests."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.services.disclosures.summary_service import (
    DisclosureSummaryInput,
    build_disclosure_summary_generator,
)
from app.services.news_summary_service import (
    NewsSummaryInput,
    build_news_summary_generator,
)


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


def _patch_transports(
    monkeypatch: pytest.MonkeyPatch,
    transports: dict[str, _Transport],
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


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(payload)}
                    ],
                }
            ]
        },
    )


def _configure_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOKEN", None)
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOOL_NAME", "run_skill")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(settings, "KASSET_AI_API_BASE_URL", "https://direct.test/v1")
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("direct-key"))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_LUNA", "direct-luna")
    monkeypatch.setattr(
        settings,
        "KASSET_AI_OPENROUTER_BASE_URL",
        "https://openrouter.test/api/v1",
    )
    monkeypatch.setattr(
        settings,
        "KASSET_AI_OPENROUTER_API_KEY",
        SecretStr("openrouter-key"),
    )
    monkeypatch.setattr(
        settings,
        "KASSET_AI_OPENROUTER_MODEL_FLASH",
        "z-ai/glm-5.3-flash",
    )


@pytest.mark.asyncio
async def test_news_summary_ignores_configured_mcp_and_uses_direct_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "https://mcp.test/rpc")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOKEN", SecretStr("mcp-token"))
    direct = _Transport(
        [
            _response(
                {
                    "summary": (
                        "테스트 기업이 신제품을 공개했다. "
                        "회사는 공급 확대 계획을 밝혔다."
                    ),
                    "sentiment": "neutral",
                    "confidence": 88,
                }
            )
        ]
    )
    _patch_transports(monkeypatch, {"https://direct.test": direct})

    generator = build_news_summary_generator()
    assert generator is not None
    generated = await generator.summarize(
        NewsSummaryInput(
            title="Test company unveils a new product",
            source="Wire",
            article_content=(
                "The test company unveiled a new product and said it plans "
                "to expand supply to customers."
            ),
            raw_excerpt=None,
        )
    )

    assert generated.model_name == "direct-luna"
    assert len(direct.requests) == 1
    assert direct.requests[0].url.host == "direct.test"


@pytest.mark.asyncio
async def test_news_summary_falls_back_direct_to_openrouter_and_audits_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport([httpx.Response(503)])
    openrouter = _Transport(
        [
            _response(
                {
                    "summary": (
                        "테스트 기업이 신제품을 공개했다. "
                        "회사는 공급 확대 계획을 밝혔다."
                    ),
                    "sentiment": "neutral",
                    "confidence": 82,
                }
            )
        ]
    )
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_news_summary_generator()
    assert generator is not None
    generated = await generator.summarize(
        NewsSummaryInput(
            title="Test company unveils a new product",
            source="Wire",
            article_content=(
                "The test company unveiled a new product and said it plans "
                "to expand supply to customers."
            ),
            raw_excerpt=None,
        )
    )

    assert generated.model_name == "z-ai/glm-5.3-flash"
    assert len(direct.requests) == 1
    assert len(openrouter.requests) == 1
    assert json.loads(openrouter.requests[0].content)["model"] == (
        "z-ai/glm-5.3-flash"
    )
    assert "reasoning" not in json.loads(openrouter.requests[0].content)


@pytest.mark.asyncio
async def test_disclosure_summary_falls_back_direct_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport([httpx.Response(503)])
    openrouter = _Transport(
        [
            _response(
                {
                    "summary": (
                        "테스트상장사가 공급 계약을 체결했다. "
                        "계약 상대방은 샘플회사다."
                    )
                }
            )
        ]
    )
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_disclosure_summary_generator()
    assert generator is not None
    summary = await generator.summarize(
        DisclosureSummaryInput(
            title="공급계약 체결",
            company="테스트상장사",
            form="주요사항보고서",
            body_excerpt="테스트상장사가 샘플회사와 공급 계약을 체결했다.",
        )
    )

    assert summary == (
        "테스트상장사가 공급 계약을 체결했다. 계약 상대방은 샘플회사다."
    )
    assert len(direct.requests) == 1
    assert len(openrouter.requests) == 1


@pytest.mark.asyncio
async def test_summary_refusal_fails_closed_without_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport(
        [
            httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "no"}],
                        }
                    ]
                },
            )
        ]
    )
    openrouter = _Transport(
        [
            _response(
                {
                    "summary": "사용되지 않는다. 사용되지 않는다.",
                    "sentiment": "neutral",
                    "confidence": 50,
                }
            )
        ]
    )
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_news_summary_generator()
    assert generator is not None
    with pytest.raises(ValueError, match="refused"):
        await generator.summarize(
            NewsSummaryInput(
                title="Test headline",
                source="Wire",
                article_content=(
                    "A sufficiently detailed article body for summary input."
                ),
                raw_excerpt=None,
            )
        )

    assert len(direct.requests) == 1
    assert openrouter.requests == []


@pytest.mark.asyncio
async def test_summary_schema_failure_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport(
        [
            _response(
                {
                    "summary": "테스트 기업이 신제품을 공개했다. 공급 확대 계획도 밝혔다.",
                    "sentiment": "neutral",
                    "confidence": 75,
                    "extra": "not allowed",
                }
            )
        ]
    )
    openrouter = _Transport([])
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_news_summary_generator()
    assert generator is not None
    with pytest.raises(ValueError, match="response shape is invalid"):
        await generator.summarize(
            NewsSummaryInput(
                title="Test headline",
                source="Wire",
                article_content=(
                    "A sufficiently detailed article body for summary input."
                ),
                raw_excerpt=None,
            )
        )

    assert len(direct.requests) == 1
    assert openrouter.requests == []


@pytest.mark.asyncio
async def test_summary_4xx_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport([httpx.Response(429, text="quota rejected")])
    openrouter = _Transport([])
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_news_summary_generator()
    assert generator is not None
    with pytest.raises(ValueError, match="HTTP 429"):
        await generator.summarize(
            NewsSummaryInput(
                title="Test headline",
                source="Wire",
                article_content=(
                    "A sufficiently detailed article body for summary input."
                ),
                raw_excerpt=None,
            )
        )

    assert len(direct.requests) == 1
    assert openrouter.requests == []


@pytest.mark.asyncio
async def test_summary_safety_validation_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    direct = _Transport(
        [
            _response(
                {
                    "summary": (
                        "테스트 기업 매수를 추천한다. "
                        "회사는 공급 확대 계획을 밝혔다."
                    ),
                    "sentiment": "positive",
                    "confidence": 75,
                }
            )
        ]
    )
    openrouter = _Transport(
        [
            _response(
                {
                    "summary": "사용되지 않는다. 사용되지 않는다.",
                    "sentiment": "neutral",
                    "confidence": 50,
                }
            )
        ]
    )
    _patch_transports(
        monkeypatch,
        {"https://direct.test": direct, "https://openrouter.test": openrouter},
    )

    generator = build_news_summary_generator()
    assert generator is not None
    with pytest.raises(ValueError, match="investment advice"):
        await generator.summarize(
            NewsSummaryInput(
                title="Test headline",
                source="Wire",
                article_content=(
                    "A sufficiently detailed article body for summary input."
                ),
                raw_excerpt=None,
            )
        )

    assert len(direct.requests) == 1
    assert openrouter.requests == []


@pytest.mark.asyncio
async def test_summary_skips_unconfigured_direct_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_routes(monkeypatch)
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    openrouter = _Transport(
        [
            _response(
                {
                    "summary": (
                        "테스트 기업이 신제품을 공개했다. "
                        "회사는 공급 확대 계획을 밝혔다."
                    ),
                    "sentiment": "neutral",
                    "confidence": 77,
                }
            )
        ]
    )
    _patch_transports(monkeypatch, {"https://openrouter.test": openrouter})

    generator = build_news_summary_generator()
    assert generator is not None
    generated = await generator.summarize(
        NewsSummaryInput(
            title="Test company unveils a new product",
            source="Wire",
            article_content=(
                "The test company unveiled a new product and said it plans "
                "to expand supply to customers."
            ),
            raw_excerpt=None,
        )
    )

    assert generated.model_name == "z-ai/glm-5.3-flash"
    assert len(openrouter.requests) == 1
