from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.ai.model_router import _TierAnalysis
from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.ai_shadow import build_ai_shadow_observation
from app.extensions.kasset.automation.candidate_ranker import (
    CandidateRankerConfig,
    CandidateRankingBatch,
    CandidateRankResult,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.policy import HardRiskResult, PortfolioPlan
from app.extensions.kasset.automation.position_sizing import (
    PositionSizingReason,
    PositionSizingResult,
    PositionSizingZeroCode,
)
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
_BULL = RegimeAssessment(
    regime=MarketRegime.BULL,
    detail="trend",
    breadth_above_sma20=Decimal("0.7"),
    median_return20=Decimal("0.1"),
    median_atr_ratio=Decimal("0.02"),
    weights=weights_for_regime(MarketRegime.BULL),
)


def _rank_result(
    symbol: str,
    *,
    position: int,
    market: str = "KR",
) -> CandidateRankResult:
    return CandidateRankResult(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        total_score=Decimal("0.9") - Decimal(position) / Decimal("100"),
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
        rank_position=position,
        ranked_total=position,
    )


def _strategy_result(symbol: str) -> StrategyResult:
    return StrategyResult(
        action=Action.BUY,
        confidence=Decimal("0.80"),
        entry=Decimal("100"),
        stop=Decimal("98"),
        target=Decimal("104"),
        rationale=("breakout",),
        evidence=(),
        strategy=StrategyName.BREAKOUT,
        version="1.0.0",
        symbol=symbol,
        market="KRX",
        as_of=_NOW,
        valid_until=_NOW + timedelta(hours=1),
    )


def _sizing_result(
    quantity: Decimal,
    *,
    zero_reasons: tuple[PositionSizingReason, ...] = (),
) -> PositionSizingResult:
    return PositionSizingResult(
        action="BUY",
        market="KRX",
        quantity=quantity,
        unrounded_quantity=quantity,
        lot_size=Decimal("1"),
        risk_budget=Decimal("1000"),
        risk_per_unit=Decimal("2"),
        risk_per_trade_rate=Decimal("0.01"),
        regime="TRENDING_UP",
        regime_multiplier=Decimal("1"),
        caps=(),
        limiting_caps=(),
        zero_reasons=zero_reasons,
    )


def _below_lot_plan() -> PortfolioPlan:
    return PortfolioPlan(
        target_weight=Decimal("0.1"),
        target_quantity=Decimal("0"),
        cash_after=Decimal("1000"),
        note="Deterministic position sizing returned zero; reasons=BELOW_MARKET_LOT.",
        position_sizing=_sizing_result(
            Decimal("0"),
            zero_reasons=(
                PositionSizingReason(
                    code=PositionSizingZeroCode.BELOW_MARKET_LOT,
                    field="quantity",
                    detail="capped quantity is below the market lot size",
                ),
            ),
        ),
    )


def _affordable_plan() -> PortfolioPlan:
    return PortfolioPlan(
        target_weight=Decimal("0.1"),
        target_quantity=Decimal("3"),
        cash_after=Decimal("700"),
        note="Deterministic ATR risk sizing; limitingCaps=RISK_BUDGET.",
        position_sizing=_sizing_result(Decimal("3")),
    )


def _stub_review_cycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ranked_symbols: tuple[str, ...],
    unaffordable: frozenset[str],
    ranker_config: CandidateRankerConfig,
) -> tuple[AIRecommendationVerticalSlice, AsyncMock, AsyncMock]:
    """Wire one owner cycle down to the ranked-row review loop, without a DB."""

    db = MagicMock()
    db.commit = AsyncMock()
    instance = AIRecommendationVerticalSlice(
        db,
        MagicMock(),
        now=_NOW,
        allowed_markets=frozenset({"KR"}),
        ranker_config=ranker_config,
    )
    portfolio_plan = AsyncMock(
        side_effect=lambda *_args, **kwargs: (
            _below_lot_plan()
            if kwargs["symbol"] in unaffordable
            else _affordable_plan()
        )
    )
    instance._policy = SimpleNamespace(  # type: ignore[assignment]
        get_snapshot=AsyncMock(
            return_value=SimpleNamespace(
                limits=SimpleNamespace(currency="KRW"),
                usage=object(),
            )
        ),
        portfolio_plan=portfolio_plan,
    )
    instance._cooldown_active = AsyncMock(return_value=False)  # type: ignore[method-assign]
    instance._position_manager = SimpleNamespace(  # type: ignore[assignment]
        run_owner=AsyncMock(return_value=())
    )
    monkeypatch.setattr(
        vertical_slice.daily_routine_service,
        "recommendation_markets",
        AsyncMock(return_value=frozenset({"KR"})),
    )
    monkeypatch.setattr(
        vertical_slice,
        "load_candidate_benchmark_returns",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        vertical_slice,
        "evaluate_ranked_shadow_setups",
        MagicMock(return_value=()),
    )
    instance._load_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            TradingCandidate(symbol, "KRX", None, "tvscreener_kr")
            for symbol in ranked_symbols
        ]
    )
    instance._sync_missing_kr_candles = AsyncMock(  # type: ignore[method-assign]
        return_value={"requested": 0, "synced": 0, "failed": 0}
    )
    instance._load_candidate_bars = AsyncMock(return_value={})  # type: ignore[method-assign]
    instance._ranker = SimpleNamespace(  # type: ignore[assignment]
        rank=MagicMock(
            return_value=CandidateRankingBatch(
                ranked=tuple(
                    _rank_result(symbol, position=index)
                    for index, symbol in enumerate(ranked_symbols, start=1)
                ),
                excluded=(),
            )
        )
    )

    def _evaluate(candidate, bars, regime, *, factor_ranking):
        strategy = _strategy_result(candidate.symbol)
        return EvaluatedCandidate(
            candidate=candidate,
            strategy_results=(strategy,),
            ensemble=WeightedEnsembleDecision(
                action=Action.BUY,
                score=Decimal("0.5"),
                confidence=Decimal("0.8"),
                agreeing=(strategy,),
                votes=(),
            ),
            factor_ranking=factor_ranking,
            regime=regime,
        )

    instance._evaluate_candidate = MagicMock(side_effect=_evaluate)  # type: ignore[method-assign]
    review_candidate = AsyncMock(
        side_effect=lambda _owner, item, _regime: (
            None,
            "low_confidence",
            instance._review_outcome(item, reason="low_confidence"),
        )
    )
    instance._review_candidate = review_candidate  # type: ignore[method-assign]
    return instance, review_candidate, portfolio_plan


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

    sizing = await instance._pre_ai_sizing(  # noqa: SLF001 - pre-AI sizing seam
        4,
        evaluated,
        _BULL,
        snapshot=SimpleNamespace(limits=object(), usage=object()),
    )
    assert isinstance(sizing, vertical_slice._PreAiSizing)  # noqa: SLF001

    await instance._persist_recommendation(  # noqa: SLF001 - production seam regression
        4,
        reviewed,
        _BULL,
        position=1,
        total=100,
        sizing=sizing,
    )

    # AI 앞단에서 한 번 계산한 plan을 저장 단계가 그대로 재사용한다.
    assert portfolio_plan.await_count == 1
    assert captured["suggested_quantity"] == Decimal("1")
    assert captured["portfolio"]["targetQuantity"] == "1"
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


@pytest.mark.asyncio
async def test_unaffordable_candidate_is_replaced_before_ai_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, review_candidate, portfolio_plan = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111", "000222"),
        unaffordable=frozenset({"000111"}),
        ranker_config=CandidateRankerConfig(),
    )

    result = await instance.run_owner(7)

    assert result["strategyEvaluatedCount"] == 2
    assert result["strategyActionableCount"] == 2
    assert result["preAiExclusions"] == {"presizing_zero_quantity:BELOW_MARKET_LOT": 1}
    exclusion = result["candidateExclusions"][0]
    assert exclusion["symbol"] == "000111"
    assert exclusion["market"] == "KR"
    assert exclusion["exclusionReason"] == "presizing_zero_quantity:BELOW_MARKET_LOT"
    assert exclusion["targetQuantity"] == "0"
    assert exclusion["rankPosition"] == 1
    zero_reasons = exclusion["portfolio"]["positionSizing"]["zeroReasons"]
    assert [reason["code"] for reason in zero_reasons] == ["BELOW_MARKET_LOT"]
    # 사이징이 0인 행은 AI를 거치지 않고, 다음 순위 후보가 그 슬롯을 쓴다.
    assert portfolio_plan.await_count == 2
    assert review_candidate.await_count == 1
    assert [
        call.args[1].candidate.symbol for call in review_candidate.await_args_list
    ] == ["000222"]
    assert result["aiReviewedCount"] == 1
    assert result["skipped"] == "no_ai_confirmed_signal"


@pytest.mark.asyncio
async def test_all_unaffordable_actionable_rows_never_reach_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, review_candidate, _plan = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111", "000222"),
        unaffordable=frozenset({"000111", "000222"}),
        ranker_config=CandidateRankerConfig(),
    )

    result = await instance.run_owner(7)

    assert result["strategyActionableCount"] == 2
    assert result["aiReviewedCount"] == 0
    assert result["preAiExclusions"] == {"presizing_zero_quantity:BELOW_MARKET_LOT": 2}
    assert result["skipped"] == "no_affordable_actionable_candidate"
    review_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_review_cap_survives_pre_ai_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, review_candidate, _plan = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111", "000222", "000333", "000444"),
        unaffordable=frozenset({"000111"}),
        ranker_config=CandidateRankerConfig(strategy_review_limit=2),
    )

    result = await instance.run_owner(9)

    # 창은 4까지 열리지만 AI로 보내는 최대 건수는 strategy_review_limit(2)다.
    assert result["strategyEvaluationWindow"] == 4
    assert result["strategyEvaluatedCount"] == 3
    assert review_candidate.await_count == 2
    assert result["aiReviewedCount"] == 2
    assert result["strategyReviewCapReached"] is True
    assert [
        call.args[1].candidate.symbol for call in review_candidate.await_args_list
    ] == ["000222", "000333"]
