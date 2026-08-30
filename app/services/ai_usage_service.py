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

import asyncio
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

#: How long the ledger append may hold up the AI call it measures.
#:
#: :func:`record_ai_call_attempts` is awaited from the ``finally`` block that
#: closes every routed AI call, so *every* call — successes included — pays
#: whatever this write costs. The shared pool is ``pool_size=5 /
#: max_overflow=10 / pool_timeout=10`` (:func:`app.core.db.build_engine`), so
#: without a deadline a saturated pool adds ~10s of pure checkout wait to each
#: AI call before the error is swallowed. Instrumentation that slows the
#: feature it measures is a worse failure than a missing row.
#:
#: Two seconds is the balance point: an order of magnitude above a healthy
#: INSERT + COMMIT (the whole batch is one statement — single-digit ms local,
#: tens of ms across an availability zone) yet negligible against the
#: transport's own budget (``OpenAiModelRouter.timeout_seconds`` defaults to
#: 60s).
AI_LEDGER_FLUSH_TIMEOUT_S = 2.0


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
class AiCallAttribution:
    """Who an AI call was made for, and which product request it belongs to.

    Both fields stay ``None`` when the caller genuinely does not know one.
    Attributing usage to a guessed owner is worse than leaving the row
    unattributed, because a wrong owner reads as a fact.
    """

    owner_user_id: int | None = None
    correlation_id: str | None = None


_ACTIVE_ATTRIBUTION: ContextVar[AiCallAttribution | None] = ContextVar(
    "ai_call_attribution",
    default=None,
)


@contextmanager
def attribute_ai_calls(
    *,
    owner_user_id: int | None = None,
    correlation_id: str | None = None,
) -> Iterator[AiCallAttribution]:
    """Attribute every ledger row written under this scope.

    Routing sits between the caller that knows *who* asked and the transport
    that performs the call, and neither identity belongs in the
    ``StructuredJsonClient`` signature that every transport implements. A
    context-scoped slot carries the identity down, mirroring the way
    :func:`capture_ai_attempt` carries telemetry back up.

    Scopes nest and merge: ``None`` means "inherit", never "clear". The owner
    is known one level above the correlation id (one owner request drives
    several analyses), so an inner scope must not erase the outer one. A blank
    correlation id carries no information and is treated as absent rather than
    written as ``''``.
    """

    current = _ACTIVE_ATTRIBUTION.get() or AiCallAttribution()
    normalized_correlation = correlation_id.strip() if correlation_id else ""
    merged = AiCallAttribution(
        owner_user_id=(
            current.owner_user_id if owner_user_id is None else owner_user_id
        ),
        correlation_id=normalized_correlation or current.correlation_id,
    )
    token = _ACTIVE_ATTRIBUTION.set(merged)
    try:
        yield merged
    finally:
        _ACTIVE_ATTRIBUTION.reset(token)


def current_ai_call_attribution() -> AiCallAttribution:
    """The attribution in force, or an all-``None`` one outside every scope."""

    return _ACTIVE_ATTRIBUTION.get() or AiCallAttribution()


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

    Bounded by :data:`AI_LEDGER_FLUSH_TIMEOUT_S`, because this runs on the AI
    call's own critical path: a stalled pool must cost the call a known small
    delay instead of a full pool-checkout timeout. An expired flush is
    swallowed like any other ledger failure but logged distinctly — a ledger
    that goes quiet under load and one that rejects rows need different fixes.

    ``asyncio.CancelledError`` is deliberately not swallowed — a cancelled AI
    call must stay cancelled. :func:`asyncio.timeout` re-raises it untouched
    when the cancellation came from the caller rather than from our deadline.
    """

    if not attempts:
        return False
    try:
        async with (
            asyncio.timeout(AI_LEDGER_FLUSH_TIMEOUT_S),
            AsyncSessionLocal() as session,
        ):
            await session.execute(insert(AiCallEvent), [_row(a) for a in attempts])
            await session.commit()
    except TimeoutError:
        logger.warning(
            "AI call ledger append exceeded %.1fs; %d attempt row(s) dropped "
            "(logical_call_id=%s). The AI call itself is unaffected.",
            AI_LEDGER_FLUSH_TIMEOUT_S,
            len(attempts),
            attempts[0].logical_call_id,
        )
        return False
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

    ``attempts`` and ``logical_calls`` are both exposed on purpose, and
    neither one is "the number of requests the product made":

    * ``attempts`` counts provider round trips, so availability fallbacks
      show up rather than hiding inside one number.
    * ``logical_calls`` counts routed client calls, which is one per model
      tier. A request that starts on terra and escalates to sol is *two*
      logical calls: the tier router builds a fresh routed client per tier
      and each mints its own ``logical_call_id``.

    Collapsing a tier-escalation chain back into a single product request is
    a row-level question, answered by ``correlation_id`` on
    ``review.ai_call_events``. It is deliberately not aggregated here: that
    column is nullable, and its granularity is a caller convention rather
    than an enforced invariant, so a count over it would be a confident
    undercount.

    Token sums are per column and skip ``NULL``, so a column reads ``0`` both
    for genuine zeros and for a window in which nobody reported that column.
    ``attempts_without_usage`` counts the rows that reported *no* token column
    at all, and that is what separates "no usage reported" from "zero tokens
    used".
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
    "AI_LEDGER_FLUSH_TIMEOUT_S",
    "MAX_AI_USAGE_WINDOW",
    "AiAttemptTelemetry",
    "COST_SOURCE_PROVIDER_REPORTED",
    "AiCallAttempt",
    "AiCallAttribution",
    "AiCallStatus",
    "AiUsageBreakdown",
    "AiUsageSummary",
    "attribute_ai_calls",
    "capture_ai_attempt",
    "current_ai_call_attribution",
    "new_logical_call_id",
    "record_ai_call_attempts",
    "report_ai_attempt_http_status",
    "report_ai_attempt_usage",
    "summarize_ai_usage",
]
