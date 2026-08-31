"""Event-driven Luna, Terra, and Sol model routing for KAsset analysis."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.ai.api_provider import OpenAiResponsesClient
from app.extensions.kasset.ai.base import StructuredJsonClient
from app.extensions.kasset.ai.prompt_context import build_owner_address_instruction
from app.extensions.kasset.ai.runtime_config import (
    DEFAULT_ROUTE_POLICY,
    AiLane,
    AiRoutePolicy,
    ai_route_provider,
)
from app.extensions.kasset.ai.structured_router import (
    AvailabilityRoutedJsonClient,
    StructuredJsonRoute,
)
from app.services.ai_usage_service import attribute_ai_calls
from app.services.research_canonical_hash import canonical_sha256


class AnalysisKind(StrEnum):
    NEWS_TRIAGE = "news_triage"
    MARKET_STATE = "market_state"
    CANDIDATE_SCAN = "candidate_scan"
    CANDIDATE_REVIEW = "candidate_review"
    TRADE_REVIEW = "trade_review"
    CRITICAL_REVIEW = "critical_review"


class _TierAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["BUY", "SELL", "HOLD", "IGNORE", "REVIEW"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    bullish_score: int = Field(ge=0, le=100)
    bearish_score: int = Field(ge=0, le=100)
    escalate: bool
    rationale_tags: list[str] = Field(
        max_length=12,
        description=(
            "Short noun-like rationale tags only; each tag must be at most 64 "
            "characters and contain no sentence punctuation or user names."
        ),
    )

    @field_validator("rationale_tags")
    @classmethod
    def validate_rationale_tags(cls, tags: list[str]) -> list[str]:
        normalized: list[str] = []
        for tag in tags:
            value = tag.strip()
            if not value or len(value) > 64 or any(mark in value for mark in ".!?\r\n"):
                raise ValueError("rationale_tags must contain short non-sentence tags")
            normalized.append(value)
        return normalized


class TierVerdict(_TierAnalysis):
    tier_used: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    tier: Literal["luna", "terra", "sol"]
    model_id: str = Field(min_length=1)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: AnalysisKind
    correlation_id: str | None


_Tier = Literal["luna", "terra", "sol"]

_STARTING_TIER: dict[AnalysisKind, _Tier] = {
    AnalysisKind.NEWS_TRIAGE: "luna",
    AnalysisKind.MARKET_STATE: "luna",
    AnalysisKind.CANDIDATE_SCAN: "luna",
    AnalysisKind.CANDIDATE_REVIEW: "terra",
    AnalysisKind.TRADE_REVIEW: "terra",
    AnalysisKind.CRITICAL_REVIEW: "sol",
}

#: 각 tier가 정책을 읽어오는 lane. 저장된 정책은 route ID 순서만 담는다.
_TIER_LANE: dict[_Tier, AiLane] = {
    "luna": AiLane.REVIEW_LUNA,
    "terra": AiLane.REVIEW_TERRA,
    "sol": AiLane.REVIEW_SOL,
}

_REASONING_EFFORT: dict[AnalysisKind, Literal["low", "medium", "high"]] = {
    AnalysisKind.NEWS_TRIAGE: "low",
    AnalysisKind.MARKET_STATE: "low",
    AnalysisKind.CANDIDATE_SCAN: "low",
    AnalysisKind.CANDIDATE_REVIEW: "medium",
    AnalysisKind.TRADE_REVIEW: "high",
    AnalysisKind.CRITICAL_REVIEW: "high",
}

_MCP_REVIEW_KINDS = frozenset(
    {
        AnalysisKind.CANDIDATE_REVIEW,
        AnalysisKind.TRADE_REVIEW,
        AnalysisKind.CRITICAL_REVIEW,
    }
)


_TIER_ANALYSIS_SCHEMA: dict[str, object] = _TierAnalysis.model_json_schema()


def _normalized_request_input(
    kind: AnalysisKind,
    payload: dict[str, object],
) -> dict[str, object]:
    """Normalize the exact JSON-safe object sent to the selected provider."""

    encoded = json.dumps(
        {"kind": kind.value, "payload": payload},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    normalized = json.loads(encoded)
    if type(normalized) is not dict:
        raise ValueError("structured AI input must normalize to a JSON object")
    return normalized


class OpenAiModelRouter:
    """Route analysis by feature, provider availability, and model tier."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        luna_model: str,
        terra_model: str,
        sol_model: str,
        openrouter_base_url: str | None = None,
        openrouter_api_key: str | None = None,
        openrouter_flash_model: str = "",
        openrouter_pro_model: str = "",
        timeout_seconds: float = 60.0,
        mcp_client: StructuredJsonClient | None = None,
        route_policy: AiRoutePolicy | None = None,
    ) -> None:
        # 한 cycle이 공유하는 불변 정책. ``None``이면 환경변수 시절과 동일한
        # 기본 순서를 쓴다. 빈 lane은 명시적 비활성화이므로 기본값으로
        # 되돌리지 않는다.
        self._route_policy: AiRoutePolicy = (
            route_policy if route_policy is not None else DEFAULT_ROUTE_POLICY
        )
        self._models: dict[_Tier, str] = {
            "luna": luna_model.strip(),
            "terra": terra_model.strip(),
            "sol": sol_model.strip(),
        }
        self._fallback_models: dict[_Tier, str] = {
            "luna": openrouter_flash_model.strip(),
            "terra": openrouter_pro_model.strip(),
            "sol": openrouter_pro_model.strip(),
        }
        self._mcp_client = mcp_client
        normalized_key = api_key.strip() if api_key is not None else ""
        self._primary_client = (
            OpenAiResponsesClient(
                name="direct-api",
                base_url=base_url,
                api_key=normalized_key,
                timeout_seconds=timeout_seconds,
            )
            if normalized_key
            else None
        )
        normalized_openrouter_key = (
            openrouter_api_key.strip() if openrouter_api_key is not None else ""
        )
        if normalized_openrouter_key and openrouter_base_url is None:
            raise ValueError(
                "OpenRouter base URL is required when its key is configured"
            )
        self._fallback_client = (
            OpenAiResponsesClient(
                name="openrouter",
                base_url=openrouter_base_url or "",
                api_key=normalized_openrouter_key,
                timeout_seconds=timeout_seconds,
            )
            if normalized_openrouter_key
            else None
        )

    async def analyze_for_owner(
        self,
        db: AsyncSession,
        owner_user_id: int,
        kind: AnalysisKind,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
    ) -> TierVerdict:
        address_instruction = await build_owner_address_instruction(db, owner_user_id)
        with attribute_ai_calls(owner_user_id=owner_user_id):
            return await self.analyze(
                kind,
                payload,
                correlation_id=correlation_id,
                address_instruction=address_instruction,
            )

    async def analyze(
        self,
        kind: AnalysisKind,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
        address_instruction: str | None = None,
    ) -> TierVerdict:
        kind = AnalysisKind(kind)
        tier = _STARTING_TIER[kind]
        verdict = await self._run_tier(
            tier,
            kind,
            payload,
            correlation_id=correlation_id,
            address_instruction=address_instruction,
        )

        if tier == "sol":
            return verdict
        if tier == "luna":
            if (
                verdict.confidence >= 0.80
                and verdict.action in {"HOLD", "IGNORE"}
                and not verdict.escalate
            ):
                return verdict
            verdict = await self._run_tier(
                "terra",
                kind,
                self._with_prior(payload, verdict),
                correlation_id=correlation_id,
                address_instruction=address_instruction,
            )

        if (
            verdict.confidence >= 0.85
            and verdict.risk != "HIGH"
            and not verdict.escalate
        ):
            return verdict
        return await self._run_tier(
            "sol",
            AnalysisKind.CRITICAL_REVIEW,
            self._with_prior(payload, verdict),
            correlation_id=correlation_id,
            address_instruction=address_instruction,
        )

    async def _run_tier(
        self,
        tier: _Tier,
        kind: AnalysisKind,
        payload: dict[str, object],
        *,
        correlation_id: str | None,
        address_instruction: str | None,
    ) -> TierVerdict:
        routes = self._lane_routes(tier, kind)

        routed_client = AvailabilityRoutedJsonClient(
            name=f"{kind.value}:{tier}",
            routes=routes,
        )
        request_model = next(
            (route.model or "" for route in routes if route.model),
            "",
        )
        return await self._request_verdict(
            routed_client,
            model=request_model,
            tier=tier,
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
            include_reasoning=True,
            address_instruction=address_instruction,
        )

    def _lane_routes(
        self,
        tier: _Tier,
        kind: AnalysisKind,
    ) -> list[StructuredJsonRoute]:
        """정책 순서대로 사용 가능한 route만 조립한다.

        credential이나 model이 비어 있는 route는 조용히 빠진다(기존 fail-closed
        동작). MCP는 정책에 있어도 MCP 대상 분석 종류에서만 쓰인다. lane이 비어
        있으면 route가 하나도 없고 ``AvailabilityRoutedJsonClient``가
        ``AiProviderUnavailable``로 끝낸다.
        """

        routes: list[StructuredJsonRoute] = []
        for route_id in self._route_policy.get(_TIER_LANE[tier], ()):
            provider = ai_route_provider(route_id)
            if provider == "mcp":
                if self._mcp_client is None or kind not in _MCP_REVIEW_KINDS:
                    continue
                tool_name = str(getattr(self._mcp_client, "tool_name", "run_skill"))
                routes.append(
                    StructuredJsonRoute(
                        client=self._mcp_client,
                        model=f"tool:{tool_name}",
                    )
                )
            elif provider == "direct-api":
                primary_model = self._models[tier]
                if self._primary_client is not None and primary_model:
                    routes.append(
                        StructuredJsonRoute(
                            client=self._primary_client,
                            model=primary_model,
                        )
                    )
            elif provider == "openrouter":
                fallback_model = self._fallback_models[tier]
                if self._fallback_client is not None and fallback_model:
                    routes.append(
                        StructuredJsonRoute(
                            client=self._fallback_client,
                            model=fallback_model,
                            include_reasoning=False,
                        )
                    )
        return routes

    @staticmethod
    async def _request_verdict(
        client: StructuredJsonClient,
        *,
        model: str,
        tier: _Tier,
        kind: AnalysisKind,
        payload: dict[str, object],
        correlation_id: str | None,
        include_reasoning: bool,
        address_instruction: str | None,
    ) -> TierVerdict:
        request_input = _normalized_request_input(kind, payload)
        input_hash = canonical_sha256(request_input)
        with attribute_ai_calls(correlation_id=correlation_id):
            raw = await client.request_json(
                model=model,
                input_payload=request_input,
                reasoning_effort=_REASONING_EFFORT[kind] if include_reasoning else None,
                schema_name="kasset_tier_verdict",
                schema=_TIER_ANALYSIS_SCHEMA,
                additional_instructions=address_instruction,
            )
        provider = getattr(raw, "provider_name", None)
        model_id = getattr(raw, "model_name", None)
        if (
            type(provider) is not str
            or not provider.strip()
            or type(model_id) is not str
            or not model_id.strip()
        ):
            raise RuntimeError(
                "structured AI route did not expose its selected provider and model"
            )
        analysis = _TierAnalysis.model_validate(raw)
        return TierVerdict(
            **analysis.model_dump(),
            tier_used=model_id,
            provider=provider,
            tier=tier,
            model_id=model_id,
            input_hash=input_hash,
            kind=kind,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _with_prior(
        payload: dict[str, object],
        verdict: TierVerdict,
    ) -> dict[str, object]:
        escalated_payload = dict(payload)
        escalated_payload["prior"] = {
            "action": verdict.action,
            "confidence": verdict.confidence,
            "risk": verdict.risk,
            "bullish_score": verdict.bullish_score,
            "bearish_score": verdict.bearish_score,
        }
        return escalated_payload


__all__ = ["AnalysisKind", "OpenAiModelRouter", "TierVerdict"]
