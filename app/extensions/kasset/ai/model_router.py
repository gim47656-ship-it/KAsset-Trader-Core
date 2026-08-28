"""Event-driven Luna, Terra, and Sol model routing for KAsset analysis."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.ai.api_provider import OpenAiResponsesClient
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.prompt_context import build_owner_address_instruction


class AnalysisKind(StrEnum):
    NEWS_TRIAGE = "news_triage"
    MARKET_STATE = "market_state"
    CANDIDATE_SCAN = "candidate_scan"
    CANDIDATE_REVIEW = "candidate_review"
    TRADE_REVIEW = "trade_review"
    CRITICAL_REVIEW = "critical_review"


class _TierAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["BUY", "SELL", "HOLD", "IGNORE", "REVIEW"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    bullish_score: int = Field(ge=0, le=100)
    bearish_score: int = Field(ge=0, le=100)
    escalate: bool
    rationale_tags: list[str] = Field(max_length=12)

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

_REASONING_EFFORT: dict[AnalysisKind, Literal["low", "medium", "high"]] = {
    AnalysisKind.NEWS_TRIAGE: "low",
    AnalysisKind.MARKET_STATE: "low",
    AnalysisKind.CANDIDATE_SCAN: "low",
    AnalysisKind.CANDIDATE_REVIEW: "medium",
    AnalysisKind.TRADE_REVIEW: "high",
    AnalysisKind.CRITICAL_REVIEW: "high",
}

_TIER_ANALYSIS_SCHEMA: dict[str, object] = _TierAnalysis.model_json_schema()


class OpenAiModelRouter:
    """Route an analysis to its starting model and escalate deterministically."""

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
    ) -> None:
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
        normalized_key = api_key.strip() if api_key is not None else ""
        self._primary_client = (
            OpenAiResponsesClient(
                name="openai-model-router",
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
                name="openrouter-model-router",
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
        primary_model = self._models[tier]
        if self._primary_client is None:
            raise AiProviderUnavailable("OpenAI model router is not configured")
        if not primary_model:
            raise AiProviderUnavailable(f"OpenAI {tier} model is not configured")
        try:
            return await self._request_verdict(
                self._primary_client,
                model=primary_model,
                kind=kind,
                payload=payload,
                correlation_id=correlation_id,
                include_reasoning=True,
                address_instruction=address_instruction,
            )
        except AiProviderUnavailable as exc:
            primary_error = exc

        fallback_model = self._fallback_models[tier]
        if self._fallback_client is None or not fallback_model:
            raise primary_error
        try:
            return await self._request_verdict(
                self._fallback_client,
                model=fallback_model,
                kind=kind,
                payload=payload,
                correlation_id=correlation_id,
                include_reasoning=False,
                address_instruction=address_instruction,
            )
        except AiProviderUnavailable as fallback_error:
            raise AiProviderUnavailable(
                f"OpenAI and OpenRouter {tier} models are unavailable: "
                f"{primary_error}; {fallback_error}"
            ) from fallback_error

    @staticmethod
    async def _request_verdict(
        client: OpenAiResponsesClient,
        *,
        model: str,
        kind: AnalysisKind,
        payload: dict[str, object],
        correlation_id: str | None,
        include_reasoning: bool,
        address_instruction: str | None,
    ) -> TierVerdict:
        raw = await client.request_json(
            model=model,
            input_payload={"kind": kind.value, "payload": payload},
            reasoning_effort=_REASONING_EFFORT[kind] if include_reasoning else None,
            schema_name="kasset_tier_verdict",
            schema=_TIER_ANALYSIS_SCHEMA,
            additional_instructions=address_instruction,
        )
        analysis = _TierAnalysis.model_validate(raw)
        return TierVerdict(
            **analysis.model_dump(),
            tier_used=model,
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
