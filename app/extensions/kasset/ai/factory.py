"""Configuration-driven assembly of the KAsset AI provider stacks.

The event pipeline uses the OpenAI-only Luna/Terra/Sol router. The legacy
``run_skill`` provider router remains available for existing MCP consumers.
"""

from __future__ import annotations

from app.core.config import settings
from app.extensions.kasset.ai.api_provider import (
    ApiProviderProfile,
    ChainedApiProvider,
    OpenAiCompatibleProvider,
)
from app.extensions.kasset.ai.base import ExternalSkillRunner
from app.extensions.kasset.ai.model_router import OpenAiModelRouter
from app.extensions.kasset.ai.provider_router import AiProviderRouter
from app.extensions.kasset.ai.subscription_provider import (
    SubscriptionAgentProvider,
    SubscriptionInvoker,
)


def build_api_provider_chain() -> ChainedApiProvider | None:
    """Build the compatibility ``run_skill`` API provider chain."""

    providers: list[ExternalSkillRunner] = []
    primary_model = (
        settings.KASSET_AI_MODEL_TERRA.strip() or settings.KASSET_AI_API_MODEL.strip()
    )
    if settings.KASSET_AI_API_KEY is not None and primary_model:
        providers.append(
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="primary-api",
                    base_url=settings.KASSET_AI_API_BASE_URL,
                    api_key=settings.KASSET_AI_API_KEY.get_secret_value(),
                    model=primary_model,
                )
            )
        )
    if (
        settings.KASSET_AI_OPENROUTER_API_KEY is not None
        and settings.KASSET_AI_OPENROUTER_MODEL.strip()
    ):
        providers.append(
            OpenAiCompatibleProvider(
                ApiProviderProfile(
                    name="openrouter",
                    base_url=settings.KASSET_AI_OPENROUTER_BASE_URL,
                    api_key=settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value(),
                    model=settings.KASSET_AI_OPENROUTER_MODEL,
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
    )


def build_ai_provider_router(
    *,
    subscription_invoker: SubscriptionInvoker | None = None,
) -> AiProviderRouter:
    """Assemble the full stack from settings.

    ``subscription_invoker`` is the process-external bridge for the operator's
    subscription account (an OpenAI/Codex account today; any agent CLI that
    reaches the same MCP server works). Without it the subscription tier
    reports unavailable and hybrid mode falls through to the API chain:
    the primary OpenAI-format endpoint first, then OpenRouter when configured.
    """

    return AiProviderRouter(
        mode=settings.KASSET_AI_PROVIDER_MODE,
        subscription=SubscriptionAgentProvider(subscription_invoker),
        api=build_api_provider_chain(),
    )


__all__ = [
    "build_ai_provider_router",
    "build_api_provider_chain",
    "build_model_router",
]
