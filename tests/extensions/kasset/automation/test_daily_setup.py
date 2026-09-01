from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.candidate_ranker import (
    CandidateRankResult,
    FactorScore,
    RankEvidence,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    PriceBar,
    StrategyFamily,
    StrategyName,
    strategies_in_family,
)
from app.extensions.kasset.automation.daily_setup import (
    DAILY_SETUP_LIMIT_RANGE,
    DailySetupConfig,
    DailySetupStatus,
    daily_setup_policy_evidence,
    evaluate_daily_setup,
    select_daily_setups,
)
from app.extensions.kasset.automation.producer import compose_weighted_ensemble
from app.extensions.kasset.automation.regime import MarketRegime, RegimeAssessment
from app.extensions.kasset.automation.strategies import STRATEGIES

# KST 12:00 on 2026-09-01: that session is in progress, 2026-08-31 is completed.
_NOW = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)

_BULL = RegimeAssessment(
    regime=MarketRegime.BULL,
    detail="상승",
    breadth_above_sma20=Decimal("0.8"),
    median_return20=Decimal("0.05"),
    median_atr_ratio=Decimal("0.02"),
    weights={
        StrategyName.MOMENTUM: Decimal("0.35"),
        StrategyName.MEAN_REVERSION: Decimal("0.10"),
        StrategyName.BREAKOUT: Decimal("0.30"),
        StrategyName.VOLATILITY_TREND: Decimal("0.25"),
    },
)


def _rising_bars(count: int = 60) -> list[PriceBar]:
    """완료 세션만 담은 상승 추세 일봉. 마지막 봉이 20봉 채널을 돌파한다."""

    start = datetime(2026, 8, 31, 0, 0, tzinfo=UTC) - timedelta(days=count - 1)
    bars: list[PriceBar] = []
    price = Decimal("100")
    for index in range(count):
        price = price + Decimal("1")
        last = index == count - 1
        bars.append(
            PriceBar(
                timestamp=start + timedelta(days=index),
                open=price,
                high=price + (Decimal("6") if last else Decimal("1")),
                low=price - Decimal("1"),
                close=price + (Decimal("5") if last else Decimal("0")),
                volume=Decimal("1000"),
            )
        )
    return bars


def _ranking(
    symbol: str,
    *,
    excess_return: Decimal = Decimal("0.10"),
    strength_source: str = "benchmark_excess_60_session_return",
    position: int = 1,
) -> CandidateRankResult:
    return CandidateRankResult(
        symbol=symbol,
        market="KR",
        total_score=Decimal("0.7"),
        factor_scores=(
            FactorScore(
                code="relative_strength",
                raw_value=excess_return,
                score=Decimal("0.7"),
                weight=Decimal("0.15"),
                contribution=Decimal("0.105"),
            ),
        ),
        penalties=(),
        data_as_of=_NOW - timedelta(days=1),
        valid_until=_NOW + timedelta(hours=6),
        exclusion_reason=None,
        atr_14=Decimal("2"),
        average_volume_20=Decimal("1000"),
        average_turnover_20=Decimal("1000000"),
        evidence=(
            RankEvidence("relative_strength_source", strength_source, "source"),
            RankEvidence("relative_strength_benchmark", "KOSPI", "benchmark"),
        ),
        sources=("tvscreener_kr",),
        is_held=False,
        is_watchlisted=False,
        eligible_for_new_buy=True,
        rank_position=position,
        ranked_total=20,
    )


def _evaluate(
    ranking: CandidateRankResult,
    bars: list[PriceBar],
    config: DailySetupConfig | None = None,
):
    return evaluate_daily_setup(
        ranking,
        bars,
        market="KRX",
        regime=_BULL,
        strategies=STRATEGIES,
        as_of=_NOW,
        config=config or DailySetupConfig(),
    )


def test_mean_reversion_is_not_part_of_the_breakout_family() -> None:
    assert strategies_in_family(StrategyFamily.BREAKOUT) == (
        StrategyName.MOMENTUM,
        StrategyName.BREAKOUT,
        StrategyName.VOLATILITY_TREND,
    )
    assert strategies_in_family(StrategyFamily.MEAN_REVERSION) == (
        StrategyName.MEAN_REVERSION,
    )


def test_mean_reversion_never_votes_in_the_breakout_consensus() -> None:
    bars = _rising_bars()
    results = tuple(
        strategy.evaluate(bars, symbol="000111", market="KRX", as_of=_NOW)
        for strategy in STRATEGIES
    )

    decision = compose_weighted_ensemble(
        results,
        _BULL.weights,
        family=StrategyFamily.BREAKOUT,
    )

    voters = {vote["strategy"] for vote in decision.votes}
    assert StrategyName.MEAN_REVERSION.name not in voters
    assert decision.family is StrategyFamily.BREAKOUT
    assert sum(Decimal(str(vote["weight"])) for vote in decision.votes) == Decimal(
        "1.000000"
    )


def test_mean_reversion_family_is_scored_in_isolation() -> None:
    bars = _rising_bars()
    results = tuple(
        strategy.evaluate(bars, symbol="000111", market="KRX", as_of=_NOW)
        for strategy in STRATEGIES
    )

    isolated = compose_weighted_ensemble(
        results,
        _BULL.weights,
        family=StrategyFamily.MEAN_REVERSION,
    )

    assert {vote["strategy"] for vote in isolated.votes} == {
        StrategyName.MEAN_REVERSION.name
    }


def test_qualified_setup_records_every_named_feature() -> None:
    setup = _evaluate(_ranking("000111"), _rising_bars())

    assert setup.status is DailySetupStatus.QUALIFIED
    assert setup.direction is Action.BUY
    assert [item.code for item in setup.features] == [
        "breakout_structure",
        "momentum_alignment",
        "daily_relative_strength",
        "mean_reversion_isolated",
    ]
    evidence = setup.as_evidence()
    assert evidence["kind"] == "daily_setup"
    assert evidence["family"] == "breakout"


def test_setup_uses_only_completed_daily_bars() -> None:
    bars = _rising_bars()
    partial = PriceBar(
        timestamp=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        open=Decimal("500"),
        high=Decimal("900"),
        low=Decimal("100"),
        close=Decimal("120"),
        volume=Decimal("99999"),
    )

    with_partial = _evaluate(_ranking("000111"), [*bars, partial])
    without_partial = _evaluate(_ranking("000111"), bars)

    assert with_partial.completed_bar_count == without_partial.completed_bar_count
    assert with_partial.completed_through == without_partial.completed_through
    assert with_partial.status is without_partial.status


def test_unproven_benchmark_relative_strength_fails_closed() -> None:
    setup = _evaluate(
        _ranking("000111", strength_source="cross_sectional_60_session_percentile"),
        _rising_bars(),
    )

    assert setup.status is DailySetupStatus.UNAVAILABLE
    assert setup.rejection_reason == "daily_relative_strength=unavailable"


def test_negative_benchmark_excess_rejects_a_long_setup() -> None:
    setup = _evaluate(
        _ranking("000111", excess_return=Decimal("-0.05")),
        _rising_bars(),
    )

    assert setup.status is DailySetupStatus.REJECTED
    assert setup.rejection_reason == "daily_relative_strength=fail"


def test_insufficient_completed_bars_is_unavailable_not_a_score_penalty() -> None:
    setup = _evaluate(_ranking("000111"), _rising_bars(count=25))

    assert setup.status is DailySetupStatus.UNAVAILABLE
    assert setup.rejection_reason == "insufficient_completed_daily_bars"


def test_setup_limit_stays_inside_the_configured_range() -> None:
    low, high = DAILY_SETUP_LIMIT_RANGE
    assert (low, high) == (10, 20)

    with pytest.raises(ValueError, match="setup_limit"):
        DailySetupConfig(setup_limit=low - 1)
    with pytest.raises(ValueError, match="setup_limit"):
        DailySetupConfig(setup_limit=high + 1)
    assert DailySetupConfig(setup_limit=high).setup_limit == high


def test_selection_caps_at_the_limit_and_keeps_rank_order() -> None:
    bars = _rising_bars()
    setups = [
        _evaluate(_ranking(f"{index:06d}", position=index), bars)
        for index in range(1, 16)
    ]

    selected = select_daily_setups(setups, config=DailySetupConfig(setup_limit=10))

    assert len(selected) == 10
    assert [item.rank_position for item in selected] == list(range(1, 11))
    assert [item.setup_position for item in selected] == list(range(1, 11))


def test_policy_evidence_names_the_isolated_family() -> None:
    evidence = daily_setup_policy_evidence(DailySetupConfig())

    assert evidence["votingStrategies"] == [
        "momentum",
        "breakout",
        "volatility_trend",
    ]
    assert evidence["isolatedStrategies"] == ["mean_reversion"]
    assert evidence["barScope"] == "completed_regular_session_daily_bars_only"
