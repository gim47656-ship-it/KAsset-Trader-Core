from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.kasset_automation_audit import build_automation_cycle_event

_NOW = datetime(2026, 8, 31, 5, 50, tzinfo=UTC)


def test_build_cycle_event_keeps_bounded_operator_evidence() -> None:
    row = build_automation_cycle_event(
        owner_user_id=4,
        observed_at=_NOW,
        finished_at=_NOW + timedelta(seconds=12),
        result={
            "candidateCount": 100,
            "rankedCount": 99,
            "strategyEvaluatedCount": 12,
            "strategyActionableCount": 1,
            "aiReviewedCount": 1,
            "aiFailureCount": 0,
            "candidateMarkets": {"KR": 94, "US": 6},
            "candidateSources": {"tvscreener_kr": 94, "watchlist": 9},
            "collectionPolicy": {
                "candidateLimit": 100,
                "minimumCandidateTarget": 50,
                "strategyReviewLimit": 12,
                "recommendationLimit": 5,
                "aiReviewActions": ["BUY", "SELL"],
            },
            "rankedCandidates": [
                {
                    "symbol": "003230",
                    "market": "KR",
                    "rankPosition": 1,
                    "totalScore": "0.809801",
                    "evidence": [{"must": "not be copied"}],
                }
            ],
            "candidateExclusions": [
                {
                    "symbol": "0126Z0",
                    "market": "KR",
                    "exclusionReason": "insufficient_history",
                }
            ],
            "aiReviewRejections": {"action_mismatch": 1},
            "aiReviewOutcomes": [
                {
                    "symbol": "003230",
                    "market": "KR",
                    "strategyAction": "BUY",
                    "aiAction": "HOLD",
                    "confidence": "0.72",
                    "reason": "action_mismatch",
                    "observedAt": "2026-08-31T05:50:00Z",
                    "provider": "mcp",
                    "tier": "terra",
                    "modelId": "gpt-5.6-terra",
                    "rationaleTags": ["breakout_not_confirmed"],
                    "rawResponse": "must not be copied",
                }
            ],
            "recommendationIds": [],
            "skipped": "no_ai_confirmed_signal",
        },
    )

    assert row.status == "completed"
    assert row.candidate_count == 100
    assert row.candidate_exclusion_count == 1
    assert row.ai_reviewed_count == 1
    assert row.candidate_markets == {"KR": 94, "US": 6}
    assert row.ranked_candidates == [
        {
            "symbol": "003230",
            "market": "KR",
            "rankPosition": 1,
            "totalScore": "0.809801",
        }
    ]
    assert row.candidate_exclusions == [
        {"symbol": "0126Z0", "market": "KR", "reason": "insufficient_history"}
    ]
    assert row.ai_review_outcomes == [
        {
            "symbol": "003230",
            "market": "KR",
            "strategyAction": "BUY",
            "aiAction": "HOLD",
            "confidence": "0.72",
            "reason": "action_mismatch",
            "observedAt": "2026-08-31T05:50:00Z",
            "provider": "mcp",
            "tier": "terra",
            "modelId": "gpt-5.6-terra",
            "rationaleTags": ["breakout_not_confirmed"],
        }
    ]
    assert "rawResponse" not in row.ai_review_outcomes[0]


def test_owner_failure_is_recorded_without_negative_counts() -> None:
    row = build_automation_cycle_event(
        owner_user_id=4,
        observed_at=_NOW,
        finished_at=_NOW - timedelta(seconds=1),
        result={
            "candidateCount": -3,
            "recommendationIds": [],
            "skipped": "owner_cycle_failed",
            "errorClass": "RuntimeError",
        },
    )

    assert row.status == "failed"
    assert row.candidate_count == 0
    assert row.finished_at == row.observed_at
