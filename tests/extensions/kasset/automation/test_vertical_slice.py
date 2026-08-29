from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.candidate_ranker import CandidateRankResult
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
    monkeypatch.setattr(
        vertical_slice,
        "current_strategy_artifact",
        lambda: SimpleNamespace(fingerprint="a" * 64),
    )
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
        candidate=TradingCandidate(
            "005930",
            "KRX",
            "삼성전자",
            "tvscreener_kr",
            turnover=Decimal("90000000"),
            volume=Decimal("900000"),
        ),
        strategy_results=(strategy,),
        ensemble=WeightedEnsembleDecision(
            action=Action.BUY,
            score=Decimal("0.5"),
            confidence=Decimal("0.8"),
            agreeing=(strategy,),
            votes=(),
        ),
        factor_ranking=CandidateRankResult(
            symbol="005930",
            market="KR",
            total_score=Decimal("0.75"),
            factor_scores=(),
            penalties=(),
            data_as_of=_NOW - timedelta(hours=1),
            valid_until=_NOW + timedelta(hours=1),
            exclusion_reason=None,
            atr_14=Decimal("2"),
            average_volume_20=Decimal("1000000"),
            average_turnover_20=Decimal("100000000"),
            evidence=(),
            sources=("tvscreener_kr",),
            is_held=False,
            is_watchlisted=False,
            eligible_for_new_buy=True,
            rank_position=1,
            ranked_total=100,
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
    portfolio_plan = AsyncMock(
        return_value=PortfolioPlan(
            target_weight=Decimal("0.1"),
            target_quantity=Decimal("1"),
            cash_after=Decimal("900"),
            note="bounded",
        )
    )
    instance = AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)
    instance._policy = SimpleNamespace(  # type: ignore[assignment]
        portfolio_plan=portfolio_plan,
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
    sizing_inputs = portfolio_plan.await_args.kwargs
    assert sizing_inputs["strategy_stop"] == Decimal("98")
    assert sizing_inputs["strategy_atr"] == Decimal("2")
    assert sizing_inputs["price_as_of"] == _NOW - timedelta(hours=1)
    assert sizing_inputs["evaluated_at"] == _NOW
    assert sizing_inputs["regime"] == MarketRegime.BULL
    assert sizing_inputs["average_volume"] == Decimal("900000")
    assert sizing_inputs["average_turnover"] == Decimal("90000000")
    assert captured["strategy_promotion"] == {
        "strategyKey": "qullamaggie_breakout_portfolio",
        "version": "1.0.0",
        "artifactFingerprint": "a" * 64,
    }


@pytest.mark.asyncio
async def test_held_positions_are_managed_before_candidate_cooldown() -> None:
    instance = AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)
    manager = AsyncMock(return_value=("position-exit:owner-4",))
    instance._position_manager = SimpleNamespace(  # type: ignore[assignment]
        run_owner=manager
    )
    instance._cooldown_active = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await instance.run_owner(4)

    manager.assert_awaited_once_with(4)
    assert result["skipped"] == "position_exit_recommendation_created"
    assert result["recommendationIds"] == ["position-exit:owner-4"]
    assert result["positionExitRecommendationIds"] == ["position-exit:owner-4"]


@pytest.mark.asyncio
async def test_ai_unavailable_still_runs_held_position_manager() -> None:
    instance = AIRecommendationVerticalSlice(MagicMock(), None, now=_NOW)
    manager = AsyncMock(return_value=())
    instance._position_manager = SimpleNamespace(  # type: ignore[assignment]
        run_owner=manager
    )
    instance._cooldown_active = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await instance.run_owner(8)

    manager.assert_awaited_once_with(8)
    assert result["skipped"] == "ai_unavailable"
    assert result["recommendationIds"] == []


def test_price_bars_restore_database_timezone_boundary() -> None:
    naive = datetime(2026, 8, 29, 1, 0)
    aware = datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    rows = [
        SimpleNamespace(
            time_utc=timestamp,
            open="100",
            high="110",
            low="90",
            close="105",
            volume="1000",
        )
        for timestamp in (naive, aware)
    ]

    bars = vertical_slice._price_bars(rows)  # noqa: SLF001 - DB boundary regression

    assert bars[0].timestamp == naive.replace(tzinfo=UTC)
    assert bars[1].timestamp == aware.astimezone(UTC)
