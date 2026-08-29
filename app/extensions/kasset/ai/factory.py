"""설정에 따라 KAsset AI provider route를 조립한다.

복잡한 검토는 MCP, direct API, OpenRouter 순서로 사용한다. 요약은 MCP를
사용하지 않고 direct API 다음 OpenRouter를 사용하며 availability 오류에만 fallback한다.
"""

from __future__ import annotations

import shlex

from app.core.config import settings
from app.extensions.kasset.ai.api_provider import (
    ApiProviderProfile,
    ChainedApiProvider,
    OpenAiCompatibleProvider,
    OpenAiResponsesClient,
)
from app.extensions.kasset.ai.base import ExternalSkillRunner
from app.extensions.kasset.ai.mcp_provider import McpStructuredJsonClient
from app.extensions.kasset.ai.model_router import OpenAiModelRouter
from app.extensions.kasset.ai.provider_router import AiProviderRouter
from app.extensions.kasset.ai.structured_router import (
    AvailabilityRoutedJsonClient,
    StructuredJsonRoute,
)
from app.extensions.kasset.ai.subscription_cli import build_cli_invoker
from app.extensions.kasset.ai.subscription_provider import (
    SubscriptionAgentProvider,
    SubscriptionInvoker,
)


def _build_mcp_json_client() -> McpStructuredJsonClient | None:
    mcp_url = settings.KASSET_AI_MCP_URL.strip()
    if not mcp_url:
        return None
    mcp_token = (
        settings.KASSET_AI_MCP_TOKEN.get_secret_value()
        if settings.KASSET_AI_MCP_TOKEN is not None
        else None
    )
    return McpStructuredJsonClient(
        url=mcp_url,
        token=mcp_token,
        tool_name=settings.KASSET_AI_MCP_TOOL_NAME,
        timeout_seconds=settings.KASSET_AI_MCP_TIMEOUT_SECONDS,
    )


def _build_api_json_routes(
    *,
    direct_model: str,
    fallback_model: str,
) -> list[StructuredJsonRoute]:
    routes: list[StructuredJsonRoute] = []
    normalized_direct_model = direct_model.strip()
    direct_key = (
        settings.KASSET_AI_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_API_KEY is not None
        else ""
    )
    if direct_key and normalized_direct_model:
        routes.append(
            StructuredJsonRoute(
                client=OpenAiResponsesClient(
                    name="direct-api",
                    base_url=settings.KASSET_AI_API_BASE_URL,
                    api_key=direct_key,
                ),
                model=normalized_direct_model,
            )
        )

    normalized_fallback_model = fallback_model.strip()
    openrouter_key = (
        settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_OPENROUTER_API_KEY is not None
        else ""
    )
    if openrouter_key and normalized_fallback_model:
        routes.append(
            StructuredJsonRoute(
                client=OpenAiResponsesClient(
                    name="openrouter",
                    base_url=settings.KASSET_AI_OPENROUTER_BASE_URL,
                    api_key=openrouter_key,
                ),
                model=normalized_fallback_model,
                include_reasoning=False,
            )
        )
    return routes


def build_summary_json_client(
    *,
    name: str,
    direct_model: str,
    fallback_model: str,
) -> AvailabilityRoutedJsonClient | None:
    """direct API -> OpenRouter 요약 route를 만든다."""

    routes = _build_api_json_routes(
        direct_model=direct_model,
        fallback_model=fallback_model,
    )
    if not routes:
        return None
    return AvailabilityRoutedJsonClient(name=name, routes=routes)


def build_api_provider_chain() -> ChainedApiProvider | None:
    """Build the compatibility ``run_skill`` API provider chain."""

    providers: list[ExternalSkillRunner] = []
    primary_model = settings.KASSET_AI_MODEL_TERRA.strip()
    primary_key = (
        settings.KASSET_AI_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_API_KEY is not None
        else ""
    )
    if primary_key and primary_model:
        providers.append(
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="primary-api",
                    base_url=settings.KASSET_AI_API_BASE_URL,
                    api_key=primary_key,
                    model=primary_model,
                )
            )
        )
    openrouter_key = (
        settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_OPENROUTER_API_KEY is not None
        else ""
    )
    if openrouter_key and settings.KASSET_AI_OPENROUTER_MODEL_PRO.strip():
        providers.append(
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="openrouter",
                    base_url=settings.KASSET_AI_OPENROUTER_BASE_URL,
                    api_key=openrouter_key,
                    model=settings.KASSET_AI_OPENROUTER_MODEL_PRO,
                )
            )
        )
    if not providers:
        return None
    return ChainedApiProvider(providers)


def build_model_router() -> OpenAiModelRouter:
    """Build the event-driven router; missing providers stay fail-closed."""

    api_key = (
        settings.KASSET_AI_API_KEY.get_secret_value()
        if settings.KASSET_AI_API_KEY is not None
        else None
    )
    openrouter_api_key = (
        settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value()
        if settings.KASSET_AI_OPENROUTER_API_KEY is not None
        else None
    )
    mcp_client = _build_mcp_json_client()
    return OpenAiModelRouter(
        base_url=settings.KASSET_AI_API_BASE_URL,
        api_key=api_key,
        luna_model=settings.KASSET_AI_MODEL_LUNA,
        terra_model=settings.KASSET_AI_MODEL_TERRA,
        sol_model=settings.KASSET_AI_MODEL_SOL,
        openrouter_base_url=settings.KASSET_AI_OPENROUTER_BASE_URL,
        openrouter_api_key=openrouter_api_key,
        openrouter_flash_model=settings.KASSET_AI_OPENROUTER_MODEL_FLASH,
        openrouter_pro_model=settings.KASSET_AI_OPENROUTER_MODEL_PRO,
        mcp_client=mcp_client,
    )


def build_ai_provider_router(
    *,
    subscription_invoker: SubscriptionInvoker | None = None,
) -> AiProviderRouter:
    """Assemble the full stack from settings.

    An explicit ``subscription_invoker`` takes precedence over the configured
    subscription CLI command. Without either bridge the subscription tier
    reports unavailable and hybrid mode falls through to the API chain:
    the primary OpenAI-format endpoint first, then OpenRouter when configured.
    """
    if subscription_invoker is None:
        subscription_command = settings.KASSET_AI_SUBSCRIPTION_CMD.strip()
        if subscription_command:
            subscription_invoker = build_cli_invoker(
                shlex.split(subscription_command),
                settings.KASSET_AI_SUBSCRIPTION_TIMEOUT_SECONDS,
            )

    return AiProviderRouter(
        mode=settings.KASSET_AI_PROVIDER_MODE,
        subscription=SubscriptionAgentProvider(subscription_invoker),
        api=build_api_provider_chain(),
    )


__all__ = [
    "build_ai_provider_router",
    "build_api_provider_chain",
    "build_model_router",
    "build_summary_json_client",
]
