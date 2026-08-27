"""OpenAI-wire-format API providers and the ordered availability chain.

OpenAI, DeepSeek, and OpenRouter all speak the same chat-completions wire
format, so one adapter plus per-provider profiles covers the whole API tier.
Swapping the production model is a configuration change, never a code change.

Fallback policy mirrors ``AiProviderRouter``: only availability failures
(connect/timeout, auth, rate limit, quota, 5xx) move to the next provider.
A reachable provider returning malformed analysis surfaces as an error —
silently retrying validation failures on another model would hide systematic
prompt or contract bugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from app.extensions.kasset.ai.base import AiProviderUnavailable, ExternalSkillRunner
from app.extensions.kasset.ai.models import SkillRequest, SkillResult

_UNAVAILABLE_STATUS = frozenset({401, 402, 403, 408, 429})
_ALLOWED_SIGNALS = frozenset({"BUY", "SELL", "HOLD", "WATCH"})

_SYSTEM_CONTRACT = (
    "You are a read-only market analysis assistant. Respond with a single "
    "JSON object and nothing else, using exactly these keys: "
    '"summary" (non-empty string), "signal" (one of "BUY", "SELL", "HOLD", '
    '"WATCH", or null), "confidence" (number between 0 and 1, or null), '
    '"rationale" (array of strings). Never propose order quantity, broker, '
    "account, leverage, or execution instructions."
)


@dataclass(frozen=True, slots=True)
class ApiProviderProfile:
    """One OpenAI-compatible endpoint. ``api_key`` is never logged."""

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


class OpenAiCompatibleProvider:
    """Chat-completions adapter for one configured endpoint profile."""

    def __init__(self, profile: ApiProviderProfile) -> None:
        self._profile = profile

    @property
    def name(self) -> str:
        return self._profile.name

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        profile = self._profile
        body = {
            "model": profile.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_CONTRACT},
                {"role": "user", "content": self._user_message(request)},
            ],
        }
        headers = {
            "Authorization": f"Bearer {profile.api_key}",
            **profile.extra_headers,
        }
        try:
            async with httpx.AsyncClient(
                base_url=profile.base_url.rstrip("/"),
                timeout=profile.timeout_seconds,
            ) as client:
                response = await client.post(
                    "/chat/completions", json=body, headers=headers
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise AiProviderUnavailable(
                f"{profile.name} unreachable: {type(exc).__name__}"
            ) from exc

        if response.status_code in _UNAVAILABLE_STATUS or response.status_code >= 500:
            raise AiProviderUnavailable(
                f"{profile.name} unavailable: HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise ValueError(
                f"{profile.name} rejected the analysis request: "
                f"HTTP {response.status_code}"
            )
        return self._parse_result(request, response)

    @staticmethod
    def _user_message(request: SkillRequest) -> str:
        payload = {
            "skill": request.skill,
            "symbol": request.symbol,
            "market": request.market,
            "instruction": request.instruction,
            "context": request.context,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _parse_result(
        self,
        request: SkillRequest,
        response: httpx.Response,
    ) -> SkillResult:
        profile = self._profile
        try:
            content = response.json()["choices"][0]["message"]["content"]
            analysis = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{profile.name} returned a malformed completion payload"
            ) from exc
        if not isinstance(analysis, dict):
            raise ValueError(f"{profile.name} did not return a JSON object")

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
    """Ordered availability chain over OpenAI-compatible providers.

    Realizes the operator's fallback intent (primary API -> OpenRouter):
    a provider is skipped only when it is unavailable; every other failure
    surfaces immediately.
    """

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
]
