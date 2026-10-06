"""Find and refill holes in the Toss 1m research table for one KST session date.

The minute collector pages back over holes it can still see and lists the ones
it gives up on in ``unfilled_gaps``. Once a newer page is stored it can no
longer see an older hole, so those holes, and any left before this repair
existed, are filled here one session date at a time.

Expected minutes come from the stored data itself, because Toss pads quiet
minutes with zero-volume bars and a complete symbol has every minute of its
segments:

* A symbol is expected in the segments it has on that date plus the segments
  it has on both neighbouring stored session dates, so a segment lost whole is
  still found.
* Symbols with the same expected segments form a class (NXT names, KRX names
  with after-hours bars, KRX-only names). A class's grid for a segment is the
  minutes at least half of its members with bars in that segment have on that
  date, plus the KST clock minutes that held that majority on every
  neighbouring date: an outage that hit most symbols at once must not shrink
  the grid. A class smaller than ``_CLASS_GRID_MIN_SYMBOLS`` borrows each
  segment from the largest class that has it.
* On a shortened session (for example a delayed open) the neighbours' clock
  minutes that did not trade that day are planned too; ``--commit`` then
  reports them as ``minutes_not_returned`` and writes nothing for them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from app.services.research_candles.toss_minute_source import (
    TOSS_MINUTE_FETCH_COUNT,
    TossMinuteCandleRow,
    TossMinutePage,
)

KST = ZoneInfo("Asia/Seoul")
# Symbols whose stored minutes are read and diffed at once while planning.
_PLAN_SYMBOL_CHUNK = 250
_CLASS_GRID_MIN_SYMBOLS = 3
logger = logging.getLogger(__name__)

Grid = dict[str, tuple[datetime, ...]]


class GapRepository(Protocol):
    async def neighbour_session_dates(
        self, *, session_date: date
    ) -> tuple[date | None, date | None]: ...

    async def session_segment_counts(
        self, *, session_date: date
    ) -> list[tuple[str, str, int]]: ...

    async def session_minute_presence(
        self, *, session_date: date, symbols: Sequence[str] | None = None
    ) -> list[tuple[str, datetime, int]]: ...

    async def session_minutes(
        self, *, session_date: date, symbols: Sequence[str]
    ) -> list[tuple[str, datetime]]: ...

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int: ...


class PageSource(Protocol):
    async def fetch(
        self,
        *,
        symbol: str,
        retrieved_at: datetime,
        batch_id: str,
        before: str | None = None,
    ) -> TossMinutePage: ...


def _kst_text(minute: datetime) -> str:
    return minute.astimezone(KST).isoformat()


@dataclass(frozen=True, slots=True)
class SymbolGap:
    symbol: str
    expected: tuple[datetime, ...]
    missing: tuple[datetime, ...]

    def runs(self) -> int:
        """Number of contiguous stretches of missing grid minutes."""

        position = {minute: index for index, minute in enumerate(self.expected)}
        indexes = [position[minute] for minute in self.missing]
        return sum(
            1
            for order, index in enumerate(indexes)
            if order == 0 or index != indexes[order - 1] + 1
        )

    def page_cursors(self, page_size: int = TOSS_MINUTE_FETCH_COUNT) -> list[datetime]:
        """Inclusive ``before`` cursors a backward walk needs, assuming Toss
        returns every grid minute (padding included)."""

        position = {minute: index for index, minute in enumerate(self.expected)}
        cursors: list[datetime] = []
        remaining = list(self.missing)
        while remaining:
            cursor = remaining[-1]
            cursors.append(cursor)
            first_covered = position[cursor] - page_size + 1
            if first_covered <= 0:
                break  # this page already reaches the previous session
            floor = self.expected[first_covered]
            remaining = [minute for minute in remaining if minute < floor]
        return cursors


@dataclass(frozen=True, slots=True)
class SymbolClass:
    segments: tuple[str, ...]
    symbols: int
    grid: Grid


@dataclass(frozen=True, slots=True)
class SessionGapPlan:
    session_date: date
    neighbour_dates: tuple[date | None, date | None]
    classes: tuple[SymbolClass, ...]
    symbols_expected: int
    symbols_without_rows: int
    gaps: tuple[SymbolGap, ...]

    def summary(self, *, top: int) -> dict[str, object]:
        ranked = sorted(self.gaps, key=lambda gap: (-len(gap.missing), gap.symbol))
        return {
            "session_date": self.session_date.isoformat(),
            "neighbour_session_dates": [
                day.isoformat() if day else None for day in self.neighbour_dates
            ],
            "symbol_classes": [
                {
                    "segments": list(item.segments),
                    "symbols": item.symbols,
                    "grid_minutes": {seg: len(m) for seg, m in item.grid.items()},
                }
                for item in self.classes
            ],
            "symbols_expected": self.symbols_expected,
            "symbols_without_rows": self.symbols_without_rows,
            "symbols_with_gaps": len(self.gaps),
            "missing_minutes": sum(len(gap.missing) for gap in self.gaps),
            "gap_runs": sum(gap.runs() for gap in self.gaps),
            "estimated_calls": sum(len(gap.page_cursors()) for gap in self.gaps),
            "largest_gaps": [
                {
                    "symbol": gap.symbol,
                    "missing_minutes": len(gap.missing),
                    "gap_runs": gap.runs(),
                    "estimated_calls": len(gap.page_cursors()),
                    "first_missing_kst": _kst_text(gap.missing[0]),
                    "last_missing_kst": _kst_text(gap.missing[-1]),
                }
                for gap in ranked[:top]
            ],
        }


def _counts_by_symbol(
    counts: Iterable[tuple[str, str, int]],
) -> dict[str, dict[str, int]]:
    by_symbol: dict[str, dict[str, int]] = defaultdict(dict)
    for symbol, segment, count in counts:
        by_symbol[symbol][segment] = count
    return by_symbol


def _majority_grid(
    presence: Iterable[tuple[str, datetime, int]], participants: dict[str, int]
) -> Grid:
    minutes: dict[str, list[datetime]] = defaultdict(list)
    for segment, minute, present in presence:
        if participants.get(segment) and 2 * present >= participants[segment]:
            minutes[segment].append(minute)
    return {segment: tuple(sorted(found)) for segment, found in minutes.items()}


def _participants(
    members: Iterable[str], on_day: dict[str, dict[str, int]]
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for symbol in members:
        for segment in on_day.get(symbol, {}):
            counts[segment] += 1
    return counts


def _expected_segments(
    on_day: dict[str, dict[str, int]],
    neighbours: Sequence[dict[str, dict[str, int]]],
) -> dict[str, frozenset[str]]:
    """Segments on the date plus those present on every neighbouring date."""

    around: dict[str, set[str]] | None = None
    for counts in neighbours:
        segments = {symbol: set(by_segment) for symbol, by_segment in counts.items()}
        around = (
            segments
            if around is None
            else {
                symbol: around[symbol] & segments[symbol]
                for symbol in around.keys() & segments.keys()
            }
        )
    around = around or {}
    expected: dict[str, frozenset[str]] = {}
    for symbol in on_day.keys() | around.keys():
        own = frozenset(on_day.get(symbol, {}))
        segments_expected = own | around.get(symbol, set())
        if segments_expected:
            expected[symbol] = segments_expected
    return expected


async def _class_grid(
    repository: GapRepository,
    *,
    members: list[str],
    session_date: date,
    on_day: dict[str, dict[str, int]],
    neighbours: Sequence[tuple[date, dict[str, dict[str, int]]]],
) -> Grid:
    """Majority minutes on the date plus the KST clock minutes that were a
    majority on every neighbouring date.

    A broad outage removes the same minutes from most symbols, so the date's
    own majority alone would shrink the grid and hide the outage.
    """

    merged: dict[str, set[datetime]] = defaultdict(set)
    for segment, minutes in _majority_grid(
        await repository.session_minute_presence(
            session_date=session_date, symbols=members
        ),
        _participants(members, on_day),
    ).items():
        merged[segment].update(minutes)
    shared: dict[str, set[time]] | None = None
    for day, counts in neighbours:
        clocks = {
            segment: {minute.astimezone(KST).time() for minute in minutes}
            for segment, minutes in _majority_grid(
                await repository.session_minute_presence(
                    session_date=day, symbols=members
                ),
                _participants(members, counts),
            ).items()
        }
        shared = (
            clocks
            if shared is None
            else {
                segment: shared[segment] & clocks[segment]
                for segment in shared.keys() & clocks.keys()
            }
        )
    for segment, clock_minutes in (shared or {}).items():
        merged[segment].update(
            datetime.combine(session_date, clock, tzinfo=KST).astimezone(UTC)
            for clock in clock_minutes
        )
    return {segment: tuple(sorted(minutes)) for segment, minutes in merged.items()}


async def plan_session_gaps(
    repository: GapRepository,
    *,
    session_date: date,
    symbols: Sequence[str] | None = None,
) -> SessionGapPlan:
    """Read-only: derive the session grids and every symbol's missing minutes."""

    on_day = _counts_by_symbol(
        await repository.session_segment_counts(session_date=session_date)
    )
    neighbour_dates = await repository.neighbour_session_dates(
        session_date=session_date
    )
    neighbours: list[tuple[date, dict[str, dict[str, int]]]] = []
    for day in neighbour_dates:
        if day is not None:
            counts = await repository.session_segment_counts(session_date=day)
            neighbours.append((day, _counts_by_symbol(counts)))
    expected_segments = _expected_segments(on_day, [c for _, c in neighbours])
    wanted = set(symbols) if symbols else None
    in_scope = sorted(
        symbol for symbol in expected_segments if wanted is None or symbol in wanted
    )

    members_by_class: dict[frozenset[str], list[str]] = defaultdict(list)
    for symbol in sorted(expected_segments):
        members_by_class[expected_segments[symbol]].append(symbol)
    ordered = sorted(
        members_by_class.items(), key=lambda item: (-len(item[1]), sorted(item[0]))
    )
    grids: dict[frozenset[str], Grid] = {}
    for segments, members in ordered:
        if len(members) >= _CLASS_GRID_MIN_SYMBOLS:
            grids[segments] = await _class_grid(
                repository,
                members=members,
                session_date=session_date,
                on_day=on_day,
                neighbours=neighbours,
            )
    # A class too small for a majority borrows each segment from the largest
    # class that has it.
    borrowed: Grid = {}
    for segments, _ in ordered:
        for segment, minutes in grids.get(segments, {}).items():
            borrowed.setdefault(segment, minutes)
    scoped_classes = {expected_segments[symbol] for symbol in in_scope}
    classes: list[SymbolClass] = []
    for segments, members in ordered:
        grid = {
            segment: grids.get(segments, {}).get(segment) or borrowed.get(segment, ())
            for segment in sorted(segments)
        }
        grids[segments] = grid
        if segments in scoped_classes:
            classes.append(
                SymbolClass(
                    segments=tuple(sorted(segments)), symbols=len(members), grid=grid
                )
            )

    candidates = [
        symbol
        for symbol in in_scope
        if any(
            on_day.get(symbol, {}).get(segment, 0) != len(minutes)
            for segment, minutes in grids[expected_segments[symbol]].items()
        )
    ]
    gaps: list[SymbolGap] = []
    for start in range(0, len(candidates), _PLAN_SYMBOL_CHUNK):
        chunk = candidates[start : start + _PLAN_SYMBOL_CHUNK]
        stored: dict[str, set[datetime]] = defaultdict(set)
        for symbol, minute in await repository.session_minutes(
            session_date=session_date, symbols=chunk
        ):
            stored[symbol].add(minute)
        for symbol in chunk:
            expected = tuple(
                sorted(
                    minute
                    for minutes in grids[expected_segments[symbol]].values()
                    for minute in minutes
                )
            )
            missing = tuple(m for m in expected if m not in stored[symbol])
            if missing:
                gaps.append(
                    SymbolGap(symbol=symbol, expected=expected, missing=missing)
                )
    return SessionGapPlan(
        session_date=session_date,
        neighbour_dates=neighbour_dates,
        classes=tuple(classes),
        symbols_expected=len(in_scope),
        symbols_without_rows=sum(1 for symbol in in_scope if symbol not in on_day),
        gaps=tuple(gaps),
    )


@dataclass
class RepairOutcome:
    calls: int = 0
    rows_upserted: int = 0
    minutes_filled: int = 0
    minutes_not_returned: int = 0
    symbols_repaired: int = 0
    failed_symbols: dict[str, str] = field(default_factory=dict)
    stopped_at_call_cap: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "rows_upserted": self.rows_upserted,
            "minutes_filled": self.minutes_filled,
            "minutes_not_returned": self.minutes_not_returned,
            "symbols_repaired": self.symbols_repaired,
            "symbols_failed": len(self.failed_symbols),
            "failed_symbols": self.failed_symbols,
            "stopped_at_call_cap": self.stopped_at_call_cap,
        }


async def repair_session_gaps(
    *,
    plan: SessionGapPlan,
    source: PageSource,
    repository: GapRepository,
    commit: Callable[[], Awaitable[None]],
    max_calls: int | None,
    now: datetime | None = None,
) -> RepairOutcome:
    """Walk ``before`` back over each symbol's holes and upsert that date's bars.

    Each symbol is committed on its own, so an interrupted run keeps what it
    wrote and a rerun only plans what is still missing.
    """

    retrieved_at = (now or datetime.now(UTC)).astimezone(UTC)
    batch_id = (
        f"toss-1m-repair-{plan.session_date:%Y%m%d}-{retrieved_at:%Y%m%dT%H%M%SZ}"
    )
    outcome = RepairOutcome()
    for gap in plan.gaps:
        if max_calls is not None and outcome.calls >= max_calls:
            outcome.stopped_at_call_cap = True
            break
        rows: list[TossMinuteCandleRow] = []
        remaining = list(gap.missing)
        try:
            while remaining:
                if max_calls is not None and outcome.calls >= max_calls:
                    outcome.stopped_at_call_cap = True
                    break
                outcome.calls += 1
                page = await source.fetch(
                    symbol=gap.symbol,
                    retrieved_at=retrieved_at,
                    batch_id=batch_id,
                    before=_kst_text(remaining[-1]),
                )
                rows.extend(
                    row
                    for row in page.rows
                    if row.session_date_kst == plan.session_date
                )
                oldest = page.oldest_time_utc
                if oldest is None:
                    break
                remaining = [minute for minute in remaining if minute < oldest]
        except Exception as exc:  # one symbol's provider failure does not stop the run
            outcome.failed_symbols[gap.symbol] = f"{type(exc).__name__}: {exc}"
            logger.warning("Toss minute repair failed symbol=%s: %s", gap.symbol, exc)
        if rows:
            outcome.rows_upserted += await repository.upsert(rows)
            await commit()
            outcome.symbols_repaired += 1
        returned = {row.time_utc for row in rows}
        filled = sum(1 for minute in gap.missing if minute in returned)
        outcome.minutes_filled += filled
        if not outcome.stopped_at_call_cap and gap.symbol not in outcome.failed_symbols:
            outcome.minutes_not_returned += len(gap.missing) - filled
        if outcome.stopped_at_call_cap:
            break
    return outcome
