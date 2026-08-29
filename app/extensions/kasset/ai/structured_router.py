"""Availability-only routing for strict structured AI transports."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.extensions.kasset.ai.base import (
    AiProviderUnavailable,
    ReasoningEffort,
    StructuredJsonClient,
)

logger = logging.getLogger(__name__)


class RoutedJsonResponse(dict[str, object]):
    """A validated provider payload with non-serialized audit attributes."""

    def __init__(
        self,
        payload: dict[str, object],
        *,
        provider_name: str,
        model_name: str,
    ) -> None:
        super().__init__(payload)
        self.provider_name = provider_name
        self.model_name = model_name


@dataclass(frozen=True, slots=True)
class StructuredJsonRoute:
    client: StructuredJsonClient
    model: str | None = None
    include_reasoning: bool = True


class AvailabilityRoutedJsonClient:
    """Try configured providers in order only for availability failures."""

    def __init__(self, *, name: str, routes: list[StructuredJsonRoute]) -> None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("structured JSON route name is required")
        self._name = normalized_name
        self._routes = tuple(routes)

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
        if not self._routes:
            raise AiProviderUnavailable(f"{self._name} is not configured: no providers")

        unavailable_reasons: list[str] = []
        for route in self._routes:
            effective_model = (route.model or model).strip()
            if not effective_model:
                continue
            logger.info(
                "KAsset AI provider attempt provider=%s model=%s schema=%s route=%s",
                route.client.name,
                effective_model,
                schema_name,
                self._name,
            )
            try:
                payload = await route.client.request_json(
                    model=effective_model,
                    input_payload=input_payload,
                    reasoning_effort=(
                        reasoning_effort if route.include_reasoning else None
                    ),
                    schema_name=schema_name,
                    schema=schema,
                    additional_instructions=additional_instructions,
                )
            except AiProviderUnavailable as exc:
                unavailable_reasons.append(
                    f"{route.client.name}/{effective_model}: {exc}"
                )
                logger.warning(
                    "KAsset AI provider unavailable provider=%s model=%s "
                    "schema=%s route=%s error_type=%s",
                    route.client.name,
                    effective_model,
                    schema_name,
                    self._name,
                    type(exc).__name__,
                )
                continue
            return RoutedJsonResponse(
                payload,
                provider_name=route.client.name,
                model_name=effective_model,
            )

        if unavailable_reasons:
            raise AiProviderUnavailable(
                f"every provider in {self._name} is unavailable: "
                + "; ".join(unavailable_reasons)
            )
        raise AiProviderUnavailable(
            f"{self._name} is not configured: no provider models"
        )


__all__ = [
    "AvailabilityRoutedJsonClient",
    "RoutedJsonResponse",
    "StructuredJsonRoute",
]
