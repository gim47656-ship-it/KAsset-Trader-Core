from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.ai.model_router import _TierAnalysis
from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.ai_shadow import build_ai_shadow_observation
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
    artifact_loader = MagicMock(return_value=SimpleNamespace(fingerprint="a" * 64))
    monkeypatch.setattr(vertical_slice, "current_strategy_artifact", artifact_loader)
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
        ai_shadow=build_ai_shadow_observation(
            SimpleNamespace(
                input_hash="b" * 64,
                provider="direct-api",
                tier="terra",
                model_id="configured-terra-model",
                action="BUY",
                risk="LOW",
                bullish_score=88,
                bearish_score=12,
                rationale_tags=["confirmed"],
                confidence=0.8,
            ),
            observed_at=_NOW,
        ),
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
    artifact_loader.assert_called_once_with()
    assert (
        instance._position_manager._strategy_fingerprint  # noqa: SLF001
        == "a" * 64
    )
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
    ai_shadow = captured["ai_shadow_evidence"]
    assert isinstance(ai_shadow, dict)
    assert ai_shadow["kind"] == "ai_shadow"
    assert ai_shadow["modelId"] == "configured-terra-model"
    assert ai_shadow["selected"] is True


@pytest.mark.asyncio
async def test_ai_invalid_response_and_action_mismatch_are_isolated() -> None:
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
    with pytest.raises(ValueError) as invalid_response:
        _TierAnalysis.model_validate(
            {
                "action": "BUY",
                "confidence": 0.8,
                "risk": "LOW",
                "bullish_score": 80,
                "bearish_score": 20,
                "escalate": False,
                "rationale_tags": ["이 문장은 태그가 아닙니다."],
            }
        )
    invalid_router = SimpleNamespace(
        analyze_for_owner=AsyncMock(side_effect=invalid_response.value)
    )
    invalid_instance = AIRecommendationVerticalSlice(
        MagicMock(), invalid_router, now=_NOW
    )
    invalid_instance._event_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value=()
    )

    (
        invalid_reviewed,
        invalid_rejection,
        invalid_outcome,
    ) = await invalid_instance._review_candidate(  # noqa: SLF001
        4,
        evaluated,
        RegimeAssessment(
            regime=MarketRegime.BULL,
            detail="trend",
            breadth_above_sma20=Decimal("0.7"),
            median_return20=Decimal("0.1"),
            median_atr_ratio=Decimal("0.02"),
            weights=weights_for_regime(MarketRegime.BULL),
        ),
    )

    assert invalid_reviewed is None
    assert invalid_rejection == "invalid_ai_response"
    assert invalid_outcome.as_dict()["reason"] == "invalid_ai_response"

    verdict = SimpleNamespace(
        input_hash="b" * 64,
        provider="mcp",
        tier="terra",
        tier_used="terra",
        model_id="gpt-5.6-terra",
        action="HOLD",
        risk="MEDIUM",
        bullish_score=55,
        bearish_score=45,
        rationale_tags=["breakout_not_confirmed"],
        confidence=0.72,
    )
    router = SimpleNamespace(analyze_for_owner=AsyncMock(return_value=verdict))
    instance = AIRecommendationVerticalSlice(MagicMock(), router, now=_NOW)
    instance._event_evidence = AsyncMock(return_value=())  # type: ignore[method-assign]

    reviewed, rejection, outcome = await instance._review_candidate(  # noqa: SLF001
        4,
        evaluated,
        RegimeAssessment(
            regime=MarketRegime.BULL,
            detail="trend",
            breadth_above_sma20=Decimal("0.7"),
            median_return20=Decimal("0.1"),
            median_atr_ratio=Decimal("0.02"),
            weights=weights_for_regime(MarketRegime.BULL),
        ),
    )

    assert reviewed is None
    assert rejection == "action_mismatch"
    assert outcome.as_dict() == {
        "symbol": "005930",
        "market": "KR",
        "strategyAction": "BUY",
        "aiAction": "HOLD",
        "confidence": "0.72",
        "reason": "action_mismatch",
        "observedAt": "2026-08-29T01:00:00Z",
        "provider": "mcp",
        "tier": "terra",
        "modelId": "gpt-5.6-terra",
        "rationaleTags": ["breakout_not_confirmed"],
        "recommendationId": None,
    }


def test_strategy_artifact_lookup_failure_prevents_recommendation_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vertical_slice,
        "current_strategy_artifact",
        MagicMock(side_effect=OSError("artifact unavailable")),
    )

    with pytest.raises(OSError, match="artifact unavailable"):
        AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)


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


@pytest.mark.asyncio
async def test_owner_market_scope_is_intersected_before_candidate_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = MagicMock()
    instance = AIRecommendationVerticalSlice(
        MagicMock(),
        router,
        now=_NOW,
        allowed_markets=frozenset({"KR"}),
    )
    instance._cooldown_active = AsyncMock(return_value=False)  # type: ignore[method-assign]
    instance._position_manager = SimpleNamespace(  # type: ignore[assignment]
        run_owner=AsyncMock(return_value=())
    )
    instance._policy = SimpleNamespace(  # type: ignore[assignment]
        get_snapshot=AsyncMock(
            return_value=SimpleNamespace(limits=SimpleNamespace(currency="KRW"))
        )
    )
    load_candidates = AsyncMock(return_value=[])
    instance._load_candidates = load_candidates  # type: ignore[method-assign]
    monkeypatch.setattr(
        vertical_slice.daily_routine_service,
        "recommendation_markets",
        AsyncMock(return_value=frozenset({"KR", "US"})),
    )

    result = await instance.run_owner(11)

    assert result["skipped"] == "screener_candidates_unavailable"
    load_candidates.assert_awaited_once_with(
        11,
        currency="KRW",
        allowed_markets=frozenset({"KR"}),
    )
    assert router.mock_calls == []


@pytest.mark.asyncio
async def test_owner_market_scope_mismatch_skips_before_candidates_or_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = MagicMock()
    instance = AIRecommendationVerticalSlice(
        MagicMock(),
        router,
        now=_NOW,
        allowed_markets=frozenset({"KR"}),
        cycle_trace_id="cyc-owner-mismatch",
    )
    instance._cooldown_active = AsyncMock(return_value=False)  # type: ignore[method-assign]
    instance._position_manager = SimpleNamespace(  # type: ignore[assignment]
        run_owner=AsyncMock(return_value=())
    )
    load_candidates = AsyncMock(side_effect=AssertionError("must not load candidates"))
    instance._load_candidates = load_candidates  # type: ignore[method-assign]
    monkeypatch.setattr(
        vertical_slice.daily_routine_service,
        "recommendation_markets",
        AsyncMock(return_value=frozenset({"US"})),
    )

    result = await instance.run_owner(12)

    assert result["ownerUserId"] == 12
    assert result["cycleTraceId"] == "cyc-owner-mismatch"
    assert result["skipped"] == "no_configured_regular_market_open"
    assert result["candidateCount"] == 0
    assert result["aiReviewedCount"] == 0
    assert result["recommendationIds"] == []
    load_candidates.assert_not_awaited()
    assert router.mock_calls == []


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
