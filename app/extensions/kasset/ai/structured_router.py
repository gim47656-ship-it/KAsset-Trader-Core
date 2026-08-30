"""Availability-only routing for strict structured AI transports."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from app.extensions.kasset.ai.base import (
    AiProviderUnavailable,
    ReasoningEffort,
    StructuredJsonClient,
)
from app.services.ai_usage_service import (
    AiAttemptTelemetry,
    AiCallAttempt,
    AiCallStatus,
    capture_ai_attempt,
    new_logical_call_id,
    record_ai_call_attempts,
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


def _attempt_row(
    *,
    logical_call_id: str,
    attempt_no: int,
    started_at: datetime,
    started_perf: float,
    feature: str,
    route_name: str,
    provider: str,
    model_name: str,
    status: AiCallStatus,
    error_type: str | None,
    telemetry: AiAttemptTelemetry,
) -> AiCallAttempt:
    """Close one attempt row.

    ``finished_at`` is derived from the monotonic clock rather than sampled
    from ``datetime.now`` a second time, so a wall-clock step (NTP) can never
    produce ``finished_at < started_at``.
    """

    latency_ms = max(0, round((perf_counter() - started_perf) * 1000))
    return AiCallAttempt(
        logical_call_id=logical_call_id,
        attempt_no=attempt_no,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=latency_ms),
        latency_ms=latency_ms,
        feature=feature,
        route_name=route_name,
        provider=provider,
        model_name=model_name,
        status=status,
        error_type=error_type,
        telemetry=telemetry,
    )


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

        # One logical request; one row per provider attempt underneath it. The
        # rows are buffered and appended once, so a fallback chain costs the
        # ledger a single INSERT instead of one per provider.
        logical_call_id = new_logical_call_id()
        attempts: list[AiCallAttempt] = []
        attempt_no = 0
        try:
            unavailable_reasons: list[str] = []
            for route in self._routes:
                effective_model = (route.model or model).strip()
                if not effective_model:
                    continue
                logger.info(
                    "KAsset AI provider attempt provider=%s model=%s "
                    "schema=%s route=%s",
                    route.client.name,
                    effective_model,
                    schema_name,
                    self._name,
                )
                # Exactly the identity already resolved for the log line above;
                # the ledger reuses it rather than recomputing it.
                attempt_no += 1
                started_at = datetime.now(UTC)
                started_perf = perf_counter()
                status: AiCallStatus = "failure"
                error_type: str | None = None

                with capture_ai_attempt() as telemetry:
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
                        # Bounded classifier only: the provider body can echo
                        # request headers, so no message ever reaches the ledger.
                        error_type = type(exc).__name__
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
                            error_type,
                        )
                        continue
                    except Exception as exc:
                        # Contract violations (refusal, malformed JSON) do not
                        # fall back, but they are still spent attempts.
                        error_type = type(exc).__name__
                        raise
                    else:
                        status = "success"
                    finally:
                        attempts.append(
                            _attempt_row(
                                logical_call_id=logical_call_id,
                                attempt_no=attempt_no,
                                started_at=started_at,
                                started_perf=started_perf,
                                feature=schema_name,
                                route_name=self._name,
                                provider=route.client.name,
                                model_name=effective_model,
                                status=status,
                                error_type=error_type,
                                telemetry=telemetry,
                            )
                        )

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
        finally:
            # Instrumentation, never a gate: this call swallows its own
            # failures, so neither the payload nor the raised error changes.
            await record_ai_call_attempts(attempts)


__all__ = [
    "AvailabilityRoutedJsonClient",
    "RoutedJsonResponse",
    "StructuredJsonRoute",
]
