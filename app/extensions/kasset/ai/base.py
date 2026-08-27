"""Common AI provider protocol for KAsset skills."""

from __future__ import annotations

from typing import Protocol

from app.extensions.kasset.ai.models import SkillRequest, SkillResult


class AiProviderUnavailable(RuntimeError):
    """Provider could not be reached or is not configured for this run."""


class ExternalSkillRunner(Protocol):
    """Minimal provider interface used by read-only KAsset skills."""

    @property
    def name(self) -> str: ...

    async def run_skill(self, request: SkillRequest) -> SkillResult: ...
