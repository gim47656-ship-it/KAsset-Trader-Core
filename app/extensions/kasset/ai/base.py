"""Common AI provider protocol for KAsset skills."""

from __future__ import annotations

from typing import Literal, Protocol

from app.extensions.kasset.ai.models import SkillRequest, SkillResult

STRUCTURED_ANALYSIS_SYSTEM_INSTRUCTIONS = (
    "You are KAsset Core's read-only market-analysis layer. Use only the JSON "
    "input supplied by the application. Return one JSON object matching the "
    "provided schema and no explanatory text. Never call tools or request more "
    "data. Never provide broker, account, leverage, quantity, or order-execution "
    "instructions."
)


class AiProviderUnavailable(RuntimeError):
    """Provider가 미설정, 연결 불가, timeout 또는 HTTP 5xx 상태다."""


ReasoningEffort = Literal["low", "medium", "high"]


class StructuredJsonClient(Protocol):
    """Provider-neutral strict-JSON analysis transport."""

    @property
    def name(self) -> str: ...

    async def request_json(
        self,
        *,
        model: str,
        input_payload: dict[str, object],
        reasoning_effort: ReasoningEffort | None,
        schema_name: str,
        schema: dict[str, object],
        additional_instructions: str | None = None,
    ) -> dict[str, object]: ...


class ExternalSkillRunner(Protocol):
    """Minimal provider interface used by read-only KAsset skills."""

    @property
    def name(self) -> str: ...

    async def run_skill(self, request: SkillRequest) -> SkillResult: ...
