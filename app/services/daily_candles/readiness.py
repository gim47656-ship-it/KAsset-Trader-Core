"""Fail-closed, read-only readiness measurement for KR/US daily candles.

The service deliberately reports only facts that can be proven from the symbol
universes, durable daily-candle tables, and the exchange session calendar.  It
does not call the ranker, backtest, or promotion state machine and performs no
DB or provider writes.
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
BenchmarkStatus = Literal["available", "insufficient", "unavailable"]

REQUIRED_HISTORY_BARS = 252
REQUIRED_BENCHMARK_BARS = 60
_CALENDAR_LOOKBACK_DAYS = 550
_FALLBACK_SOURCES = ("toss", "toss_fallback", "yahoo", "yahoo_fallback")
_DELISTED_STATUSES = frozenset({"delisted", "상장폐지"})


@dataclass(frozen=True, slots=True)
class BenchmarkCoverage:
    """Durable benchmark history present in a daily-candle table."""

    market: MarketName
    symbol: str
    start: datetime | None
    end: datetime | None
    count: int
    source: str | None
    sources: tuple[str, ...]
    status: BenchmarkStatus


@dataclass(frozen=True, slots=True)
class MarketReadiness:
    """Immutable, counts-only readiness evidence for one equity market."""

    market: MarketName
    total_symbol_count: int
    active_symbol_count: int
    inactive_symbol_count: int
    symbols_with_exactly_251_bars: int
    symbols_with_at_least_252_bars: int
    eligible_symbol_count: int
    stale_bar_count: int
    future_bar_count: int
    duplicate_timestamp_count: int
    ohlc_anomaly_count: int
    missing_expected_trading_day_count: int | None
    calendar_status: CalendarStatus
    corporate_action_status: CorporateActionStatus
    list_date_covered_symbol_count: int
    delist_date_covered_inactive_count: int
    point_in_time_available: bool
    inactive_with_candles_count: int
    delisted_symbol_count: int
    delisted_with_candles_count: int
    includes_delisted: bool
    fallback_only: bool
    benchmark: BenchmarkCoverage
    blockers: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyCandlesReadiness:
    """Promotion-oriented evidence without promotion state-machine effects."""

    as_of: datetime
    required_history_bars: int
    markets: tuple[MarketReadiness, ...]
    promotion_ready: bool
    blockers: tuple[str, ...]
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
    benchmark_symbol: str
    has_adjusted_close: bool


_CONFIG: dict[MarketName, _MarketConfig] = {
    "kr": _MarketConfig(
        universe_table="kr_symbol_universe",
        candle_table="kr_candles_1d",
        benchmark_symbol="KOSPI",
        has_adjusted_close=False,
    ),
    "us": _MarketConfig(
        universe_table="us_symbol_universe",
        candle_table="us_candles_1d",
        benchmark_symbol="SPY",
        has_adjusted_close=True,
    ),
}


def _market_query(market: MarketName, config: _MarketConfig):
    fallback_sql = ", ".join(f"'{value}'" for value in _FALLBACK_SOURCES)
    if config.has_adjusted_close:
        adjustment_columns = """
            COUNT(*) FILTER (
                WHERE adj_close IS NOT NULL AND adj_close <> close
            ) AS corporate_action_suspect_count,
            COUNT(*) FILTER (WHERE adj_close IS NULL)
                AS missing_adjusted_close_count
        """
    else:
        adjustment_columns = """
            0::bigint AS corporate_action_suspect_count,
            COUNT(*) AS missing_adjusted_close_count
        """

    # Identifiers come exclusively from the closed _CONFIG constant above.
    return text(
        f"""/* daily_candles_readiness:market:{market} */
        WITH per_timestamp AS (
            SELECT
                symbol,
                time,
                COUNT(*) AS partition_row_count,
                COUNT(*) FILTER (
                    WHERE open::text = 'NaN'
                       OR high::text = 'NaN'
                       OR low::text = 'NaN'
                       OR close::text = 'NaN'
                       OR volume::text = 'NaN'
                       OR open <= 0
                       OR high <= 0
                       OR low <= 0
                       OR close <= 0
                       OR volume < 0
                       OR low > LEAST(open, close)
                       OR GREATEST(open, close) > high
                ) AS ohlc_anomaly_count,
                BOOL_AND(source IN ({fallback_sql})) AS fallback_only,
                {adjustment_columns}
            FROM public.{config.candle_table}
            GROUP BY symbol, time
        )
        SELECT
            u.symbol,
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
            ) AS observed_expected_session_count,
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
                SUM(p.corporate_action_suspect_count)
                    FILTER (WHERE p.time <= :as_of),
                0
            ) AS corporate_action_suspect_count,
            COALESCE(
                SUM(p.missing_adjusted_close_count)
                    FILTER (WHERE p.time <= :as_of),
                0
            ) AS missing_adjusted_close_count
        FROM public.{config.universe_table} AS u
        LEFT JOIN per_timestamp AS p ON p.symbol = u.symbol
        GROUP BY
            u.symbol,
            u.is_active,
            u.listing_status,
            u.list_date,
            u.delist_date
        ORDER BY u.symbol
        """
    )


def _benchmark_query(market: MarketName, config: _MarketConfig):
    return text(
        f"""/* daily_candles_readiness:benchmark:{market} */
        SELECT
            MIN(time) AS start_at,
            MAX(time) AS end_at,
            COUNT(DISTINCT time) AS bar_count,
            STRING_AGG(DISTINCT source, ',' ORDER BY source) AS sources
        FROM public.{config.candle_table}
        WHERE symbol = :symbol
          AND time <= :as_of
        """
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _completed_expected_sessions(
    market: MarketName,
    as_of: datetime,
) -> tuple[date, ...]:
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
    return tuple(completed[-REQUIRED_HISTORY_BARS:])


def _int(row: Mapping[str, object], key: str) -> int:
    return max(int(row.get(key) or 0), 0)


def _is_delisted(row: Mapping[str, object]) -> bool:
    if row.get("delist_date") is not None:
        return True
    status = str(row.get("listing_status") or "").strip().casefold()
    return status in _DELISTED_STATUSES


def _benchmark_coverage(
    market: MarketName,
    config: _MarketConfig,
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
        symbol=config.benchmark_symbol,
        start=cast(datetime | None, row.get("start_at")),
        end=cast(datetime | None, row.get("end_at")),
        count=count,
        source=source,
        sources=sources,
        status=status,
    )


def _issue(market: MarketName, code: str) -> str:
    return f"{market}:{code}"


def _evaluate_market(
    *,
    market: MarketName,
    rows: Sequence[Mapping[str, object]],
    expected_sessions: tuple[date, ...],
    benchmark: BenchmarkCoverage,
) -> MarketReadiness:
    total = len(rows)
    active_rows = [row for row in rows if bool(row.get("is_active"))]
    inactive_rows = [row for row in rows if not bool(row.get("is_active"))]
    active = len(active_rows)
    inactive = len(inactive_rows)

    exactly_251 = sum(1 for row in rows if _int(row, "bar_count") == 251)
    at_least_252 = sum(
        1 for row in rows if _int(row, "bar_count") >= REQUIRED_HISTORY_BARS
    )
    future = sum(_int(row, "future_bar_count") for row in rows)
    duplicates = sum(_int(row, "duplicate_timestamp_count") for row in rows)
    ohlc = sum(_int(row, "ohlc_anomaly_count") for row in rows)

    calendar_status: CalendarStatus = (
        "available" if expected_sessions else "unavailable"
    )
    latest_expected = expected_sessions[-1] if expected_sessions else None
    stale = 0
    missing: int | None = 0 if expected_sessions else None
    eligible = 0
    for row in active_rows:
        latest = cast(datetime | None, row.get("latest_bar_at"))
        row_stale = bool(
            latest_expected is not None
            and latest is not None
            and latest.astimezone(UTC).date() < latest_expected
        )
        stale += int(row_stale)

        row_missing = 0
        if expected_sessions:
            listed = cast(date | None, row.get("list_date"))
            expected_count = sum(
                1
                for session_day in expected_sessions
                if listed is None or session_day >= listed
            )
            row_missing = max(
                expected_count - _int(row, "observed_expected_session_count"),
                0,
            )
            missing = cast(int, missing) + row_missing

        if (
            _int(row, "bar_count") >= REQUIRED_HISTORY_BARS
            and not row_stale
            and row_missing == 0
            and _int(row, "future_bar_count") == 0
            and _int(row, "duplicate_timestamp_count") == 0
            and _int(row, "ohlc_anomaly_count") == 0
        ):
            eligible += 1

    list_date_covered = sum(1 for row in rows if row.get("list_date") is not None)
    delist_date_covered_inactive = sum(
        1 for row in inactive_rows if row.get("delist_date") is not None
    )
    point_in_time = bool(
        total > 0
        and list_date_covered == total
        and delist_date_covered_inactive == inactive
    )

    inactive_with_candles = sum(
        1 for row in inactive_rows if _int(row, "bar_count") > 0
    )
    delisted_rows = [row for row in inactive_rows if _is_delisted(row)]
    delisted_with_candles = sum(
        1 for row in delisted_rows if _int(row, "bar_count") > 0
    )
    includes_delisted = bool(
        delisted_rows and delisted_with_candles == len(delisted_rows)
    )

    candle_rows = sum(_int(row, "candle_row_count") for row in rows)
    fallback_only = bool(
        candle_rows > 0
        and all(
            bool(row.get("fallback_only"))
            for row in rows
            if _int(row, "candle_row_count") > 0
        )
    )

    adjustment_suspects = sum(
        _int(row, "corporate_action_suspect_count") for row in rows
    )
    missing_adjustments = sum(_int(row, "missing_adjusted_close_count") for row in rows)
    if adjustment_suspects:
        corporate_action: CorporateActionStatus = "suspected"
    elif market == "kr" or candle_rows == 0 or missing_adjustments:
        corporate_action = "unknown"
    else:
        corporate_action = "clear"

    blockers: list[str] = []
    reasons: list[str] = []

    def block(code: str) -> None:
        value = _issue(market, code)
        blockers.append(value)
        reasons.append(value)

    if total == 0:
        block("empty_universe")
    if eligible == 0:
        block("eligible_symbols_zero")
    if (
        active > 0
        and sum(
            1 for row in active_rows if _int(row, "bar_count") >= REQUIRED_HISTORY_BARS
        )
        < active
    ):
        block("insufficient_history")
    if stale:
        block("stale_bar")
    if future:
        block("future_bar")
    if duplicates:
        block("duplicate_bar_timestamp")
    if ohlc:
        block("invalid_ohlcv")
    if calendar_status == "unavailable":
        block("calendar_unavailable")
    elif missing:
        block("missing_expected_trading_days")
    if benchmark.status != "available":
        block("benchmark_unavailable")
    if not point_in_time:
        block("point_in_time_unavailable")
    if not includes_delisted:
        block("delisted_not_included")
    if corporate_action == "suspected":
        block("corporate_action_suspected")
    elif corporate_action == "unknown":
        block("corporate_action_unknown")
    if fallback_only:
        reasons.append(_issue(market, "fallback_only"))

    return MarketReadiness(
        market=market,
        total_symbol_count=total,
        active_symbol_count=active,
        inactive_symbol_count=inactive,
        symbols_with_exactly_251_bars=exactly_251,
        symbols_with_at_least_252_bars=at_least_252,
        eligible_symbol_count=eligible,
        stale_bar_count=stale,
        future_bar_count=future,
        duplicate_timestamp_count=duplicates,
        ohlc_anomaly_count=ohlc,
        missing_expected_trading_day_count=missing,
        calendar_status=calendar_status,
        corporate_action_status=corporate_action,
        list_date_covered_symbol_count=list_date_covered,
        delist_date_covered_inactive_count=delist_date_covered_inactive,
        point_in_time_available=point_in_time,
        inactive_with_candles_count=inactive_with_candles,
        delisted_symbol_count=len(delisted_rows),
        delisted_with_candles_count=delisted_with_candles,
        includes_delisted=includes_delisted,
        fallback_only=fallback_only,
        benchmark=benchmark,
        blockers=tuple(blockers),
        reasons=tuple(reasons),
    )


class DailyCandlesReadinessService:
    """Measure DB-backed PAPER promotion readiness using SELECT statements only."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def measure(
        self,
        *,
        as_of: datetime | None = None,
        markets: tuple[MarketName, ...] = ("kr", "us"),
    ) -> DailyCandlesReadiness:
        measured_at = _aware_utc(as_of or datetime.now(UTC))
        if not markets or len(set(markets)) != len(markets):
            raise ValueError("markets must be a non-empty unique tuple")
        unsupported = set(markets) - set(_CONFIG)
        if unsupported:
            raise ValueError(f"unsupported markets: {sorted(unsupported)!r}")

        market_results: list[MarketReadiness] = []
        for market in markets:
            config = _CONFIG[market]
            expected_sessions = _completed_expected_sessions(market, measured_at)
            result = await self._db.execute(
                _market_query(market, config),
                {
                    "as_of": measured_at,
                    "expected_sessions": list(expected_sessions),
                },
            )
            rows = cast(
                Sequence[Mapping[str, object]],
                result.mappings().all(),
            )
            benchmark_result = await self._db.execute(
                _benchmark_query(market, config),
                {"symbol": config.benchmark_symbol, "as_of": measured_at},
            )
            benchmark_row = cast(
                Mapping[str, object],
                benchmark_result.mappings().one(),
            )
            benchmark = _benchmark_coverage(market, config, benchmark_row)
            market_results.append(
                _evaluate_market(
                    market=market,
                    rows=rows,
                    expected_sessions=expected_sessions,
                    benchmark=benchmark,
                )
            )

        blockers = tuple(
            blocker for item in market_results for blocker in item.blockers
        )
        reasons = tuple(reason for item in market_results for reason in item.reasons)
        return DailyCandlesReadiness(
            as_of=measured_at,
            required_history_bars=REQUIRED_HISTORY_BARS,
            markets=tuple(market_results),
            promotion_ready=not blockers,
            blockers=blockers,
            reasons=reasons,
        )


__all__ = [
    "BenchmarkCoverage",
    "DailyCandlesReadiness",
    "DailyCandlesReadinessService",
    "MarketReadiness",
    "REQUIRED_BENCHMARK_BARS",
    "REQUIRED_HISTORY_BARS",
]
