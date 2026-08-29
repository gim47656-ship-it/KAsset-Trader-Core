from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.policy import HardRiskResult, PortfolioPlan
from app.extensions.kasset.automation.producer import WeightedEnsembleDecision
from app.extensions.kasset.automation.regime import (
    MarketRegime,
    RegimeAssessment,
    weights_for_regime,
)
from app.extensions.kasset.automation.vertical_slice import (
    AIRecommendationVerticalSlice,
    EvaluatedCandidate,
    ReviewedCandidate,
    TradingCandidate,
)
from app.schemas.ai_recommendations import RecommendationRanking

_NOW = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_vertical_slice_ranking_includes_schema_required_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = StrategyResult(
        action=Action.BUY,
        confidence=Decimal("0.80"),
        entry=Decimal("100"),
        stop=Decimal("98"),
        target=Decimal("104"),
        rationale=("breakout",),
        evidence=(),
        strategy=StrategyName.BREAKOUT,
        version="1.0.0",
        symbol="005930",
        market="KRX",
        as_of=_NOW,
        valid_until=_NOW + timedelta(hours=1),
    )
    evaluated = EvaluatedCandidate(
        candidate=TradingCandidate("005930", "KRX", "삼성전자", "tvscreener_kr"),
        strategy_results=(strategy,),
        ensemble=WeightedEnsembleDecision(
            action=Action.BUY,
            score=Decimal("0.5"),
            confidence=Decimal("0.8"),
            agreeing=(strategy,),
            votes=(),
        ),
    )
    reviewed = ReviewedCandidate(
        evaluated=evaluated,
        external=ExternalEvidence(
            source="model_router:test",
            symbol="005930",
            market="KRX",
            action=Action.BUY,
            confidence=Decimal("0.8"),
            as_of=_NOW,
            valid_until=_NOW + timedelta(hours=1),
            rationale=("confirmed",),
        ),
        events=(),
        event_score=Decimal("0"),
        score=Decimal("0.525"),
    )
    captured: dict[str, object] = {}

    class RecordingProducer:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def produce(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(action="BUY")

    monkeypatch.setattr(vertical_slice, "RecommendationProducer", RecordingProducer)
    instance = AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)
    instance._policy = SimpleNamespace(  # type: ignore[assignment]
        portfolio_plan=AsyncMock(
            return_value=PortfolioPlan(
                target_weight=Decimal("0.1"),
                target_quantity=Decimal("1"),
                cash_after=Decimal("900"),
                note="bounded",
            )
        ),
        evaluate_hard_risk=AsyncMock(
            return_value=HardRiskResult(passed=True, checks=(), blocked_reason=None)
        ),
    )

    await instance._persist_recommendation(  # noqa: SLF001 - production seam regression
        4,
        reviewed,
        RegimeAssessment(
            regime=MarketRegime.BULL,
            detail="trend",
            breadth_above_sma20=Decimal("0.7"),
            median_return20=Decimal("0.1"),
            median_atr_ratio=Decimal("0.02"),
            weights=weights_for_regime(MarketRegime.BULL),
        ),
        position=1,
        total=100,
        snapshot=SimpleNamespace(limits=object(), usage=object()),
    )

    assert captured["name"] == "삼성전자"
    ranking = captured["ranking"]
    assert isinstance(ranking, dict)
    assert ranking["total"] == 100
    RecommendationRanking.model_validate(ranking)
