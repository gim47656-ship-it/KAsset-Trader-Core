"""Contract tests for the AI call ledger instrumentation.

Two guarantees are load-bearing here:

* attempts are counted honestly (one row per provider try, grouped under one
  ``logical_call_id``, failures included with a bounded ``error_type``), and
* the ledger can never break the AI call it measures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete, select

from app.extensions.kasset.ai import structured_router
from app.extensions.kasset.ai.api_provider import OpenAiResponsesClient
from app.extensions.kasset.ai.base import AiProviderUnavailable, ReasoningEffort
from app.extensions.kasset.ai.structured_router import (
    AvailabilityRoutedJsonClient,
    StructuredJsonRoute,
)
from app.models.ai_call_events import AiCallEvent
from app.services import ai_usage_service
from app.services.ai_usage_service import AiCallAttempt

_SCHEMA: dict[str, object] = {"type": "object", "additionalProperties": False}


class _FakeClient:
    """Structured transport whose per-attempt outcome is scripted."""

    def __init__(self, name: str, outcomes: list[object]) -> None:
        self._name = name
        self._outcomes = list(outcomes)
        self.calls = 0

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
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome


class _UsageReportingClient(_FakeClient):
    """Transport that reports usage the way ``OpenAiResponsesClient`` does."""

    def __init__(
        self,
        name: str,
        outcomes: list[object],
        *,
        usage: dict[str, object] | None,
    ) -> None:
        super().__init__(name, outcomes)
        self._usage = usage

    async def request_json(self, **kwargs: object) -> dict[str, object]:
        if self._usage is not None:
            ai_usage_service.report_ai_attempt_usage(**self._usage)  # type: ignore[arg-type]
        return await super().request_json(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def captured_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[AiCallAttempt]:
    """Intercept the ledger append and expose the rows it was handed."""

    recorded: list[AiCallAttempt] = []

    async def _capture(attempts: list[AiCallAttempt]) -> bool:
        recorded.extend(attempts)
        return True

    monkeypatch.setattr(structured_router, "record_ai_call_attempts", _capture)
    return recorded


async def _run(client: AvailabilityRoutedJsonClient) -> dict[str, object]:
    return await client.request_json(
        model="default-model",
        input_payload={"kind": "unit"},
        reasoning_effort="low",
        schema_name="unit_schema",
        schema=_SCHEMA,
    )


@pytest.mark.asyncio
async def test_fallback_records_one_row_per_attempt_under_one_logical_call(
    captured_attempts: list[AiCallAttempt],
) -> None:
    primary = _FakeClient("direct-api", [AiProviderUnavailable("HTTP 503")])
    fallback = _FakeClient("openrouter", [{"ok": True}])
    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(client=primary, model="primary-model"),
            StructuredJsonRoute(client=fallback, model="fallback-model"),
        ],
    )

    payload = await _run(client)

    assert payload == {"ok": True}
    assert len(captured_attempts) == 2
    assert len({attempt.logical_call_id for attempt in captured_attempts}) == 1
    assert [attempt.attempt_no for attempt in captured_attempts] == [1, 2]

    failed, succeeded = captured_attempts
    assert failed.status == "failure"
    assert failed.provider == "direct-api"
    assert failed.model_name == "primary-model"
    # Bounded classifier only: no provider message, no header echo.
    assert failed.error_type == "AiProviderUnavailable"
    assert "503" not in (failed.error_type or "")
    assert succeeded.status == "success"
    assert succeeded.error_type is None
    assert succeeded.provider == "openrouter"
    assert succeeded.model_name == "fallback-model"
    for attempt in captured_attempts:
        assert attempt.feature == "unit_schema"
        assert attempt.route_name == "unit:terra"
        assert attempt.latency_ms >= 0
        assert attempt.finished_at >= attempt.started_at


@pytest.mark.asyncio
async def test_every_provider_failing_still_records_each_attempt(
    captured_attempts: list[AiCallAttempt],
) -> None:
    client = AvailabilityRoutedJsonClient(
        name="unit:sol",
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("direct-api", [AiProviderUnavailable("down")]),
                model="m1",
            ),
            StructuredJsonRoute(
                client=_FakeClient("openrouter", [AiProviderUnavailable("down")]),
                model="m2",
            ),
        ],
    )

    with pytest.raises(AiProviderUnavailable):
        await _run(client)

    assert [a.status for a in captured_attempts] == ["failure", "failure"]
    assert [a.attempt_no for a in captured_attempts] == [1, 2]
    assert {a.error_type for a in captured_attempts} == {"AiProviderUnavailable"}


@pytest.mark.asyncio
async def test_non_availability_failure_is_recorded_and_still_propagates(
    captured_attempts: list[AiCallAttempt],
) -> None:
    """A refusal/malformed payload does not fall back, but it is a spent attempt."""

    client = AvailabilityRoutedJsonClient(
        name="unit:luna",
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("direct-api", [ValueError("refused")]),
                model="m1",
            ),
            StructuredJsonRoute(client=_FakeClient("openrouter", [{}]), model="m2"),
        ],
    )

    with pytest.raises(ValueError):
        await _run(client)

    assert len(captured_attempts) == 1
    assert captured_attempts[0].status == "failure"
    assert captured_attempts[0].error_type == "ValueError"


@pytest.mark.asyncio
async def test_transport_without_usage_leaves_tokens_null(
    captured_attempts: list[AiCallAttempt],
) -> None:
    """MCP/subscription-style transports report nothing; NULL is not zero."""

    client = AvailabilityRoutedJsonClient(
        name="unit:mcp",
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("kasset-mcp", [{"ok": True}]),
                model="tool:run_skill",
            )
        ],
    )

    await _run(client)

    telemetry = captured_attempts[0].telemetry
    assert telemetry.prompt_tokens is None
    assert telemetry.completion_tokens is None
    assert telemetry.total_tokens is None
    assert telemetry.cost_amount is None


@pytest.mark.asyncio
async def test_transport_reported_usage_reaches_the_attempt_row(
    captured_attempts: list[AiCallAttempt],
) -> None:
    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(
                client=_UsageReportingClient(
                    "direct-api",
                    [{"ok": True}],
                    usage={
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                ),
                model="m1",
            )
        ],
    )

    await _run(client)

    telemetry = captured_attempts[0].telemetry
    assert telemetry.prompt_tokens == 11
    assert telemetry.completion_tokens == 7
    assert telemetry.total_tokens == 18


@pytest.mark.asyncio
async def test_usage_reported_outside_an_attempt_is_a_noop() -> None:
    """No active attempt means no ambient state to corrupt."""

    ai_usage_service.report_ai_attempt_usage(prompt_tokens=5)
    ai_usage_service.report_ai_attempt_http_status(200)


# --------------------------------------------------------------------------- #
# Instrumentation must never break the feature it measures                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ledger_write_failure_does_not_change_a_successful_ai_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("ledger database is on fire")

    monkeypatch.setattr(ai_usage_service, "AsyncSessionLocal", _explode)

    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("direct-api", [{"verdict": "HOLD"}]),
                model="m1",
            )
        ],
    )

    with caplog.at_level("WARNING", logger="app.services.ai_usage_service"):
        payload = await _run(client)

    assert payload == {"verdict": "HOLD"}
    assert payload.provider_name == "direct-api"  # type: ignore[attr-defined]
    assert any("AI call ledger append failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_ledger_write_failure_preserves_the_original_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AI failure must surface, not a ledger failure standing in for it."""

    def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("ledger database is on fire")

    monkeypatch.setattr(ai_usage_service, "AsyncSessionLocal", _explode)

    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("direct-api", [AiProviderUnavailable("down")]),
                model="m1",
            )
        ],
    )

    with pytest.raises(AiProviderUnavailable, match="every provider in unit:terra"):
        await _run(client)


@pytest.mark.asyncio
async def test_record_ai_call_attempts_swallows_database_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("no database here")

    monkeypatch.setattr(ai_usage_service, "AsyncSessionLocal", _explode)

    started = datetime.now(UTC)
    committed = await ai_usage_service.record_ai_call_attempts(
        [
            AiCallAttempt(
                logical_call_id="aic-unit",
                attempt_no=1,
                started_at=started,
                finished_at=started,
                latency_ms=0,
                feature="unit_schema",
                route_name="unit:terra",
                provider="direct-api",
                model_name="m1",
                status="success",
            )
        ]
    )

    assert committed is False


@pytest.mark.asyncio
async def test_record_ai_call_attempts_ignores_an_empty_batch() -> None:
    assert await ai_usage_service.record_ai_call_attempts([]) is False


# --------------------------------------------------------------------------- #
# Real transport: token extraction from the wire                               #
# --------------------------------------------------------------------------- #


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._response


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: _Transport) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _envelope(usage: dict[str, object] | None) -> dict[str, object]:
    envelope: dict[str, object] = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps({"ok": True})}],
            }
        ]
    }
    if usage is not None:
        envelope["usage"] = usage
    return envelope


async def _responses_attempt(
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, object] | None,
    captured_attempts: list[AiCallAttempt],
) -> AiCallAttempt:
    _patch_transport(
        monkeypatch,
        _Transport(httpx.Response(200, json=_envelope(usage))),
    )
    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(
                client=OpenAiResponsesClient(
                    name="direct-api",
                    base_url="https://example.test/v1",
                    api_key="unit-key",
                ),
                model="m1",
            )
        ],
    )
    await _run(client)
    return captured_attempts[0]


@pytest.mark.asyncio
async def test_responses_usage_block_populates_token_counts(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(
        monkeypatch,
        {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        captured_attempts,
    )

    assert attempt.telemetry.prompt_tokens == 120
    assert attempt.telemetry.completion_tokens == 30
    assert attempt.telemetry.total_tokens == 150
    assert attempt.telemetry.http_status == 200


@pytest.mark.asyncio
async def test_missing_total_is_derived_only_from_reported_halves(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(
        monkeypatch,
        {"prompt_tokens": 40, "completion_tokens": 2},
        captured_attempts,
    )

    assert attempt.telemetry.total_tokens == 42


@pytest.mark.asyncio
async def test_response_without_usage_block_keeps_tokens_null_not_zero(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(monkeypatch, None, captured_attempts)

    assert attempt.telemetry.prompt_tokens is None
    assert attempt.telemetry.completion_tokens is None
    assert attempt.telemetry.total_tokens is None
    assert attempt.telemetry.http_status == 200


@pytest.mark.asyncio
async def test_misshaped_usage_values_are_not_guessed_at(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(
        monkeypatch,
        {"input_tokens": "many", "output_tokens": None, "total_tokens": -3},
        captured_attempts,
    )

    assert attempt.telemetry.prompt_tokens is None
    assert attempt.telemetry.completion_tokens is None
    assert attempt.telemetry.total_tokens is None


@pytest.mark.asyncio
async def test_cost_without_an_explicit_currency_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(
        monkeypatch,
        {"input_tokens": 5, "output_tokens": 5, "cost": 0.0042},
        captured_attempts,
    )

    assert attempt.telemetry.cost_amount is None
    assert attempt.telemetry.cost_currency is None
    assert attempt.telemetry.cost_source is None


@pytest.mark.asyncio
async def test_cost_with_amount_and_currency_is_recorded_as_provider_reported(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    attempt = await _responses_attempt(
        monkeypatch,
        {
            "input_tokens": 5,
            "output_tokens": 5,
            "cost": "0.0042",
            "cost_currency": "USD",
        },
        captured_attempts,
    )

    assert attempt.telemetry.cost_amount == Decimal("0.0042")
    assert attempt.telemetry.cost_currency == "USD"
    assert attempt.telemetry.cost_source == "provider_reported"


@pytest.mark.asyncio
async def test_unavailable_status_still_records_the_http_status(
    monkeypatch: pytest.MonkeyPatch,
    captured_attempts: list[AiCallAttempt],
) -> None:
    _patch_transport(
        monkeypatch, _Transport(httpx.Response(503, json={"error": "down"}))
    )
    client = AvailabilityRoutedJsonClient(
        name="unit:terra",
        routes=[
            StructuredJsonRoute(
                client=OpenAiResponsesClient(
                    name="direct-api",
                    base_url="https://example.test/v1",
                    api_key="unit-key",
                ),
                model="m1",
            )
        ],
    )

    with pytest.raises(AiProviderUnavailable):
        await _run(client)

    attempt = captured_attempts[0]
    assert attempt.status == "failure"
    assert attempt.error_type == "AiProviderUnavailable"
    assert attempt.telemetry.http_status == 503
    assert attempt.telemetry.total_tokens is None


# --------------------------------------------------------------------------- #
# The rows actually land in review.ai_call_events, from a caller with no session
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_attempts_are_committed_without_a_caller_supplied_session(
    db_session,
) -> None:
    """``request_json`` takes no session; the recorder opens its own."""

    route_name = f"ledgerdb:{datetime.now(UTC).timestamp()}"
    client = AvailabilityRoutedJsonClient(
        name=route_name,
        routes=[
            StructuredJsonRoute(
                client=_FakeClient("direct-api", [AiProviderUnavailable("down")]),
                model="primary-model",
            ),
            StructuredJsonRoute(
                client=_UsageReportingClient(
                    "openrouter",
                    [{"ok": True}],
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                ),
                model="fallback-model",
            ),
        ],
    )

    try:
        await _run(client)

        rows = (
            (
                await db_session.execute(
                    select(AiCallEvent)
                    .where(AiCallEvent.route_name == route_name)
                    .order_by(AiCallEvent.attempt_no)
                )
            )
            .scalars()
            .all()
        )

        assert [row.attempt_no for row in rows] == [1, 2]
        assert len({row.logical_call_id for row in rows}) == 1
        assert [row.status for row in rows] == ["failure", "success"]
        assert rows[0].error_type == "AiProviderUnavailable"
        assert rows[0].total_tokens is None
        assert rows[1].total_tokens == 7
        assert rows[1].prompt_tokens == 3
        assert rows[1].completion_tokens == 4
        assert rows[0].cost_amount is None
        assert rows[0].finished_at - rows[0].started_at >= timedelta(0)
    finally:
        await db_session.execute(
            delete(AiCallEvent).where(AiCallEvent.route_name == route_name)
        )
        await db_session.commit()
