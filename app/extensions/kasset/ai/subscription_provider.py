"""Bridge for externally hosted subscription-backed AI agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.models import SkillRequest, SkillResult

SubscriptionInvoker = Callable[
    [SkillRequest], Awaitable[SkillResult | Mapping[str, Any]]
]


class SubscriptionAgentProvider:
    """Adapter for a Codex/Claude-style agent connected outside this process.

    The caller injects an async function that performs the external interaction.
    This provider only normalizes the result into the shared SkillResult contract.
    """

    def __init__(self, invoke_agent: SubscriptionInvoker | None = None) -> None:
        self._invoke_agent = invoke_agent

    @property
    def name(self) -> str:
        return "subscription"

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        if self._invoke_agent is None:
            raise AiProviderUnavailable("subscription agent bridge is not configured")

        try:
            raw = await self._invoke_agent(request)
        except AiProviderUnavailable:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise AiProviderUnavailable(
                f"subscription agent unavailable: {type(exc).__name__}"
            ) from exc

        if isinstance(raw, SkillResult):
            return raw.model_copy(
                update={
                    "provider": "subscription",
                    "skill": request.skill,
                    "correlation_id": request.correlation_id,
                }
            )

        payload = dict(raw)
        payload["provider"] = "subscription"
        payload["skill"] = request.skill
        payload["correlation_id"] = request.correlation_id
        return SkillResult.model_validate(payload)
