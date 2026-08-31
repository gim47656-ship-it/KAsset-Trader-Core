from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.automation.benchmark_relative_strength import (
    benchmark_symbol_for_exchange,
    compute_benchmark_return_60,
    load_candidate_benchmark_returns,
)
from app.extensions.kasset.automation.candidate_ranker import (
    BenchmarkReturn,
    CandidateMetadata,
    CandidateRanker,
)
from app.extensions.kasset.automation.contracts import PriceBar
from app.services.daily_candles.repository import DailyCandleRow, MarketKey

_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


def _benchmark_rows(
    symbol: str,
    *,
    first: Decimal = Decimal("100"),
    last: Decimal = Decimal("110"),
    latest: datetime = _NOW - timedelta(hours=1),
) -> tuple[DailyCandleRow, ...]:
    rows: list[DailyCandleRow] = []
    for index in range(61):
        close = first + (last - first) * Decimal(index) / Decimal("60")
        rows.append(
            DailyCandleRow(
                time_utc=latest - timedelta(days=60 - index),
                symbol=symbol,
                partition="KRX" if symbol != "SPY" else "NYSE",
                open=float(close),
                high=float(close),
                low=float(close),
                close=float(close),
                adj_close=float(close) if symbol == "SPY" else None,
                volume=1.0,
                value=1.0,
                source="test",
            )
        )
    return tuple(rows)


def _candidate_bars(
    *, latest: datetime = _NOW - timedelta(hours=1)
) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            timestamp=latest - timedelta(days=259 - index),
            open=Decimal("1990") + Decimal(index),
            high=Decimal("2001") + Decimal(index),
            low=Decimal("1989") + Decimal(index),
            close=Decimal("2000") + Decimal(index),
            volume=Decimal("1000000"),
        )
        for index in range(260)
    )


def _metadata(symbol: str, market: str) -> CandidateMetadata:
    return CandidateMetadata(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        sources=("test",),
        screener_turnover=Decimal("500000000"),
    )


@pytest.mark.parametrize(
    ("exchange", "expected"),
    (("KOSPI", "KOSPI"), ("kosdaq", "KOSDAQ")),
)
def test_kr_exchange_maps_to_own_benchmark(exchange: str, expected: str) -> None:
    assert benchmark_symbol_for_exchange(exchange) == expected


@pytest.mark.asyncio
async def test_runtime_loader_fetches_kospi_kosdaq_and_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [
                    ("005930", "KOSPI"),
                    ("035900", "KOSDAQ"),
                ]
            )
        )
    )
    fetch_recent_batch = AsyncMock(
        side_effect=[
            {
                "KOSPI": _benchmark_rows("KOSPI"),
                "KOSDAQ": _benchmark_rows("KOSDAQ"),
            },
            {"SPY": _benchmark_rows("SPY")},
        ]
    )
    monkeypatch.setattr(
        "app.extensions.kasset.automation.benchmark_relative_strength."
        "DailyCandlesRepository.fetch_recent_batch",
        fetch_recent_batch,
    )

    loaded = await load_candidate_benchmark_returns(
        db,  # type: ignore[arg-type]
        (("KR", "005930"), ("KR", "035900"), ("US", "AAPL")),
        as_of=_NOW,
        maximum_age=timedelta(days=7),
    )

    assert {key: value.benchmark_symbol for key, value in loaded.items()} == {
        ("KR", "005930"): "KOSPI",
        ("KR", "035900"): "KOSDAQ",
        ("US", "AAPL"): "SPY",
    }
    assert fetch_recent_batch.await_args_list[0].kwargs == {
        "market": MarketKey.KR,
        "symbols": ["KOSDAQ", "KOSPI"],
        "partition": "KRX",
        "count": 61,
    }
    assert fetch_recent_batch.await_args_list[1].kwargs == {
        "market": MarketKey.US,
        "symbols": ["SPY"],
        "partition": None,
        "count": 61,
    }


def test_us_candidate_uses_spy_benchmark() -> None:
    metadata = _metadata("AAPL", "US")
    benchmark = BenchmarkReturn(
        market="US",
        return_60=Decimal("0.10"),
        data_as_of=_NOW - timedelta(hours=1),
        benchmark_symbol="SPY",
    )

    result = (
        CandidateRanker()
        .rank(
            (metadata,),
            {metadata.key: _candidate_bars()},
            as_of=_NOW,
            allowed_markets=frozenset({"US"}),
            benchmark_returns_60_by_candidate={metadata.key: benchmark},
        )
        .ranked[0]
    )

    evidence = {item.code: item.value for item in result.evidence}
    assert evidence["relative_strength_source"] == (
        "benchmark_excess_60_session_return"
    )
    assert evidence["relative_strength_benchmark"] == "SPY"


def test_kospi_and_kosdaq_candidates_keep_distinct_benchmarks() -> None:
    kospi = _metadata("005930", "KR")
    kosdaq = _metadata("035900", "KR")
    histories = {kospi.key: _candidate_bars(), kosdaq.key: _candidate_bars()}

    ranked = (
        CandidateRanker()
        .rank(
            (kospi, kosdaq),
            histories,
            as_of=_NOW,
            allowed_markets=frozenset({"KR"}),
            benchmark_returns_60_by_candidate={
                kospi.key: BenchmarkReturn(
                    market="KR",
                    return_60=Decimal("0.01"),
                    data_as_of=_NOW - timedelta(hours=1),
                    benchmark_symbol="KOSPI",
                ),
                kosdaq.key: BenchmarkReturn(
                    market="KR",
                    return_60=Decimal("0.03"),
                    data_as_of=_NOW - timedelta(hours=1),
                    benchmark_symbol="KOSDAQ",
                ),
            },
        )
        .ranked
    )

    by_symbol = {item.symbol: item for item in ranked}
    kospi_evidence = {item.code: item.value for item in by_symbol["005930"].evidence}
    kosdaq_evidence = {item.code: item.value for item in by_symbol["035900"].evidence}
    assert kospi_evidence["relative_strength_benchmark"] == "KOSPI"
    assert kosdaq_evidence["relative_strength_benchmark"] == "KOSDAQ"
    kospi_rs = next(
        item
        for item in by_symbol["005930"].factor_scores
        if item.code == "relative_strength"
    )
    kosdaq_rs = next(
        item
        for item in by_symbol["035900"].factor_scores
        if item.code == "relative_strength"
    )
    assert kospi_rs.raw_value - kosdaq_rs.raw_value == Decimal("0.020000")


def test_partial_market_benchmark_batch_uses_one_cross_sectional_scale() -> None:
    kospi = _metadata("005930", "KR")
    kosdaq = _metadata("035900", "KR")

    ranked = CandidateRanker().rank(
        (kospi, kosdaq),
        {kospi.key: _candidate_bars(), kosdaq.key: _candidate_bars()},
        as_of=_NOW,
        allowed_markets=frozenset({"KR"}),
        benchmark_returns_60_by_candidate={
            kospi.key: BenchmarkReturn(
                market="KR",
                return_60=Decimal("0.01"),
                data_as_of=_NOW - timedelta(hours=1),
                benchmark_symbol="KOSPI",
            )
        },
    )

    assert {
        next(
            evidence.value
            for evidence in item.evidence
            if evidence.code == "relative_strength_source"
        )
        for item in ranked.ranked
    } == {"cross_sectional_60_session_percentile"}
    assert all(
        not any(
            evidence.code == "relative_strength_benchmark" for evidence in item.evidence
        )
        for item in ranked.ranked
    )


def test_stale_benchmark_fails_closed() -> None:
    result = compute_benchmark_return_60(
        _benchmark_rows("KOSPI", latest=_NOW - timedelta(days=7)),
        market="KR",
        benchmark_symbol="KOSPI",
        as_of=_NOW,
        maximum_age=timedelta(days=7),
    )
    assert result is None


def test_future_benchmark_fails_closed() -> None:
    result = compute_benchmark_return_60(
        _benchmark_rows("KOSDAQ", latest=_NOW + timedelta(minutes=1)),
        market="KR",
        benchmark_symbol="KOSDAQ",
        as_of=_NOW,
        maximum_age=timedelta(days=7),
    )
    assert result is None


def test_missing_benchmark_uses_cross_sectional_fallback() -> None:
    metadata = _metadata("005930", "KR")
    result = (
        CandidateRanker()
        .rank(
            (metadata,),
            {metadata.key: _candidate_bars()},
            as_of=_NOW,
            allowed_markets=frozenset({"KR"}),
        )
        .ranked[0]
    )
    evidence = {item.code: item.value for item in result.evidence}
    assert evidence["relative_strength_source"] == (
        "cross_sectional_60_session_percentile"
    )
    assert "relative_strength_benchmark" not in evidence


def test_benchmark_evidence_records_identity_return_and_timestamp() -> None:
    metadata = _metadata("005930", "KR")
    data_as_of = _NOW - timedelta(hours=1)
    result = (
        CandidateRanker()
        .rank(
            (metadata,),
            {metadata.key: _candidate_bars()},
            as_of=_NOW,
            allowed_markets=frozenset({"KR"}),
            benchmark_returns_60_by_candidate={
                metadata.key: BenchmarkReturn(
                    market="KR",
                    return_60=Decimal("0.012345"),
                    data_as_of=data_as_of,
                    benchmark_symbol="KOSPI",
                )
            },
        )
        .ranked[0]
    )
    evidence = {item.code: item.value for item in result.evidence}
    assert evidence["relative_strength_benchmark"] == "KOSPI"
    assert evidence["relative_strength_benchmark_return_60"] == "0.012345"
    assert evidence["relative_strength_benchmark_data_as_of"] == (
        data_as_of.isoformat().replace("+00:00", "Z")
    )
