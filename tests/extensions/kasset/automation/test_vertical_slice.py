from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.ai.model_router import _TierAnalysis
from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.account_state_gate import (
    AccountState,
    AccountStateEvaluation,
    AccountStateSnapshot,
    AccountStateThresholds,
)
from app.extensions.kasset.automation.ai_shadow import build_ai_shadow_observation
from app.extensions.kasset.automation.candidate_ranker import (
    CandidateRankerConfig,
    CandidateRankingBatch,
    CandidateRankResult,
    RankEvidence,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    ExternalEvidence,
    PriceBar,
    StrategyFamily,
    StrategyName,
    StrategyResult,
)
from app.extensions.kasset.automation.daily_setup import (
    DAILY_SETUP_SCHEMA_VERSION,
    DailySetup,
    DailySetupStatus,
)
from app.extensions.kasset.automation.decision_evidence import (
    AiReviewStatus,
    ai_review_from_observation,
    unknown_news_shadow,
)
from app.extensions.kasset.automation.intraday_data import (
    CompletedIntradayBars,
    IntradayBarsUnavailable,
    load_index_session_bars,
)
from app.extensions.kasset.automation.intraday_triggers import (
    INDEX_INTRADAY_UNAVAILABLE,
    OPENING_RANGE_BREAKOUT,
    RELATIVE_VOLUME_5M,
    RELATIVE_VOLUME_20M,
    IntradayTriggerDecision,
    SameTimeVolumeBaseline,
    TriggerDecisionStatus,
    TriggerResult,
    TriggerStatus,
    decide_intraday_triggers,
    intraday_relative_strength,
)
from app.extensions.kasset.automation.market_session import RegularSession
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
    AdmittedCandidate,
    AIRecommendationVerticalSlice,
    EvaluatedCandidate,
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
_ACCOUNT_THRESHOLDS = AccountStateThresholds.from_risk_rates(
    daily_target_rate_pct=Decimal("0.5"),
    max_daily_loss_rate_pct=Decimal("1.0"),
)


def _account_state(state: AccountState = AccountState.NORMAL) -> AccountStateEvaluation:
    multiplier = Decimal("0") if state is AccountState.EXIT_ONLY else Decimal("1")
    return AccountStateEvaluation(
        market="KRX",
        state=state,
        profit_ratio=Decimal("0.006")
        if state is AccountState.EXIT_ONLY
        else Decimal("0"),
        peak_drawdown_ratio=Decimal("0"),
        multiplier=multiplier,
        thresholds=_ACCOUNT_THRESHOLDS,
    )


def _account_snapshot(
    state: AccountState = AccountState.NORMAL,
) -> AccountStateSnapshot:
    return AccountStateSnapshot(
        thresholds=_ACCOUNT_THRESHOLDS,
        evaluations=(_account_state(state),),
    )


def _rank_result(
    symbol: str,
    *,
    position: int,
    market: str = "KR",
    benchmark_symbol: str | None = None,
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
        evidence=(
            (
                RankEvidence(
                    code="relative_strength_benchmark",
                    value=benchmark_symbol,
                    detail="fixture benchmark",
                ),
            )
            if benchmark_symbol is not None
            else ()
        ),
        sources=("tvscreener_kr",),
        is_held=False,
        is_watchlisted=False,
        eligible_for_new_buy=True,
        rank_position=position,
        ranked_total=position,
    )


def _strategy_result(
    symbol: str,
    *,
    strategy: StrategyName = StrategyName.BREAKOUT,
) -> StrategyResult:
    return StrategyResult(
        action=Action.BUY,
        confidence=Decimal("0.80"),
        entry=Decimal("100"),
        stop=Decimal("98"),
        target=Decimal("104"),
        rationale=(f"{strategy.value} breakout-family signal",),
        evidence=(),
        strategy=strategy,
        version="1.0.0",
        symbol=symbol,
        market="KRX",
        as_of=_NOW,
        valid_until=_NOW + timedelta(hours=1),
    )


def _qualified_setup(ranking: CandidateRankResult) -> DailySetup:
    results = tuple(
        _strategy_result(ranking.symbol, strategy=name) for name in StrategyName
    )
    agreeing = tuple(
        result
        for result in results
        if result.strategy is not StrategyName.MEAN_REVERSION
    )
    ensemble = WeightedEnsembleDecision(
        family=StrategyFamily.BREAKOUT,
        action=Action.BUY,
        score=Decimal("0.800000"),
        confidence=Decimal("0.800000"),
        agreeing=agreeing,
        votes=(),
    )
    return DailySetup(
        schema_version=DAILY_SETUP_SCHEMA_VERSION,
        symbol=ranking.symbol,
        market="KRX",
        family=StrategyFamily.BREAKOUT,
        status=DailySetupStatus.QUALIFIED,
        direction=Action.BUY,
        features=(),
        strategy_results=results,
        ensemble=ensemble,
        completed_bar_count=30,
        completed_through=_NOW - timedelta(days=1),
        evaluated_at=_NOW,
        rejection_reason=None,
        rank_position=ranking.rank_position,
    )


def _completed_intraday_bars(symbol: str) -> CompletedIntradayBars:
    data_as_of = _NOW - timedelta(minutes=1)
    session = RegularSession(
        market="kr",
        session_date=_NOW.date(),
        opens_at=_NOW - timedelta(hours=1),
        closes_at=_NOW + timedelta(hours=5),
    )
    return CompletedIntradayBars(
        symbol=symbol,
        market="KRX",
        period="5m",
        bar_interval=timedelta(minutes=5),
        session=session,
        bars=(
            PriceBar(
                timestamp=data_as_of - timedelta(minutes=5),
                open=Decimal("99"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=Decimal("2000"),
            ),
        ),
        source="fixture:completed_intraday",
        data_as_of=data_as_of,
    )


def _completed_intraday_window(
    symbol: str,
    *,
    market: str = "KRX",
    volumes: tuple[Decimal, ...] = (
        Decimal("200"),
        Decimal("200"),
        Decimal("200"),
        Decimal("200"),
    ),
) -> CompletedIntradayBars:
    bar_interval = timedelta(minutes=5)
    data_as_of = _NOW - timedelta(minutes=1)
    latest_start = data_as_of - bar_interval
    bars = tuple(
        PriceBar(
            timestamp=latest_start - bar_interval * (len(volumes) - index - 1),
            open=Decimal("99"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=volume,
        )
        for index, volume in enumerate(volumes)
    )
    return CompletedIntradayBars(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        period="5m",
        bar_interval=bar_interval,
        session=RegularSession(
            market="kr" if market == "KRX" else "us",
            session_date=_NOW.date(),
            opens_at=_NOW - timedelta(hours=6),
            closes_at=_NOW + timedelta(hours=1),
        ),
        bars=bars,
        source="fixture:completed_intraday",
        data_as_of=data_as_of,
    )


def _evaluated_candidate(
    symbol: str,
    *,
    market: str = "KRX",
) -> EvaluatedCandidate:
    ranking = _rank_result(
        symbol,
        position=1,
        market="KR" if market == "KRX" else "US",
    )
    setup = replace(_qualified_setup(ranking), market=market)
    assert isinstance(setup.ensemble, WeightedEnsembleDecision)
    return EvaluatedCandidate(
        candidate=TradingCandidate(
            symbol,
            market,  # type: ignore[arg-type]
            None,
            "fixture",
        ),
        strategy_results=setup.strategy_results,
        ensemble=setup.ensemble,
        setup=setup,
        factor_ranking=ranking,
        regime=_BULL,
    )


def _session_rvol_decision(
    symbol: str,
    *,
    triggered: bool,
) -> IntradayTriggerDecision:
    trigger_status = TriggerStatus.ACTIVE if triggered else TriggerStatus.INACTIVE
    triggers = tuple(
        TriggerResult(
            code=code,
            status=trigger_status,
            value="2" if triggered else "0.5",
            threshold="1.5",
            source="fixture:completed_intraday",
            as_of=_NOW - timedelta(minutes=1),
            detail="fixture same-session RVOL",
        )
        for code in (RELATIVE_VOLUME_5M, RELATIVE_VOLUME_20M)
    )
    decision = decide_intraday_triggers(
        triggers,
        symbol=symbol,
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_NOW,
    )
    return replace(
        decision,
        status=(
            TriggerDecisionStatus.TRIGGERED
            if triggered
            else TriggerDecisionStatus.NOT_TRIGGERED
        ),
        blocked_reason=None if triggered else "relative_volume_not_satisfied",
    )


def _stub_shadow_storage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline_loader: AsyncMock,
) -> tuple[list[object], MagicMock]:
    shadow_db = MagicMock()
    shadow_db.commit = AsyncMock()
    shadow_db.rollback = AsyncMock()
    shadow_db.execute = AsyncMock()

    @asynccontextmanager
    async def shadow_session():
        yield shadow_db

    recorded: list[object] = []

    async def record_many(rows: tuple[object, ...] | list[object]) -> int:
        recorded.extend(rows)
        return len(rows)

    monkeypatch.setattr(vertical_slice, "_session", shadow_session)
    monkeypatch.setattr(
        vertical_slice,
        "load_same_time_bucket_volumes",
        baseline_loader,
    )
    monkeypatch.setattr(
        vertical_slice,
        "RvolShadowRepository",
        MagicMock(
            return_value=SimpleNamespace(record_many=AsyncMock(side_effect=record_many))
        ),
    )
    return recorded, shadow_db


def _fresh_trigger_decision(symbol: str) -> IntradayTriggerDecision:
    data_as_of = _NOW - timedelta(minutes=1)
    triggers = (
        TriggerResult(
            code=OPENING_RANGE_BREAKOUT,
            status=TriggerStatus.ACTIVE,
            value="101",
            threshold="100",
            source="fixture:completed_intraday",
            as_of=data_as_of,
            detail="completed bar closed above the opening range",
        ),
        TriggerResult(
            code=RELATIVE_VOLUME_5M,
            status=TriggerStatus.ACTIVE,
            value="2.0",
            threshold="1.5",
            source="fixture:completed_intraday",
            as_of=data_as_of,
            detail="completed-bar relative volume expanded",
        ),
    )
    return decide_intraday_triggers(
        triggers,
        symbol=symbol,
        market="KRX",
        direction=Action.BUY,
        evaluated_at=_NOW,
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
) -> tuple[AIRecommendationVerticalSlice, AsyncMock, AsyncMock, AsyncMock]:
    """Supply qualified daily setups and fresh triggers at their production seams."""

    db = MagicMock()
    db.commit = AsyncMock()
    analyze_for_owner = AsyncMock(
        return_value=SimpleNamespace(
            input_hash="b" * 64,
            provider="mcp",
            tier="terra",
            model_id="gpt-5.6-terra",
            action="HOLD",
            risk="MEDIUM",
            bullish_score=45,
            bearish_score=55,
            rationale_tags=["breakout_not_confirmed"],
            confidence=0.72,
        )
    )
    instance = AIRecommendationVerticalSlice(
        db,
        SimpleNamespace(analyze_for_owner=analyze_for_owner),
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
                limits=SimpleNamespace(
                    currency="KRW",
                    daily_target_rate_pct=Decimal("0.5"),
                    max_daily_loss_rate_pct=Decimal("1.0"),
                ),
                usage=object(),
                usage_by_currency={"KRW": object(), "USD": object()},
            )
        ),
        portfolio_plan=portfolio_plan,
    )
    instance._account_state_gate = SimpleNamespace(  # type: ignore[assignment]
        evaluate_owner=AsyncMock(return_value=_account_snapshot())
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
    monkeypatch.setattr(
        vertical_slice,
        "evaluate_daily_setup",
        MagicMock(
            side_effect=lambda ranking, _bars, **_kwargs: _qualified_setup(ranking)
        ),
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

    async def _load_intraday(
        setups: tuple[DailySetup, ...],
    ) -> dict[tuple[str, str], CompletedIntradayBars]:
        return {
            ("KR", setup.symbol): _completed_intraday_bars(setup.symbol)
            for setup in setups
        }

    def _decide_trigger(
        item: EvaluatedCandidate,
        *,
        intraday: object,
        index_bars: object,
        previous_close: Decimal | None,
    ) -> IntradayTriggerDecision:
        del index_bars
        assert previous_close is None
        assert isinstance(intraday, CompletedIntradayBars)
        assert _NOW - intraday.data_as_of <= timedelta(minutes=12)
        return _fresh_trigger_decision(item.candidate.symbol)

    instance._load_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        side_effect=_load_intraday
    )
    instance._load_index_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    instance._decide_triggers = MagicMock(  # type: ignore[method-assign]
        side_effect=_decide_trigger
    )
    instance._record_same_time_rvol_shadow = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )
    instance._news_source_health = AsyncMock(  # type: ignore[method-assign]
        return_value={"KR": False}
    )
    instance._news_shadow = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _candidate, **_kwargs: unknown_news_shadow(
            observed_at=_NOW,
            detail="fixture source health is intentionally unproven",
        )
    )
    persist_recommendation = AsyncMock(
        side_effect=lambda _owner, item, _regime, **_kwargs: SimpleNamespace(
            id=f"rec:{item.evaluated.candidate.symbol}",
            action=item.decision.action.value,
        )
    )
    instance._persist_recommendation = persist_recommendation  # type: ignore[method-assign]
    return instance, analyze_for_owner, portfolio_plan, persist_recommendation


@pytest.mark.asyncio
async def test_pre_ai_sizing_selects_usage_for_candidate_book() -> None:
    instance = AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)
    portfolio_plan = AsyncMock(return_value=_affordable_plan())
    instance._policy = SimpleNamespace(  # type: ignore[assignment]
        portfolio_plan=portfolio_plan
    )
    krw_usage = object()
    usd_usage = object()

    result = await instance._pre_ai_sizing(  # noqa: SLF001 - 장부 선택 계약
        7,
        _evaluated_candidate("AAPL", market="US"),
        _BULL,
        snapshot=SimpleNamespace(
            limits=object(),
            usage=krw_usage,
            usage_by_currency={"KRW": krw_usage, "USD": usd_usage},
        ),
    )

    assert isinstance(result, vertical_slice._PreAiSizing)  # noqa: SLF001
    assert portfolio_plan.await_args.kwargs["usage"] is usd_usage


@pytest.mark.asyncio
async def test_vertical_slice_loads_kospi_and_kosdaq_benchmarks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rankings = (
        _rank_result("005930", position=1, benchmark_symbol="KOSPI"),
        _rank_result("035900", position=2, benchmark_symbol="KOSDAQ"),
    )
    setups = tuple(_qualified_setup(ranking) for ranking in rankings)
    session = RegularSession(
        market="kr",
        session_date=_NOW.date(),
        opens_at=_NOW - timedelta(hours=1),
        closes_at=_NOW + timedelta(hours=5),
    )
    calls: list[tuple[str, str]] = []

    async def load_index(**kwargs):
        calls.append((kwargs["index_symbol"], kwargs["market"]))
        return _completed_intraday_bars(kwargs["index_symbol"])

    monkeypatch.setattr(vertical_slice, "load_index_session_bars", load_index)
    instance = object.__new__(AIRecommendationVerticalSlice)
    instance._now = _NOW  # type: ignore[attr-defined]

    loaded = await instance._load_index_intraday_bars(
        setups,
        ranking_by_key={ranking.key: ranking for ranking in rankings},
        session_by_market={"KR": session},
    )

    assert set(loaded) == {"KOSPI", "KOSDAQ"}
    assert calls == [("KOSDAQ", "KRX"), ("KOSPI", "KRX")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_symbol", "index_close", "expected_excess"),
    (("KOSPI", "102", "0.020000"), ("KOSDAQ", "98", "0.060000")),
)
async def test_kr_benchmark_indices_produce_available_relative_strength(
    monkeypatch: pytest.MonkeyPatch,
    index_symbol: str,
    index_close: str,
    expected_excess: str,
) -> None:
    session = RegularSession(
        market="kr",
        session_date=_NOW.date(),
        opens_at=_NOW - timedelta(hours=1),
        closes_at=_NOW + timedelta(hours=5),
    )
    first_timestamp = _NOW - timedelta(minutes=15)
    latest_completed_timestamp = _NOW - timedelta(minutes=10)
    partial_timestamp = _NOW
    candles = [
        SimpleNamespace(
            timestamp=first_timestamp,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1000,
            source="toss",
        ),
        SimpleNamespace(
            timestamp=latest_completed_timestamp,
            open=100,
            high=max(Decimal(index_close), Decimal("100")) + Decimal("1"),
            low=min(Decimal(index_close), Decimal("100")) - Decimal("1"),
            close=Decimal(index_close),
            volume=1000,
            source="toss",
        ),
        SimpleNamespace(
            timestamp=partial_timestamp,
            open=100,
            high=1000,
            low=1,
            close=999,
            volume=1000,
            source="toss",
        ),
    ]
    get_ohlcv = AsyncMock(return_value=candles)
    monkeypatch.setattr(
        "app.services.market_data.service.get_ohlcv",
        get_ohlcv,
    )

    loaded = await load_index_session_bars(
        index_symbol=index_symbol,
        market="KRX",
        as_of=_NOW,
        session=session,
    )

    assert isinstance(loaded, CompletedIntradayBars)
    assert [bar.timestamp for bar in loaded.bars] == [
        first_timestamp,
        latest_completed_timestamp,
    ]
    get_ohlcv.assert_awaited_once_with(
        symbol=index_symbol,
        market="equity_kr",
        period="5m",
        count=84,
    )
    candidate_bars = (
        PriceBar(
            timestamp=first_timestamp,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        ),
        PriceBar(
            timestamp=latest_completed_timestamp,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("104"),
            volume=Decimal("1000"),
        ),
    )
    strength = intraday_relative_strength(
        candidate_bars,
        loaded.bars,
        direction=Action.BUY,
        threshold=Decimal("0"),
        bar_interval=timedelta(minutes=5),
        source="toss",
        index_source=loaded.source,
    )

    assert strength.status is TriggerStatus.ACTIVE
    assert strength.value == expected_excess
    assert strength.source == "toss"


@pytest.mark.asyncio
async def test_kr_index_provider_failure_keeps_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RegularSession(
        market="kr",
        session_date=_NOW.date(),
        opens_at=_NOW - timedelta(hours=1),
        closes_at=_NOW + timedelta(hours=5),
    )
    monkeypatch.setattr(
        "app.services.market_data.service.get_ohlcv",
        AsyncMock(side_effect=RuntimeError("provider down")),
    )

    loaded = await load_index_session_bars(
        index_symbol="KOSPI",
        market="KRX",
        as_of=_NOW,
        session=session,
    )

    assert isinstance(loaded, IntradayBarsUnavailable)
    assert loaded.blocked_reason == INDEX_INTRADAY_UNAVAILABLE
    assert "intraday_provider_unavailable" in loaded.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (("invalid", "intraday_bar_invalid"), ("stale", "intraday_bars_stale")),
)
async def test_kr_index_unusable_or_stale_bars_remain_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_reason: str,
) -> None:
    session = RegularSession(
        market="kr",
        session_date=_NOW.date(),
        opens_at=_NOW - timedelta(hours=1),
        closes_at=_NOW + timedelta(hours=5),
    )
    if case == "invalid":
        timestamp = _NOW - timedelta(minutes=10)
        high = Decimal("99")
    else:
        timestamp = session.opens_at
        high = Decimal("101")
    monkeypatch.setattr(
        "app.services.market_data.service.get_ohlcv",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    timestamp=timestamp,
                    open=Decimal("100"),
                    high=high,
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("1000"),
                    source="toss",
                )
            ]
        ),
    )

    loaded = await load_index_session_bars(
        index_symbol="KOSDAQ",
        market="KRX",
        as_of=_NOW,
        session=session,
    )

    assert isinstance(loaded, IntradayBarsUnavailable)
    assert loaded.blocked_reason == expected_reason


@pytest.mark.asyncio
async def test_vertical_slice_ranking_includes_schema_required_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_loader = MagicMock(return_value=SimpleNamespace(fingerprint="a" * 64))
    monkeypatch.setattr(vertical_slice, "current_strategy_artifact", artifact_loader)
    ranking_result = CandidateRankResult(
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
    )
    setup = _qualified_setup(ranking_result)
    assert setup.ensemble is not None
    evaluated = EvaluatedCandidate(
        candidate=TradingCandidate(
            "005930",
            "KRX",
            "삼성전자",
            "tvscreener_kr",
            turnover=Decimal("90000000"),
            volume=Decimal("900000"),
        ),
        strategy_results=setup.strategy_results,
        ensemble=setup.ensemble,
        setup=setup,
        factor_ranking=ranking_result,
        regime=_BULL,
    )
    trigger_decision = _fresh_trigger_decision("005930")
    decision = ExternalEvidence(
        source="kasset_technical_decision:daily_setup+intraday_triggers",
        symbol="005930",
        market="KRX",
        action=Action.BUY,
        confidence=Decimal("0.8"),
        as_of=_NOW,
        valid_until=_NOW + timedelta(hours=1),
        rationale=("Daily Setup and fresh intraday triggers admitted the candidate.",),
        evidence=(setup.as_evidence(), trigger_decision.as_evidence()),
    )
    ai_shadow = build_ai_shadow_observation(
        SimpleNamespace(
            input_hash="b" * 64,
            provider="direct-api",
            tier="terra",
            model_id="configured-terra-model",
            action="HOLD",
            risk="MEDIUM",
            bullish_score=45,
            bearish_score=55,
            rationale_tags=["breakout_not_confirmed"],
            confidence=0.72,
        ),
        observed_at=_NOW,
    )
    admitted = AdmittedCandidate(
        evaluated=evaluated,
        trigger_decision=trigger_decision,
        decision=decision,
        ai_review=ai_review_from_observation(
            status=AiReviewStatus.DISAGREES,
            observation=ai_shadow,
            detail="technical direction=BUY aiAction=HOLD",
        ),
        news_shadow=unknown_news_shadow(
            observed_at=_NOW,
            detail="news source health was not proven",
        ),
        events=(),
        score=Decimal("0.525"),
        ai_shadow=ai_shadow,
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
        snapshot=SimpleNamespace(
            limits=object(),
            usage=object(),
            usage_by_currency={"KRW": object(), "USD": object()},
        ),
    )
    assert isinstance(sizing, vertical_slice._PreAiSizing)  # noqa: SLF001

    await instance._persist_recommendation(  # noqa: SLF001 - production seam regression
        4,
        admitted,
        _BULL,
        position=1,
        total=100,
        sizing=sizing,
    )

    assert portfolio_plan.await_count == 1
    assert captured["decision_evidence"] is decision
    assert captured["strategy_family"] is StrategyFamily.BREAKOUT
    assert captured["suggested_quantity"] == Decimal("1")
    portfolio = captured["portfolio"]
    assert isinstance(portfolio, dict)
    assert portfolio["targetQuantity"] == "1"
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
    advisory = captured["advisory_evidence"]
    assert isinstance(advisory, list)
    assert [item["kind"] for item in advisory] == [
        "daily_setup",
        "intraday_triggers",
        "ai_review",
        "news_shadow",
        "decision_cohorts",
    ]
    assert advisory[2]["gating"] is False
    assert advisory[2]["status"] == "disagrees"
    assert advisory[3]["gating"] is False
    assert advisory[4]["liveCohort"] == "technical_only"
    ai_shadow_evidence = captured["ai_shadow_evidence"]
    assert isinstance(ai_shadow_evidence, dict)
    assert ai_shadow_evidence["kind"] == "ai_shadow"
    assert ai_shadow_evidence["modelId"] == "configured-terra-model"
    assert ai_shadow_evidence["validatedResponse"]["action"] == "HOLD"
    assert ai_shadow_evidence["selectionReason"] == (
        "ranked_final_selection_after_technical_gate"
    )


@pytest.mark.asyncio
async def test_ai_invalid_response_and_action_mismatch_are_non_gating() -> None:
    ranking = _rank_result("005930", position=1)
    setup = _qualified_setup(ranking)
    assert setup.ensemble is not None
    evaluated = EvaluatedCandidate(
        candidate=TradingCandidate(
            "005930",
            "KRX",
            "삼성전자",
            "tvscreener_kr",
        ),
        strategy_results=setup.strategy_results,
        ensemble=setup.ensemble,
        setup=setup,
        factor_ranking=ranking,
        regime=_BULL,
    )
    trigger_decision = _fresh_trigger_decision("005930")
    news_shadow = unknown_news_shadow(
        observed_at=_NOW,
        detail="news source health was not proven",
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

    invalid_review = await invalid_instance._review_candidate(  # noqa: SLF001
        4,
        evaluated,
    )

    assert invalid_review.ai_review.status is AiReviewStatus.INVALID
    assert invalid_review.ai_review.failure_reason == "invalid_ai_response"
    assert invalid_review.ai_shadow is None
    admitted_after_invalid = vertical_slice._admitted_candidate(  # noqa: SLF001
        evaluated,
        trigger_decision=trigger_decision,
        review=invalid_review,
        news_shadow=news_shadow,
        now=_NOW,
    )
    assert admitted_after_invalid.decision.action is Action.BUY

    verdict = SimpleNamespace(
        input_hash="b" * 64,
        provider="mcp",
        tier="terra",
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

    mismatch_review = await instance._review_candidate(  # noqa: SLF001
        4,
        evaluated,
    )

    assert mismatch_review.ai_review.status is AiReviewStatus.DISAGREES
    assert mismatch_review.ai_review.action == "HOLD"
    admitted_after_mismatch = vertical_slice._admitted_candidate(  # noqa: SLF001
        evaluated,
        trigger_decision=trigger_decision,
        review=mismatch_review,
        news_shadow=news_shadow,
        now=_NOW,
    )
    assert admitted_after_mismatch.decision.action is Action.BUY
    assert admitted_after_mismatch.ai_review.status is AiReviewStatus.DISAGREES


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
    db = MagicMock()
    db.commit = AsyncMock()
    instance = AIRecommendationVerticalSlice(
        db,
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
            return_value=SimpleNamespace(
                limits=SimpleNamespace(
                    currency="KRW",
                    daily_target_rate_pct=Decimal("0.5"),
                    max_daily_loss_rate_pct=Decimal("1.0"),
                )
            )
        )
    )
    instance._account_state_gate = SimpleNamespace(  # type: ignore[assignment]
        evaluate_owner=AsyncMock(return_value=_account_snapshot())
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
    db.commit.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_news_source_health_binds_naive_utc_cutoff() -> None:
    captured_cutoffs: list[datetime] = []
    captured_markets: list[str] = []

    async def scalar(statement: object) -> int:
        params = tuple(statement.compile().params.values())  # type: ignore[attr-defined]
        cutoff = next(value for value in params if isinstance(value, datetime))
        market = next(value for value in params if value in {"kr", "us"})
        captured_cutoffs.append(cutoff)
        captured_markets.append(str(market))
        if cutoff.tzinfo is not None or cutoff.utcoffset() is not None:
            raise TypeError("can't subtract offset-naive and offset-aware datetimes")
        return 1

    db = MagicMock()
    db.scalar = AsyncMock(side_effect=scalar)
    instance = object.__new__(AIRecommendationVerticalSlice)
    instance._db = db  # type: ignore[attr-defined]
    instance._now = _NOW  # type: ignore[attr-defined]

    health = await instance._news_source_health(  # noqa: SLF001 - DB 경계 회귀
        frozenset({"KR", "US"})
    )

    assert health == {"KR": True, "US": True}
    assert captured_markets == ["kr", "us"]
    assert captured_cutoffs == [
        datetime(2026, 8, 28, 1, 0),
        datetime(2026, 8, 28, 1, 0),
    ]
    assert all(cutoff.tzinfo is None for cutoff in captured_cutoffs)


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
async def test_unaffordable_candidate_is_replaced_before_non_gating_ai_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, analyze_for_owner, portfolio_plan, persist_recommendation = (
        _stub_review_cycle(
            monkeypatch,
            ranked_symbols=("000111", "000222"),
            unaffordable=frozenset({"000111"}),
            ranker_config=CandidateRankerConfig(),
        )
    )

    result = await instance.run_owner(7)

    assert result["strategyEvaluatedCount"] == 2
    assert result["strategyActionableCount"] == 2
    assert result["dailySetupStatuses"] == {"qualified": 2}
    assert result["dailySetupSelectedCount"] == 2
    assert result["intradayTriggerStatuses"] == {"triggered": 2}
    assert result["preAiExclusions"] == {"presizing_zero_quantity:BELOW_MARKET_LOT": 1}
    exclusion = result["candidateExclusions"][0]
    assert exclusion["symbol"] == "000111"
    assert exclusion["market"] == "KR"
    assert exclusion["exclusionReason"] == "presizing_zero_quantity:BELOW_MARKET_LOT"
    assert exclusion["targetQuantity"] == "0"
    assert exclusion["rankPosition"] == 1
    zero_reasons = exclusion["portfolio"]["positionSizing"]["zeroReasons"]
    assert [reason["code"] for reason in zero_reasons] == ["BELOW_MARKET_LOT"]
    assert portfolio_plan.await_count == 2
    assert analyze_for_owner.await_count == 1
    assert [call.args[3]["symbol"] for call in analyze_for_owner.await_args_list] == [
        "000222"
    ]
    assert result["aiReviewedCount"] == 1
    assert result["aiReviewRejections"] == {"ai_disagrees": 1}
    assert result["recommendationIds"] == ["rec:000222"]
    assert "skipped" not in result
    persist_recommendation.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_unaffordable_actionable_rows_never_reach_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, analyze_for_owner, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111", "000222"),
        unaffordable=frozenset({"000111", "000222"}),
        ranker_config=CandidateRankerConfig(),
    )

    result = await instance.run_owner(7)

    assert result["strategyActionableCount"] == 2
    assert result["dailySetupStatuses"] == {"qualified": 2}
    assert result["intradayTriggerStatuses"] == {"triggered": 2}
    assert result["aiReviewedCount"] == 0
    assert result["preAiExclusions"] == {"presizing_zero_quantity:BELOW_MARKET_LOT": 2}
    assert result["skipped"] == "no_affordable_actionable_candidate"
    analyze_for_owner.assert_not_awaited()
    persist_recommendation.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_gating_ai_review_is_not_capped_after_technical_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, analyze_for_owner, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111", "000222", "000333", "000444"),
        unaffordable=frozenset({"000111"}),
        ranker_config=CandidateRankerConfig(strategy_review_limit=2),
    )

    result = await instance.run_owner(9)

    assert result["strategyEvaluationWindow"] == 4
    assert result["strategyEvaluatedCount"] == 4
    assert result["dailySetupSelectedCount"] == 4
    assert result["intradayTriggerStatuses"] == {"triggered": 4}
    assert analyze_for_owner.await_count == 3
    assert result["aiReviewedCount"] == 3
    assert result["strategyReviewCapReached"] is False
    assert [call.args[3]["symbol"] for call in analyze_for_owner.await_args_list] == [
        "000222",
        "000333",
        "000444",
    ]
    assert result["recommendationIds"] == [
        "rec:000222",
        "rec:000333",
        "rec:000444",
    ]
    assert persist_recommendation.await_count == 3


@pytest.mark.asyncio
async def test_exit_only_is_counted_before_sizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, analyze_for_owner, portfolio_plan, persist_recommendation = (
        _stub_review_cycle(
            monkeypatch,
            ranked_symbols=("000111",),
            unaffordable=frozenset(),
            ranker_config=CandidateRankerConfig(),
        )
    )
    instance._account_state_gate = SimpleNamespace(  # type: ignore[assignment]
        evaluate_owner=AsyncMock(return_value=_account_snapshot(AccountState.EXIT_ONLY))
    )

    result = await instance.run_owner(7)

    assert result["preAiExclusions"] == {"exit_only": 1}
    exclusion = result["candidateExclusions"][0]
    assert exclusion["source"] == "account_state_gate"
    assert exclusion["symbol"] == "000111"
    assert exclusion["reason"] == "exit_only"
    portfolio_plan.assert_not_awaited()
    analyze_for_owner.assert_not_awaited()
    persist_recommendation.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_cycle_uses_last_daily_close_before_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _analyze, _plan, _persist = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("000111",),
        unaffordable=frozenset(),
        ranker_config=CandidateRankerConfig(),
    )
    previous = PriceBar(
        timestamp=_NOW - timedelta(days=1),
        open=Decimal("94"),
        high=Decimal("96"),
        low=Decimal("93"),
        close=Decimal("95"),
        volume=Decimal("1000"),
    )
    same_session = replace(
        previous,
        timestamp=_NOW,
        close=Decimal("105"),
    )
    instance._load_candidate_bars = AsyncMock(  # type: ignore[method-assign]
        return_value={("KR", "000111"): (previous, same_session)}
    )
    decide = MagicMock(return_value=_fresh_trigger_decision("000111"))
    instance._decide_triggers = decide  # type: ignore[method-assign]

    await instance.run_owner(7)

    assert decide.call_args.kwargs["previous_close"] == Decimal("95")


@pytest.mark.parametrize(
    ("aligned_open", "previous_close", "expected_unavailable"),
    [
        (False, Decimal("100"), "session_open_unavailable"),
        (True, None, "previous_close_unavailable"),
    ],
)
def test_trigger_inputs_mark_missing_session_prices_unavailable(
    aligned_open: bool,
    previous_close: Decimal | None,
    expected_unavailable: str,
) -> None:
    instance = AIRecommendationVerticalSlice(MagicMock(), MagicMock(), now=_NOW)
    intraday = _completed_intraday_bars("005930")
    if aligned_open:
        intraday = replace(
            intraday,
            bars=(
                replace(
                    intraday.bars[0],
                    timestamp=intraday.session.opens_at,
                ),
            ),
        )

    decision = instance._decide_triggers(  # noqa: SLF001 - no-chase 입력 계약
        _evaluated_candidate("005930"),
        intraday=intraday,
        index_bars=None,
        previous_close=previous_close,
    )

    no_chase = decision.as_evidence()["noChase"]
    assert no_chase["gapUp"]["unavailable"] == expected_unavailable  # type: ignore[index]


def test_admitted_candidate_valid_until_is_bounded_by_trigger() -> None:
    item = _evaluated_candidate("005930")
    trigger_decision = replace(
        _fresh_trigger_decision("005930"),
        valid_until=_NOW + timedelta(minutes=10),
    )
    ai_shadow = build_ai_shadow_observation(
        SimpleNamespace(
            input_hash="b" * 64,
            provider="direct-api",
            tier="terra",
            model_id="configured-terra-model",
            action="HOLD",
            risk="MEDIUM",
            bullish_score=45,
            bearish_score=55,
            rationale_tags=["breakout_not_confirmed"],
            confidence=0.72,
        ),
        observed_at=_NOW,
    )
    review = vertical_slice._AiReviewOutcomeBundle(  # noqa: SLF001 - admission 계약
        ai_review=ai_review_from_observation(
            status=AiReviewStatus.DISAGREES,
            observation=ai_shadow,
            detail="technical direction=BUY aiAction=HOLD",
        ),
        ai_shadow=ai_shadow,
        bullish_score=45,
        bearish_score=55,
    )

    admitted = vertical_slice._admitted_candidate(  # noqa: SLF001 - admission 계약
        item,
        trigger_decision=trigger_decision,
        review=review,
        news_shadow=unknown_news_shadow(
            observed_at=_NOW,
            detail="fixture source health is intentionally unproven",
        ),
        now=_NOW,
    )

    assert admitted.decision.valid_until == trigger_decision.valid_until


@pytest.mark.asyncio
async def test_same_time_rvol_shadow_active_does_not_change_trigger_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _analyze, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("005930",),
        unaffordable=frozenset(),
        ranker_config=CandidateRankerConfig(),
    )
    delattr(instance, "_record_same_time_rvol_shadow")
    instance._load_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda setups: {
            ("KR", setup.symbol): _completed_intraday_window(setup.symbol)
            for setup in setups
        }
    )
    session_decision = _session_rvol_decision("005930", triggered=False)
    decision_before = (session_decision.status, session_decision.blocked_reason)
    instance._decide_triggers = MagicMock(  # type: ignore[method-assign]
        return_value=session_decision
    )

    async def load_baseline(_db: object, **kwargs: object):
        requests = kwargs["requests"]
        assert isinstance(requests, dict)
        bucket_count = len(next(iter(requests.values())))  # type: ignore[union-attr]
        baseline_volume = Decimal("100") * bucket_count
        return {
            str(symbol): [
                SameTimeVolumeBaseline(
                    session_date=_NOW.date() - timedelta(days=offset),
                    volume=baseline_volume,
                )
                for offset in range(1, 11)
            ]
            for symbol in requests
        }

    baseline_loader = AsyncMock(side_effect=load_baseline)
    recorded, _shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=baseline_loader,
    )

    result = await instance.run_owner(7)

    assert (
        session_decision.status,
        session_decision.blocked_reason,
    ) == decision_before
    assert result["intradayTriggerStatuses"] == {"not_triggered": 1}
    assert result["intradayTriggers"][0]["blockedReason"] == (
        "relative_volume_not_satisfied"
    )
    assert result["sameTimeRvolShadow"] == {"active": 2}
    assert result["recommendationIds"] == []
    persist_recommendation.assert_not_awaited()
    assert baseline_loader.await_count == 2
    assert len(recorded) == 1
    observation = recorded[0]
    assert observation.same_time_status_5m == "active"  # type: ignore[attr-defined]
    assert observation.same_time_status_20m == "active"  # type: ignore[attr-defined]
    assert observation.session_status_5m == "inactive"  # type: ignore[attr-defined]
    assert observation.session_status_20m == "inactive"  # type: ignore[attr-defined]
    assert observation.session_decision_status == "inactive"  # type: ignore[attr-defined]
    assert observation.session_decision_reason == (  # type: ignore[attr-defined]
        "relative_volume_not_satisfied"
    )
    assert observation.same_time_baseline_median_5m == Decimal(  # type: ignore[attr-defined]
        "100"
    )
    assert observation.same_time_baseline_median_20m == Decimal(  # type: ignore[attr-defined]
        "400"
    )


@pytest.mark.asyncio
async def test_same_time_rvol_baseline_failure_preserves_owner_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _analyze, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("005930",),
        unaffordable=frozenset(),
        ranker_config=CandidateRankerConfig(),
    )
    delattr(instance, "_record_same_time_rvol_shadow")
    instance._load_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda setups: {
            ("KR", setup.symbol): _completed_intraday_window(setup.symbol)
            for setup in setups
        }
    )
    baseline_loader = AsyncMock(side_effect=RuntimeError("fixture baseline failure"))
    recorded, shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=baseline_loader,
    )

    result = await instance.run_owner(7)

    assert result["intradayTriggerStatuses"] == {"triggered": 1}
    assert result["intradayTriggers"][0]["blockedReason"] is None
    assert result["recommendationIds"] == ["rec:005930"]
    persist_recommendation.assert_awaited_once()
    assert result["sameTimeRvolShadow"] == {"unavailable:baseline_load_failed": 2}
    assert baseline_loader.await_count == 2
    assert shadow_db.rollback.await_count == 2
    assert len(recorded) == 1
    observation = recorded[0]
    assert observation.same_time_status_5m == (  # type: ignore[attr-defined]
        "unavailable:baseline_load_failed"
    )
    assert observation.same_time_status_20m == (  # type: ignore[attr-defined]
        "unavailable:baseline_load_failed"
    )


@pytest.mark.asyncio
async def test_same_time_rvol_shadow_batches_krx_candidates_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = AIRecommendationVerticalSlice(
        MagicMock(),
        MagicMock(),
        now=_NOW,
        cycle_trace_id="cyc-rvol-shadow",
    )
    krx_symbols = ("000001", "000002", "000003", "000004", "000005")
    us_symbols = ("AAPL", "MSFT")
    candidates = tuple(
        (
            _evaluated_candidate(symbol),
            _completed_intraday_window(symbol),
            _session_rvol_decision(symbol, triggered=True),
        )
        for symbol in krx_symbols
    ) + tuple(
        (
            _evaluated_candidate(symbol, market="US"),
            _completed_intraday_window(symbol, market="US"),
            _session_rvol_decision(symbol, triggered=True),
        )
        for symbol in us_symbols
    )
    baseline_loader = AsyncMock(return_value={})
    recorded, shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=baseline_loader,
    )

    summary = await instance._record_same_time_rvol_shadow(  # noqa: SLF001
        11,
        candidates,
    )

    assert baseline_loader.await_count == 2
    for call in baseline_loader.await_args_list:
        requests = call.kwargs["requests"]
        assert tuple(requests) == krx_symbols
    assert {
        row.symbol  # type: ignore[attr-defined]
        for row in recorded
    } == set(krx_symbols)
    assert all(row.market == "KRX" for row in recorded)  # type: ignore[attr-defined]
    assert summary == {"unavailable:insufficient_baseline_days": 10}
    shadow_db.commit.assert_awaited_once()
    shadow_db.execute.assert_awaited_once_with(
        vertical_slice._SAME_TIME_RVOL_STATEMENT_TIMEOUT_SQL  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_same_time_rvol_shadow_requests_each_symbols_own_latest_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = AIRecommendationVerticalSlice(
        MagicMock(),
        MagicMock(),
        now=_NOW,
        cycle_trace_id="cyc-rvol-own-buckets",
    )
    first = _completed_intraday_window("000001")
    second_base = _completed_intraday_window("000002")
    second = replace(
        second_base,
        bars=tuple(
            replace(bar, timestamp=bar.timestamp - timedelta(minutes=10))
            for bar in second_base.bars
        ),
    )
    baseline_loader = AsyncMock(return_value={})
    _recorded, _shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=baseline_loader,
    )

    await instance._record_same_time_rvol_shadow(  # noqa: SLF001
        11,
        (
            (
                _evaluated_candidate("000001"),
                first,
                _session_rvol_decision("000001", triggered=True),
            ),
            (
                _evaluated_candidate("000002"),
                second,
                _session_rvol_decision("000002", triggered=True),
            ),
        ),
    )

    assert baseline_loader.await_count == 2
    requests_by_window = {
        len(next(iter(call.kwargs["requests"].values()))): call.kwargs["requests"]
        for call in baseline_loader.await_args_list
    }
    assert requests_by_window[1] == {
        "000001": (time(9, 54),),
        "000002": (time(9, 44),),
    }
    assert requests_by_window[4] == {
        "000001": (time(9, 39), time(9, 44), time(9, 49), time(9, 54)),
        "000002": (time(9, 29), time(9, 34), time(9, 39), time(9, 44)),
    }


@pytest.mark.asyncio
async def test_same_time_rvol_shadow_timeout_preserves_owner_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _analyze, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("005930",),
        unaffordable=frozenset(),
        ranker_config=CandidateRankerConfig(),
    )
    delattr(instance, "_record_same_time_rvol_shadow")
    instance._load_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda setups: {
            ("KR", setup.symbol): _completed_intraday_window(setup.symbol)
            for setup in setups
        }
    )

    async def never_returns(_db: object, **_kwargs: object) -> object:
        await asyncio.Future()
        raise AssertionError("unreachable")

    recorded, _shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=AsyncMock(side_effect=never_returns),
    )
    monkeypatch.setattr(
        vertical_slice,
        "_SAME_TIME_RVOL_SHADOW_TIMEOUT_SECONDS",
        0.01,
    )

    result = await asyncio.wait_for(instance.run_owner(7), timeout=1)

    assert result["sameTimeRvolShadow"] == {"unavailable:shadow_timeout": 2}
    assert result["recommendationIds"] == ["rec:005930"]
    persist_recommendation.assert_awaited_once()
    assert recorded == []


@pytest.mark.asyncio
async def test_same_time_rvol_duplicate_baseline_is_logged_and_cycle_completes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    instance, _analyze, _plan, persist_recommendation = _stub_review_cycle(
        monkeypatch,
        ranked_symbols=("005930",),
        unaffordable=frozenset(),
        ranker_config=CandidateRankerConfig(),
    )
    delattr(instance, "_record_same_time_rvol_shadow")
    instance._load_intraday_bars = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda setups: {
            ("KR", setup.symbol): _completed_intraday_window(setup.symbol)
            for setup in setups
        }
    )

    async def load_duplicate_baseline(_db: object, **kwargs: object):
        return {
            symbol: [
                SameTimeVolumeBaseline(
                    session_date=_NOW.date() - timedelta(days=max(1, offset)),
                    volume=Decimal("100"),
                )
                for offset in range(10)
            ]
            for symbol in kwargs["requests"]
        }

    recorded, _shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=AsyncMock(side_effect=load_duplicate_baseline),
    )
    caplog.set_level(logging.WARNING, logger=vertical_slice.__name__)

    result = await instance.run_owner(7)

    assert result["sameTimeRvolShadow"] == {"unavailable:shadow_pipeline_failed": 2}
    assert result["recommendationIds"] == ["rec:005930"]
    persist_recommendation.assert_awaited_once()
    assert recorded == []
    assert any(
        record.exc_info is not None
        and "same-time RVOL shadow contract violation" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_same_time_rvol_shadow_write_failure_replaces_pending_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = AIRecommendationVerticalSlice(
        MagicMock(),
        MagicMock(),
        now=_NOW,
        cycle_trace_id="cyc-rvol-write-failure",
    )

    async def load_baseline(_db: object, **kwargs: object):
        return {
            symbol: [
                SameTimeVolumeBaseline(
                    session_date=_NOW.date() - timedelta(days=offset),
                    volume=Decimal("100") * len(bucket_starts),
                )
                for offset in range(1, 11)
            ]
            for symbol, bucket_starts in kwargs["requests"].items()
        }

    recorded, shadow_db = _stub_shadow_storage(
        monkeypatch,
        baseline_loader=AsyncMock(side_effect=load_baseline),
    )
    monkeypatch.setattr(
        vertical_slice,
        "RvolShadowRepository",
        MagicMock(
            return_value=SimpleNamespace(
                record_many=AsyncMock(side_effect=RuntimeError("fixture write failure"))
            )
        ),
    )

    summary = await instance._record_same_time_rvol_shadow(  # noqa: SLF001
        11,
        (
            (
                _evaluated_candidate("005930"),
                _completed_intraday_window("005930"),
                _session_rvol_decision("005930", triggered=True),
            ),
        ),
    )

    assert summary == {"unavailable:shadow_write_failed": 2}
    assert recorded == []
    shadow_db.commit.assert_not_awaited()
    shadow_db.rollback.assert_awaited_once()
