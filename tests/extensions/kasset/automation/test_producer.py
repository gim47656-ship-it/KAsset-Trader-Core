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


def _external(action: str = "buy") -> dict[str, object]:
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
        strategy_results=_strategy_quorum(),
        external_evidence=_external(),
        suggested_quantity="2",
        now=_NOW,
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
    assert len(draft.evidence) == 5  # type: ignore[attr-defined]
    assert {
        item["strategy"]
        for item in draft.evidence  # type: ignore[attr-defined]
        if item["kind"] == "strategy"
    } == {strategy.value for strategy in StrategyName}


@pytest.mark.asyncio
async def test_external_contradiction_fails_closed_to_hold() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        external_evidence=_external("sell"),
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.suggested_quantity is None  # type: ignore[attr-defined]
    assert "external evidence does not confirm" in " ".join(  # type: ignore[attr-defined]
        draft.risks
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
        external_evidence=_external(),
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert any("missing strategies" in risk for risk in draft.risks)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_future_external_evidence_cannot_confirm_a_trade() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    external = _external()
    external["derived_as_of"] = (_NOW + timedelta(minutes=1)).isoformat()

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        external_evidence=external,
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.confidence == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_external_evidence_for_another_symbol_fails_closed() -> None:
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    external = _external()
    external["symbol"] = "MSFT"

    await producer.produce(
        symbol="AAPL",
        market="US",
        strategy_results=_strategy_quorum(),
        external_evidence=external,
        suggested_quantity="2",
        now=_NOW,
    )

    draft = persistence.calls[0][1]
    assert draft.action == Action.HOLD  # type: ignore[attr-defined]
    assert draft.confidence == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_injected_external_evidence_uses_the_same_deterministic_boundary() -> (
    None
):
    persistence = RecordingPersistence()
    producer = RecommendationProducer(
        owner_user_id="user-a",
        persistence=persistence,
    )
    external = ExternalEvidence(
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
        external_evidence=external,
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
