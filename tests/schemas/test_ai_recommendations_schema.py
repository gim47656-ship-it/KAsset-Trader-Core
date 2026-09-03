from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

from app.schemas.ai_recommendations import build_recommendation_response

_STRATEGY_VOTES = [
    {
        "vote": "BUY",
        "score": "0.178125",
        "family": "breakout",
        "weight": "0.187500",
        "strategy": "MOMENTUM",
    },
    {
        "vote": "BUY",
        "score": "0.206329",
        "family": "breakout",
        "weight": "0.312500",
        "strategy": "BREAKOUT",
    },
    {
        "vote": "HOLD",
        "score": "0.000000",
        "family": "breakout",
        "weight": "0.500000",
        "strategy": "VOLATILITY_TREND",
    },
]


def _recommendation_row(strategy_votes: list[dict[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        id="rec-6835c14e",
        owner_user_id=1,
        action="BUY",
        decision="APPROVED",
        market="KRX",
        symbol="138040",
        name=None,
        currency="KRW",
        headline="메리츠금융지주 매수 검토 의견",
        rationale=["전략 투표와 위험 검사를 통과했습니다."],
        risks=[],
        evidence=[
            {
                "title": "AI trading vertical-slice review evidence",
                "source": "kasset_vertical_slice",
                "kind": "ai_vertical_slice",
                "regime": "VOLATILE",
                "strategyVotes": strategy_votes,
                "ranking": {
                    "score": "0.615191",
                    "position": 1,
                    "total": 96,
                    "note": "후보 96개 중 1위입니다.",
                },
                "hardRisk": {
                    "passed": True,
                    "checks": [
                        {"rule": rule, "detail": "통과", "passed": True}
                        for rule in (
                            "DAILY_MAX_LOSS",
                            "BUDGET",
                            "POSITION",
                            "ORDER_COUNT",
                            "AI_SHADOW",
                            "DAILY_GOAL",
                        )
                    ],
                    "blockedReason": None,
                },
            }
        ],
        confidence="0.615191",
        reference_price="138000",
        suggested_quantity="1",
        source="kasset-automation",
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
        valid_until=None,
        decided_at=datetime(2026, 9, 3, tzinfo=UTC),
        paper_execution_status="SUCCEEDED",
        paper_execution_error=None,
    )


def test_build_recommendation_response_preserves_strategy_vote_family() -> None:
    response = build_recommendation_response(_recommendation_row(_STRATEGY_VOTES))

    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["strategyVotes"] == _STRATEGY_VOTES
    assert payload["ranking"] == {
        "score": "0.615191",
        "position": 1,
        "total": 96,
        "note": "후보 96개 중 1위입니다.",
    }
    assert [check["rule"] for check in payload["hardRisk"]["checks"]] == [
        "DAILY_MAX_LOSS",
        "BUDGET",
        "POSITION",
        "ORDER_COUNT",
        "AI_SHADOW",
        "DAILY_GOAL",
    ]


def test_build_recommendation_response_accepts_new_strategy_family() -> None:
    votes = deepcopy(_STRATEGY_VOTES)
    votes[0]["family"] = "event_driven"

    response = build_recommendation_response(_recommendation_row(votes))

    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["strategyVotes"] == votes


def test_build_recommendation_response_accepts_legacy_votes_without_family() -> None:
    legacy_votes = deepcopy(_STRATEGY_VOTES)
    for vote in legacy_votes:
        vote.pop("family")

    response = build_recommendation_response(_recommendation_row(legacy_votes))

    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["strategyVotes"] == legacy_votes
