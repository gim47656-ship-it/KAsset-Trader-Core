"""Write and read side of the AI call ledger (``review.ai_call_events``).

Two halves that share one table:

* **Write** — :func:`record_ai_call_attempts` appends attempt rows. It is
  instrumentation, so it is *best effort by contract*: a ledger failure must
  never change or abort the AI call that produced it. Every failure is
  swallowed and logged. This mirrors :mod:`app.core.session_blacklist`, which
  already opens its own ``AsyncSessionLocal`` and degrades on ``Exception``
  instead of propagating.

* **Read** — :func:`summarize_ai_usage` aggregates a bounded time window for
  the operations dashboard.

Session ownership: the writer never borrows the caller's session. AI calls run
inside HTTP requests, taskiq workers and the MCP server alike, and most of
those call sites hold no ``AsyncSession`` at all. Joining a caller transaction
would also mean (a) a business rollback silently erases the audit trail and
(b) a rejected ledger INSERT poisons the caller's transaction. So the writer
always opens one short-lived session of its own; a caller without a session is
the normal case, not a special case.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Row, distinct, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.ai_call_events import (
    COST_SOURCE_PROVIDER_REPORTED,
    AiCallEvent,
    AiCallStatus,
)

logger = logging.getLogger(__name__)

#: Widest window :func:`summarize_ai_usage` will scan. Production runs on two
#: vCPUs; an unbounded "all time" aggregate is how a dashboard takes the API
#: down.
MAX_AI_USAGE_WINDOW = timedelta(days=92)


def new_logical_call_id() -> str:
    """Identifier shared by every provider attempt of one logical AI request."""

    return f"aic-{uuid.uuid4()}"


# --------------------------------------------------------------------------- #
# Write side                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class AiAttemptTelemetry:
    """What the transport learned about one attempt.

    Every field stays ``None`` unless the provider actually reported it. A
    transport that returns no usage block (MCP, subscription CLI, Cloudflare,
    Hermes) leaves this untouched, and the ledger row keeps ``NULL`` so a
    reader can tell "not reported" from "zero".
    """

    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    cost_source: str | None = None


_ACTIVE_ATTEMPT: ContextVar[AiAttemptTelemetry | None] = ContextVar(
    "ai_call_attempt_telemetry",
    default=None,
)


@contextmanager
def capture_ai_attempt() -> Iterator[AiAttemptTelemetry]:
    """Open a telemetry slot that the transport underneath can fill in.

    The routing layer knows which provider it is about to call but not how many
    tokens that call burned; only the HTTP transport sees the ``usage`` block.
    A context-scoped slot carries that upward without widening the
    ``StructuredJsonClient`` return contract, which every transport implements.
    """

    telemetry = AiAttemptTelemetry()
    token = _ACTIVE_ATTEMPT.set(telemetry)
    try:
        yield telemetry
    finally:
        _ACTIVE_ATTEMPT.reset(token)


def report_ai_attempt_http_status(status_code: int) -> None:
    """Record the provider's HTTP status. No-op outside an attempt."""

    telemetry = _ACTIVE_ATTEMPT.get()
    if telemetry is not None:
        telemetry.http_status = status_code


def report_ai_attempt_usage(
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cost_amount: Decimal | None = None,
    cost_currency: str | None = None,
    cost_source: str | None = None,
) -> None:
    """Record provider-reported usage. No-op outside an attempt.

    Cost is accepted only as an amount/currency/source triple; a bare number
    without a stated currency is dropped rather than guessed at.
    """

    telemetry = _ACTIVE_ATTEMPT.get()
    if telemetry is None:
        return
    telemetry.prompt_tokens = prompt_tokens
    telemetry.completion_tokens = completion_tokens
    telemetry.total_tokens = total_tokens
    if cost_amount is not None and cost_currency and cost_source:
        telemetry.cost_amount = cost_amount
        telemetry.cost_currency = cost_currency
        telemetry.cost_source = cost_source


@dataclass(frozen=True, slots=True)
class AiCallAttempt:
    """One ``review.ai_call_events`` row, ready to append."""

    logical_call_id: str
    attempt_no: int
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    feature: str
    route_name: str
    provider: str
    model_name: str
    status: AiCallStatus
    error_type: str | None = None
    telemetry: AiAttemptTelemetry = field(default_factory=AiAttemptTelemetry)
    owner_user_id: int | None = None
    correlation_id: str | None = None


def _row(attempt: AiCallAttempt) -> dict[str, object]:
    telemetry = attempt.telemetry
    return {
        "logical_call_id": attempt.logical_call_id,
        "attempt_no": attempt.attempt_no,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "latency_ms": attempt.latency_ms,
        "feature": attempt.feature,
        "route_name": attempt.route_name,
        "provider": attempt.provider,
        "model_name": attempt.model_name,
        "status": attempt.status,
        "error_type": attempt.error_type,
        "http_status": telemetry.http_status,
        "prompt_tokens": telemetry.prompt_tokens,
        "completion_tokens": telemetry.completion_tokens,
        "total_tokens": telemetry.total_tokens,
        "cost_amount": telemetry.cost_amount,
        "cost_currency": telemetry.cost_currency,
        "cost_source": telemetry.cost_source,
        "owner_user_id": attempt.owner_user_id,
        "correlation_id": attempt.correlation_id,
    }


async def record_ai_call_attempts(attempts: Sequence[AiCallAttempt]) -> bool:
    """Append every attempt of one logical call in a single INSERT.

    Never raises: instrumentation that can break the feature it measures is
    worse than no instrumentation. Returns whether the rows were committed so
    callers *may* log, not so they may react.

    ``asyncio.CancelledError`` is deliberately not swallowed — a cancelled AI
    call must stay cancelled.
    """

    if not attempts:
        return False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(insert(AiCallEvent), [_row(a) for a in attempts])
            await session.commit()
    except Exception:
        logger.warning(
            "AI call ledger append failed; %d attempt row(s) dropped "
            "(logical_call_id=%s). The AI call itself is unaffected.",
            len(attempts),
            attempts[0].logical_call_id,
            exc_info=True,
        )
        return False
    return True


# --------------------------------------------------------------------------- #
# Read side                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AiUsageBreakdown:
    """Aggregates for one provider, model or feature."""

    key: str
    attempts: int
    logical_calls: int
    success_attempts: int
    failure_attempts: int
    success_rate: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    attempts_without_usage: int
    cost_amount: Decimal | None
    cost_currency: str | None


@dataclass(frozen=True, slots=True)
class AiUsageSummary:
    """AI usage over ``[since, until)``.

    ``attempts`` and ``logical_calls`` are both exposed on purpose.
    ``attempts`` alone hides nothing but also flatters nothing — it counts
    every provider round trip, so fallbacks and tier escalations show up.
    ``logical_calls`` counts what the product actually asked for. Reporting
    only one of the two hides either the real spend or the real workload.

    Token sums skip rows that reported no usage; ``attempts_without_usage``
    counts those rows so a reader never reads "0 tokens" as "no usage".
    """

    since: datetime
    until: datetime
    logical_calls: int
    attempts: int
    success_attempts: int
    failure_attempts: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    attempts_without_usage: int
    cost_amount: Decimal | None
    cost_currency: str | None
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    by_provider: tuple[AiUsageBreakdown, ...]
    by_model: tuple[AiUsageBreakdown, ...]
    by_feature: tuple[AiUsageBreakdown, ...]


_NO_USAGE = (
    AiCallEvent.prompt_tokens.is_(None)
    & AiCallEvent.completion_tokens.is_(None)
    & AiCallEvent.total_tokens.is_(None)
)


def _aggregates() -> list[ColumnElement[object]]:
    """The aggregate list shared by the totals and breakdown queries."""

    return [
        func.count().label("attempts"),
        func.count(distinct(AiCallEvent.logical_call_id)).label("logical_calls"),
        func.count().filter(AiCallEvent.status == "success").label("success_attempts"),
        func.count().filter(AiCallEvent.status == "failure").label("failure_attempts"),
        func.coalesce(func.sum(AiCallEvent.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(AiCallEvent.completion_tokens), 0).label(
            "completion_tokens"
        ),
        func.coalesce(func.sum(AiCallEvent.total_tokens), 0).label("total_tokens"),
        func.count().filter(_NO_USAGE).label("attempts_without_usage"),
        func.sum(AiCallEvent.cost_amount).label("cost_amount"),
        func.count(distinct(AiCallEvent.cost_currency)).label("cost_currencies"),
        func.min(AiCallEvent.cost_currency).label("cost_currency"),
    ]


def _resolved_cost(
    amount: Decimal | None,
    currency_count: int,
    currency: str | None,
) -> tuple[Decimal | None, str | None]:
    """Report a cost only when exactly one currency is present.

    Adding USD to KRW produces a confident, meaningless number. When several
    currencies land in the same bucket the honest answer is "not summarizable".
    """

    if amount is None or currency is None or currency_count != 1:
        if currency_count > 1:
            logger.warning(
                "AI usage cost spans %d currencies in one bucket; reporting none",
                currency_count,
            )
        return None, None
    return amount, currency


def _validated_window(since: datetime, until: datetime) -> None:
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("summarize_ai_usage requires a timezone-aware 'since'")
    if until.tzinfo is None or until.utcoffset() is None:
        raise ValueError("summarize_ai_usage requires a timezone-aware 'until'")
    if since >= until:
        raise ValueError("summarize_ai_usage requires since < until")
    if until - since > MAX_AI_USAGE_WINDOW:
        raise ValueError(
            "summarize_ai_usage window exceeds "
            f"{MAX_AI_USAGE_WINDOW.days} days; narrow the range"
        )


def _breakdown_row(key: str, row: Row[Any]) -> AiUsageBreakdown:
    attempts = int(row.attempts)
    success_attempts = int(row.success_attempts)
    cost_amount, cost_currency = _resolved_cost(
        row.cost_amount,
        int(row.cost_currencies),
        row.cost_currency,
    )
    return AiUsageBreakdown(
        key=key,
        attempts=attempts,
        logical_calls=int(row.logical_calls),
        success_attempts=success_attempts,
        failure_attempts=int(row.failure_attempts),
        success_rate=(success_attempts / attempts) if attempts else 0.0,
        prompt_tokens=int(row.prompt_tokens),
        completion_tokens=int(row.completion_tokens),
        total_tokens=int(row.total_tokens),
        attempts_without_usage=int(row.attempts_without_usage),
        cost_amount=cost_amount,
        cost_currency=cost_currency,
    )


async def _breakdown(
    db: AsyncSession,
    dimension: ColumnElement[str],
    *,
    since: datetime,
    until: datetime,
) -> tuple[AiUsageBreakdown, ...]:
    statement = (
        select(dimension.label("key"), *_aggregates())
        .where(AiCallEvent.started_at >= since, AiCallEvent.started_at < until)
        .group_by(dimension)
        .order_by(func.count().desc(), dimension.asc())
    )
    result = await db.execute(statement)
    return tuple(_breakdown_row(str(row.key), row) for row in result.all())


async def summarize_ai_usage(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime,
) -> AiUsageSummary:
    """Aggregate ``review.ai_call_events`` over the half-open ``[since, until)``.

    Both bounds must be timezone-aware and the window is capped at
    :data:`MAX_AI_USAGE_WINDOW`; there is no "all time" mode.
    """

    _validated_window(since, until)

    totals_statement = select(
        *_aggregates(),
        func.percentile_disc(0.5)
        .within_group(AiCallEvent.latency_ms)
        .label("p50_latency_ms"),
        func.percentile_disc(0.95)
        .within_group(AiCallEvent.latency_ms)
        .label("p95_latency_ms"),
    ).where(AiCallEvent.started_at >= since, AiCallEvent.started_at < until)
    totals = (await db.execute(totals_statement)).one()

    cost_amount, cost_currency = _resolved_cost(
        totals.cost_amount,
        int(totals.cost_currencies),
        totals.cost_currency,
    )
    return AiUsageSummary(
        since=since,
        until=until,
        logical_calls=int(totals.logical_calls),
        attempts=int(totals.attempts),
        success_attempts=int(totals.success_attempts),
        failure_attempts=int(totals.failure_attempts),
        prompt_tokens=int(totals.prompt_tokens),
        completion_tokens=int(totals.completion_tokens),
        total_tokens=int(totals.total_tokens),
        attempts_without_usage=int(totals.attempts_without_usage),
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        p50_latency_ms=(
            None if totals.p50_latency_ms is None else int(totals.p50_latency_ms)
        ),
        p95_latency_ms=(
            None if totals.p95_latency_ms is None else int(totals.p95_latency_ms)
        ),
        by_provider=await _breakdown(
            db, AiCallEvent.provider, since=since, until=until
        ),
        by_model=await _breakdown(db, AiCallEvent.model_name, since=since, until=until),
        by_feature=await _breakdown(db, AiCallEvent.feature, since=since, until=until),
    )


__all__ = [
    "MAX_AI_USAGE_WINDOW",
    "AiAttemptTelemetry",
    "COST_SOURCE_PROVIDER_REPORTED",
    "AiCallAttempt",
    "AiCallStatus",
    "AiUsageBreakdown",
    "AiUsageSummary",
    "capture_ai_attempt",
    "new_logical_call_id",
    "record_ai_call_attempts",
    "report_ai_attempt_http_status",
    "report_ai_attempt_usage",
    "summarize_ai_usage",
]
