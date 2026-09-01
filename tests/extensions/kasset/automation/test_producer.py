from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation import (
    Action,
    ExternalEvidence,
    RationaleEvidence,
    RecommendationProducer,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.ai_shadow import AI_SHADOW_SCHEMA_VERSION
from app.extensions.kasset.automation.contracts import StrategyFamily

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class RecordingPersistence:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_recommendation(
        self, *, owner_user_id: str, draft: object
    ) -> object:
        self.calls.append((owner_user_id, draft))
        return {"id": "rec-1", "draft": draft}


def _result(strategy: StrategyName, action: Action) -> StrategyResult:
    return StrategyResult(
        action=action,
        confidence=Decimal("0.80"),
        entry=Decimal("100"),
        stop=Decimal("95") if action == Action.BUY else None,
        target=Decimal("110") if action == Action.BUY else None,
        rationale=(f"{strategy.value} deterministic signal",),
        evidence=(RationaleEvidence("FIXTURE", "1", "Synthetic evidence."),),
        strategy=strategy,
        version="1.0.0",
        symbol="AAPL",
        market="US",
        as_of=_NOW - timedelta(minutes=5),
        valid_until=_NOW + timedelta(hours=1),
    )


def _strategy_quorum() -> list[StrategyResult]:
    return [
        _result(StrategyName.MOMENTUM, Action.BUY),
        _result(StrategyName.MEAN_REVERSION, Action.HOLD),
        _result(StrategyName.BREAKOUT, Action.BUY),
        _result(StrategyName.VOLATILITY_TREND, Action.BUY),
    ]


def _decision_evidence(action: str = "buy") -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "market_type": "equity_us",
        "source": "analyze_stock_impl",
        "derived_as_of": (_NOW - timedelta(minutes=2)).isoformat(),
        "valid_for_seconds": 3600,
        "recommendation": {
            "action": action,
            "confidence": "high",
            "reasoning": "Deterministic upstream indicators agree.",
            "insufficient_inputs": [],
        },
    }


def _ai_shadow() -> dict[str, object]:
    return {
        "title": "AI shadow final selection",
        "source": "kasset_ai_shadow",
        "kind": "ai_shadow",
        "schemaVersion": AI_SHADOW_SCHEMA_VERSION,
        "inputHash": "a" * 64,
        "provider": "direct-api",
        "tier": "terra",
        "modelId": "configured-terra-model",
        "validatedResponse": {
            "action": "HOLD",
            "risk": "MEDIUM",
            "bullishScore": 45,
            "bearishScore": 55,
            "rationaleTags": ["breakout_not_confirmed"],
        },
        "confidence": "0.10",
        "selected": True,
        "selectionReason": "ranked_final_selection_after_technical_gate",
        "observedAt": "2026-08-27T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_producer_persists_owner_scoped_consensus_without_order_side_effect() -> (
    None
):
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )

    persisted = await producer.produce(
        symbol="aapl",
        market="us",
        name="Apple Inc.",
        strategy_results=_strategy_quorum(),
        decision_evidence=_decision_evidence(),
        suggested_quantity="2",
        now=_NOW,
        ai_shadow_evidence=_ai_shadow(),
        advisory_evidence=(
            {
                "kind": "ai_review",
                "gating": False,
                "status": "unavailable",
                "failureReason": "provider_unavailable",
            },
        ),
    )

    assert persisted["id"] == "rec-1"  # type: ignore[index]
    assert len(persistence.calls) == 1
    owner_user_id, draft = persistence.calls[0]
    assert owner_user_id == "user-a"
    assert draft.owner_user_id == "user-a"  # type: ignore[attr-defined]
    assert draft.action == Action.BUY  # type: ignore[attr-defined]
    assert draft.suggested_quantity == Decimal("2")  # type: ignore[attr-defined]
    assert draft.reference_price == Decimal("100")  # type: ignore[attr-defined]
    assert draft.source == "kasset-automation"  # type: ignore[attr-defined]
    assert len(draft.evidence) == 7  # type: ignore[attr-defined]
    assert draft.name == "Apple Inc."  # type: ignore[attr-defined]
    assert draft.headline == "Apple Inc. 매수 검토 의견"  # type: ignore[attr-defined]
    assert draft.rationale == (  # type: ignore[attr-defined]
        "전략 투표 결과는 모멘텀=매수, 평균회귀=관망, 돌파=매수, 변동성추세=매수입니다.",
        "기술 판정 의견은 매수이며 신뢰도는 0.75입니다.",
    )
    assert "Deterministic strategy votes" not in " ".join(  # type: ignore[attr-defined]
        draft.rationale
    )
    assert {
        item["strategy"]
        for item in draft.evidence  # type: ignore[attr-defined]
        if item["kind"] == "strategy"
    } == {strategy.value for strategy in StrategyName}
    detail = next(
        item
        for item in draft.evidence  # type: ignore[attr-defined]
        if item["kind"] == "ai_vertical_slice"
    )
    assert detail["strategyFamily"] == StrategyFamily.BREAKOUT.value
    assert detail["strategyVotes"] == [
        {
            "strategy": "MOMENTUM",
            "family": StrategyFamily.BREAKOUT.value,
            "vote": "BUY",
            "weight": "0.333333",
            "score": "0.266667",
        },
        {
            "strategy": "BREAKOUT",
            "family": StrategyFamily.BREAKOUT.value,
            "vote": "BUY",
            "weight": "0.333333",
            "score": "0.266667",
        },
        {
            "strategy": "VOLATILITY_TREND",
            "family": StrategyFamily.BREAKOUT.value,
            "vote": "BUY",
            "weight": "0.333333",
            "score": "0.266667",
        },
    ]
    assert detail["aiRationale"] == ["Deterministic upstream indicators agree."]
    advisory = next(
        item
        for item in draft.evidence  # type: ignore[attr-defined]
        if item["kind"] == "ai_review"
    )
    assert advisory["gating"] is False
    assert advisory["status"] == "unavailable"
    shadow = next(
        item
        for item in draft.evidence  # type: ignore[attr-defined]
        if item["kind"] == "ai_shadow"
    )
    assert shadow == _ai_shadow()


@pytest.mark.asyncio
async def test_us_buy_survives_hard_risk_review_block_with_evidence() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    hard_risk = {
        "passed": False,
        "blockedReason": "currency-market mismatch",
        "checks": [
            {
                "rule": "POSITION",
                "passed": False,
                "detail": "market=US; expectedMarket=KRX",
            }
        ],
    }

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        decision_evidence=_decision_evidence(),
        suggested_quantity="2",
        now=_NOW,
        hard_risk=hard_risk,
    )

    _, draft = persistence.calls[0]
    assert draft.action == Action.BUY  # type: ignore[attr-defined]
    assert draft.suggested_quantity == Decimal("2")  # type: ignore[attr-defined]
    risks = draft.risks  # type: ignore[attr-defined]
    evidence = draft.evidence  # type: ignore[attr-defined]
    assert "hard risk blocked: currency-market mismatch" in risks
    detail = next(item for item in evidence if item["kind"] == "ai_vertical_slice")
    assert detail["hardRisk"] == hard_risk


@pytest.mark.asyncio
async def test_technical_decision_contradiction_fails_closed_to_hold() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        decision_evidence=_decision_evidence("sell"),
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.suggested_quantity is None  # type: ignore[attr-defined]
    assert (
        "technical decision evidence does not confirm the breakout family direction"
        in draft.risks  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_missing_strategy_cannot_create_actionable_recommendation() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum()[:-1],
        decision_evidence=_decision_evidence(),
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert any("missing strategies" in risk for risk in draft.risks)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_future_decision_evidence_cannot_confirm_a_trade() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    decision = _decision_evidence()
    decision["derived_as_of"] = (_NOW + timedelta(minutes=1)).isoformat()

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        decision_evidence=decision,
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.confidence == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_decision_evidence_for_another_symbol_fails_closed() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    decision = _decision_evidence()
    decision["symbol"] = "MSFT"

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        decision_evidence=decision,
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.confidence == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_injected_decision_evidence_uses_the_same_deterministic_boundary() -> (
    None
):
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    decision = ExternalEvidence(
        source="injected-test-analysis",
        symbol="AAPL",
        market="US",
        action=Action.BUY,
        confidence=Decimal("0.9"),
        as_of=_NOW - timedelta(minutes=1),
        valid_until=_NOW + timedelta(hours=1),
        rationale=("Injected evidence agrees.",),
    )

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        decision_evidence=decision,
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.BUY  # type: ignore[attr-defined]


def test_producer_requires_canonical_owner() -> None:
    with pytest.raises(ValueError, match="owner_user_id"):
        RecommendationProducer(
            owner_user_id=" ",
            persistence=RecordingPersistence(),
        )
