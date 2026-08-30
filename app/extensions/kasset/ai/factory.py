"""설정과 저장된 route 정책에 따라 KAsset AI provider route를 조립한다.

lane별 순서는 ``AiRuntimeSnapshot``이 정하고, credential과 model 문자열은 여전히
``settings``에서만 온다. snapshot을 주지 않으면 환경변수만 있던 시절과 동일한
기본 순서(``DEFAULT_ROUTE_POLICY``)를 쓴다. 정책에 있어도 credential이나 model이
비어 있는 route는 조용히 빠지고, availability 오류에만 다음 route로 넘어간다.
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
from app.extensions.kasset.ai.runtime_config import (
    DEFAULT_ROUTE_POLICY,
    AiLane,
    AiRouteId,
    AiRuntimeSnapshot,
    ai_route_provider,
)
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


def _lane_route_order(
    snapshot: AiRuntimeSnapshot | None,
    lane: AiLane,
) -> tuple[AiRouteId, ...]:
    """lane의 정책 순서. snapshot이 없으면 환경변수 시절 기본 순서."""

    if snapshot is None:
        return DEFAULT_ROUTE_POLICY[lane]
    return snapshot.routes(lane)


def _build_api_json_routes(
    *,
    direct_model: str,
    fallback_model: str,
    route_order: tuple[AiRouteId, ...],
) -> list[StructuredJsonRoute]:
    normalized_direct_model = direct_model.strip()
    direct_key = (
        settings.KASSET_AI_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_API_KEY is not None
        else ""
    )
    normalized_fallback_model = fallback_model.strip()
    openrouter_key = (
        settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_OPENROUTER_API_KEY is not None
        else ""
    )

    routes: list[StructuredJsonRoute] = []
    for route_id in route_order:
        provider = ai_route_provider(route_id)
        if provider == "direct-api":
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
        elif provider == "openrouter" and openrouter_key and normalized_fallback_model:
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
    snapshot: AiRuntimeSnapshot | None = None,
) -> AvailabilityRoutedJsonClient | None:
    """``summary_luna`` 정책 순서로 요약 route를 만든다.

    lane이 비어 있거나 사용 가능한 route가 없으면 ``None``을 돌려주고, 호출자는
    "미설정" 상태로 처리한다. 환경변수 기본값으로 되돌아가지 않는다.
    """

    routes = _build_api_json_routes(
        direct_model=direct_model,
        fallback_model=fallback_model,
        route_order=_lane_route_order(snapshot, AiLane.SUMMARY_LUNA),
    )
    if not routes:
        return None
    return AvailabilityRoutedJsonClient(name=name, routes=routes)


def build_api_provider_chain(
    *,
    snapshot: AiRuntimeSnapshot | None = None,
) -> ChainedApiProvider | None:
    """``compat_skill`` 정책 순서로 호환 ``run_skill`` API chain을 만든다.

    이 lane의 호출은 ``review.ai_call_events`` 원장을 우회하므로 화면에서
    latest success가 ``null``로 보고된다. "실패"가 아니라 "측정 불가"다.
    """

    route_order = _lane_route_order(snapshot, AiLane.COMPAT_SKILL)
    providers: list[ExternalSkillRunner] = []
    primary_model = settings.KASSET_AI_MODEL_TERRA.strip()
    primary_key = (
        settings.KASSET_AI_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_API_KEY is not None
        else ""
    )
    openrouter_model = settings.KASSET_AI_OPENROUTER_MODEL_PRO.strip()
    openrouter_key = (
        settings.KASSET_AI_OPENROUTER_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_OPENROUTER_API_KEY is not None
        else ""
    )
    for route_id in route_order:
        provider = ai_route_provider(route_id)
        if provider == "direct-api":
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
        elif provider == "openrouter" and openrouter_key and openrouter_model:
            providers.append(
                OpenAiCompatibleProvider(
                    ApiProviderProfile(
                        name="openrouter",
                        base_url=settings.KASSET_AI_OPENROUTER_BASE_URL,
                        api_key=openrouter_key,
                        model=openrouter_model,
                    )
                )
            )
    if not providers:
        return None
    return ChainedApiProvider(providers)


def build_model_router(
    *,
    snapshot: AiRuntimeSnapshot | None = None,
) -> OpenAiModelRouter:
    """검토 lane router를 만든다. 미설정 provider는 그대로 fail-closed다."""

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
        route_policy=snapshot.lanes if snapshot is not None else None,
    )


def build_ai_provider_router(
    *,
    subscription_invoker: SubscriptionInvoker | None = None,
    snapshot: AiRuntimeSnapshot | None = None,
) -> AiProviderRouter:
    """``compat_skill`` 정책으로 호환 stack 전체를 조립한다.

    정책에서 ``subscription_cli``가 빠지면 구독 bridge는 명시적으로 비활성화되며
    호출자가 넘긴 ``subscription_invoker``도 쓰지 않는다. bridge가 없으면 구독
    tier는 unavailable을 보고하고 hybrid mode는 API chain으로 넘어간다. mode
    자체(``KASSET_AI_PROVIDER_MODE``)의 의미는 바뀌지 않는다.
    """

    route_order = _lane_route_order(snapshot, AiLane.COMPAT_SKILL)
    if AiRouteId.SUBSCRIPTION_CLI not in route_order:
        subscription_invoker = None
    elif subscription_invoker is None:
        subscription_command = settings.KASSET_AI_SUBSCRIPTION_CMD.strip()
        if subscription_command:
            subscription_invoker = build_cli_invoker(
                shlex.split(subscription_command),
                settings.KASSET_AI_SUBSCRIPTION_TIMEOUT_SECONDS,
            )

    return AiProviderRouter(
        mode=settings.KASSET_AI_PROVIDER_MODE,
        subscription=SubscriptionAgentProvider(subscription_invoker),
        api=build_api_provider_chain(snapshot=snapshot),
    )


__all__ = [
    "build_ai_provider_router",
    "build_api_provider_chain",
    "build_model_router",
    "build_summary_json_client",
]
