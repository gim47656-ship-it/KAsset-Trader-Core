"""Responses API providers and the ordered legacy availability chain."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.extensions.kasset.ai.base import AiProviderUnavailable, ExternalSkillRunner
from app.extensions.kasset.ai.models import SkillRequest, SkillResult

_SYSTEM_CONTRACT = (
    "You are KAsset Core's read-only market-analysis layer. Use only the JSON "
    "input supplied by the application. Return one JSON object matching the "
    "provided schema and no explanatory text. Never call tools or request more "
    "data. Never provide broker, account, leverage, quantity, or order-execution "
    "instructions."
)

_SKILL_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 20_000},
        "signal": {
            "anyOf": [
                {"type": "string", "enum": ["BUY", "SELL", "HOLD", "WATCH"]},
                {"type": "null"},
            ]
        },
        "confidence": {
            "anyOf": [
                {"type": "number", "minimum": 0.0, "maximum": 1.0},
                {"type": "null"},
            ]
        },
        "rationale": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "signal", "confidence", "rationale"],
    "additionalProperties": False,
}

_ALLOWED_SIGNALS = frozenset({"BUY", "SELL", "HOLD", "WATCH"})

ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ApiProviderProfile:
    """One Responses API endpoint. ``api_key`` is never logged."""

    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for attribute in ("name", "base_url", "api_key", "model"):
            if not str(getattr(self, attribute)).strip():
                raise ValueError(f"profile {attribute} is required")


class OpenAiResponsesClient:
    """Small Responses API client shared by skills and tiered routing."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("client name is required")
        if not base_url.strip():
            raise ValueError("client base_url is required")
        if not api_key.strip():
            raise ValueError("client api_key is required")
        self._name = name
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._extra_headers = dict(extra_headers or {})

    async def request_json(
        self,
        *,
        model: str,
        input_payload: dict[str, object],
        reasoning_effort: ReasoningEffort | None,
        schema_name: str,
        schema: dict[str, object],
        additional_instructions: str | None = None,
    ) -> dict[str, object]:
        """Request one strict structured response without exposing any tools."""

        instructions = _SYSTEM_CONTRACT
        if additional_instructions is not None and additional_instructions.strip():
            instructions = f"{instructions} {additional_instructions.strip()}"
        body: dict[str, object] = {
            "model": model,
            "instructions": instructions,
            "input": json.dumps(
                input_payload,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "strict": True,
                    "name": schema_name,
                    "schema": schema,
                }
            },
        }
        if reasoning_effort is not None:
            body["reasoning"] = {"effort": reasoning_effort}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            **self._extra_headers,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post("responses", json=body, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise AiProviderUnavailable(
                f"{self._name} unreachable: {type(exc).__name__}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise AiProviderUnavailable(
                f"{self._name} unavailable: HTTP {response.status_code}"
            )
        if not response.is_success:
            excerpt = response.text.replace(self._api_key, "[REDACTED]")[:200]
            if not excerpt:
                excerpt = "<empty response body>"
            raise ValueError(
                f"{self._name} rejected the analysis request: "
                f"HTTP {response.status_code}: {excerpt}"
            )

        output_text = self._extract_output_text(response)
        try:
            analysis = json.loads(output_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{self._name} returned malformed structured output"
            ) from exc
        if not isinstance(analysis, dict):
            raise ValueError(f"{self._name} did not return a JSON object")
        return analysis

    def _extract_output_text(self, response: httpx.Response) -> str:
        try:
            output = response.json()["output"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{self._name} returned a malformed Responses payload"
            ) from exc
        if not isinstance(output, list):
            raise ValueError(f"{self._name} returned a malformed Responses payload")

        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "refusal":
                raise ValueError(f"{self._name} refused the analysis request")
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise ValueError(f"{self._name} refused the analysis request")
                if part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text
        raise ValueError(f"{self._name} returned empty structured output")


class OpenAiCompatibleProvider:
    """Preserve ``run_skill`` over the shared Responses API client."""

    def __init__(self, profile: ApiProviderProfile) -> None:
        self._profile = profile
        self._client = OpenAiResponsesClient(
            name=profile.name,
            base_url=profile.base_url,
            api_key=profile.api_key,
            timeout_seconds=profile.timeout_seconds,
            extra_headers=profile.extra_headers,
        )

    @property
    def name(self) -> str:
        return self._profile.name

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        analysis = await self._client.request_json(
            model=self._profile.model,
            input_payload=self._input_payload(request),
            reasoning_effort="medium",
            schema_name="kasset_skill_result",
            schema=_SKILL_RESULT_SCHEMA,
        )
        return self._parse_result(request, analysis)

    @staticmethod
    def _input_payload(request: SkillRequest) -> dict[str, object]:
        return {
            "skill": request.skill,
            "symbol": request.symbol,
            "market": request.market,
            "instruction": request.instruction,
            "context": request.context,
        }

    def _parse_result(
        self,
        request: SkillRequest,
        analysis: dict[str, object],
    ) -> SkillResult:
        profile = self._profile
        summary = analysis.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"{profile.name} analysis is missing a summary")
        signal = analysis.get("signal")
        if signal is not None:
            signal = str(signal).strip().upper()
            if signal not in _ALLOWED_SIGNALS:
                raise ValueError(f"{profile.name} returned an unknown signal")
        confidence = analysis.get("confidence")
        rationale = analysis.get("rationale")
        return SkillResult(
            skill=request.skill,
            provider="api",
            summary=summary.strip(),
            signal=signal,
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            rationale=[
                str(item)
                for item in (rationale if isinstance(rationale, list) else [])
                if str(item).strip()
            ],
            metadata={"provider_profile": profile.name, "model": profile.model},
            correlation_id=request.correlation_id,
        )


class ChainedApiProvider:
    """Fallback only when a configured Responses endpoint is unavailable."""

    def __init__(self, providers: list[ExternalSkillRunner]) -> None:
        if not providers:
            raise ValueError("at least one API provider is required")
        self._providers = list(providers)

    @property
    def name(self) -> str:
        return "api"

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        reasons: list[str] = []
        for provider in self._providers:
            try:
                return await provider.run_skill(request)
            except AiProviderUnavailable as exc:
                reasons.append(str(exc))
        raise AiProviderUnavailable(
            "every API provider is unavailable: " + "; ".join(reasons)
        )


__all__ = [
    "ApiProviderProfile",
    "ChainedApiProvider",
    "OpenAiCompatibleProvider",
    "OpenAiResponsesClient",
]
