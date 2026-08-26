from __future__ import annotations

import pytest

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.models import AiProviderMode, SkillRequest, SkillResult
from app.extensions.kasset.ai.provider_router import AiProviderRouter


class _Provider:
    def __init__(self, name: str, *, unavailable: bool = False) -> None:
        self._name = name
        self._unavailable = unavailable
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        self.calls += 1
        if self._unavailable:
            raise AiProviderUnavailable(self._name)
        return SkillResult(
            skill=request.skill,
            provider="subscription" if self._name == "subscription" else "api",
            summary=f"handled by {self._name}",
            signal="HOLD",
            confidence=0.5,
            correlation_id=request.correlation_id,
        )


@pytest.mark.asyncio
async def test_hybrid_falls_back_only_when_subscription_unavailable() -> None:
    subscription = _Provider("subscription", unavailable=True)
    api = _Provider("api")
    router = AiProviderRouter(
        mode=AiProviderMode.HYBRID,
        subscription=subscription,
        api=api,
    )
    request = SkillRequest(skill="technical_analysis", instruction="analyze")

    result = await router.run_skill(request)

    assert result.provider == "api"
    assert subscription.calls == 1
    assert api.calls == 1


def test_skill_request_rejects_credential_like_context() -> None:
    with pytest.raises(ValueError, match="forbidden credential-like keys"):
        SkillRequest(
            skill="technical_analysis",
            instruction="analyze",
            context={"broker": {"api_key": "must-not-leave-runtime"}},
        )
