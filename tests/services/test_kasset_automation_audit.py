from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.kasset_automation_audit import (
    build_automation_cycle_event,
    build_paper_execution_event,
    new_cycle_trace_id,
)

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
                },
                {
                    "title": "Intraday trigger exclusion",
                    "source": "kasset_intraday_triggers",
                    "kind": "candidate_exclusion",
                    "symbol": "005930",
                    "market": "KR",
                    "exclusionReason": (
                        "intraday_trigger_not_satisfied:"
                        "opening_range_breakout=inactive,session_vwap_reclaim=inactive"
                    ),
                    "dailySetup": {"must": "not be copied"},
                    "intradayTriggers": {
                        "title": "Completed intraday entry triggers",
                        "source": "kasset_intraday_triggers",
                        "kind": "intraday_triggers",
                        "schemaVersion": "kasset.intraday-triggers.v1",
                        "symbol": "005930",
                        "market": "KRX",
                        "direction": "BUY",
                        "status": "not_triggered",
                        "evaluatedAt": "2026-08-31T05:50:00Z",
                        "dataAsOf": "2026-08-31T05:45:00Z",
                        "blockedReason": None,
                        "policy": {"must": "not be copied"},
                        "triggers": [
                            {
                                "code": "opening_range_breakout",
                                "status": "inactive",
                                "value": "101.000000",
                                "threshold": "105.000000",
                                "source": "toss",
                                "asOf": "2026-08-31T05:45:00Z",
                                "detail": "must not be copied",
                                "unavailableReason": None,
                            },
                            {
                                "code": "intraday_relative_strength",
                                "status": "unavailable",
                                "value": None,
                                "threshold": "0.000000",
                                "source": None,
                                "asOf": None,
                                "detail": "must not be copied",
                                "unavailableReason": "index_intraday_unavailable",
                            },
                        ],
                    },
                },
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
        },
    )

    assert row.status == "completed"
    assert row.candidate_count == 100
    assert row.candidate_exclusion_count == 2
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
        {"symbol": "0126Z0", "market": "KR", "reason": "insufficient_history"},
        {
            "symbol": "005930",
            "market": "KR",
            "reason": (
                "intraday_trigger_not_satisfied:"
                "opening_range_breakout=inactive,session_vwap_reclaim=inactive"
            ),
            "source": "kasset_intraday_triggers",
            "intradayTriggers": {
                "schemaVersion": "kasset.intraday-triggers.v1",
                "status": "not_triggered",
                "direction": "BUY",
                "dataAsOf": "2026-08-31T05:45:00Z",
                "blockedReason": None,
                "triggers": [
                    {
                        "code": "opening_range_breakout",
                        "status": "inactive",
                        "value": "101.000000",
                        "threshold": "105.000000",
                        "source": "toss",
                        "asOf": "2026-08-31T05:45:00Z",
                        "unavailableReason": None,
                    },
                    {
                        "code": "intraday_relative_strength",
                        "status": "unavailable",
                        "value": None,
                        "threshold": "0.000000",
                        "source": None,
                        "asOf": None,
                        "unavailableReason": "index_intraday_unavailable",
                    },
                ],
            },
        },
    ]
    # 세부 진단은 남기되 사람이 읽는 서술과 정책 원본은 행에 복사하지 않는다.
    excluded = row.candidate_exclusions[1]
    assert "dailySetup" not in excluded
    assert "policy" not in excluded["intradayTriggers"]
    assert all(
        "detail" not in trigger for trigger in excluded["intradayTriggers"]["triggers"]
    )
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
            "recommendationId": None,
        }
    ]
    assert "rawResponse" not in row.ai_review_outcomes[0]


@pytest.mark.parametrize(
    "reason",
    [
        "no_regular_market_open",
        "no_configured_regular_market_open",
        # 기술 판정이 정상적으로 진입 후보를 하나도 남기지 않은 상태들.
        # 장애가 아니므로 completed로 집계되면 성공률 지표가 오염된다.
        "daily_setup_not_qualified",
        "no_breakout_family_direction",
        "intraday_trigger_not_satisfied",
        "no_affordable_actionable_candidate",
    ],
)
def test_a_normal_no_entry_cycle_is_recorded_as_skipped(reason: str) -> None:
    row = build_automation_cycle_event(
        owner_user_id=4,
        observed_at=_NOW,
        finished_at=_NOW,
        result={
            "cycleTraceId": "cyc-market-closed",
            "candidateCount": 0,
            "rankedCount": 0,
            "strategyEvaluatedCount": 0,
            "strategyActionableCount": 0,
            "aiReviewedCount": 0,
            "aiFailureCount": 0,
            "recommendationIds": [],
            "skipped": reason,
        },
    )

    assert row.status == "skipped"
    assert row.skipped_reason == reason
    assert row.cycle_trace_id == "cyc-market-closed"
    assert row.candidate_count == 0
    assert row.ai_reviewed_count == 0


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


def test_cycle_event_joins_its_trace_to_the_accepted_recommendation() -> None:
    trace = new_cycle_trace_id()
    row = build_automation_cycle_event(
        owner_user_id=4,
        observed_at=_NOW,
        finished_at=_NOW + timedelta(seconds=3),
        result={
            "cycleTraceId": trace,
            "recommendationIds": ["rec-1", "rec-2"],
            "aiReviewOutcomes": [
                {
                    "symbol": "003230",
                    "market": "KR",
                    "strategyAction": "BUY",
                    "reason": "accepted",
                    "observedAt": "2026-08-31T05:50:00Z",
                    "recommendationId": "rec-1",
                },
                {
                    "symbol": "005930",
                    "market": "KR",
                    "strategyAction": "BUY",
                    "reason": "action_mismatch",
                    "observedAt": "2026-08-31T05:50:00Z",
                },
            ],
        },
    )

    assert row.cycle_trace_id == trace
    assert row.recommendation_ids == ["rec-1", "rec-2"]
    assert row.recommendation_count == 2
    accepted = [
        outcome for outcome in row.ai_review_outcomes if outcome["reason"] == "accepted"
    ]
    assert [outcome["recommendationId"] for outcome in accepted] == ["rec-1"]
    # 채택되지 않은 후보는 추천 id를 갖지 않는다.
    assert row.ai_review_outcomes[1]["recommendationId"] is None


def test_cycle_event_bounds_the_recommendation_id_list_but_not_the_count() -> None:
    produced = [f"rec-{index}" for index in range(120)]
    row = build_automation_cycle_event(
        owner_user_id=4,
        observed_at=_NOW,
        finished_at=_NOW,
        result={"cycleTraceId": " ", "recommendationIds": produced},
    )

    assert len(row.recommendation_ids) == 50
    assert row.recommendation_ids == produced[:50]
    assert row.recommendation_count == 120
    # 공백만 있는 추적 id는 저장하지 않는다. DB CHECK가 거부하는 값이다.
    assert row.cycle_trace_id is None


def test_execution_event_keeps_the_recommendation_order_and_attempt() -> None:
    row = build_paper_execution_event(
        owner_user_id=7,
        origin="AUTO_PAPER",
        status="SUBMITTED",
        reason="idempotent_replay",
        recommendation_id="  rec-9  ",
        observed_at=_NOW,
        attempt_count=2,
        replayed=True,
        cycle_trace_id="cyc-abc",
        paper_order_id="ord-1",
        promotion_bypass_reason="모의투자 계좌 완전 자동매매 게이트 개방",
    )

    assert row.owner_user_id == 7
    assert row.recommendation_id == "rec-9"
    assert row.origin == "AUTO_PAPER"
    assert row.status == "SUBMITTED"
    assert row.reason == "idempotent_replay"
    assert row.attempt_count == 2
    assert row.replayed is True
    assert row.cycle_trace_id == "cyc-abc"
    assert row.paper_order_id == "ord-1"
    assert row.observed_at == _NOW
    assert row.started_at == _NOW
    assert row.finished_at == _NOW


@pytest.mark.parametrize(
    ("origin", "status"),
    [("LIVE", "SUBMITTED"), ("auto_paper", "SUBMITTED"), ("AUTO_PAPER", "DONE")],
)
def test_execution_event_rejects_an_unknown_origin_or_status(
    origin: str,
    status: str,
) -> None:
    with pytest.raises(ValueError):
        build_paper_execution_event(
            owner_user_id=7,
            origin=origin,
            status=status,
            reason="submitted",
            recommendation_id="rec-9",
            observed_at=_NOW,
        )


def test_execution_event_bounds_its_reason_and_requires_a_recommendation() -> None:
    row = build_paper_execution_event(
        owner_user_id=7,
        origin="APPROVAL",
        status="FAILED",
        reason="submit_ambiguous:" + "X" * 500,
        recommendation_id="rec-9",
        observed_at=_NOW,
        attempt_count=-4,
    )

    assert len(row.reason) == 256
    assert row.reason.startswith("submit_ambiguous:")
    assert row.attempt_count == 0
    assert row.paper_order_id is None

    with pytest.raises(ValueError):
        build_paper_execution_event(
            owner_user_id=7,
            origin="APPROVAL",
            status="FAILED",
            reason="submit_rejected",
            recommendation_id="   ",
            observed_at=_NOW,
        )
