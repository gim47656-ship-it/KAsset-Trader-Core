from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.services.daily_candles import readiness as readiness_module
from app.services.daily_candles.readiness import DailyCandlesReadinessService

_AS_OF = datetime(2026, 1, 6, 12, tzinfo=UTC)
_ACTION_SPECS = (
    (
        "face_value_change",
        "/uapi/domestic-stock/v1/ksdinfo/rev-split",
        "HHKDB669105C0",
    ),
    (
        "paid_in_capital_increase",
        "/uapi/domestic-stock/v1/ksdinfo/paidin-capin",
        "HHKDB669100C0",
    ),
    (
        "bonus_issue",
        "/uapi/domestic-stock/v1/ksdinfo/bonus-issue",
        "HHKDB669101C0",
    ),
    (
        "dividend",
        "/uapi/domestic-stock/v1/ksdinfo/dividend",
        "HHKDB669102C0",
    ),
)


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _ReadOnlySession:
    def __init__(
        self,
        *,
        cohorts: dict[str, dict[str, object] | None],
        members: dict[str, list[dict[str, object]]],
        benchmarks: dict[str, list[dict[str, object]]],
        coverage: list[dict[str, object]] | None = None,
        sessions: list[dict[str, object]] | None = None,
    ) -> None:
        self._cohorts = cohorts
        self._members = members
        self._benchmarks = benchmarks
        self._coverage = coverage or []
        self._sessions = sessions or []
        self.statements: list[str] = []
        self.parameters: list[dict[str, object]] = []

    async def execute(
        self, statement: object, parameters: dict[str, object] | None = None
    ) -> _Rows:
        sql = str(statement)
        params = dict(parameters or {})
        self.statements.append(sql)
        self.parameters.append(params)
        for market in ("kr", "us"):
            if f"daily_candles_readiness:cohort:{market}" in sql:
                row = self._cohorts.get(market)
                return _Rows([row] if row is not None else [])
            if f"daily_candles_readiness:sessions:{market}" in sql:
                return _Rows(self._sessions)
            if f"daily_candles_readiness:market:{market}" in sql:
                return _Rows(self._members.get(market, []))
            if f"daily_candles_readiness:benchmark:{market}" in sql:
                return _Rows(self._benchmarks.get(market, []))
        if "daily_candles_readiness:corporate_actions:kr" in sql:
            return _Rows(self._coverage)
        raise AssertionError(f"unexpected statement: {sql}")


def _sessions(count: int = 252) -> tuple[date, ...]:
    start = date(2025, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _session_rows(
    sessions: tuple[date, ...],
    *,
    member_count: int = 1,
    benchmark_count: int = 1,
    absent: tuple[date, ...] = (),
    benchmark_only: tuple[date, ...] = (),
) -> list[dict[str, object]]:
    """Per-session cohort observation rows the readiness probe reads."""

    rows: list[dict[str, object]] = []
    for session_day in sessions:
        if session_day in absent:
            continue
        rows.append(
            {
                "session_date": session_day,
                "member_symbol_count": (
                    0 if session_day in benchmark_only else member_count
                ),
                "benchmark_symbol_count": benchmark_count,
            }
        )
    return rows


def _cohort(
    market: str,
    *,
    cohort_id: str | None = None,
    scope: str = "historical_pit",
    requested_size: int = 1,
    effective_date: date = date(2024, 1, 1),
) -> dict[str, object]:
    return {
        "cohort_id": cohort_id or f"{market}-cohort",
        "market": market,
        "selection_as_of": datetime(2024, 1, 2, tzinfo=UTC),
        "selection_date": date(2024, 1, 2),
        "effective_date": effective_date,
        "selection_method": "latest_market_cap",
        "requested_size": requested_size,
        "active_member_count": requested_size,
        "valuation_snapshot_date": date(2024, 1, 1),
        "valuation_snapshot_source": "naver_finance" if market == "kr" else "yahoo",
        "evidence_scope": scope,
    }


def _member(
    symbol: str,
    *,
    sessions: tuple[date, ...],
    rank: int = 1,
    member_kind: str = "active",
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
    fallback_only: bool = False,
    invalid_adjustment: int = 0,
) -> dict[str, object]:
    latest_day = latest_day or sessions[-1]
    if observed_expected is None:
        observed_expected = min(bar_count, len(sessions))
    return {
        "symbol": symbol,
        "member_rank": rank,
        "member_kind": member_kind,
        "market_cap": 1_000_000 - rank,
        "eligibility_facts": {"selected": True},
        "is_active": active,
        "listing_status": listing_status,
        "list_date": list_date,
        "delist_date": delist_date,
        "bar_count": bar_count,
        "observed_expected_session_count": observed_expected,
        "first_bar_at": datetime(2020, 1, 1, tzinfo=UTC) if bar_count else None,
        "latest_bar_at": (
            datetime.combine(latest_day, datetime.min.time(), tzinfo=UTC)
            if bar_count
            else None
        ),
        "future_bar_count": future,
        "duplicate_timestamp_count": duplicate,
        "ohlc_anomaly_count": ohlc,
        "candle_row_count": bar_count,
        "fallback_only": fallback_only,
        "invalid_adjustment_count": invalid_adjustment,
    }


def _delisted_member(
    symbol: str,
    *,
    sessions: tuple[date, ...],
    rank: int = 2,
    delist_date: date | None = None,
) -> dict[str, object]:
    """Cohort member kept after delisting, proving membership is not survivor-only."""
    return _member(
        symbol,
        sessions=sessions,
        rank=rank,
        active=False,
        listing_status="delisted",
        delist_date=delist_date or (sessions[-1] + timedelta(days=1)),
    )


def _benchmark(
    symbol: str,
    *,
    count: int = 61,
    source: str = "kis",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "member_rank": 1,
        "start_at": datetime(2025, 1, 1, tzinfo=UTC) if count else None,
        "end_at": datetime(2025, 12, 31, tzinfo=UTC) if count else None,
        "bar_count": count,
        "sources": source if count else None,
    }


def _coverage_rows(
    symbols: list[str],
    sessions: tuple[date, ...],
    *,
    row_count: int = 0,
) -> list[dict[str, object]]:
    return [
        {
            "symbol": symbol,
            "source": "kis_openapi",
            "provider": "KIS",
            "provider_endpoint": endpoint,
            "provider_tr_id": tr_id,
            "action_kind": action_kind,
            "requested_from_date": sessions[0],
            "requested_to_date": sessions[-1],
            "status": "success",
            "row_count": row_count,
        }
        for symbol in symbols
        for action_kind, endpoint, tr_id in _ACTION_SPECS
    ]


async def _measure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    market: str,
    members: list[dict[str, object]],
    cohort: dict[str, object] | None = None,
    benchmark: list[dict[str, object]] | None = None,
    coverage: list[dict[str, object]] | None = None,
    sessions: tuple[date, ...] | None = None,
    session_rows: list[dict[str, object]] | None = None,
    cohort_ids: dict[str, str] | None = None,
) -> tuple[Any, _ReadOnlySession]:
    candidates = _sessions() if sessions is None else sessions
    monkeypatch.setattr(
        readiness_module,
        "_completed_candidate_sessions",
        lambda selected_market, as_of: candidates,
    )
    resolved_cohort = cohort if cohort is not None else _cohort(market)
    resolved_benchmark = benchmark
    if resolved_benchmark is None:
        resolved_benchmark = [_benchmark("KOSPI" if market == "kr" else "SPY")]
    db = _ReadOnlySession(
        cohorts={market: resolved_cohort},
        members={market: members},
        benchmarks={market: resolved_benchmark},
        coverage=coverage,
        sessions=(_session_rows(candidates) if session_rows is None else session_rows),
    )
    result = await DailyCandlesReadinessService(db).measure(
        as_of=_AS_OF,
        markets=(market,),  # type: ignore[arg-type]
        cohort_ids=cohort_ids,  # type: ignore[arg-type]
    )
    return result, db


@pytest.mark.asyncio
async def test_no_cohort_fails_closed_without_querying_the_live_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        readiness_module,
        "_completed_candidate_sessions",
        lambda market, as_of: _sessions(),
    )
    db = _ReadOnlySession(
        cohorts={"us": None},
        members={"us": []},
        benchmarks={"us": []},
    )

    result = await DailyCandlesReadinessService(db).measure(
        as_of=_AS_OF,
        markets=("us",),
        cohort_ids={"us": "missing"},
    )

    market = result.for_market("us")
    assert market.cohort is None
    assert market.daily_history_ready is False
    assert result.promotion_ready is False
    assert market.daily_history_blockers == ("us:cohort_not_found",)
    assert market.historical_evidence_ready is False
    assert market.historical_evidence_blockers == ("us:cohort_not_found",)
    assert len(db.statements) == 1
    assert db.parameters[0]["cohort_id"] == "missing"


@pytest.mark.asyncio
async def test_exact_100_member_denominator_ignores_master_rows_outside_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    members = [
        _member(f"S{rank:05d}", sessions=sessions, rank=rank) for rank in range(1, 101)
    ]

    result, db = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=100),
        members=members,
        sessions=sessions,
        cohort_ids={"us": "us-cohort"},
    )

    market = result.for_market("us")
    assert market.total_symbol_count == 100
    assert market.cohort_active_member_count == 100
    assert market.eligible_symbol_count == 100
    assert market.daily_history_ready is True
    member_sql = next(
        sql for sql in db.statements if "daily_candles_readiness:market:us" in sql
    )
    assert "FROM public.kasset_research_cohort_members" in member_sql
    assert "member_kind IN ('active', 'forced')" in member_sql
    assert "FROM public.us_symbol_universe AS u" not in member_sql


@pytest.mark.asyncio
async def test_forced_missing_history_does_not_block_core_readiness_or_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=2),
        members=[
            _member("AAPL", sessions=sessions),
            _delisted_member("OLDCO", sessions=sessions, rank=2),
            _member(
                "SOXL",
                sessions=sessions,
                rank=3,
                member_kind="forced",
                bar_count=0,
                observed_expected=0,
                invalid_adjustment=1,
            ),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.total_symbol_count == 2
    assert market.cohort_active_member_count == 2
    assert market.forced_member_count == 1
    assert market.symbols_with_at_least_252_bars == 2
    assert market.eligible_symbol_count == 2
    assert market.stale_bar_count == 0
    assert market.missing_expected_trading_day_count == 0
    assert market.adjustment_covered_symbol_count == 2
    assert market.corporate_action_status == "clear"
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert result.promotion_ready is True


@pytest.mark.asyncio
async def test_forced_missing_kr_action_coverage_does_not_block_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="kr",
        cohort=_cohort("kr", requested_size=2),
        members=[
            _member("005930", sessions=sessions),
            _delisted_member("000660", sessions=sessions, rank=2),
            _member(
                "069500",
                sessions=sessions,
                rank=3,
                member_kind="forced",
                bar_count=0,
                observed_expected=0,
                invalid_adjustment=1,
            ),
        ],
        coverage=_coverage_rows(["005930", "000660"], sessions),
        sessions=sessions,
    )

    market = result.for_market("kr")
    assert market.total_symbol_count == 2
    assert market.forced_member_count == 1
    assert market.corporate_action_covered_symbol_count == 2
    assert market.corporate_action_status == "clear"
    assert market.daily_history_ready is True
    assert market.promotion_ready is True


@pytest.mark.asyncio
async def test_benchmark_is_resolved_from_the_cohort_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, db = await _measure(
        monkeypatch,
        market="us",
        members=[_member("AAPL", sessions=sessions)],
        benchmark=[_benchmark("CUSTOM-BENCH")],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.benchmark_member_count == 1
    assert market.benchmark.symbol == "CUSTOM-BENCH"
    benchmark_sql = next(
        sql for sql in db.statements if "daily_candles_readiness:benchmark:us" in sql
    )
    assert "member_kind = 'benchmark'" in benchmark_sql
    assert "SPY" not in benchmark_sql


@pytest.mark.asyncio
async def test_252_bar_threshold_and_quality_checks_are_not_weakened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member(
                "BOUNDARY",
                sessions=sessions,
                bar_count=251,
                observed_expected=251,
                latest_day=sessions[-2],
                future=1,
                duplicate=1,
                ohlc=1,
            )
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.symbols_with_exactly_251_bars == 1
    assert market.symbols_with_at_least_252_bars == 0
    assert market.eligible_symbol_count == 0
    assert {
        "us:insufficient_history",
        "us:stale_bar",
        "us:future_bar",
        "us:duplicate_bar_timestamp",
        "us:invalid_ohlcv",
        "us:missing_expected_trading_days",
    }.issubset(market.daily_history_blockers)


@pytest.mark.asyncio
async def test_ineligible_member_is_excluded_without_blocking_ready_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member("READY", sessions=sessions),
            _member(
                "SHORT",
                sessions=sessions,
                bar_count=251,
                observed_expected=251,
                latest_day=sessions[-2],
                invalid_adjustment=1,
            ),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.total_symbol_count == 2
    assert len(market.excluded_symbols) == 1
    assert market.excluded_symbols[0].symbol == "SHORT"
    assert market.excluded_symbols[0].reasons == (
        "insufficient_history",
        "stale_bar",
        "missing_expected_trading_days",
        "adjustment_coverage_incomplete",
    )
    assert market.eligible_symbol_count == 1
    assert market.eligible_symbols == ("READY",)
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert "us:insufficient_history" not in market.daily_history_blockers
    assert "us:adjustment_coverage_incomplete" not in market.daily_history_blockers


@pytest.mark.asyncio
async def test_forward_cohort_reaches_promotion_ready_with_unresolved_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort(
            "us",
            scope="forward_paper",
            effective_date=date(2025, 6, 1),
        ),
        members=[_member("AAPL", sessions=sessions)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.daily_history_ready is True
    assert result.daily_history_ready is True
    # Forward PAPER promotion only needs the obtainable evidence.
    assert market.promotion_ready is True
    assert result.promotion_ready is True
    assert market.blockers == ()
    # The historical claims stay explicitly unproven, never silently dropped.
    assert market.historical_evidence_ready is False
    assert result.historical_evidence_ready is False
    assert "us:cohort_not_historical_pit" in market.historical_evidence_blockers
    assert (
        "us:cohort_window_predates_effective_date"
        in market.historical_evidence_blockers
    )
    assert set(market.historical_evidence_blockers) <= set(market.unresolved_evidence)
    assert set(market.historical_evidence_blockers) <= set(market.reasons)


@pytest.mark.asyncio
async def test_historical_pit_label_without_delisted_evidence_is_not_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, db = await _measure(
        monkeypatch,
        market="us",
        members=[_member("AAPL", sessions=sessions)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.includes_delisted is False
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert market.historical_evidence_ready is False
    assert result.historical_evidence_ready is False
    assert "us:delisted_members_absent" in market.historical_evidence_blockers
    cohort_sql = next(
        sql for sql in db.statements if "daily_candles_readiness:cohort:us" in sql
    )
    assert db.parameters[0]["cohort_id"] is None
    assert "created_at <= :as_of" in cohort_sql
    assert "selection_as_of <= :as_of" in cohort_sql
    assert "ORDER BY selection_as_of DESC" in cohort_sql


@pytest.mark.asyncio
async def test_historical_pit_cohort_with_delisted_survivor_is_fully_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=2),
        members=[
            _member("AAPL", sessions=sessions),
            _delisted_member("OLDCO", sessions=sessions, rank=2),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.point_in_time_available is True
    assert market.includes_delisted is True
    assert market.list_date_covered_symbol_count == 2
    assert market.members_listed_after_cohort_start == 0
    assert market.delist_date_covered_inactive_count == 1
    assert market.blockers == ()
    assert market.promotion_ready is True
    assert market.historical_evidence_blockers == ()
    assert market.historical_evidence_ready is True
    assert market.unresolved_evidence == ()


@pytest.mark.asyncio
async def test_missing_list_date_blocks_history_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=2),
        members=[
            _member("AAPL", sessions=sessions, list_date=None),
            _delisted_member("OLDCO", sessions=sessions, rank=2),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.list_date_covered_symbol_count == 1
    assert market.point_in_time_available is False
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert market.historical_evidence_ready is False
    assert "us:list_date_coverage_incomplete" in market.historical_evidence_blockers
    assert "us:point_in_time_unavailable" in market.historical_evidence_blockers


@pytest.mark.asyncio
async def test_member_listed_after_cohort_start_blocks_history_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=3),
        members=[
            _member("AAPL", sessions=sessions),
            _delisted_member("OLDCO", sessions=sessions, rank=2),
            _member("NEWCO", sessions=sessions, rank=3, list_date=sessions[10]),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.members_listed_after_cohort_start == 1
    assert market.point_in_time_available is False
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert market.historical_evidence_ready is False
    assert "us:member_listed_after_cohort_start" in market.historical_evidence_blockers


@pytest.mark.asyncio
async def test_inactive_member_without_delist_date_blocks_history_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=2),
        members=[
            _member("AAPL", sessions=sessions),
            _member(
                "OLDCO",
                sessions=sessions,
                rank=2,
                active=False,
                listing_status="delisted",
                delist_date=None,
            ),
        ],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.delist_date_covered_inactive_count == 0
    assert market.inactive_symbol_count == 1
    assert market.point_in_time_available is False
    assert market.historical_evidence_ready is False
    assert "us:delist_date_coverage_incomplete" in market.historical_evidence_blockers
    assert "us:delisted_members_absent" in market.historical_evidence_blockers


@pytest.mark.asyncio
async def test_later_inactive_member_is_retained_only_after_cohort_effective_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    inactive = _member(
        "OLDCO",
        sessions=sessions,
        active=False,
        listing_status="delisted",
        delist_date=date(2025, 7, 1),
    )
    usable, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", effective_date=date(2024, 1, 1)),
        members=[inactive],
        sessions=sessions,
    )
    historical_misuse, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", effective_date=date(2025, 6, 1)),
        members=[inactive],
        sessions=sessions,
    )

    assert usable.for_market("us").includes_delisted is True
    assert historical_misuse.for_market("us").includes_delisted is False
    assert (
        "us:cohort_window_predates_effective_date"
        in historical_misuse.for_market("us").historical_evidence_blockers
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("row_count", [0, 3])
async def test_kr_success_coverage_is_clear_for_zero_or_real_action_rows(
    monkeypatch: pytest.MonkeyPatch,
    row_count: int,
) -> None:
    sessions = _sessions()
    members = [_member("005930", sessions=sessions)]
    result, _ = await _measure(
        monkeypatch,
        market="kr",
        members=members,
        coverage=_coverage_rows(["005930"], sessions, row_count=row_count),
        sessions=sessions,
    )

    market = result.for_market("kr")
    assert market.corporate_action_status == "clear"
    assert market.price_adjustment_status == "covered"
    assert market.corporate_action_covered_symbol_count == 1
    assert market.adjustment_covered_symbol_count == 1
    assert market.daily_history_ready is True
    assert "kr:corporate_action_unknown" not in market.historical_evidence_blockers
    assert "kr:corporate_action_unknown" not in market.unresolved_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "gap", "missing"])
async def test_kr_missing_action_ledger_is_unresolved_not_a_daily_blocker(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sessions = _sessions()
    coverage = _coverage_rows(["005930"], sessions)
    target = next(row for row in coverage if row["action_kind"] == "dividend")
    if failure == "failed":
        target["status"] = "failed"
    elif failure == "gap":
        target["requested_from_date"] = sessions[1]
    else:
        coverage.remove(target)

    result, _ = await _measure(
        monkeypatch,
        market="kr",
        members=[_member("005930", sessions=sessions)],
        coverage=coverage,
        sessions=sessions,
    )

    market = result.for_market("kr")
    assert market.corporate_action_status == "unknown"
    assert market.corporate_action_covered_symbol_count == 0
    # The obtainable adjusted-price evidence is intact, so the daily history is
    # usable; the missing KSD ledger is recorded, not faked and not ignored.
    assert market.price_adjustment_status == "covered"
    assert market.daily_history_ready is True
    assert "kr:corporate_action_unknown" not in market.daily_history_blockers
    assert "kr:corporate_action_unknown" in market.historical_evidence_blockers
    assert "kr:corporate_action_unknown" in market.unresolved_evidence


@pytest.mark.asyncio
async def test_kr_requires_explicit_adjusted_candle_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="kr",
        members=[_member("005930", sessions=sessions, invalid_adjustment=1)],
        coverage=_coverage_rows(["005930"], sessions),
        sessions=sessions,
    )

    market = result.for_market("kr")
    assert market.corporate_action_status == "unknown"
    assert market.adjustment_covered_symbol_count == 0
    assert market.price_adjustment_status == "incomplete"
    assert "kr:adjustment_coverage_incomplete" in market.daily_history_blockers


@pytest.mark.asyncio
async def test_us_adjusted_close_difference_is_expected_not_suspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, db = await _measure(
        monkeypatch,
        market="us",
        members=[_member("SPLIT", sessions=sessions, invalid_adjustment=0)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.corporate_action_status == "clear"
    assert market.adjustment_covered_symbol_count == 1
    member_sql = next(
        sql for sql in db.statements if "daily_candles_readiness:market:us" in sql
    )
    assert "adj_close <> close" not in member_sql
    assert "adj_close <= 0" in member_sql


@pytest.mark.asyncio
async def test_us_missing_adjusted_close_blocks_the_daily_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[_member("NOADJ", sessions=sessions, invalid_adjustment=1)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.corporate_action_status == "unknown"
    assert market.price_adjustment_status == "incomplete"
    assert "us:adjustment_coverage_incomplete" in market.daily_history_blockers
    assert "us:corporate_action_unknown" in market.historical_evidence_blockers


@pytest.mark.asyncio
async def test_fallback_only_history_never_proves_historical_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[_member("AAPL", sessions=sessions, fallback_only=True)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.daily_history_ready is True
    assert market.promotion_ready is True
    assert market.historical_evidence_ready is False
    assert "us:fallback_only" in market.historical_evidence_blockers


@pytest.mark.asyncio
async def test_benchmark_requires_61_bars_for_a_60_session_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[_member("AAPL", sessions=sessions)],
        benchmark=[_benchmark("SPY", count=60)],
        sessions=sessions,
    )

    market = result.for_market("us")
    assert market.benchmark.status == "insufficient"
    assert "us:benchmark_unavailable" in market.daily_history_blockers


@pytest.mark.asyncio
async def test_window_rolls_back_one_unsynced_session_instead_of_going_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cron gap between session close and ingest is lag, not a data defect."""

    candidates = _sessions(253)
    stored = candidates[:-1]
    result, db = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member(
                "AAPL",
                sessions=stored,
                bar_count=252,
                observed_expected=252,
                latest_day=stored[-1],
            )
        ],
        sessions=candidates,
        session_rows=_session_rows(stored),
    )

    market = result.for_market("us")
    assert market.latest_completed_session == candidates[-1]
    assert market.evaluated_window_end == stored[-1]
    assert market.evaluated_window_start == stored[0]
    assert market.ingest_lag_session_count == 1
    assert market.stale_bar_count == 0
    assert market.missing_expected_trading_day_count == 0
    assert market.daily_history_ready is True
    assert "us:ingest_lag_window_rolled_back" in market.unresolved_evidence
    member_params = next(
        params
        for statement, params in zip(db.statements, db.parameters, strict=True)
        if "daily_candles_readiness:market:us" in statement
    )
    assert member_params["expected_sessions"] == list(stored)


@pytest.mark.asyncio
async def test_ingest_lag_beyond_the_tolerance_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _sessions(255)
    stored = candidates[:-3]
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member(
                "AAPL",
                sessions=stored,
                bar_count=252,
                observed_expected=252,
                latest_day=stored[-1],
            )
        ],
        sessions=candidates,
        session_rows=_session_rows(stored),
    )

    market = result.for_market("us")
    assert market.ingest_lag_session_count == 3
    assert market.daily_history_ready is False
    assert "us:ingest_lag_exceeded" in market.daily_history_blockers


@pytest.mark.asyncio
async def test_empty_store_keeps_the_calendar_anchor_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _sessions(253)
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member("AAPL", sessions=candidates, bar_count=0, observed_expected=0)
        ],
        sessions=candidates,
        session_rows=[],
    )

    market = result.for_market("us")
    assert market.evaluated_window_end == candidates[-1]
    assert market.ingest_lag_session_count == len(candidates)
    assert market.stale_bar_count == 1
    assert market.daily_history_ready is False
    assert {
        "us:ingest_lag_exceeded",
        "us:stale_bar",
        "us:insufficient_history",
    }.issubset(market.daily_history_blockers)


@pytest.mark.asyncio
async def test_session_no_source_evidences_is_recorded_not_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calendar session absent from every durable source is uncertainty."""

    candidates = _sessions(252)
    closed_day = candidates[100]
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member(
                "AAPL",
                sessions=candidates,
                bar_count=252,
                observed_expected=251,
            )
        ],
        sessions=candidates,
        session_rows=_session_rows(candidates, absent=(closed_day,)),
    )

    market = result.for_market("us")
    assert market.unevidenced_session_count == 1
    assert market.unevidenced_sessions == (closed_day,)
    assert market.missing_expected_trading_day_count == 0
    assert market.daily_history_ready is True
    assert "us:calendar_session_unevidenced" in market.unresolved_evidence


@pytest.mark.asyncio
async def test_session_the_benchmark_evidences_is_still_a_missing_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The benchmark proves the session traded, so a symbol gap stays a defect."""

    candidates = _sessions(252)
    traded_day = candidates[100]
    result, _ = await _measure(
        monkeypatch,
        market="us",
        members=[
            _member(
                "AAPL",
                sessions=candidates,
                bar_count=252,
                observed_expected=251,
            )
        ],
        sessions=candidates,
        session_rows=_session_rows(candidates, benchmark_only=(traded_day,)),
    )

    market = result.for_market("us")
    assert market.unevidenced_session_count == 0
    assert market.missing_expected_trading_day_count == 1
    assert market.daily_history_ready is False
    assert "us:missing_expected_trading_days" in market.daily_history_blockers


@pytest.mark.asyncio
async def test_member_delisted_inside_the_window_is_not_stale_or_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _sessions(252)
    delist_day = candidates[200]
    survivors = candidates[:201]
    result, _ = await _measure(
        monkeypatch,
        market="us",
        cohort=_cohort("us", requested_size=2),
        members=[
            _member("AAPL", sessions=candidates),
            _member(
                "OLDCO",
                sessions=survivors,
                rank=2,
                active=False,
                listing_status="delisted",
                delist_date=delist_day,
                bar_count=252,
                observed_expected=len(survivors),
                latest_day=delist_day,
            ),
        ],
        sessions=candidates,
        session_rows=_session_rows(candidates),
    )

    market = result.for_market("us")
    assert market.stale_bar_count == 0
    assert market.missing_expected_trading_day_count == 0
    assert market.eligible_symbol_count == 2
    assert market.daily_history_ready is True
