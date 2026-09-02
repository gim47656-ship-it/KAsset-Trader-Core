"""Settings contracts for feature-specific AI provider routing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.extensions.kasset.ai.factory import build_model_router
from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient


def test_openrouter_fallback_defaults_to_official_glm_slug() -> None:
    for field_name in (
        "KASSET_AI_OPENROUTER_MODEL_FLASH",
        "KASSET_AI_OPENROUTER_MODEL_PRO",
    ):
        assert Settings.model_fields[field_name].default == "z-ai/glm-5.3-flash"
    assert "KASSET_AI_API_MODEL" not in Settings.model_fields
    assert "KASSET_AI_OPENROUTER_MODEL" not in Settings.model_fields


def test_news_summary_daily_limit_defaults_and_accepts_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.model_fields["KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT"].default == 100
    monkeypatch.setenv("KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT", "37")

    assert Settings().KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT == 37


def test_mcp_settings_accept_absolute_http_url_and_normalize_values() -> None:
    configured = Settings(
        KASSET_AI_MCP_URL=" https://mcp.example.test/rpc/ ",
        KASSET_AI_MCP_TOOL_NAME=" review_market ",
        KASSET_AI_MCP_TIMEOUT_SECONDS=12.5,
    )

    assert configured.KASSET_AI_MCP_URL == "https://mcp.example.test/rpc"
    assert configured.KASSET_AI_MCP_TOOL_NAME == "review_market"
    assert configured.KASSET_AI_MCP_TIMEOUT_SECONDS == 12.5


def test_model_router_factory_builds_configured_mcp_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "https://mcp.test/rpc")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOKEN", None)
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOOL_NAME", "review_market")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)

    router = build_model_router()

    assert isinstance(router._mcp_client, McpStructuredJsonClient)
    assert router._mcp_client.tool_name == "review_market"
    assert router._primary_client is None
    assert router._fallback_client is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mcp.example.test/rpc",
        "https://user:password@mcp.example.test/rpc",
        "https://mcp.example.test/rpc?token=secret",
        "https://mcp.example.test/rpc#fragment",
        "not-a-url",
    ],
)
def test_mcp_url_rejects_unsafe_or_non_http_values(url: str) -> None:
    with pytest.raises(ValidationError, match="absolute HTTP"):
        Settings(KASSET_AI_MCP_URL=url)


@pytest.mark.parametrize("timeout", [0, -1, 120.1, float("inf"), float("nan")])
def test_mcp_timeout_is_positive_finite_and_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(KASSET_AI_MCP_TIMEOUT_SECONDS=timeout)


def test_mcp_tool_name_must_not_be_blank() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        Settings(KASSET_AI_MCP_TOOL_NAME="   ")
