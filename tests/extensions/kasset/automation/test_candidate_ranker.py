from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.automation import vertical_slice
from app.extensions.kasset.automation.candidate_ranker import (
    BenchmarkReturn,
    CandidateMetadata,
    CandidateRanker,
    CandidateRankingBatch,
    cap_candidate_universe,
)
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.vertical_slice import (
    AIRecommendationVerticalSlice,
    TradingCandidate,
)
from app.models.trading import InstrumentType

_NOW = datetime(2026, 8, 29, 20, 0, tzinfo=UTC)


def _metadata(
    symbol: str,
    *,
    market: str = "US",
    turnover: str = "5000000",
    held: bool = False,
    watchlisted: bool = False,
) -> CandidateMetadata:
    return CandidateMetadata(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        sources=("test_screener",),
        screener_turnover=Decimal(turnover),
        screener_volume=Decimal("1000000"),
        is_held=held,
        is_watchlisted=watchlisted,
    )


def _bars(
    *,
    base: Decimal = Decimal("100"),
    daily_gain: Decimal = Decimal("0.20"),
    count: int = 260,
    latest: datetime = _NOW - timedelta(hours=1),
) -> tuple[PriceBar, ...]:
    output: list[PriceBar] = []
    for index in range(count):
        close = base + daily_gain * Decimal(index)
        spread = Decimal("2") if index < count - 14 else Decimal("0.50")
        volume = Decimal("1000000")
        if count - 8 <= index < count - 3:
            volume = Decimal("500000")
        elif index >= count - 3:
            volume = Decimal("2000000")
        output.append(
            PriceBar(
                timestamp=latest - timedelta(days=count - index - 1),
                open=close - Decimal("0.10"),
                high=close + spread,
                low=close - spread,
                close=close,
                volume=volume,
            )
        )
    return tuple(output)


def _flat_bars(price: Decimal) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            timestamp=bar.timestamp,
            open=price,
            high=price + Decimal("0.10"),
            low=price - Decimal("0.10"),
            close=price,
            volume=bar.volume,
        )
        for bar in _bars()
    )


def _result_for(
    symbol: str = "AAA",
    *,
    market: str = "US",
    bars: tuple[PriceBar, ...] | None = None,
) -> CandidateRankingBatch:
    metadata = _metadata(symbol, market=market)
    key = (market, symbol)
    return CandidateRanker().rank(
        (metadata,),
        {
            key: bars
            or _bars(base=Decimal("2000") if market == "KR" else Decimal("100"))
        },
        as_of=_NOW,
        allowed_markets=frozenset({market}),
    )


def test_ranking_is_deterministic_and_uses_symbol_as_tie_breaker() -> None:
    first = _metadata("BBB")
    second = _metadata("AAA")
    histories = {first.key: _bars(), second.key: _bars()}
    ranker = CandidateRanker()

    forward = ranker.rank(
        (first, second),
        histories,
        as_of=_NOW,
        allowed_markets=frozenset({"US"}),
    )
    reverse = ranker.rank(
        (second, first),
        histories,
        as_of=_NOW,
        allowed_markets=frozenset({"US"}),
    )

    assert [item.symbol for item in forward.ranked] == ["AAA", "BBB"]
    assert [item.symbol for item in reverse.ranked] == ["AAA", "BBB"]
    assert [item.total_score for item in forward.ranked] == [
        item.total_score for item in reverse.ranked
    ]


def test_factor_contract_covers_breakout_inputs_and_penalties() -> None:
    result = _result_for().ranked[0]
    factors = {item.code: item for item in result.factor_scores}
    penalties = {item.code: item for item in result.penalties}

    assert set(factors) == {
        "liquidity",
        "relative_strength",
        "momentum_20",
        "momentum_60",
        "momentum_120",
        "high_distance_20",
        "high_distance_52_week",
        "higher_high_higher_low",
        "atr_compression",
        "pre_breakout_volume_contraction",
        "recent_volume_expansion",
        "weekly_trend",
    }
    assert set(penalties) == {
        "breakout_overextension",
        "abnormal_volume_hangover",
    }
    assert factors["momentum_20"].raw_value > 0
    assert factors["atr_compression"].score > 0
    assert factors["pre_breakout_volume_contraction"].score > 0
    assert factors["recent_volume_expansion"].score > 0
    assert any(item.code == "high_52_week" for item in result.evidence)


def test_overextension_and_abnormal_volume_hangover_reduce_score() -> None:
    baseline = list(_bars())
    prior_high = max(bar.high for bar in baseline[-21:-1])
    extended_close = prior_high + Decimal("25")
    baseline[-2] = replace(baseline[-2], volume=Decimal("10000000"))
    baseline[-1] = replace(
        baseline[-1],
        open=extended_close - Decimal("1"),
        high=extended_close + Decimal("1"),
        low=extended_close - Decimal("2"),
        close=extended_close,
        volume=Decimal("2000000"),
    )

    result = _result_for(bars=tuple(baseline)).ranked[0]
    penalties = {item.code: item for item in result.penalties}

    assert penalties["breakout_overextension"].deduction > 0
    assert penalties["abnormal_volume_hangover"].deduction > 0


def test_benchmark_relative_strength_is_used_when_supplied() -> None:
    metadata = _metadata("AAA")
    result = (
        CandidateRanker()
        .rank(
            (metadata,),
            {metadata.key: _bars()},
            as_of=_NOW,
            benchmark_returns_60={
                "US": BenchmarkReturn(
                    market="US",
                    return_60=Decimal("0.01"),
                    data_as_of=_NOW - timedelta(hours=1),
                )
            },
            allowed_markets=frozenset({"US"}),
        )
        .ranked[0]
    )

    source = next(
        item for item in result.evidence if item.code == "relative_strength_source"
    )
    relative_strength = next(
        item for item in result.factor_scores if item.code == "relative_strength"
    )
    momentum_60 = next(
        item for item in result.factor_scores if item.code == "momentum_60"
    )
    assert source.value == "benchmark_excess_60_session_return"
    assert relative_strength.raw_value == momentum_60.raw_value - Decimal("0.010000")


def test_future_benchmark_is_ignored_without_lookahead() -> None:
    metadata = _metadata("AAA")
    result = (
        CandidateRanker()
        .rank(
            (metadata,),
            {metadata.key: _bars()},
            as_of=_NOW,
            allowed_markets=frozenset({"US"}),
            benchmark_returns_60={
                "US": BenchmarkReturn(
                    market="US",
                    return_60=Decimal("9"),
                    data_as_of=_NOW + timedelta(seconds=1),
                )
            },
        )
        .ranked[0]
    )

    source = next(
        item for item in result.evidence if item.code == "relative_strength_source"
    )
    assert source.value == "cross_sectional_60_session_percentile"


@pytest.mark.parametrize(
    ("metadata", "bars", "reason"),
    [
        (_metadata("SHORT"), _bars(count=251), "insufficient_history"),
        (
            _metadata("CHEAP"),
            _flat_bars(Decimal("1")),
            "minimum_price",
        ),
        (
            _metadata("ILLIQUID", turnover="99999"),
            _bars(),
            "minimum_turnover",
        ),
        (
            _metadata("BROKEN"),
            (
                *_bars()[:-1],
                replace(
                    _bars()[-1],
                    high=_bars()[-1].close - Decimal("1"),
                ),
            ),
            "invalid_ohlcv",
        ),
        (
            _metadata("NAIVE"),
            (
                *_bars()[:-1],
                replace(
                    _bars()[-1],
                    timestamp=datetime(2026, 8, 29, 19, 0),
                ),
            ),
            "invalid_bar_timestamp",
        ),
    ],
)
def test_hard_filters_fail_closed(
    metadata: CandidateMetadata,
    bars: tuple[PriceBar, ...],
    reason: str,
) -> None:
    batch = CandidateRanker().rank(
        (metadata,),
        {metadata.key: bars},
        as_of=_NOW,
        allowed_markets=frozenset({"US"}),
    )

    assert batch.ranked == ()
    assert batch.excluded[0].exclusion_reason == reason


def test_stale_and_future_bars_are_rejected_without_truncating_lookahead() -> None:
    stale = _metadata("STALE")
    future = _metadata("FUTURE")
    stale_bars = _bars(latest=_NOW - timedelta(days=8))
    future_bars = (
        *_bars()[:-1],
        replace(_bars()[-1], timestamp=_NOW + timedelta(seconds=1)),
    )

    batch = CandidateRanker().rank(
        (future, stale),
        {future.key: future_bars, stale.key: stale_bars},
        as_of=_NOW,
        allowed_markets=frozenset({"US"}),
    )

    assert batch.ranked == ()
    assert {item.symbol: item.exclusion_reason for item in batch.excluded} == {
        "FUTURE": "future_bar",
        "STALE": "stale_bar",
    }


def test_held_candidates_survive_universe_and_strategy_caps() -> None:
    candidates = (
        _metadata("AAA"),
        _metadata("BBB"),
        _metadata("CCC"),
        _metadata("HELD", held=True),
    )
    capped = cap_candidate_universe(candidates, limit=2)
    batch = CandidateRanker().rank(
        capped,
        {candidate.key: _bars() for candidate in capped},
        as_of=_NOW,
        allowed_markets=frozenset({"US"}),
    )
    reviewed = batch.for_strategy_review(1)

    assert [item.symbol for item in capped] == ["AAA", "BBB", "HELD"]
    assert "HELD" in {item.symbol for item in reviewed}


def test_market_filter_excludes_buy_review_but_retains_held_identity() -> None:
    kr = _metadata("005930", market="KR", turnover="500000000")
    held_us = _metadata("AAPL", held=True)
    batch = CandidateRanker().rank(
        (held_us, kr),
        {
            kr.key: _bars(base=Decimal("70000")),
            held_us.key: _bars(),
        },
        as_of=_NOW,
        allowed_markets=frozenset({"KR"}),
    )

    assert [item.symbol for item in batch.ranked] == ["005930"]
    excluded = batch.excluded[0]
    assert excluded.symbol == "AAPL"
    assert excluded.exclusion_reason == "market_not_allowed"
    assert excluded.is_held is True


def test_rank_evidence_is_timezone_safe_and_complete() -> None:
    result = _result_for().ranked[0]
    evidence = result.as_evidence()

    assert evidence["dataAsOf"] == "2026-08-29T19:00:00Z"
    assert evidence["validUntil"] == "2026-08-30T20:00:00Z"
    assert evidence["exclusionReason"] is None
    assert evidence["rankPosition"] == 1
    assert evidence["atr14"] is not None
    assert evidence["averageVolume20"] is not None
    assert evidence["averageTurnover20"] is not None
    assert evidence["rankedTotal"] == 1
    assert evidence["candidateSources"] == ["test_screener"]
    assert evidence["factorScores"]
    assert evidence["penalties"]


@pytest.mark.asyncio
async def test_candidate_universe_order_and_owner_scoped_holdings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vertical_slice.watchlist_service,
        "list_items",
        AsyncMock(
            return_value=SimpleNamespace(
                items=[
                    SimpleNamespace(symbol="MSFT", market="US", name="Microsoft"),
                    SimpleNamespace(
                        symbol="005930",
                        market="KRX",
                        name="Samsung Electronics",
                    ),
                ]
            )
        ),
    )
    live_candidate = TradingCandidate(
        symbol="051910",
        market="KRX",
        name="LG Chem",
        source="tvscreener_kr",
        turnover=Decimal("900000000"),
        volume=Decimal("100000"),
    )
    live_loader = AsyncMock(return_value=(live_candidate,))
    monkeypatch.setattr(
        vertical_slice,
        "_load_live_kr_candidates",
        live_loader,
    )
    live_us_candidate = TradingCandidate(
        symbol="TSLA",
        market="US",
        name="Tesla",
        source="tvscreener_us",
    )
    live_us_loader = AsyncMock(return_value=(live_us_candidate,))
    monkeypatch.setattr(
        vertical_slice,
        "_load_live_us_candidates",
        live_us_loader,
    )

    holdings_result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                symbol="000660",
                instrument_type=InstrumentType.equity_kr,
            )
        ]
    )
    kr_snapshot_result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                symbol="035420",
                daily_turnover=Decimal("800000000"),
                daily_volume=100000,
            )
        ]
    )
    us_snapshot_result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                symbol="AAPL",
                daily_turnover=Decimal("7000000"),
                daily_volume=200000,
            )
        ]
    )
    db = MagicMock()
    db.scalars = AsyncMock(
        side_effect=[
            holdings_result,
            kr_snapshot_result,
            us_snapshot_result,
        ]
    )
    db.scalar = AsyncMock(
        side_effect=[
            date(2026, 8, 29),
            date(2026, 8, 29),
        ]
    )
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(all=lambda: [("035420", "NAVER")]),
            SimpleNamespace(all=lambda: [("AAPL", "Apple")]),
        ]
    )
    instance = AIRecommendationVerticalSlice(db, MagicMock(), now=_NOW)

    candidates = await instance._load_candidates(  # noqa: SLF001
        41,
        currency="KRW",
        allowed_markets=frozenset({"KR", "US"}),
    )

    assert [candidate.symbol for candidate in candidates[:7]] == [
        "MSFT",
        "005930",
        "000660",
        "035420",
        "AAPL",
        "051910",
        "TSLA",
    ]
    assert candidates[0].is_watchlisted is True
    assert candidates[2].is_held is True
    assert candidates[3].turnover == Decimal("800000000")
    assert candidates[4].turnover == Decimal("7000000")
    assert live_loader.await_count == 1
    assert live_us_loader.await_count == 1
    holdings_statement = db.scalars.await_args_list[0].args[0]
    assert 41 in holdings_statement.compile().params.values()
