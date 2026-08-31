"""Bounded, secret-free persistence for KAsset automation evidence.

Two append-only ledgers live here: the recommendation-cycle row and the PAPER
execution-attempt row. Both are written in their own transaction so an
observability failure can never change a trading decision, and both are joined
by ``cycle_trace_id`` rather than by a foreign key for the same reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.ai_recommendations import AIRecommendation
from app.models.kasset_automation_cycle_events import KAssetAutomationCycleEvent
from app.models.kasset_paper_execution_events import (
    KASSET_PAPER_EXECUTION_ORIGINS,
    KASSET_PAPER_EXECUTION_STATUSES,
    KAssetPaperExecutionEvent,
)

_EARLY_SKIP_REASONS = frozenset(
    {
        "ai_unavailable",
        "position_exit_recommendation_created",
        "recommendation_cooldown_active",
        "screener_candidates_unavailable",
    }
)
_MAX_MAP_ITEMS = 16
_MAX_REVIEW_OUTCOMES = 64
_MAX_RANKED_CANDIDATES = 50
_MAX_EXCLUSIONS = 50
_MAX_RECOMMENDATION_IDS = 50
_MAX_TRACE_TEXT = 64
_MAX_REASON_TEXT = 256


def new_cycle_trace_id() -> str:
    """Mint one recommendation-cycle trace id for a single owner cycle."""

    return f"cyc-{uuid4().hex}"


def build_automation_cycle_event(
    *,
    owner_user_id: int,
    observed_at: datetime,
    finished_at: datetime,
    result: Mapping[str, object],
) -> KAssetAutomationCycleEvent:
    """Project one internal cycle result into the closed append-only row shape."""

    observed = _aware_utc(observed_at)
    finished = _aware_utc(finished_at)
    if finished < observed:
        finished = observed

    skipped_reason = _optional_text(result.get("skipped"), maximum=128)
    status = _status_for_result(skipped_reason, result.get("errorClass"))
    raw_recommendation_ids = result.get("recommendationIds")
    recommendation_count = (
        len(raw_recommendation_ids)
        if isinstance(raw_recommendation_ids, list | tuple)
        else 0
    )

    return KAssetAutomationCycleEvent(
        owner_user_id=int(owner_user_id),
        cycle_trace_id=_optional_text(
            result.get("cycleTraceId"), maximum=_MAX_TRACE_TEXT
        ),
        observed_at=observed,
        finished_at=finished,
        status=status,
        skipped_reason=skipped_reason,
        candidate_count=_nonnegative_int(result.get("candidateCount")),
        ranked_count=_nonnegative_int(result.get("rankedCount")),
        candidate_exclusion_count=_sequence_count(result.get("candidateExclusions")),
        strategy_evaluated_count=_nonnegative_int(result.get("strategyEvaluatedCount")),
        strategy_actionable_count=_nonnegative_int(
            result.get("strategyActionableCount")
        ),
        ai_reviewed_count=_nonnegative_int(result.get("aiReviewedCount")),
        ai_failure_count=_nonnegative_int(result.get("aiFailureCount")),
        recommendation_count=recommendation_count,
        candidate_markets=_count_map(result.get("candidateMarkets")),
        candidate_sources=_count_map(result.get("candidateSources")),
        collection_policy=_collection_policy(result.get("collectionPolicy")),
        ranked_candidates=_ranked_candidates(result.get("rankedCandidates")),
        candidate_exclusions=_candidate_exclusions(result.get("candidateExclusions")),
        ai_review_rejections=_count_map(result.get("aiReviewRejections")),
        ai_review_outcomes=_review_outcomes(result.get("aiReviewOutcomes")),
        recommendation_ids=_recommendation_ids(raw_recommendation_ids),
    )


async def record_automation_cycle_event(
    *,
    owner_user_id: int,
    observed_at: datetime,
    finished_at: datetime,
    result: Mapping[str, object],
) -> None:
    """Commit telemetry in its own transaction, isolated from recommendation rows."""

    row = build_automation_cycle_event(
        owner_user_id=owner_user_id,
        observed_at=observed_at,
        finished_at=finished_at,
        result=result,
    )
    async with AsyncSessionLocal() as db:
        db.add(row)
        await db.commit()


def build_paper_execution_event(
    *,
    owner_user_id: int,
    origin: str,
    status: str,
    reason: str,
    recommendation_id: str,
    observed_at: datetime,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    attempt_count: int = 0,
    replayed: bool = False,
    cycle_trace_id: str | None = None,
    paper_order_id: str | None = None,
    promotion_bypass_reason: str | None = None,
) -> KAssetPaperExecutionEvent:
    """Project one PAPER execution attempt into the closed append-only row shape."""

    if origin not in KASSET_PAPER_EXECUTION_ORIGINS:
        raise ValueError(f"unknown paper execution origin: {origin}")
    if status not in KASSET_PAPER_EXECUTION_STATUSES:
        raise ValueError(f"unknown paper execution status: {status}")
    recommendation = _optional_text(recommendation_id, maximum=_MAX_TRACE_TEXT)
    if recommendation is None:
        raise ValueError("recommendation_id is required")
    observed = _aware_utc(observed_at)
    started = _aware_utc(started_at) if started_at is not None else observed
    finished = _aware_utc(finished_at) if finished_at is not None else observed
    if finished < started:
        finished = started
    return KAssetPaperExecutionEvent(
        owner_user_id=int(owner_user_id),
        recommendation_id=recommendation,
        cycle_trace_id=_optional_text(cycle_trace_id, maximum=_MAX_TRACE_TEXT),
        origin=origin,
        status=status,
        reason=_optional_text(reason, maximum=_MAX_REASON_TEXT) or "unspecified",
        attempt_count=_nonnegative_int(attempt_count),
        replayed=bool(replayed),
        paper_order_id=_optional_text(paper_order_id, maximum=_MAX_TRACE_TEXT),
        promotion_bypass_reason=_optional_text(promotion_bypass_reason, maximum=128),
        started_at=started,
        finished_at=finished,
        observed_at=observed,
    )


async def record_paper_execution_event(
    *,
    owner_user_id: int,
    origin: str,
    status: str,
    reason: str,
    recommendation_id: str,
    observed_at: datetime,
    replayed: bool = False,
    promotion_bypass_reason: str | None = None,
) -> None:
    """Commit one execution attempt in its own transaction.

    The attempt count, resulting PAPER order, and cycle trace are read back from
    the owner's recommendation row after the execution transaction settled, so
    the ledger records what actually persisted instead of what was intended. The
    read is owner-scoped: another owner's recommendation id resolves to nothing
    rather than to that owner's execution state.
    """

    async with AsyncSessionLocal() as db:
        recommendation = (
            await db.scalars(
                select(AIRecommendation).where(
                    AIRecommendation.owner_user_id == int(owner_user_id),
                    AIRecommendation.id == recommendation_id,
                )
            )
        ).one_or_none()
        row = build_paper_execution_event(
            owner_user_id=owner_user_id,
            origin=origin,
            status=status,
            reason=reason,
            recommendation_id=recommendation_id,
            observed_at=observed_at,
            started_at=(
                observed_at
                if recommendation is None
                else recommendation.paper_execution_claimed_at or observed_at
            ),
            finished_at=(
                observed_at
                if recommendation is None
                else recommendation.paper_execution_completed_at or observed_at
            ),
            attempt_count=(
                0
                if recommendation is None
                else recommendation.paper_execution_attempt_count
            ),
            replayed=replayed,
            cycle_trace_id=(
                None if recommendation is None else recommendation.cycle_trace_id
            ),
            paper_order_id=(
                None if recommendation is None else recommendation.paper_order_id
            ),
            promotion_bypass_reason=promotion_bypass_reason,
        )
        db.add(row)
        await db.commit()


def _status_for_result(skipped_reason: str | None, error_class: object) -> str:
    if _optional_text(error_class, maximum=128) is not None:
        return "failed"
    if skipped_reason in _EARLY_SKIP_REASONS:
        return "skipped"
    return "completed"


def _count_map(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, int] = {}
    for raw_key, raw_count in list(value.items())[:_MAX_MAP_ITEMS]:
        key = _optional_text(raw_key, maximum=64)
        if key is None:
            continue
        output[key] = _nonnegative_int(raw_count)
    return output


def _collection_policy(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    actions = value.get("aiReviewActions")
    normalized_actions = (
        [
            text
            for item in list(actions)[:8]
            if (text := _optional_text(item, maximum=16)) is not None
        ]
        if isinstance(actions, Sequence) and not isinstance(actions, str | bytes)
        else []
    )
    return {
        "candidateLimit": _nonnegative_int(value.get("candidateLimit")),
        "minimumCandidateTarget": _nonnegative_int(value.get("minimumCandidateTarget")),
        "strategyReviewLimit": _nonnegative_int(value.get("strategyReviewLimit")),
        "recommendationLimit": _nonnegative_int(value.get("recommendationLimit")),
        "aiReviewActions": normalized_actions,
    }


def _ranked_candidates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    output: list[dict[str, object]] = []
    for item in list(value)[:_MAX_RANKED_CANDIDATES]:
        if not isinstance(item, Mapping):
            continue
        symbol = _optional_text(item.get("symbol"), maximum=32)
        market = _optional_text(item.get("market"), maximum=16)
        if symbol is None or market is None:
            continue
        output.append(
            {
                "symbol": symbol,
                "market": market,
                "rankPosition": _nonnegative_int(item.get("rankPosition")),
                "totalScore": _decimal_text(item.get("totalScore")),
            }
        )
    return output


def _candidate_exclusions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    output: list[dict[str, str]] = []
    for item in list(value)[:_MAX_EXCLUSIONS]:
        if not isinstance(item, Mapping):
            continue
        symbol = _optional_text(item.get("symbol"), maximum=32)
        market = _optional_text(item.get("market"), maximum=16)
        reason = _optional_text(item.get("exclusionReason"), maximum=128)
        if symbol is None or market is None or reason is None:
            continue
        output.append({"symbol": symbol, "market": market, "reason": reason})
    return output


def _review_outcomes(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    output: list[dict[str, object]] = []
    for item in list(value)[:_MAX_REVIEW_OUTCOMES]:
        if not isinstance(item, Mapping):
            continue
        symbol = _optional_text(item.get("symbol"), maximum=32)
        market = _optional_text(item.get("market"), maximum=16)
        strategy_action = _optional_text(item.get("strategyAction"), maximum=16)
        reason = _optional_text(item.get("reason"), maximum=128)
        observed_at = _optional_text(item.get("observedAt"), maximum=40)
        if None in {symbol, market, strategy_action, reason, observed_at}:
            continue
        raw_tags = item.get("rationaleTags")
        tags = (
            [
                text
                for tag in list(raw_tags)[:12]
                if (text := _optional_text(tag, maximum=64)) is not None
            ]
            if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, str | bytes)
            else []
        )
        output.append(
            {
                "symbol": symbol,
                "market": market,
                "strategyAction": strategy_action,
                "aiAction": _optional_text(item.get("aiAction"), maximum=16),
                "confidence": _confidence_text(item.get("confidence")),
                "reason": reason,
                "observedAt": observed_at,
                "provider": _optional_text(item.get("provider"), maximum=128),
                "tier": _optional_text(item.get("tier"), maximum=16),
                "modelId": _optional_text(item.get("modelId"), maximum=256),
                "rationaleTags": tags,
                "recommendationId": _optional_text(
                    item.get("recommendationId"), maximum=_MAX_TRACE_TEXT
                ),
            }
        )
    return output


def _recommendation_ids(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [
        text
        for item in list(value)[:_MAX_RECOMMENDATION_IDS]
        if (text := _optional_text(item, maximum=_MAX_TRACE_TEXT)) is not None
    ]


def _sequence_count(value: object) -> int:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return 0
    return len(value)


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _decimal_text(value: object) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(parsed) if parsed.is_finite() else None


def _confidence_text(value: object) -> str | None:
    text = _decimal_text(value)
    if text is None:
        return None
    parsed = Decimal(text)
    return text if Decimal("0") <= parsed <= Decimal("1") else None


def _optional_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:maximum]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "build_automation_cycle_event",
    "build_paper_execution_event",
    "new_cycle_trace_id",
    "record_automation_cycle_event",
    "record_paper_execution_event",
]
