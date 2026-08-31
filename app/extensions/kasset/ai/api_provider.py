"""Responses API providers and the ordered legacy availability chain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx

from app.extensions.kasset.ai.base import (
    STRUCTURED_ANALYSIS_SYSTEM_INSTRUCTIONS,
    AiProviderUnavailable,
    ExternalSkillRunner,
    ReasoningEffort,
)
from app.extensions.kasset.ai.models import SkillRequest, SkillResult
from app.services.ai_usage_service import (
    COST_SOURCE_PROVIDER_REPORTED,
    report_ai_attempt_http_status,
    report_ai_attempt_usage,
)

_SYSTEM_CONTRACT = STRUCTURED_ANALYSIS_SYSTEM_INSTRUCTIONS

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


# The Responses API reports ``input_tokens``/``output_tokens``; OpenAI-compatible
# gateways in front of the same endpoint still emit the Chat Completions names.
# Both are accepted; anything else is left unreported rather than guessed.
_PROMPT_TOKEN_KEYS = ("input_tokens", "prompt_tokens")
_COMPLETION_TOKEN_KEYS = ("output_tokens", "completion_tokens")


def _token_count(usage: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value >= 0:
            return value
    return None


def _reported_cost(
    usage: Mapping[str, object],
) -> tuple[Decimal | None, str | None, str | None]:
    """Accept a cost only as an amount plus an explicit currency.

    There is no local price table here on purpose: model prices change without
    notice and a stale table reports confident, wrong money. A provider that
    quotes an amount but names no currency is therefore not recorded — the
    ledger keeps NULL and the dashboard shows "not reported".
    """

    currency = usage.get("cost_currency")
    amount = usage.get("cost")
    if not isinstance(currency, str) or not currency.strip():
        return None, None, None
    if isinstance(amount, bool) or not isinstance(amount, int | float | str):
        return None, None, None
    try:
        parsed = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None, None, None
    if not parsed.is_finite() or parsed < 0:
        return None, None, None
    return parsed, currency.strip(), COST_SOURCE_PROVIDER_REPORTED


def _report_provider_usage(envelope: object) -> None:
    """Forward the provider's own usage block to the AI call ledger.

    A missing or mis-shaped ``usage`` block reports nothing, so the attempt row
    keeps NULL tokens. Character-count estimates are never substituted.
    """

    if not isinstance(envelope, Mapping):
        return
    usage = envelope.get("usage")
    if not isinstance(usage, Mapping):
        return
    prompt_tokens = _token_count(usage, _PROMPT_TOKEN_KEYS)
    completion_tokens = _token_count(usage, _COMPLETION_TOKEN_KEYS)
    total_tokens = _token_count(usage, ("total_tokens",))
    if (
        total_tokens is None
        and prompt_tokens is not None
        and completion_tokens is not None
    ):
        total_tokens = prompt_tokens + completion_tokens
    cost_amount, cost_currency, cost_source = _reported_cost(usage)
    report_ai_attempt_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_source=cost_source,
    )


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

    @property
    def name(self) -> str:
        return self._name

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

        # The transport is the only layer that sees the wire status; the ledger
        # attempt row is opened one level up, in the availability router.
        report_ai_attempt_http_status(response.status_code)

        if response.status_code in {408, 429} or response.status_code >= 500:
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

        try:
            envelope = response.json()
        except ValueError as exc:
            raise ValueError(
                f"{self._name} returned a malformed Responses payload"
            ) from exc
        _report_provider_usage(envelope)

        output_text = self._extract_output_text(envelope)
        try:
            analysis = json.loads(output_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{self._name} returned malformed structured output"
            ) from exc
        if not isinstance(analysis, dict):
            raise ValueError(f"{self._name} did not return a JSON object")
        return analysis

    def _extract_output_text(self, envelope: object) -> str:
        output = envelope.get("output") if isinstance(envelope, Mapping) else None
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
        if set(analysis) != {"summary", "signal", "confidence", "rationale"}:
            raise ValueError(f"{profile.name} analysis response shape is invalid")
        summary = analysis["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"{profile.name} analysis is missing a summary")
        signal = analysis["signal"]
        if signal is not None and (
            not isinstance(signal, str) or signal not in _ALLOWED_SIGNALS
        ):
            raise ValueError(f"{profile.name} returned an unknown signal")
        confidence = analysis["confidence"]
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= confidence <= 1.0
        ):
            raise ValueError(f"{profile.name} returned invalid confidence")
        rationale = analysis["rationale"]
        if not isinstance(rationale, list) or any(
            not isinstance(item, str) for item in rationale
        ):
            raise ValueError(f"{profile.name} returned invalid rationale")
        return SkillResult(
            skill=request.skill,
            provider="api",
            summary=summary.strip(),
            signal=signal,
            confidence=confidence,
            rationale=[item for item in rationale if item.strip()],
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
