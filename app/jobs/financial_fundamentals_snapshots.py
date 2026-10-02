"""Dry-run-first job runner for financial_fundamentals_snapshots (ROB-422 PR1, KR-only)."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST
from app.mcp_server.tooling.screening.instrument_type import classify_kr_instrument
from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.builder import (
    DartFetchPlan,
    FundamentalsFetcher,
    build_financial_fundamentals_for_symbols,
    dart_fetch_plan,
    default_dart_fetcher,
    latest_completed_period,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)
from app.services.snapshot_commit_guard import PartialCommitBlocked


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    include_quarterly: bool = False
    concurrency: int = 4
    commit: bool = False
    collected_at: dt.datetime | None = None
    estimate_only: bool = False
    allow_partial: bool = False
    # ROB-441/433: DART budget-split. Skip symbols that already have a snapshot so
    # daily re-runs advance through uncollected DART-eligible common stocks (the
    # full KR active common-stock universe exceeds the 18k daily budget → must be
    # split across days). With --limit N this selects the NEXT N uncollected
    # symbols after excluding preferred/ETF/REIT/SPAC/non-6-digit universe rows.
    # One-shot backfill mode: a collected symbol is never revisited here.
    skip_existing: bool = False
    # Continuous refresh mode (exclusive with skip_existing): pick the symbols
    # whose stored rows are missing the newest ended period, carry partial rows,
    # were never collected, or are due for a correction sweep — see
    # select_refresh_due — and fit them into the daily DART budget.
    refresh_due: bool = False


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotSample:
    symbol: str
    fiscal_period: str
    period_type: str
    filing_date: dt.date | None
    revenue: Decimal | None
    net_income: Decimal | None
    payout_ratio: Decimal | None
    data_state: str


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    started_at: dt.datetime
    finished_at: dt.datetime
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[FinancialFundamentalsSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()
    # Upper bound of DART calls for the selected symbols (DartFetchPlan.max_requests).
    projected_requests: int | None = None
    # Due symbols left for a later run because --limit or the daily budget was full.
    deferred_symbols: int = 0
    # Selected symbols per refresh reason (refresh_due mode only).
    selection: dict[str, int] = field(default_factory=dict)
    # The metered budget ran out mid-fetch; nothing was committed. Not a success.
    budget_exhausted: bool = False
    # Symbols were fetched but every fetch failed or came back empty: no row was
    # built, so nothing was collected. Not a success (no-due runs never fetch).
    no_rows_collected: bool = False


_KR_DART_SYMBOL_RE = re.compile(r"^\d{6}$")


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm != "kr":
        raise ValueError(f"PR1 supports market='kr' only, got: {market}")
    return market_norm


def _is_kr_dart_common_symbol(
    symbol: object,
    name: object,
    security_type: object = None,
    is_common_share: object = None,
) -> bool:
    """Return whether a KR universe row is suitable for OpenDART common-stock fetches.

    The DART backfill uses stock-code lookups. The active KR universe can contain
    non-6-digit synthetic/exchange codes (for example NXT-like letter suffixes) and
    non-common instruments such as preferred shares, ETFs, REITs, and SPACs. Those
    rows should not consume OpenDART budget in the default backfill candidate path.
    Explicit ``--symbol`` overrides remain operator-controlled and are not filtered
    here.

    ``security_type``/``is_common_share`` come from the Toss master columns of
    ``kr_symbol_universe`` and are authoritative when present (an ETF whose name
    carries no ETF token used to slip through, and a confirmed common share
    whose code ends in 5/7/9 used to be dropped as preferred). Rows the master
    has not classified yet fall back to the name rules. REIT/SPAC names stay
    excluded either way.
    """
    symbol_text = str(symbol or "").strip().upper()
    if not _KR_DART_SYMBOL_RE.fullmatch(symbol_text):
        return False
    if security_type is not None and str(security_type).strip().upper() != "STOCK":
        return False
    if is_common_share is False:
        return False
    kind = classify_kr_instrument(symbol_text, name, security_type)
    if kind == "preferred" and is_common_share is True:
        return True
    return kind == "common"


def _filter_kr_dart_common_symbols(
    rows: list[tuple[str, str, str | None, bool | None]],
) -> list[str]:
    return [
        str(symbol).strip().upper()
        for symbol, name, security_type, is_common_share in rows
        if _is_kr_dart_common_symbol(symbol, name, security_type, is_common_share)
    ]


async def _kr_dart_common_universe() -> list[str]:
    from app.models.kr_symbol_universe import KRSymbolUniverse

    async with AsyncSessionLocal() as session:
        stmt = (
            sa.select(
                KRSymbolUniverse.symbol,
                KRSymbolUniverse.name,
                KRSymbolUniverse.security_type,
                KRSymbolUniverse.is_common_share,
            )
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol)
        )
        result = await session.execute(stmt)
        rows = [(r[0], r[1], r[2], r[3]) for r in result.all()]
        return _filter_kr_dart_common_symbols(rows)


async def resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    _validate_market(market)
    if override:
        return [s.strip().upper() for s in override if s.strip()]
    return (await _kr_dart_common_universe())[:limit]


async def resolve_active_universe(market: str) -> list[str]:
    _validate_market(market)
    return await _kr_dart_common_universe()


async def _already_collected_symbols(market: str) -> set[str]:
    """Symbols with ≥1 existing financial_fundamentals snapshot (ROB-441 budget-split:
    --skip-existing drops these so daily re-runs advance through uncollected symbols)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(FinancialFundamentalsSnapshot.symbol)
            .where(FinancialFundamentalsSnapshot.market == market)
            .distinct()
        )
        return {r[0] for r in result.all()}


@dataclass(frozen=True)
class SymbolCollectionEvidence:
    """What the stored DART rows already prove about one symbol."""

    # Newest source_collected_at: rows are only written by a fetch that
    # succeeded, so a failed fetch never advances this.
    last_collected_at: dt.datetime
    has_latest_period: bool
    earliest_partial_period_end: dt.date | None


async def _collection_evidence(
    market: str, latest_period: str, *, partial_since: dt.date
) -> dict[str, SymbolCollectionEvidence]:
    """One aggregate per symbol. Partial rows before ``partial_since`` (outside
    the 5-year fetch window) are ignored: no plan can ever re-fetch them."""
    f = FinancialFundamentalsSnapshot
    partial_in_window = sa.and_(
        f.data_state == "partial", f.period_end_date >= partial_since
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                f.symbol,
                sa.func.max(f.source_collected_at),
                sa.func.bool_or(f.fiscal_period == latest_period),
                sa.func.min(sa.case((partial_in_window, f.period_end_date))),
            )
            .where(f.market == market, f.source == "dart")
            .group_by(f.symbol)
        )
        return {
            symbol: SymbolCollectionEvidence(
                last_collected_at=last,
                has_latest_period=bool(has_latest),
                earliest_partial_period_end=partial_end,
            )
            for symbol, last, has_latest, partial_end in result.all()
        }


@dataclass(frozen=True)
class FundamentalsCollectionTarget:
    symbol: str
    reason: str
    plan: DartFetchPlan


# Conservative defaults against the shared 20k/day OpenDART key: an ended period
# that is still unfiled is rechecked weekly, and every collected symbol is
# re-fetched at least monthly so 정정 filings replace the stored figures.
REFRESH_RECHECK_INTERVAL = dt.timedelta(days=7)
REFRESH_CORRECTION_INTERVAL = dt.timedelta(days=30)
_FULL_ANNUAL_YEARS = 5
_RECENT_ANNUAL_YEARS = 1


class _BudgetAllocator:
    """Greedy fit of targets under a symbol cap and the metered request budget.

    Uses each plan's worst-case call count, so a run that stays inside the
    allocation cannot trip the fail-stop in increment_and_check_budget.
    """

    def __init__(self, *, limit: int | None, budget: int) -> None:
        self.limit = limit
        self.budget = budget
        self.selected: list[FundamentalsCollectionTarget] = []
        self.spent = 0
        self.deferred = 0

    def room_for(self, cost: int) -> int:
        slots = [] if self.limit is None else [self.limit - len(self.selected)]
        if self.budget > 0:
            slots.append((self.budget - self.spent) // max(1, cost))
        return max(0, min(slots)) if slots else -1

    def offer(self, target: FundamentalsCollectionTarget) -> None:
        cost = target.plan.max_requests
        if self.limit is not None and len(self.selected) >= self.limit:
            self.deferred += 1
        elif self.budget > 0 and self.spent + cost > self.budget:
            self.deferred += 1
        else:
            self.selected.append(target)
            self.spent += cost


def select_refresh_due(
    pool: list[str],
    evidence: dict[str, SymbolCollectionEvidence],
    *,
    now: dt.datetime,
    include_quarterly: bool,
    limit: int | None,
    budget: int,
) -> _BudgetAllocator:
    """Order due symbols by reason and fit them into ``limit`` and ``budget``.

    1. missing_latest — the newest ended period is not stored and either nobody
       looked since it ended or the last look is a week old (unfiled yet).
    2. partial — a row in the fetch window has no resolvable filing date;
       retried weekly.
    3. uncollected — no row at all; full 5-year backfill.
    4. stale — the correction sweep, oldest collection first.

    Failed fetches write no rows, so a failing symbol stays due and keeps its
    place. To keep such failures from starving everything behind them, a
    fairness slice of the sorted pool is served first: it holds as many
    symbols as always fit (worst-case plan) and advances by its own size each
    day, so every due symbol is selected within ceil(len(pool) / slice) days.
    The rest of the budget then goes to the priority order above.
    """
    today = now.astimezone(KST).date()
    latest_period, latest_end = latest_completed_period(
        today, include_quarterly=include_quarterly
    )
    latest_ended_at = dt.datetime.combine(
        latest_end + dt.timedelta(days=1), dt.time.min, tzinfo=KST
    )
    full = dart_fetch_plan(
        today=today,
        include_quarterly=include_quarterly,
        annual_years=_FULL_ANNUAL_YEARS,
    )
    recent = dart_fetch_plan(
        today=today,
        include_quarterly=include_quarterly,
        annual_years=_RECENT_ANNUAL_YEARS,
    )
    recent_first_year = min(y.year for y in recent.years)
    recheck_before = now - REFRESH_RECHECK_INTERVAL
    correction_before = now - REFRESH_CORRECTION_INTERVAL

    missing: list[tuple[dt.datetime, str, DartFetchPlan]] = []
    partial: list[tuple[dt.datetime, str, DartFetchPlan]] = []
    stale: list[tuple[dt.datetime, str, DartFetchPlan]] = []
    uncollected: list[tuple[dt.datetime, str, DartFetchPlan]] = []
    for symbol in pool:
        ev = evidence.get(symbol)
        if ev is None:
            uncollected.append((now, symbol, full))
            continue
        # Partial rows older than the recent window need the full window again.
        plan = (
            full
            if ev.earliest_partial_period_end is not None
            and ev.earliest_partial_period_end.year < recent_first_year
            else recent
        )
        entry = (ev.last_collected_at, symbol, plan)
        rechecked = ev.last_collected_at <= recheck_before
        if not ev.has_latest_period and (
            ev.last_collected_at < latest_ended_at or rechecked
        ):
            missing.append(entry)
        elif ev.earliest_partial_period_end is not None and rechecked:
            partial.append(entry)
        elif ev.last_collected_at <= correction_before:
            stale.append(entry)

    ordered = [
        FundamentalsCollectionTarget(symbol, reason, plan)
        for reason, entries in (
            ("missing_latest", missing),
            ("partial", partial),
            ("uncollected", uncollected),
            ("stale", stale),
        )
        for _, symbol, plan in sorted(entries, key=lambda e: (e[0], e[1]))
    ]
    alloc = _BudgetAllocator(limit=limit, budget=budget)
    slice_size = alloc.room_for(full.max_requests)
    head: set[str] = set()
    if 0 < slice_size < len(ordered):
        ring = sorted(set(pool))
        start = (today.toordinal() * slice_size) % len(ring)
        head = {ring[(start + i) % len(ring)] for i in range(slice_size)}
    for target in ordered:
        if target.symbol in head:
            alloc.offer(target)
    for target in ordered:
        if target.symbol not in head:
            alloc.offer(target)
    return alloc


def _payload_key(p: FinancialFundamentalsUpsert) -> tuple[str, str, str, str]:
    return (
        p.market.strip().lower(),
        p.symbol.strip().upper(),
        p.fiscal_period,
        p.source.strip().lower(),
    )


# asyncpg는 쿼리당 바인드 인자를 32767개까지 받는다. 키 하나가 인자 4개를 쓰므로
# 전 종목(400 x 분기 포함 약 20 period) 백필이 이 한도를 넘지 않게 나눠 조회한다.
_IDEMPOTENCY_KEYS_PER_QUERY = 5000


async def _classify_idempotency(
    payloads: list[FinancialFundamentalsUpsert],
) -> dict[str, int]:
    keys = [_payload_key(p) for p in payloads]
    duplicate = sum(c - 1 for c in Counter(keys).values() if c > 1)
    unique = set(keys)
    if not unique:
        return {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": duplicate}
    unique_keys = list(unique)
    existing: set[tuple[str, str, str, str]] = set()
    async with AsyncSessionLocal() as session:
        for start in range(0, len(unique_keys), _IDEMPOTENCY_KEYS_PER_QUERY):
            conditions = [
                sa.and_(
                    FinancialFundamentalsSnapshot.market == m,
                    FinancialFundamentalsSnapshot.symbol == s,
                    FinancialFundamentalsSnapshot.fiscal_period == fp,
                    FinancialFundamentalsSnapshot.source == src,
                )
                for m, s, fp, src in unique_keys[
                    start : start + _IDEMPOTENCY_KEYS_PER_QUERY
                ]
            ]
            result = await session.execute(
                sa.select(
                    FinancialFundamentalsSnapshot.market,
                    FinancialFundamentalsSnapshot.symbol,
                    FinancialFundamentalsSnapshot.fiscal_period,
                    FinancialFundamentalsSnapshot.source,
                ).where(sa.or_(*conditions))
            )
            existing.update(tuple(row) for row in result.all())
    return {
        "wouldInsert": len(unique) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate,
    }


async def _commit_payloads(payloads: list[FinancialFundamentalsUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await FinancialFundamentalsSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(p: FinancialFundamentalsUpsert) -> FinancialFundamentalsSnapshotSample:
    return FinancialFundamentalsSnapshotSample(
        symbol=p.symbol,
        fiscal_period=p.fiscal_period,
        period_type=p.period_type,
        filing_date=p.filing_date,
        revenue=p.revenue,
        net_income=p.net_income,
        payout_ratio=p.payout_ratio,
        data_state=p.data_state,
    )


async def run_financial_fundamentals_snapshot_build(
    request: FinancialFundamentalsSnapshotBuildRequest,
    *,
    fetcher: FundamentalsFetcher | None = None,
) -> FinancialFundamentalsSnapshotBuildResult:
    import logging

    logger = logging.getLogger(__name__)

    from app.core.config import settings
    from app.services.financial_fundamentals_snapshots.builder import (
        DartDailyRequestBudgetExceeded,
        reset_request_count,
    )

    if request.refresh_due and request.skip_existing:
        raise ValueError("refresh_due and skip_existing are mutually exclusive")
    market = _validate_market(request.market)
    started_at = dt.datetime.now(dt.UTC)
    collected_at = request.collected_at or started_at
    use_fetcher = fetcher or default_dart_fetcher
    budget = settings.opendart_daily_request_budget
    full_plan = dart_fetch_plan(
        today=collected_at.astimezone(KST).date(),
        include_quarterly=request.include_quarterly,
        annual_years=_FULL_ANNUAL_YEARS,
    )
    requested = [s.strip().upper() for s in request.symbols if s.strip()]
    selection_notes: list[str] = []
    selection: dict[str, int] = {}
    if request.refresh_due:
        pool = requested or await resolve_active_universe(market)
        latest_period, _ = latest_completed_period(
            collected_at.astimezone(KST).date(),
            include_quarterly=request.include_quarterly,
        )
        alloc = select_refresh_due(
            pool,
            await _collection_evidence(
                market, latest_period, partial_since=full_plan.listing_start
            ),
            now=collected_at,
            include_quarterly=request.include_quarterly,
            limit=None if (request.all_symbols or requested) else request.limit or 20,
            budget=budget,
        )
        selection = dict(Counter(t.reason for t in alloc.selected))
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(selection.items()))
        not_due = len(pool) - len(alloc.selected) - alloc.deferred
        selection_notes.append(
            f"refresh_due: latest ended period {latest_period}; "
            f"selected {reasons or 'none'}; {not_due} not due"
        )
    else:
        explicit = bool(requested)
        if request.skip_existing:
            # Budget-split: resolve the DART-eligible common-stock candidate pool,
            # drop already-collected symbols, then (for --limit) take the NEXT N
            # uncollected symbols that fit the daily budget.
            pool = requested or await resolve_active_universe(market)
            done = await _already_collected_symbols(market)
            remaining = [s for s in pool if s not in done]
            skipped_existing = len(pool) - len(remaining)
            if skipped_existing:
                selection_notes.append(
                    f"skip_existing: {skipped_existing} already-collected symbols "
                    f"skipped; {len(remaining)} uncollected remain"
                )
            limit = None if (request.all_symbols or explicit) else request.limit or 20
        else:
            remaining = await (
                resolve_active_universe(market)
                if request.all_symbols
                else resolve_symbols(market, requested, request.limit or 20)
            )
            limit = None
        # Explicit --symbol lists stay operator-controlled: projected, not trimmed.
        alloc = _BudgetAllocator(limit=limit, budget=0 if explicit else budget)
        for symbol in remaining:
            alloc.offer(FundamentalsCollectionTarget(symbol, "backfill", full_plan))
    targets = alloc.selected
    symbols = [t.symbol for t in targets]
    plans = {t.symbol: t.plan for t in targets}
    projected = alloc.spent
    if alloc.deferred:
        selection_notes.append(
            f"deferred: {alloc.deferred} due symbols left for a later run "
            f"(limit/daily budget {budget})"
        )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market,
            symbols_resolved=0,
            snapshots_built=0,
            committed=False,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=(*selection_notes, "no symbols resolved"),
            projected_requests=0,
            deferred_symbols=alloc.deferred,
            selection=selection,
        )

    logger.info(
        "Projected DART requests for %d symbols (include_quarterly=%s): %d (budget: %d)",
        len(symbols),
        request.include_quarterly,
        projected,
        budget,
    )

    if request.estimate_only:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market,
            symbols_resolved=len(symbols),
            snapshots_built=0,
            committed=False,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=(
                *selection_notes,
                f"estimate-only: projected {projected} DART requests; "
                "no fetch performed",
            ),
            projected_requests=projected,
            deferred_symbols=alloc.deferred,
            selection=selection,
        )

    if request.commit and not request.allow_partial:
        raise PartialCommitBlocked(
            "fundamentals commit blocked: fundamentals is an incremental "
            "backfill (DART budget); pass --allow-partial to commit a partial "
            "backfill",
            market=market,
            metric="symbols",
            reason="incremental_backfill",
        )

    reset_request_count()
    should_commit = request.commit
    budget_exhausted = False
    try:
        build = await build_financial_fundamentals_for_symbols(
            market=market,
            symbols=symbols,
            collected_at=collected_at,
            fetcher=use_fetcher,
            include_quarterly=request.include_quarterly,
            concurrency=request.concurrency,
            plans=plans,
        )
        payloads = list(build.payloads)
        warnings = build.warnings
    except DartDailyRequestBudgetExceeded as exc:
        logger.warning("DART daily request budget exceeded during build: %s", exc)
        payloads = list(exc.payloads)
        warnings = exc.warnings
        should_commit = False
        budget_exhausted = True

    idempotency = (
        await _classify_idempotency(payloads)
        if payloads
        else {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0}
    )
    if should_commit and payloads:
        await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return FinancialFundamentalsSnapshotBuildResult(
        market=market,
        symbols_resolved=len(symbols),
        snapshots_built=len(payloads),
        committed=should_commit and bool(payloads),
        started_at=started_at,
        finished_at=finished_at,
        idempotency=idempotency,
        samples=tuple(_sample(p) for p in payloads[:10]),
        warnings=(*selection_notes, *warnings),
        projected_requests=projected,
        deferred_symbols=alloc.deferred,
        selection=selection,
        budget_exhausted=budget_exhausted,
        no_rows_collected=not payloads and not budget_exhausted,
    )
