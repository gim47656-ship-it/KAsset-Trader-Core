"""Provider selection and fail-closed hybrid fallback for KAsset AI skills."""

from __future__ import annotations

from app.extensions.kasset.ai.base import AiProvider, AiProviderUnavailable
from app.extensions.kasset.ai.models import AiProviderMode, SkillRequest, SkillResult


class AiProviderRouter:
    def __init__(
        self,
        *,
        mode: AiProviderMode | str,
        subscription: AiProvider | None = None,
        api: AiProvider | None = None,
    ) -> None:
        self._mode = AiProviderMode(mode)
        self._subscription = subscription
        self._api = api

    @property
    def mode(self) -> AiProviderMode:
        return self._mode

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        if self._mode is AiProviderMode.SUBSCRIPTION:
            return await self._require(self._subscription, "subscription").run_skill(request)

        if self._mode is AiProviderMode.API:
            return await self._require(self._api, "api").run_skill(request)

        subscription = self._require(self._subscription, "subscription")
        try:
            return await subscription.run_skill(request)
        except AiProviderUnavailable:
            # Availability-only fallback. Validation errors, malformed output,
            # or safety failures must surface instead of being hidden by retrying
            # through another model/provider.
            return await self._require(self._api, "api").run_skill(request)

    @staticmethod
    def _require(provider: AiProvider | None, name: str) -> AiProvider:
        if provider is None:
            raise AiProviderUnavailable(f"{name} provider is not configured")
        return provider
