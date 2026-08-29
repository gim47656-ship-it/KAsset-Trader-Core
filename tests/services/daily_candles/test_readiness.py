from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.us_symbol_universe import USSymbolUniverse
from app.services.daily_candles import readiness as readiness_module
from app.services.daily_candles.readiness import DailyCandlesReadinessService

_AS_OF = datetime(2026, 1, 6, 12, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows

    def one(self) -> dict[str, object]:
        assert len(self._rows) == 1
        return self._rows[0]


class _ReadOnlySession:
    def __init__(
        self,
        *,
        markets: dict[str, list[dict[str, object]]],
        benchmarks: dict[str, dict[str, object]],
    ) -> None:
        self._markets = markets
        self._benchmarks = benchmarks
        self.statements: list[str] = []
        self.parameters: list[dict[str, object]] = []

    async def execute(
        self, statement: object, parameters: dict[str, object] | None = None
    ) -> _Rows:
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(dict(parameters or {}))
        for market in ("kr", "us"):
            if f"daily_candles_readiness:market:{market}" in sql:
                return _Rows(self._markets.get(market, []))
            if f"daily_candles_readiness:benchmark:{market}" in sql:
                return _Rows([self._benchmarks.get(market, _benchmark())])
        raise AssertionError(f"unexpected statement: {sql}")


def _sessions(count: int = 252) -> tuple[date, ...]:
    start = date(2025, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _symbol(
    symbol: str,
    *,
    sessions: tuple[date, ...],
    active: bool = True,
    bar_count: int = 252,
    observed_expected: int | None = None,
    latest_day: date | None = None,
    listing_status: str | None = "listed",
    list_date: date | None = date(2020, 1, 1),
    delist_date: date | None = None,
    future: int = 0,
    duplicate: int = 0,
    ohlc: int = 0,
    candle_rows: int | None = None,
    fallback_only: bool = False,
    corporate_suspect: int = 0,
    missing_adjustment: int = 0,
) -> dict[str, object]:
    latest_day = latest_day or sessions[-1]
    if observed_expected is None:
        observed_expected = min(bar_count, len(sessions))
    if candle_rows is None:
        candle_rows = bar_count
    return {
        "symbol": symbol,
        "is_active": active,
        "listing_status": listing_status,
        "list_date": list_date,
        "delist_date": delist_date,
        "bar_count": bar_count,
        "observed_expected_session_count": observed_expected,
        "first_bar_at": datetime(2020, 1, 1, tzinfo=UTC) if bar_count else None,
        "latest_bar_at": datetime.combine(latest_day, datetime.min.time(), tzinfo=UTC)
        if bar_count
        else None,
        "future_bar_count": future,
        "duplicate_timestamp_count": duplicate,
        "ohlc_anomaly_count": ohlc,
        "candle_row_count": candle_rows,
        "has_primary_source": not fallback_only,
        "fallback_only": fallback_only,
        "corporate_action_suspect_count": corporate_suspect,
        "missing_adjusted_close_count": missing_adjustment,
    }


def _benchmark(
    *,
    count: int = 0,
    source: str | None = None,
) -> dict[str, object]:
    return {
        "start_at": datetime(2025, 1, 1, tzinfo=UTC) if count else None,
        "end_at": datetime(2025, 12, 31, tzinfo=UTC) if count else None,
        "bar_count": count,
        "sources": source,
    }


async def _measure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    market: str,
    rows: list[dict[str, object]],
    benchmark: dict[str, object] | None = None,
    sessions: tuple[date, ...] | None = None,
) -> tuple[Any, _ReadOnlySession]:
    expected = _sessions() if sessions is None else sessions
    monkeypatch.setattr(
        readiness_module,
        "_completed_expected_sessions",
        lambda selected_market, as_of: expected,
    )
    db = _ReadOnlySession(
        markets={market: rows},
        benchmarks={market: benchmark or _benchmark()},
    )
    result = await DailyCandlesReadinessService(db).measure(
        as_of=_AS_OF,
        markets=(market,),  # type: ignore[arg-type]
    )
    return result, db


@pytest.mark.asyncio
async def test_empty_market_fails_closed_with_explicit_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, db = await _measure(monkeypatch, market="us", rows=[])
    market = result.for_market("us")

    assert market.total_symbol_count == 0
    assert market.active_symbol_count == 0
    assert market.inactive_symbol_count == 0
    assert market.symbols_with_at_least_252_bars == 0
    assert market.eligible_symbol_count == 0
    assert market.benchmark.count == 0
    assert market.benchmark.status == "unavailable"
    assert market.corporate_action_status == "unknown"
    assert market.point_in_time_available is False
    assert market.includes_delisted is False
    assert result.promotion_ready is False
    assert {
        "us:empty_universe",
        "us:eligible_symbols_zero",
        "us:benchmark_unavailable",
        "us:point_in_time_unavailable",
        "us:delisted_not_included",
    }.issubset(result.blockers)
    assert len(db.statements) == 2
    assert all(
        forbidden not in sql.upper()
        for sql in db.statements
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "MERGE ")
    )


@pytest.mark.asyncio
async def test_251_252_history_boundary_is_reported_without_lowering_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    rows = [
        _symbol(
            "BOUNDARY251",
            sessions=sessions,
            bar_count=251,
            observed_expected=251,
        ),
        _symbol("BOUNDARY252", sessions=sessions, bar_count=252),
    ]
    result, db = await _measure(
        monkeypatch,
        market="kr",
        rows=rows,
        benchmark=_benchmark(count=60, source="kis"),
        sessions=sessions,
    )
    market = result.for_market("kr")

    assert result.required_history_bars == 252
    assert market.symbols_with_exactly_251_bars == 1
    assert market.symbols_with_at_least_252_bars == 1
    assert market.eligible_symbol_count == 1
    assert market.missing_expected_trading_day_count == 1
    assert "kr:insufficient_history" in market.blockers
    assert "kr:missing_expected_trading_days" in market.blockers
    assert market.benchmark.symbol == "KOSPI"
    assert db.parameters[-1]["symbol"] == "KOSPI"


@pytest.mark.asyncio
async def test_stale_future_cross_partition_duplicate_and_ohlc_anomalies_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    row = _symbol(
        "ANOMALY",
        sessions=sessions,
        observed_expected=251,
        latest_day=sessions[-2],
        future=1,
        duplicate=2,
        ohlc=3,
    )
    result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=[row],
        benchmark=_benchmark(count=60, source="kis"),
        sessions=sessions,
    )
    market = result.for_market("us")

    assert market.stale_bar_count == 1
    assert market.future_bar_count == 1
    assert market.duplicate_timestamp_count == 2
    assert market.ohlc_anomaly_count == 3
    assert market.missing_expected_trading_day_count == 1
    assert market.eligible_symbol_count == 0
    assert {
        "us:stale_bar",
        "us:future_bar",
        "us:duplicate_bar_timestamp",
        "us:invalid_ohlcv",
        "us:missing_expected_trading_days",
    }.issubset(market.blockers)


@pytest.mark.asyncio
async def test_calendar_unavailable_is_not_reported_as_zero_missing_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    row = _symbol("CALENDAR", sessions=sessions)
    result, _ = await _measure(
        monkeypatch,
        market="kr",
        rows=[row],
        benchmark=_benchmark(count=60, source="kis"),
        sessions=(),
    )
    market = result.for_market("kr")

    assert market.calendar_status == "unavailable"
    assert market.missing_expected_trading_day_count is None
    assert "kr:calendar_unavailable" in market.blockers


@pytest.mark.asyncio
async def test_inactive_delisted_bars_prove_delisted_inclusion_and_pit_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    rows = [
        _symbol("ACTIVE", sessions=sessions),
        _symbol(
            "OLDCO",
            sessions=sessions,
            active=False,
            bar_count=180,
            listing_status="delisted",
            list_date=date(2010, 1, 1),
            delist_date=date(2024, 6, 30),
        ),
    ]
    result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=rows,
        benchmark=_benchmark(count=60, source="kis"),
        sessions=sessions,
    )
    market = result.for_market("us")

    assert market.inactive_symbol_count == 1
    assert market.inactive_with_candles_count == 1
    assert market.delisted_symbol_count == 1
    assert market.delisted_with_candles_count == 1
    assert market.list_date_covered_symbol_count == 2
    assert market.delist_date_covered_inactive_count == 1
    assert market.point_in_time_available is True
    assert market.includes_delisted is True
    assert market.corporate_action_status == "clear"
    assert "us:point_in_time_unavailable" not in market.blockers
    assert "us:delisted_not_included" not in market.blockers


@pytest.mark.asyncio
async def test_corporate_action_unknown_and_suspected_remain_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    unknown_result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=[
            _symbol(
                "NOADJ",
                sessions=sessions,
                missing_adjustment=252,
            )
        ],
        benchmark=_benchmark(count=60, source="kis"),
        sessions=sessions,
    )
    unknown = unknown_result.for_market("us")
    assert unknown.corporate_action_status == "unknown"
    assert "us:corporate_action_unknown" in unknown.blockers

    suspected_result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=[
            _symbol(
                "SPLIT",
                sessions=sessions,
                corporate_suspect=1,
            )
        ],
        benchmark=_benchmark(count=60, source="kis"),
        sessions=sessions,
    )
    suspected = suspected_result.for_market("us")
    assert suspected.corporate_action_status == "suspected"
    assert "us:corporate_action_suspected" in suspected.blockers


@pytest.mark.asyncio
async def test_spy_requires_durable_60_bar_history_not_a_current_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    row = _symbol("AAPL", sessions=sessions)

    missing_result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=[row],
        benchmark=_benchmark(),
        sessions=sessions,
    )
    missing = missing_result.for_market("us").benchmark
    assert missing.symbol == "SPY"
    assert missing.count == 0
    assert missing.start is None
    assert missing.end is None
    assert missing.source is None
    assert missing.status == "unavailable"
    assert "us:benchmark_unavailable" in missing_result.blockers

    present_result, session = await _measure(
        monkeypatch,
        market="us",
        rows=[row],
        benchmark=_benchmark(count=60, source="yahoo_fallback"),
        sessions=sessions,
    )
    present = present_result.for_market("us").benchmark
    assert present.symbol == "SPY"
    assert present.count == 60
    assert present.start == datetime(2025, 1, 1, tzinfo=UTC)
    assert present.end == datetime(2025, 12, 31, tzinfo=UTC)
    assert present.source == "yahoo_fallback"
    assert present.sources == ("yahoo_fallback",)
    assert present.status == "available"
    assert "us:benchmark_unavailable" not in present_result.blockers
    assert session.parameters[-1]["symbol"] == "SPY"


@pytest.mark.asyncio
async def test_fallback_only_is_explicit_evidence_not_primary_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        rows=[_symbol("FALLBACK", sessions=sessions, fallback_only=True)],
        benchmark=_benchmark(count=60, source="yahoo_fallback"),
        sessions=sessions,
    )
    market = result.for_market("us")

    assert market.fallback_only is True
    assert "us:fallback_only" in market.reasons


@pytest.mark.asyncio
async def test_multiple_partitions_at_one_symbol_time_are_semantic_duplicates(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = f"RD{uuid.uuid4().hex[:12].upper()}"
    timestamp = datetime(2026, 1, 5, tzinfo=UTC)
    monkeypatch.setattr(
        readiness_module,
        "_completed_expected_sessions",
        lambda market, as_of: (timestamp.date(),),
    )
    db_session.add(
        USSymbolUniverse(
            symbol=symbol,
            exchange="NASD",
            name_kr="",
            name_en="Readiness duplicate",
            is_active=True,
            listing_status="listed",
            list_date=date(2020, 1, 1),
        )
    )
    await db_session.flush()
    try:
        for exchange in ("NASD", "NYSE"):
            await db_session.execute(
                text(
                    """
                    INSERT INTO public.us_candles_1d (
                        time, symbol, exchange, open, high, low, close,
                        adj_close, volume, value, source
                    ) VALUES (
                        :time, :symbol, :exchange, 100, 110, 90, 105,
                        105, 10, 1050, 'kis'
                    )
                    """
                ),
                {"time": timestamp, "symbol": symbol, "exchange": exchange},
            )
        await db_session.flush()

        result = await DailyCandlesReadinessService(db_session).measure(
            as_of=_AS_OF,
            markets=("us",),
        )
        market = result.for_market("us")

        assert market.duplicate_timestamp_count >= 1
        assert "us:duplicate_bar_timestamp" in market.blockers
    finally:
        await db_session.rollback()
