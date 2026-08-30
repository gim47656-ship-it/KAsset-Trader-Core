"""Aggregation contract for the operations dashboard's AI usage panel.

The two numbers the screen must never conflate are proven here: attempts vs
logical calls, and "zero tokens" vs "no usage reported".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.models.ai_call_events import AiCallEvent
from app.services.ai_usage_service import (
    MAX_AI_USAGE_WINDOW,
    summarize_ai_usage,
)

_WINDOW = timedelta(hours=1)


def _isolated_window() -> tuple[datetime, datetime]:
    """A private slice of the timeline so parallel suites cannot collide."""

    offset = uuid.uuid4().int % 10_000_000
    since = datetime(1990, 1, 1, tzinfo=UTC) + timedelta(seconds=offset)
    return since, since + _WINDOW


def _event(
    *,
    logical_call_id: str,
    attempt_no: int,
    started_at: datetime,
    latency_ms: int,
    feature: str,
    provider: str,
    model_name: str,
    status: str,
    error_type: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cost_amount: Decimal | None = None,
    cost_currency: str | None = None,
) -> AiCallEvent:
    return AiCallEvent(
        logical_call_id=logical_call_id,
        attempt_no=attempt_no,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=latency_ms),
        latency_ms=latency_ms,
        feature=feature,
        route_name="unit:route",
        provider=provider,
        model_name=model_name,
        status=status,
        error_type=error_type,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_source=None if cost_amount is None else "provider_reported",
    )


async def _seed(db_session, since: datetime, until: datetime, events) -> None:
    await db_session.execute(
        delete(AiCallEvent).where(
            AiCallEvent.started_at >= since, AiCallEvent.started_at < until
        )
    )
    db_session.add_all(events)
    await db_session.commit()


async def _purge(db_session, since: datetime, until: datetime) -> None:
    await db_session.execute(
        delete(AiCallEvent).where(
            AiCallEvent.started_at >= since, AiCallEvent.started_at < until
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_attempts_and_logical_calls_diverge_when_routing_falls_back(
    db_session,
) -> None:
    since, until = _isolated_window()
    call_a = f"aic-{uuid.uuid4()}"
    call_b = f"aic-{uuid.uuid4()}"
    call_c = f"aic-{uuid.uuid4()}"
    events = [
        # One logical call that fell back: two attempts, one provider each.
        _event(
            logical_call_id=call_a,
            attempt_no=1,
            started_at=since + timedelta(minutes=1),
            latency_ms=10,
            feature="unit_alpha",
            provider="direct-api",
            model_name="gpt-terra",
            status="failure",
            error_type="AiProviderUnavailable",
        ),
        _event(
            logical_call_id=call_a,
            attempt_no=2,
            started_at=since + timedelta(minutes=2),
            latency_ms=20,
            feature="unit_alpha",
            provider="openrouter",
            model_name="or-pro",
            status="success",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        ),
        _event(
            logical_call_id=call_b,
            attempt_no=1,
            started_at=since + timedelta(minutes=3),
            latency_ms=30,
            feature="unit_alpha",
            provider="direct-api",
            model_name="gpt-terra",
            status="success",
            prompt_tokens=50,
            completion_tokens=5,
            total_tokens=55,
        ),
        # MCP transport: succeeded, reported no usage at all.
        _event(
            logical_call_id=call_c,
            attempt_no=1,
            started_at=since + timedelta(minutes=4),
            latency_ms=40,
            feature="unit_beta",
            provider="kasset-mcp",
            model_name="tool:run_skill",
            status="success",
        ),
    ]
    await _seed(db_session, since, until, events)

    try:
        summary = await summarize_ai_usage(db_session, since=since, until=until)

        # Four provider round trips served three product requests.
        assert summary.attempts == 4
        assert summary.logical_calls == 3
        assert summary.success_attempts == 3
        assert summary.failure_attempts == 1

        # Sums skip the rows that reported nothing, and those rows are counted.
        assert summary.prompt_tokens == 150
        assert summary.completion_tokens == 25
        assert summary.total_tokens == 175
        assert summary.attempts_without_usage == 2

        assert summary.p50_latency_ms == 20
        assert summary.p95_latency_ms == 40
        assert summary.cost_amount is None
        assert summary.cost_currency is None

        by_provider = {row.key: row for row in summary.by_provider}
        assert set(by_provider) == {"direct-api", "openrouter", "kasset-mcp"}
        assert by_provider["direct-api"].attempts == 2
        assert by_provider["direct-api"].logical_calls == 2
        assert by_provider["direct-api"].success_rate == pytest.approx(0.5)
        assert by_provider["direct-api"].attempts_without_usage == 1
        assert by_provider["kasset-mcp"].total_tokens == 0
        assert by_provider["kasset-mcp"].attempts_without_usage == 1
        # Busiest provider first.
        assert summary.by_provider[0].key == "direct-api"

        by_feature = {row.key: row for row in summary.by_feature}
        assert by_feature["unit_alpha"].attempts == 3
        assert by_feature["unit_alpha"].logical_calls == 2
        assert by_feature["unit_beta"].attempts == 1

        by_model = {row.key: row for row in summary.by_model}
        assert by_model["gpt-terra"].attempts == 2
        assert by_model["or-pro"].total_tokens == 120
    finally:
        await _purge(db_session, since, until)


@pytest.mark.asyncio
async def test_zero_token_row_is_not_reported_as_missing_usage(db_session) -> None:
    """A genuine zero must not be laundered into "not reported"."""

    since, until = _isolated_window()
    await _seed(
        db_session,
        since,
        until,
        [
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since + timedelta(minutes=1),
                latency_ms=5,
                feature="unit_zero",
                provider="direct-api",
                model_name="gpt-terra",
                status="success",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )
        ],
    )

    try:
        summary = await summarize_ai_usage(db_session, since=since, until=until)

        assert summary.attempts == 1
        assert summary.total_tokens == 0
        assert summary.attempts_without_usage == 0
    finally:
        await _purge(db_session, since, until)


@pytest.mark.asyncio
async def test_single_currency_cost_is_summed(db_session) -> None:
    since, until = _isolated_window()
    await _seed(
        db_session,
        since,
        until,
        [
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since + timedelta(minutes=1),
                latency_ms=5,
                feature="unit_cost",
                provider="openrouter",
                model_name="or-pro",
                status="success",
                total_tokens=10,
                cost_amount=Decimal("0.0042"),
                cost_currency="USD",
            ),
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since + timedelta(minutes=2),
                latency_ms=5,
                feature="unit_cost",
                provider="openrouter",
                model_name="or-pro",
                status="success",
                total_tokens=10,
                cost_amount=Decimal("0.0058"),
                cost_currency="USD",
            ),
        ],
    )

    try:
        summary = await summarize_ai_usage(db_session, since=since, until=until)

        assert summary.cost_amount == Decimal("0.0100")
        assert summary.cost_currency == "USD"
    finally:
        await _purge(db_session, since, until)


@pytest.mark.asyncio
async def test_mixed_currencies_report_no_total_instead_of_a_wrong_one(
    db_session,
) -> None:
    since, until = _isolated_window()
    await _seed(
        db_session,
        since,
        until,
        [
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since + timedelta(minutes=1),
                latency_ms=5,
                feature="unit_cost",
                provider="openrouter",
                model_name="or-pro",
                status="success",
                cost_amount=Decimal("1"),
                cost_currency="USD",
            ),
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since + timedelta(minutes=2),
                latency_ms=5,
                feature="unit_cost",
                provider="openrouter",
                model_name="or-pro",
                status="success",
                cost_amount=Decimal("1300"),
                cost_currency="KRW",
            ),
        ],
    )

    try:
        summary = await summarize_ai_usage(db_session, since=since, until=until)

        assert summary.cost_amount is None
        assert summary.cost_currency is None
    finally:
        await _purge(db_session, since, until)


@pytest.mark.asyncio
async def test_empty_window_returns_zeros_and_no_percentiles(db_session) -> None:
    since, until = _isolated_window()
    await _purge(db_session, since, until)

    summary = await summarize_ai_usage(db_session, since=since, until=until)

    assert summary.attempts == 0
    assert summary.logical_calls == 0
    assert summary.prompt_tokens == 0
    assert summary.attempts_without_usage == 0
    assert summary.cost_amount is None
    assert summary.p50_latency_ms is None
    assert summary.p95_latency_ms is None
    assert summary.by_provider == ()
    assert summary.by_model == ()
    assert summary.by_feature == ()


@pytest.mark.asyncio
async def test_window_bounds_are_half_open(db_session) -> None:
    since, until = _isolated_window()
    await _seed(
        db_session,
        since,
        until,
        [
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=since,
                latency_ms=1,
                feature="unit_edge",
                provider="direct-api",
                model_name="gpt-terra",
                status="success",
            ),
            _event(
                logical_call_id=f"aic-{uuid.uuid4()}",
                attempt_no=1,
                started_at=until,
                latency_ms=1,
                feature="unit_edge",
                provider="direct-api",
                model_name="gpt-terra",
                status="success",
            ),
        ],
    )

    try:
        summary = await summarize_ai_usage(db_session, since=since, until=until)

        # ``since`` is included, ``until`` is not.
        assert summary.attempts == 1
    finally:
        await _purge(db_session, since, until + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_naive_bounds_are_rejected(db_session) -> None:
    since, until = _isolated_window()

    with pytest.raises(ValueError, match="timezone-aware 'since'"):
        await summarize_ai_usage(
            db_session, since=since.replace(tzinfo=None), until=until
        )
    with pytest.raises(ValueError, match="timezone-aware 'until'"):
        await summarize_ai_usage(
            db_session, since=since, until=until.replace(tzinfo=None)
        )


@pytest.mark.asyncio
async def test_inverted_window_is_rejected(db_session) -> None:
    since, until = _isolated_window()

    with pytest.raises(ValueError, match="since < until"):
        await summarize_ai_usage(db_session, since=until, until=since)
    with pytest.raises(ValueError, match="since < until"):
        await summarize_ai_usage(db_session, since=since, until=since)


@pytest.mark.asyncio
async def test_unbounded_window_is_refused(db_session) -> None:
    """A 2 vCPU production database does not get an 'all time' scan."""

    since, _ = _isolated_window()

    with pytest.raises(ValueError, match="window exceeds"):
        await summarize_ai_usage(
            db_session,
            since=since,
            until=since + MAX_AI_USAGE_WINDOW + timedelta(seconds=1),
        )

    # Exactly at the cap is allowed.
    summary = await summarize_ai_usage(
        db_session, since=since, until=since + MAX_AI_USAGE_WINDOW
    )
    assert summary.attempts >= 0
