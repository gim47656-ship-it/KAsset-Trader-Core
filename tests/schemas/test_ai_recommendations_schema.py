from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.extensions.kasset.automation.position_sizing import (
    PositionSizeCap,
    PositionSizeCapCode,
)
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


_ACCOUNT_STATE_EVIDENCE: dict[str, object] = {
    "schemaVersion": "kasset.account-state.v1",
    "state": "STAGED_REDUCTION",
    "profitRatio": "0.012500",
    "peakDrawdownRatio": "-0.003100",
    "multiplier": "0.75",
    "thresholds": {
        "stagedProfitRatio": "0.005",
        "exitOnlyProfitRatio": "0.01",
        "stagedPeakDrawdownRatio": "0.005",
        "stagedReductionMultiplier": "0.75",
    },
    "unavailable": None,
    "persistFailed": None,
}

_LOSS_STREAK_LOCKED_EVIDENCE: dict[str, object] = {
    "schemaVersion": "kasset.loss-streak-gate.v1",
    "globalLock": None,
    "symbolLock": {
        "scope": "SYMBOL",
        "symbol": "138040",
        "streakCount": 2,
        "lossLimit": 2,
        "newestLossAt": "2026-09-02T03:15:00+00:00",
        "expiresAt": "2026-09-03T03:15:00+00:00",
        "reason": "LOSS_STREAK_LIMIT_REACHED",
    },
    "streakGlobal": 0,
    "streakSymbol": 2,
    "unavailable": None,
    "persistFailed": None,
}

_LOSS_STREAK_SELL_BYPASS_EVIDENCE: dict[str, object] = {
    "schemaVersion": "kasset.loss-streak-gate.v1",
    "globalLock": None,
    "symbolLock": None,
    "streakGlobal": 0,
    "streakSymbol": 0,
    "unavailable": None,
    "persistFailed": None,
}

_BUY_POSITION_SIZING_EVIDENCE: dict[str, object] = {
    "action": "BUY",
    "market": "KRX",
    "quantity": "1",
    "unroundedQuantity": "1.500000",
    "lotSize": "1",
    "entryPrice": "138000",
    "strategyStop": "133860",
    "strategyAtr": "2070",
    "riskBudget": "12420",
    "riskPerUnit": "4140",
    "riskPerTradeRate": "0.01",
    "regime": "TRENDING_UP",
    "regimeMultiplier": "1",
    "accountStateMultiplier": "0.75",
    "caps": [{"code": "RISK_BUDGET", "quantity": "1"}],
    "limitingCaps": ["RISK_BUDGET"],
    "zeroReasons": [],
}


def _hard_risk_evidence(
    *,
    account_state: dict[str, object],
    loss_streak: dict[str, object],
) -> dict[str, object]:
    """Shape Hard Risk the way evaluate_hard_risk persists it."""

    return {
        "passed": True,
        "checks": [
            {"rule": rule, "passed": True, "detail": "통과"}
            for rule in ("DAILY_MAX_LOSS", "ACCOUNT_STATE", "LOSS_STREAK", "BUDGET")
        ],
        "blockedReason": None,
        "accountState": account_state,
        "lossStreak": loss_streak,
    }


def test_build_recommendation_response_keeps_buy_sizing_evidence() -> None:
    """A BUY row carries the account-state multiplier and both hard-risk snapshots."""

    row = _recommendation_row(deepcopy(_STRATEGY_VOTES))
    row.evidence[0]["portfolio"] = {
        "targetWeight": "0.1",
        "targetQuantity": "1",
        "cashAfter": "9862000",
        "note": "Deterministic ATR risk sizing; limitingCaps=RISK_BUDGET.",
        "positionSizing": deepcopy(_BUY_POSITION_SIZING_EVIDENCE),
    }
    row.evidence[0]["hardRisk"] = _hard_risk_evidence(
        account_state=deepcopy(_ACCOUNT_STATE_EVIDENCE),
        loss_streak=deepcopy(_LOSS_STREAK_LOCKED_EVIDENCE),
    )

    response = build_recommendation_response(row)

    payload = response.model_dump(mode="json", by_alias=True)
    stored = payload["evidence"][0]

    assert payload["strategyVotes"] == _STRATEGY_VOTES
    assert payload["ranking"] == {
        "score": "0.615191",
        "position": 1,
        "total": 96,
        "note": "후보 96개 중 1위입니다.",
    }
    assert payload["rationale"] == row.rationale
    assert payload["portfolio"]["positionSizing"] == _BUY_POSITION_SIZING_EVIDENCE
    assert payload["hardRisk"]["accountState"] == _ACCOUNT_STATE_EVIDENCE
    assert payload["hardRisk"]["lossStreak"] == _LOSS_STREAK_LOCKED_EVIDENCE
    assert stored["portfolio"]["positionSizing"] == _BUY_POSITION_SIZING_EVIDENCE


def test_build_recommendation_response_keeps_sell_exit_evidence() -> None:
    """A position-manager SELL keeps its exit and standalone Hard Risk evidence."""

    row = _recommendation_row(deepcopy(_STRATEGY_VOTES))
    row.action = "SELL"
    row.rationale = ["STOP 손절선 도달: currentStop=133860"]
    row.paper_execution_status = None
    row.evidence = [
        {
            "title": "Deterministic PAPER position exit",
            "source": "position_manager",
            "kind": "position_exit",
            "exitKind": "STOP",
            "idempotencyKey": "138040-exit-1",
            "paperPositionId": 12,
            "positionCycleId": "cycle-138040-1",
            "quantityFraction": "1",
            "initialAtr": "2070",
            "initialStop": "133860",
            "currentStop": "133860",
            "evaluationHorizon": "intraday",
            "barAsOf": "2026-09-02T03:15:00+00:00",
            "barPeriod": "5m",
            "barSource": "toss",
            "dataAsOf": "2026-09-02T03:15:00+00:00",
        },
        {
            "title": "PAPER exit Hard Risk",
            "source": "kasset_hard_risk",
            "kind": "hard_risk",
            **_hard_risk_evidence(
                account_state=deepcopy(_ACCOUNT_STATE_EVIDENCE),
                loss_streak=deepcopy(_LOSS_STREAK_SELL_BYPASS_EVIDENCE),
            ),
        },
    ]

    response = build_recommendation_response(row)

    payload = response.model_dump(mode="json", by_alias=True)
    exit_evidence = payload["evidence"][0]

    assert payload["action"] == "SELL"
    assert payload["rationale"] == row.rationale
    assert exit_evidence["kind"] == "position_exit"
    assert exit_evidence["exitKind"] == "STOP"
    assert exit_evidence["initialStop"] == "133860"
    assert exit_evidence["currentStop"] == "133860"
    assert payload["evidence"][1]["accountState"] == _ACCOUNT_STATE_EVIDENCE
    assert payload["evidence"][1]["lossStreak"] == _LOSS_STREAK_SELL_BYPASS_EVIDENCE


def test_build_recommendation_response_keeps_historical_sizing_shape() -> None:
    """Rows persisted before the multiplier evidence keep their original wire shape."""

    legacy_sizing = deepcopy(_BUY_POSITION_SIZING_EVIDENCE)
    legacy_sizing.pop("accountStateMultiplier")
    row = _recommendation_row(deepcopy(_STRATEGY_VOTES))
    row.evidence[0]["portfolio"] = {
        "targetWeight": "0.1",
        "targetQuantity": "1",
        "cashAfter": "9862000",
        "note": "Deterministic ATR risk sizing; limitingCaps=RISK_BUDGET.",
        "positionSizing": legacy_sizing,
    }

    response = build_recommendation_response(row)

    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["portfolio"]["positionSizing"] == legacy_sizing
    assert "accountState" not in payload["hardRisk"]
    assert "lossStreak" not in payload["hardRisk"]


def test_build_recommendation_response_reads_stored_exponent_decimals() -> None:
    """운영에 저장된 지수 표기 Decimal이 값 변화 없이 평문으로 나간다."""

    sizing = deepcopy(_BUY_POSITION_SIZING_EVIDENCE)
    sizing["caps"] = [{"code": "RISK_BUDGET", "quantity": "2.5E+2"}]
    sizing["unroundedQuantity"] = "2.5E+2"
    row = _recommendation_row(deepcopy(_STRATEGY_VOTES))
    row.evidence[0]["entryPrice"] = "1.38E+5"
    row.evidence[0]["portfolio"] = {
        "targetWeight": "0.1",
        "targetQuantity": "2.5E+2",
        "cashAfter": "9862000",
        "note": "Deterministic ATR risk sizing; limitingCaps=RISK_BUDGET.",
        "positionSizing": sizing,
    }

    payload = build_recommendation_response(row).model_dump(mode="json", by_alias=True)

    assert payload["portfolio"]["positionSizing"]["caps"] == [
        {"code": "RISK_BUDGET", "quantity": "250"}
    ]
    assert payload["portfolio"]["positionSizing"]["unroundedQuantity"] == "250"
    assert payload["portfolio"]["targetQuantity"] == "250"
    assert payload["entryPrice"] == "138000"
    # 감사 기록인 원본 evidence는 저장된 그대로 남는다.
    assert payload["evidence"][0]["portfolio"]["positionSizing"]["caps"] == [
        {"code": "RISK_BUDGET", "quantity": "2.5E+2"}
    ]


def test_position_size_cap_evidence_is_plain_decimal_text() -> None:
    """지수 표기로 남는 Decimal도 평문으로 저장돼 응답 계약을 다시 깨지 않는다."""

    quantity = Decimal("2.5E+2")
    cap = PositionSizeCap(PositionSizeCapCode.RISK_BUDGET, quantity)

    assert str(quantity) == "2.5E+2"
    assert quantity == Decimal("250")
    assert cap.as_evidence() == {"code": "RISK_BUDGET", "quantity": "250"}
