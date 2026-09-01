"""Fail-closed, cohort-scoped readiness measurement for KR/US daily candles.

The service reads immutable research cohorts and durable candle/coverage evidence.
It never expands a cohort to the current symbol master and performs no writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.market_events.session_calendar import (
    regular_session_bounds,
    trading_sessions_in_range,
)

MarketName = Literal["kr", "us"]
CalendarStatus = Literal["available", "unavailable"]
CorporateActionStatus = Literal["suspected", "clear", "unknown"]
PriceAdjustmentStatus = Literal["covered", "incomplete"]
BenchmarkStatus = Literal["available", "insufficient", "unavailable"]

REQUIRED_HISTORY_BARS = 252
REQUIRED_BENCHMARK_BARS = 61  # 60-session return requires both endpoints
#: 방금 닫힌 세션의 일봉이 아직 저장소에 없는 상태는 데이터 결함이 아니라 수집
#: cron이 아직 돌지 않았다는 사실이다(KR 16:30 KST, US 22:00 UTC). 평가 창을
#: 실제로 적재된 최신 세션까지 되감고 되감은 세션 수를 증거로 남긴다. 이 한도를
#: 넘으면 수집이 실제로 밀린 것이므로 fail-closed한다.
MAX_INGEST_LAG_SESSIONS = 1
#: 되감기 폭을 측정하기 위해 252 창 위에 얹는 여유 세션 수. 이보다 더 밀리면
#: lag는 이 값에서 포화하고 blocker만 남는다.
_INGEST_LAG_PROBE_SESSIONS = 10
_CALENDAR_LOOKBACK_DAYS = 550
_FALLBACK_SOURCES = frozenset({"toss", "toss_fallback", "yahoo", "yahoo_fallback"})
_DELISTED_STATUSES = frozenset({"delisted", "상장폐지"})
_KR_ADJUSTED_SOURCES = frozenset({"kis", "toss"})
_KR_ACTION_SPECS = (
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


@dataclass(frozen=True, slots=True)
class CohortEvidence:
    """Identity and selection facts for one durable research cohort."""

    cohort_id: str
    market: MarketName
    selection_as_of: datetime
    selection_date: date
    effective_date: date
    method: str
    requested_size: int
    active_member_count: int
    valuation_snapshot_date: date
    valuation_snapshot_source: str
    evidence_scope: str


@dataclass(frozen=True, slots=True)
class BenchmarkCoverage:
    """Durable benchmark history selected by the cohort."""

    market: MarketName
    symbol: str
    start: datetime | None
    end: datetime | None
    count: int
    source: str | None
    sources: tuple[str, ...]
    status: BenchmarkStatus


@dataclass(frozen=True, slots=True)
class SymbolReadinessExclusion:
    """One active cohort member excluded from the forward evaluation sample."""

    symbol: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketReadiness:
    """Immutable readiness evidence for one market cohort."""

    market: MarketName
    cohort: CohortEvidence | None
    evaluated_window_start: date | None
    evaluated_window_end: date | None
    latest_completed_session: date | None
    ingest_lag_session_count: int
    unevidenced_session_count: int
    unevidenced_sessions: tuple[date, ...]
    total_symbol_count: int
    cohort_active_member_count: int
    forced_member_count: int
    benchmark_member_count: int
    active_symbol_count: int
    inactive_symbol_count: int
    symbols_with_exactly_251_bars: int
    symbols_with_at_least_252_bars: int
    eligible_symbol_count: int
    eligible_symbols: tuple[str, ...]
    excluded_symbols: tuple[SymbolReadinessExclusion, ...]
    stale_bar_count: int
    future_bar_count: int
    duplicate_timestamp_count: int
    ohlc_anomaly_count: int
    missing_expected_trading_day_count: int | None
    calendar_status: CalendarStatus
    price_adjustment_status: PriceAdjustmentStatus
    corporate_action_status: CorporateActionStatus
    corporate_action_covered_symbol_count: int
    adjustment_covered_symbol_count: int
    list_date_covered_symbol_count: int
    members_listed_after_cohort_start: int
    delist_date_covered_inactive_count: int
    point_in_time_available: bool
    inactive_with_candles_count: int
    delisted_symbol_count: int
    delisted_with_candles_count: int
    includes_delisted: bool
    fallback_only: bool
    benchmark: BenchmarkCoverage
    daily_history_ready: bool
    promotion_ready: bool
    historical_evidence_ready: bool
    daily_history_blockers: tuple[str, ...]
    blockers: tuple[str, ...]
    historical_evidence_blockers: tuple[str, ...]
    unresolved_evidence: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyCandlesReadiness:
    """Counts-only daily-history and promotion readiness evidence."""

    as_of: datetime
    required_history_bars: int
    markets: tuple[MarketReadiness, ...]
    daily_history_ready: bool
    promotion_ready: bool
    historical_evidence_ready: bool
    daily_history_blockers: tuple[str, ...]
    blockers: tuple[str, ...]
    historical_evidence_blockers: tuple[str, ...]
    unresolved_evidence: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def eligible_symbol_count(self) -> int:
        return sum(item.eligible_symbol_count for item in self.markets)

    def for_market(self, market: MarketName) -> MarketReadiness:
        for item in self.markets:
            if item.market == market:
                return item
        raise KeyError(market)


@dataclass(frozen=True, slots=True)
class _MarketConfig:
    universe_table: str
    candle_table: str
    has_adjusted_close: bool


_CONFIG: dict[MarketName, _MarketConfig] = {
    "kr": _MarketConfig(
        universe_table="kr_symbol_universe",
        candle_table="kr_candles_1d",
        has_adjusted_close=False,
    ),
    "us": _MarketConfig(
        universe_table="us_symbol_universe",
        candle_table="us_candles_1d",
        has_adjusted_close=True,
    ),
}


def _cohort_query(market: MarketName):
    return text(
        f"""/* daily_candles_readiness:cohort:{market} */
        SELECT
            cohort_id,
            market,
            selection_as_of,
            selection_date,
            effective_date,
            selection_method,
            requested_size,
            active_member_count,
            valuation_snapshot_date,
            valuation_snapshot_source,
            evidence_scope
        FROM public.kasset_research_cohorts
        WHERE market = :market
          AND selection_method = 'latest_market_cap'
          AND selection_as_of <= :as_of
          AND created_at <= :as_of
          AND (CAST(:cohort_id AS text) IS NULL OR cohort_id = CAST(:cohort_id AS text))
        ORDER BY selection_as_of DESC, selection_date DESC,
                 effective_date DESC, cohort_id DESC
        LIMIT 1
        """
    )


def _market_query(market: MarketName, config: _MarketConfig):
    fallback_sql = ", ".join(f"'{value}'" for value in sorted(_FALLBACK_SOURCES))
    if config.has_adjusted_close:
        adjustment_column = """
            COUNT(*) FILTER (
                WHERE (time AT TIME ZONE 'UTC')::date =
                          ANY(CAST(:expected_sessions AS date[]))
                  AND (
                      adj_close IS NULL
                      OR adj_close::text IN ('NaN', 'Infinity', '-Infinity')
                      OR adj_close <= 0
                  )
            ) AS invalid_adjustment_count
        """
    else:
        adjusted_sources_sql = ", ".join(
            f"'{value}'" for value in sorted(_KR_ADJUSTED_SOURCES)
        )
        adjustment_column = f"""
            COUNT(*) FILTER (
                WHERE (time AT TIME ZONE 'UTC')::date =
                          ANY(CAST(:expected_sessions AS date[]))
                  AND (
                      source IS NULL
                      OR source NOT IN ({adjusted_sources_sql})
                  )
            ) AS invalid_adjustment_count
        """

    # Identifiers come exclusively from the closed _CONFIG constant above.
    return text(
        f"""/* daily_candles_readiness:market:{market} */
        WITH selected_members AS (
            SELECT symbol, rank, member_kind, market_cap, eligibility_facts
            FROM public.kasset_research_cohort_members
            WHERE cohort_id = :cohort_id
              AND member_kind IN ('active', 'forced')
        ),
        per_timestamp AS (
            SELECT
                symbol,
                time,
                COUNT(*) AS partition_row_count,
                COUNT(*) FILTER (
                    WHERE open::text IN ('NaN', 'Infinity', '-Infinity')
                       OR high::text IN ('NaN', 'Infinity', '-Infinity')
                       OR low::text IN ('NaN', 'Infinity', '-Infinity')
                       OR close::text IN ('NaN', 'Infinity', '-Infinity')
                       OR volume::text IN ('NaN', 'Infinity', '-Infinity')
                       OR open <= 0
                       OR high <= 0
                       OR low <= 0
                       OR close <= 0
                       OR volume < 0
                       OR low > LEAST(open, close)
                       OR GREATEST(open, close) > high
                ) AS ohlc_anomaly_count,
                BOOL_AND(source IN ({fallback_sql})) AS fallback_only,
                {adjustment_column}
            FROM public.{config.candle_table}
            WHERE symbol IN (SELECT symbol FROM selected_members)
            GROUP BY symbol, time
        )
        SELECT
            m.symbol,
            m.rank AS member_rank,
            m.member_kind,
            m.market_cap,
            m.eligibility_facts,
            u.is_active,
            u.listing_status,
            u.list_date,
            u.delist_date,
            COUNT(p.time) FILTER (WHERE p.time <= :as_of) AS bar_count,
            COUNT(DISTINCT (p.time AT TIME ZONE 'UTC')::date) FILTER (
                WHERE p.time <= :as_of
                  AND (p.time AT TIME ZONE 'UTC')::date =
                      ANY(CAST(:expected_sessions AS date[]))
                  AND (
                      u.list_date IS NULL
                      OR (p.time AT TIME ZONE 'UTC')::date >= u.list_date
                  )
                  AND (
                      u.delist_date IS NULL
                      OR (p.time AT TIME ZONE 'UTC')::date <= u.delist_date
                  )
            ) AS observed_expected_session_count,
            MIN(p.time) FILTER (WHERE p.time <= :as_of) AS first_bar_at,
            MAX(p.time) FILTER (WHERE p.time <= :as_of) AS latest_bar_at,
            COUNT(p.time) FILTER (WHERE p.time > :as_of) AS future_bar_count,
            COALESCE(
                SUM(GREATEST(p.partition_row_count - 1, 0)),
                0
            ) AS duplicate_timestamp_count,
            COALESCE(SUM(p.ohlc_anomaly_count), 0) AS ohlc_anomaly_count,
            COALESCE(
                SUM(p.partition_row_count) FILTER (WHERE p.time <= :as_of),
                0
            ) AS candle_row_count,
            COALESCE(
                BOOL_AND(p.fallback_only) FILTER (WHERE p.time <= :as_of),
                FALSE
            ) AS fallback_only,
            COALESCE(
                SUM(p.invalid_adjustment_count) FILTER (WHERE p.time <= :as_of),
                0
            ) AS invalid_adjustment_count
        FROM selected_members AS m
        LEFT JOIN public.{config.universe_table} AS u ON u.symbol = m.symbol
        LEFT JOIN per_timestamp AS p ON p.symbol = m.symbol
        GROUP BY
            m.symbol,
            m.rank,
            m.member_kind,
            m.market_cap,
            m.eligibility_facts,
            u.is_active,
            u.listing_status,
            u.list_date,
            u.delist_date
        ORDER BY m.rank, m.member_kind, m.symbol
        """
    )


def _benchmark_query(market: MarketName, config: _MarketConfig):
    return text(
        f"""/* daily_candles_readiness:benchmark:{market} */
        SELECT
            m.symbol,
            m.rank AS member_rank,
            MIN(c.time) AS start_at,
            MAX(c.time) AS end_at,
            COUNT(DISTINCT c.time) AS bar_count,
            STRING_AGG(DISTINCT c.source, ',' ORDER BY c.source) AS sources
        FROM public.kasset_research_cohort_members AS m
        LEFT JOIN public.{config.candle_table} AS c
          ON c.symbol = m.symbol AND c.time <= :as_of
        WHERE m.cohort_id = :cohort_id
          AND m.member_kind = 'benchmark'
        GROUP BY m.symbol, m.rank
        ORDER BY m.rank, m.symbol
        """
    )


def _session_coverage_query(market: MarketName, config: _MarketConfig):
    """Per-session observation counts for the cohort over the candidate window.

    The evaluated window is anchored on data the store actually holds, and a
    calendar session that neither an active member nor the durable benchmark
    evidences is recorded as unevidenced instead of being charged to every
    symbol as a missing trading day. Identifiers come from ``_CONFIG`` only.
    """
    return text(
        f"""/* daily_candles_readiness:sessions:{market} */
        WITH cohort_symbols AS (
            SELECT symbol, member_kind
            FROM public.kasset_research_cohort_members
            WHERE cohort_id = :cohort_id
              AND member_kind IN ('active', 'benchmark')
        ),
        observed AS (
            SELECT
                (c.time AT TIME ZONE 'UTC')::date AS session_date,
                s.member_kind,
                c.symbol
            FROM public.{config.candle_table} AS c
            JOIN cohort_symbols AS s ON s.symbol = c.symbol
            WHERE c.time <= :as_of
              AND (c.time AT TIME ZONE 'UTC')::date =
                  ANY(CAST(:candidate_sessions AS date[]))
        )
        SELECT
            session_date,
            COUNT(DISTINCT symbol) FILTER (WHERE member_kind = 'active')
                AS member_symbol_count,
            COUNT(DISTINCT symbol) FILTER (WHERE member_kind = 'benchmark')
                AS benchmark_symbol_count
        FROM observed
        GROUP BY session_date
        ORDER BY session_date
        """
    )


def _kr_coverage_query():
    return text(
        """/* daily_candles_readiness:corporate_actions:kr */
        SELECT
            m.symbol,
            coverage.source,
            coverage.provider,
            coverage.provider_endpoint,
            coverage.provider_tr_id,
            coverage.action_kind,
            coverage.requested_from_date,
            coverage.requested_to_date,
            coverage.status,
            coverage.row_count
        FROM public.kasset_research_cohort_members AS m
        LEFT JOIN public.kasset_corporate_action_fetch_coverage AS coverage
          ON coverage.symbol = m.symbol
         AND coverage.requested_to_date >= :window_start
         AND coverage.requested_from_date <= :window_end
         AND coverage.completed_at <= :as_of
        WHERE m.cohort_id = :cohort_id
          AND m.member_kind IN ('active', 'forced')
        ORDER BY m.rank, m.member_kind, m.symbol,
                 coverage.action_kind, coverage.requested_from_date,
                 coverage.requested_to_date
        """
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _completed_candidate_sessions(
    market: MarketName,
    as_of: datetime,
) -> tuple[date, ...]:
    """The newest completed sessions: the 252 window plus the lag probe margin."""

    start = as_of.date() - timedelta(days=_CALENDAR_LOOKBACK_DAYS)
    sessions = trading_sessions_in_range(market, start, as_of.date())
    if not sessions:
        return ()

    completed: list[date] = []
    for session_day in sessions:
        if session_day < as_of.date():
            completed.append(session_day)
            continue
        bounds = regular_session_bounds(market, session_day)
        if bounds is not None and bounds[1].astimezone(UTC) <= as_of:
            completed.append(session_day)
    limit = REQUIRED_HISTORY_BARS + _INGEST_LAG_PROBE_SESSIONS
    return tuple(completed[-limit:])


@dataclass(frozen=True, slots=True)
class _SessionWindow:
    """The 252-session window the store can actually be held to."""

    expected: tuple[date, ...]
    latest_completed: date | None
    ingest_lag_sessions: int
    unevidenced: tuple[date, ...]


def _resolve_session_window(
    candidate_sessions: tuple[date, ...],
    session_rows: Sequence[Mapping[str, object]],
) -> _SessionWindow:
    """Anchor the evaluated window on the newest session the cohort evidences.

    Sessions newer than that anchor are ingest lag, not per-symbol data gaps;
    sessions inside the window that neither an active member nor the benchmark
    evidences are calendar uncertainty and are reported, not charged.
    """
    if not candidate_sessions:
        return _SessionWindow(
            expected=(),
            latest_completed=None,
            ingest_lag_sessions=0,
            unevidenced=(),
        )
    member_counts: dict[date, int] = {}
    benchmark_counts: dict[date, int] = {}
    for row in session_rows:
        session_day = row.get("session_date")
        if not isinstance(session_day, date):
            continue
        member_counts[session_day] = _int(row, "member_symbol_count")
        benchmark_counts[session_day] = _int(row, "benchmark_symbol_count")

    observed_indexes = [
        index
        for index, session_day in enumerate(candidate_sessions)
        if member_counts.get(session_day, 0) > 0
    ]
    if observed_indexes:
        end_index = observed_indexes[-1]
        lag = len(candidate_sessions) - 1 - end_index
    else:
        # Nothing stored at all: keep the calendar anchor so stale/missing fire.
        end_index = len(candidate_sessions) - 1
        lag = len(candidate_sessions)
    start_index = max(end_index + 1 - REQUIRED_HISTORY_BARS, 0)
    expected = candidate_sessions[start_index : end_index + 1]
    unevidenced = tuple(
        session_day
        for session_day in expected
        if member_counts.get(session_day, 0) == 0
        and benchmark_counts.get(session_day, 0) == 0
    )
    return _SessionWindow(
        expected=expected,
        latest_completed=candidate_sessions[-1],
        ingest_lag_sessions=lag,
        unevidenced=unevidenced,
    )


def _int(row: Mapping[str, object], key: str) -> int:
    return max(int(row.get(key) or 0), 0)


def _is_delisted(row: Mapping[str, object]) -> bool:
    if row.get("delist_date") is not None:
        return True
    status = str(row.get("listing_status") or "").strip().casefold()
    return status in _DELISTED_STATUSES


def _cohort_evidence(
    market: MarketName,
    row: Mapping[str, object],
) -> CohortEvidence:
    return CohortEvidence(
        cohort_id=str(row["cohort_id"]),
        market=market,
        selection_as_of=_aware_utc(cast(datetime, row["selection_as_of"])),
        selection_date=cast(date, row["selection_date"]),
        effective_date=cast(date, row["effective_date"]),
        method=str(row["selection_method"]),
        requested_size=_int(row, "requested_size"),
        active_member_count=_int(row, "active_member_count"),
        valuation_snapshot_date=cast(date, row["valuation_snapshot_date"]),
        valuation_snapshot_source=str(row["valuation_snapshot_source"]),
        evidence_scope=str(row["evidence_scope"]),
    )


def _empty_benchmark(market: MarketName) -> BenchmarkCoverage:
    return BenchmarkCoverage(
        market=market,
        symbol="",
        start=None,
        end=None,
        count=0,
        source=None,
        sources=(),
        status="unavailable",
    )


def _benchmark_coverage(
    market: MarketName,
    row: Mapping[str, object],
) -> BenchmarkCoverage:
    count = _int(row, "bar_count")
    raw_sources = str(row.get("sources") or "")
    sources = tuple(value for value in raw_sources.split(",") if value)
    if count == 0:
        status: BenchmarkStatus = "unavailable"
    elif count < REQUIRED_BENCHMARK_BARS:
        status = "insufficient"
    else:
        status = "available"
    source = sources[0] if len(sources) == 1 else "mixed" if sources else None
    return BenchmarkCoverage(
        market=market,
        symbol=str(row.get("symbol") or ""),
        start=cast(datetime | None, row.get("start_at")),
        end=cast(datetime | None, row.get("end_at")),
        count=count,
        source=source,
        sources=sources,
        status=status,
    )


def _issue(market: MarketName, code: str) -> str:
    return f"{market}:{code}"


def _intervals_cover(
    rows: Sequence[Mapping[str, object]],
    *,
    window_start: date,
    window_end: date,
) -> bool:
    intervals = sorted(
        (
            cast(date, row["requested_from_date"]),
            cast(date, row["requested_to_date"]),
        )
        for row in rows
        if row.get("status") == "success"
        and str(row.get("source") or "").strip().casefold() == "kis_openapi"
        and str(row.get("provider") or "").strip().casefold() == "kis"
        and row.get("requested_from_date") is not None
        and row.get("requested_to_date") is not None
    )
    cursor = window_start
    for interval_start, interval_end in intervals:
        if interval_end < cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = interval_end + timedelta(days=1)
        if cursor > window_end:
            return True
    return cursor > window_end


def _kr_covered_symbols(
    coverage_rows: Sequence[Mapping[str, object]],
    symbols: Sequence[str],
    *,
    window_start: date | None,
    window_end: date | None,
) -> frozenset[str]:
    if window_start is None or window_end is None:
        return frozenset()
    by_identity: dict[tuple[str, str, str, str], list[Mapping[str, object]]] = {}
    for row in coverage_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        identity = (
            symbol,
            str(row.get("action_kind") or "").strip(),
            str(row.get("provider_endpoint") or "").strip(),
            str(row.get("provider_tr_id") or "").strip(),
        )
        if all(identity):
            by_identity.setdefault(identity, []).append(row)
    covered = {
        symbol
        for symbol in symbols
        if all(
            _intervals_cover(
                by_identity.get((symbol, action_kind, endpoint, tr_id), ()),
                window_start=window_start,
                window_end=window_end,
            )
            for action_kind, endpoint, tr_id in _KR_ACTION_SPECS
        )
    }
    return frozenset(covered)


def _evaluate_market(
    *,
    market: MarketName,
    cohort: CohortEvidence | None,
    rows: Sequence[Mapping[str, object]],
    window: _SessionWindow,
    benchmark: BenchmarkCoverage,
    benchmark_member_count: int,
    coverage_rows: Sequence[Mapping[str, object]],
) -> MarketReadiness:
    expected_sessions = window.expected
    window_start = expected_sessions[0] if expected_sessions else None
    window_end = expected_sessions[-1] if expected_sessions else None
    unevidenced = frozenset(window.unevidenced)
    # Only sessions some durable source evidences can be charged to a symbol.
    evaluable_sessions = tuple(
        session_day
        for session_day in expected_sessions
        if session_day not in unevidenced
    )
    core_rows = [row for row in rows if row.get("member_kind") == "active"]
    total = len(core_rows)
    cohort_active = total
    forced = sum(1 for row in rows if row.get("member_kind") == "forced")
    active_rows = [row for row in core_rows if bool(row.get("is_active"))]
    inactive_rows = [row for row in core_rows if not bool(row.get("is_active"))]
    active = len(active_rows)
    inactive = len(inactive_rows)

    exactly_251 = sum(1 for row in core_rows if _int(row, "bar_count") == 251)
    at_least_252 = sum(
        1 for row in core_rows if _int(row, "bar_count") >= REQUIRED_HISTORY_BARS
    )
    future = sum(_int(row, "future_bar_count") for row in core_rows)
    duplicates = sum(_int(row, "duplicate_timestamp_count") for row in core_rows)
    ohlc = sum(_int(row, "ohlc_anomaly_count") for row in core_rows)

    calendar_status: CalendarStatus = (
        "available" if expected_sessions else "unavailable"
    )
    stale = 0
    missing: int | None = 0 if expected_sessions else None
    eligible_symbols: list[str] = []
    excluded_symbols: list[SymbolReadinessExclusion] = []
    adjustment_covered = 0
    for row in core_rows:
        latest = cast(datetime | None, row.get("latest_bar_at"))
        listed = cast(date | None, row.get("list_date"))
        delisted = cast(date | None, row.get("delist_date"))
        # A member that stopped trading inside the window is not stale for the
        # sessions it could not have traded in.
        row_last_session = window_end
        if (
            window_end is not None
            and isinstance(delisted, date)
            and delisted < window_end
        ):
            row_last_session = delisted
        row_stale = bool(
            row_last_session is not None
            and (latest is None or latest.astimezone(UTC).date() < row_last_session)
        )
        stale += int(row_stale)

        row_missing = 0
        if expected_sessions:
            expected_count = sum(
                1
                for session_day in evaluable_sessions
                if (listed is None or session_day >= listed)
                and (not isinstance(delisted, date) or session_day <= delisted)
            )
            row_missing = max(
                expected_count - _int(row, "observed_expected_session_count"),
                0,
            )
            missing = cast(int, missing) + row_missing
            if row_missing == 0 and _int(row, "invalid_adjustment_count") == 0:
                adjustment_covered += 1

        symbol = str(row.get("symbol") or "").strip().upper()
        exclusion_reasons: list[str] = []
        if _int(row, "bar_count") < REQUIRED_HISTORY_BARS:
            exclusion_reasons.append("insufficient_history")
        if row_stale:
            exclusion_reasons.append("stale_bar")
        if row_missing:
            exclusion_reasons.append("missing_expected_trading_days")
        if _int(row, "future_bar_count"):
            exclusion_reasons.append("future_bar")
        if _int(row, "duplicate_timestamp_count"):
            exclusion_reasons.append("duplicate_bar_timestamp")
        if _int(row, "ohlc_anomaly_count"):
            exclusion_reasons.append("invalid_ohlcv")
        if _int(row, "invalid_adjustment_count"):
            exclusion_reasons.append("adjustment_coverage_incomplete")
        if exclusion_reasons:
            excluded_symbols.append(
                SymbolReadinessExclusion(
                    symbol=symbol,
                    reasons=tuple(exclusion_reasons),
                )
            )
        else:
            eligible_symbols.append(symbol)

    list_date_covered = sum(1 for row in core_rows if row.get("list_date") is not None)
    delist_date_covered_inactive = sum(
        1 for row in inactive_rows if row.get("delist_date") is not None
    )
    # The cohort claims membership as of its effective date, and the backtest window
    # may start earlier than the evaluated readiness window. Anchoring on the cohort
    # effective date is therefore the strictest available reference: a member listed
    # after it did not exist when the cohort claims it was selected.
    listed_after_cohort_start = sum(
        1
        for row in core_rows
        if cohort is not None
        and isinstance(row.get("list_date"), date)
        and cast(date, row["list_date"]) > cohort.effective_date
    )
    membership_period_usable = bool(
        cohort is not None
        and window_start is not None
        and window_start >= cohort.effective_date
    )
    # Point-in-time membership is proven by persisted member lifecycle rows that
    # cover the whole evaluated window, never by the declared evidence scope alone.
    # A cohort assembled from the present-day universe cannot satisfy this.
    constituent_history_proven = bool(
        membership_period_usable
        and total > 0
        and list_date_covered == total
        and listed_after_cohort_start == 0
        and delist_date_covered_inactive == inactive
    )
    point_in_time = bool(
        membership_period_usable
        and cohort is not None
        and cohort.evidence_scope == "historical_pit"
        and constituent_history_proven
    )

    inactive_with_candles = sum(
        1 for row in inactive_rows if _int(row, "bar_count") > 0
    )
    delisted_rows = [row for row in inactive_rows if _is_delisted(row)]
    delisted_with_candles = sum(
        1 for row in delisted_rows if _int(row, "bar_count") > 0
    )
    includes_delisted = bool(
        membership_period_usable
        and cohort is not None
        and any(
            _int(row, "bar_count") > 0
            and isinstance(row.get("delist_date"), date)
            and cast(date, row["delist_date"]) >= cohort.effective_date
            for row in delisted_rows
        )
    )

    candle_rows = sum(_int(row, "candle_row_count") for row in core_rows)
    fallback_only = bool(
        candle_rows > 0
        and all(
            bool(row.get("fallback_only"))
            for row in core_rows
            if _int(row, "candle_row_count") > 0
        )
    )

    symbols = [str(row.get("symbol") or "").strip().upper() for row in core_rows]
    kr_covered = (
        _kr_covered_symbols(
            coverage_rows,
            symbols,
            window_start=window_start,
            window_end=window_end,
        )
        if market == "kr"
        else frozenset()
    )
    corporate_action_covered = len(kr_covered) if market == "kr" else adjustment_covered
    # Split-adjusted price coverage is obtainable from the configured sources
    # (KIS/Toss for KR, adjusted close for US) and gates the daily history.
    price_adjustment_status: PriceAdjustmentStatus = (
        "covered"
        if total > 0 and calendar_status == "available" and adjustment_covered == total
        else "incomplete"
    )
    # The KSD action ledger is a strictly stronger, independent proof. Without it
    # the action history stays unknown; that is historical-track evidence and is
    # reported as unresolved rather than faked.
    if price_adjustment_status == "covered" and (
        market == "us" or len(kr_covered) == total
    ):
        corporate_action: CorporateActionStatus = "clear"
    else:
        corporate_action = "unknown"

    daily_blockers: list[str] = []

    def daily_block(code: str) -> None:
        daily_blockers.append(_issue(market, code))

    if cohort is None:
        daily_block("cohort_not_found")
    else:
        if total == 0:
            daily_block("cohort_members_empty")
        if (
            cohort_active != cohort.active_member_count
            or cohort_active != cohort.requested_size
        ):
            daily_block("cohort_member_count_mismatch")
        if not eligible_symbols:
            daily_block("eligible_symbols_zero")
            if total > 0 and at_least_252 < total:
                daily_block("insufficient_history")
            if total > 0 and at_least_252 == total:
                daily_block("member_not_eligible")
            if stale:
                daily_block("stale_bar")
            if future:
                daily_block("future_bar")
            if duplicates:
                daily_block("duplicate_bar_timestamp")
            if ohlc:
                daily_block("invalid_ohlcv")
            if missing:
                daily_block("missing_expected_trading_days")
            if price_adjustment_status != "covered":
                daily_block("adjustment_coverage_incomplete")
        if calendar_status == "unavailable":
            daily_block("calendar_unavailable")
        else:
            if len(expected_sessions) < REQUIRED_HISTORY_BARS:
                daily_block("expected_session_window_incomplete")
            if window.ingest_lag_sessions > MAX_INGEST_LAG_SESSIONS:
                daily_block("ingest_lag_exceeded")
        if benchmark_member_count != 1:
            daily_block("benchmark_member_count_invalid")
        if benchmark.status != "available":
            daily_block("benchmark_unavailable")

    # Forward PAPER promotion is gated on evidence the configured sources can
    # actually produce. Historical-fidelity claims (point-in-time membership,
    # delisted survivors, the KSD action ledger, a primary-provider price
    # series) form a separate track that stays explicitly unproven until the
    # underlying evidence exists.
    promotion_blockers = list(daily_blockers)
    historical_blockers: list[str] = []

    def historical_block(code: str) -> None:
        historical_blockers.append(_issue(market, code))

    if cohort is None:
        historical_block("cohort_not_found")
    else:
        if not benchmark.sources:
            promotion_blockers.append(_issue(market, "benchmark_source_missing"))
        if cohort.evidence_scope != "historical_pit":
            historical_block("cohort_not_historical_pit")
        if not membership_period_usable:
            historical_block("cohort_window_predates_effective_date")
        if list_date_covered < total:
            historical_block("list_date_coverage_incomplete")
        if listed_after_cohort_start:
            historical_block("member_listed_after_cohort_start")
        if delist_date_covered_inactive < inactive:
            historical_block("delist_date_coverage_incomplete")
        if not point_in_time:
            historical_block("point_in_time_unavailable")
        if not includes_delisted:
            historical_block("delisted_members_absent")
        if corporate_action != "clear":
            historical_block("corporate_action_unknown")
        if fallback_only:
            historical_block("fallback_only")
        if benchmark.sources and set(benchmark.sources) <= _FALLBACK_SOURCES:
            historical_block("benchmark_fallback_only")

    unresolved = list(historical_blockers)
    if window.unevidenced:
        unresolved.append(_issue(market, "calendar_session_unevidenced"))
    if window.ingest_lag_sessions:
        unresolved.append(_issue(market, "ingest_lag_window_rolled_back"))

    return MarketReadiness(
        market=market,
        cohort=cohort,
        evaluated_window_start=window_start,
        evaluated_window_end=window_end,
        latest_completed_session=window.latest_completed,
        ingest_lag_session_count=window.ingest_lag_sessions,
        unevidenced_session_count=len(window.unevidenced),
        unevidenced_sessions=window.unevidenced,
        total_symbol_count=total,
        cohort_active_member_count=cohort_active,
        forced_member_count=forced,
        benchmark_member_count=benchmark_member_count,
        active_symbol_count=active,
        inactive_symbol_count=inactive,
        symbols_with_exactly_251_bars=exactly_251,
        symbols_with_at_least_252_bars=at_least_252,
        eligible_symbol_count=len(eligible_symbols),
        eligible_symbols=tuple(eligible_symbols),
        excluded_symbols=tuple(excluded_symbols),
        stale_bar_count=stale,
        future_bar_count=future,
        duplicate_timestamp_count=duplicates,
        ohlc_anomaly_count=ohlc,
        missing_expected_trading_day_count=missing,
        calendar_status=calendar_status,
        price_adjustment_status=price_adjustment_status,
        corporate_action_status=corporate_action,
        corporate_action_covered_symbol_count=corporate_action_covered,
        adjustment_covered_symbol_count=adjustment_covered,
        list_date_covered_symbol_count=list_date_covered,
        members_listed_after_cohort_start=listed_after_cohort_start,
        delist_date_covered_inactive_count=delist_date_covered_inactive,
        point_in_time_available=point_in_time,
        inactive_with_candles_count=inactive_with_candles,
        delisted_symbol_count=len(delisted_rows),
        delisted_with_candles_count=delisted_with_candles,
        includes_delisted=includes_delisted,
        fallback_only=fallback_only,
        benchmark=benchmark,
        daily_history_ready=not daily_blockers,
        promotion_ready=not promotion_blockers,
        historical_evidence_ready=not historical_blockers,
        daily_history_blockers=tuple(daily_blockers),
        blockers=tuple(promotion_blockers),
        historical_evidence_blockers=tuple(historical_blockers),
        unresolved_evidence=tuple(unresolved),
        reasons=tuple(promotion_blockers) + tuple(historical_blockers),
    )


class DailyCandlesReadinessService:
    """Measure cohort-scoped daily-history and PAPER promotion readiness."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def measure(
        self,
        *,
        as_of: datetime | None = None,
        markets: tuple[MarketName, ...] = ("kr", "us"),
        cohort_ids: Mapping[MarketName, str] | None = None,
    ) -> DailyCandlesReadiness:
        measured_at = _aware_utc(as_of or datetime.now(UTC))
        if not markets or len(set(markets)) != len(markets):
            raise ValueError("markets must be a non-empty unique tuple")
        unsupported = set(markets) - set(_CONFIG)
        if unsupported:
            raise ValueError(f"unsupported markets: {sorted(unsupported)!r}")
        requested_cohorts = dict(cohort_ids or {})
        extra_cohort_markets = set(requested_cohorts) - set(markets)
        if extra_cohort_markets:
            raise ValueError(
                "cohort_ids contains unrequested markets: "
                f"{sorted(extra_cohort_markets)!r}"
            )
        if any(not str(value).strip() for value in requested_cohorts.values()):
            raise ValueError("cohort ids must be non-blank")

        market_results: list[MarketReadiness] = []
        for market in markets:
            config = _CONFIG[market]
            requested_cohort_id = requested_cohorts.get(market)
            cohort_result = await self._db.execute(
                _cohort_query(market),
                {
                    "market": market,
                    "as_of": measured_at,
                    "cohort_id": requested_cohort_id,
                },
            )
            cohort_rows = cast(
                Sequence[Mapping[str, object]], cohort_result.mappings().all()
            )
            cohort = _cohort_evidence(market, cohort_rows[0]) if cohort_rows else None
            if cohort is None:
                market_results.append(
                    _evaluate_market(
                        market=market,
                        cohort=None,
                        rows=(),
                        window=_resolve_session_window((), ()),
                        benchmark=_empty_benchmark(market),
                        benchmark_member_count=0,
                        coverage_rows=(),
                    )
                )
                continue

            candidate_sessions = _completed_candidate_sessions(market, measured_at)
            session_rows: Sequence[Mapping[str, object]] = ()
            if candidate_sessions:
                session_result = await self._db.execute(
                    _session_coverage_query(market, config),
                    {
                        "cohort_id": cohort.cohort_id,
                        "as_of": measured_at,
                        "candidate_sessions": list(candidate_sessions),
                    },
                )
                session_rows = cast(
                    Sequence[Mapping[str, object]],
                    session_result.mappings().all(),
                )
            window = _resolve_session_window(candidate_sessions, session_rows)
            expected_sessions = window.expected
            member_result = await self._db.execute(
                _market_query(market, config),
                {
                    "cohort_id": cohort.cohort_id,
                    "as_of": measured_at,
                    "expected_sessions": list(expected_sessions),
                },
            )
            rows = cast(Sequence[Mapping[str, object]], member_result.mappings().all())
            benchmark_result = await self._db.execute(
                _benchmark_query(market, config),
                {"cohort_id": cohort.cohort_id, "as_of": measured_at},
            )
            benchmark_rows = cast(
                Sequence[Mapping[str, object]], benchmark_result.mappings().all()
            )
            benchmark = (
                _benchmark_coverage(market, benchmark_rows[0])
                if len(benchmark_rows) == 1
                else _empty_benchmark(market)
            )

            coverage_rows: Sequence[Mapping[str, object]] = ()
            if market == "kr" and expected_sessions:
                coverage_result = await self._db.execute(
                    _kr_coverage_query(),
                    {
                        "cohort_id": cohort.cohort_id,
                        "window_start": expected_sessions[0],
                        "window_end": expected_sessions[-1],
                        "as_of": measured_at,
                    },
                )
                coverage_rows = cast(
                    Sequence[Mapping[str, object]],
                    coverage_result.mappings().all(),
                )

            market_results.append(
                _evaluate_market(
                    market=market,
                    cohort=cohort,
                    rows=rows,
                    window=window,
                    benchmark=benchmark,
                    benchmark_member_count=len(benchmark_rows),
                    coverage_rows=coverage_rows,
                )
            )

        daily_history_blockers = tuple(
            blocker
            for item in market_results
            for blocker in item.daily_history_blockers
        )
        blockers = tuple(
            blocker for item in market_results for blocker in item.blockers
        )
        historical_blockers = tuple(
            blocker
            for item in market_results
            for blocker in item.historical_evidence_blockers
        )
        unresolved = tuple(
            code for item in market_results for code in item.unresolved_evidence
        )
        reasons = tuple(reason for item in market_results for reason in item.reasons)
        return DailyCandlesReadiness(
            as_of=measured_at,
            required_history_bars=REQUIRED_HISTORY_BARS,
            markets=tuple(market_results),
            daily_history_ready=not daily_history_blockers,
            promotion_ready=not blockers,
            historical_evidence_ready=not historical_blockers,
            daily_history_blockers=daily_history_blockers,
            blockers=blockers,
            historical_evidence_blockers=historical_blockers,
            unresolved_evidence=unresolved,
            reasons=reasons,
        )


__all__ = [
    "BenchmarkCoverage",
    "CohortEvidence",
    "DailyCandlesReadiness",
    "DailyCandlesReadinessService",
    "MAX_INGEST_LAG_SESSIONS",
    "MarketReadiness",
    "SymbolReadinessExclusion",
    "REQUIRED_BENCHMARK_BARS",
    "REQUIRED_HISTORY_BARS",
]
