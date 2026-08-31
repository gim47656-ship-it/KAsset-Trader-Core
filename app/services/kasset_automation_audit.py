"""Bounded, secret-free persistence for KAsset recommendation-cycle telemetry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.core.db import AsyncSessionLocal
from app.models.kasset_automation_cycle_events import KAssetAutomationCycleEvent

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
    recommendation_ids = result.get("recommendationIds")
    recommendation_count = (
        len(recommendation_ids) if isinstance(recommendation_ids, list | tuple) else 0
    )

    return KAssetAutomationCycleEvent(
        owner_user_id=int(owner_user_id),
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
            }
        )
    return output


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


__all__ = ["build_automation_cycle_event", "record_automation_cycle_event"]
