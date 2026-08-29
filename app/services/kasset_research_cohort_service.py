"""Build immutable current-forward market-cap cohorts for PAPER research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.kasset_research_cohorts import (
    KAssetResearchCohort,
    KAssetResearchCohortMember,
)
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.us_symbol_universe import USSymbolUniverse

Market = Literal["kr", "us"]
_SUPPORTED_US_EXCHANGES = frozenset({"NASD", "NYSE", "AMEX"})
_ALLOWED_SOURCES = {
    "kr": frozenset({"naver_finance", "toss_openapi", "tvscreener"}),
    "us": frozenset({"yahoo", "toss_openapi", "tvscreener"}),
}
_BENCHMARKS = {"kr": "KOSPI", "us": "SPY"}


class KAssetResearchCohortError(RuntimeError):
    pass


@dataclass(frozen=True)
class EligibleValuation:
    symbol: str
    market_cap: Decimal
    eligibility_facts: dict[str, Any]


@dataclass(frozen=True)
class CohortMember:
    symbol: str
    rank: int
    member_kind: Literal["active", "forced", "benchmark"]
    market_cap: Decimal | None
    eligibility_facts: dict[str, Any]


@dataclass(frozen=True)
class KAssetResearchCohortBuildResult:
    cohort_id: str
    mode: str
    market: str
    selection_as_of: str
    selection_date: str
    effective_date: str
    selection_method: str
    requested_size: int
    valuation_snapshot_date: str
    valuation_snapshot_source: str
    active_members: int
    forced_members: int
    benchmark_members: int
    rows_inserted: int
    duplicate: bool
    members: tuple[CohortMember, ...]
    evidence_scope: str = "forward_paper"
    historical_point_in_time_available: bool = False
    historical_point_in_time_note: str = (
        "Eligibility is evaluated from the current universe; this cohort does not "
        "claim historical point-in-time membership before selection_as_of."
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["members"] = [
            {
                **member,
                "market_cap": (
                    str(member["market_cap"])
                    if member["market_cap"] is not None
                    else None
                ),
            }
            for member in payload["members"]
        ]
        return payload


def _normalize_market(value: str) -> Market:
    market = str(value or "").strip().lower()
    if market not in {"kr", "us"}:
        raise ValueError("market must be kr or us")
    return cast(Market, market)


def _normalize_symbol(market: Market, value: str) -> str:
    symbol = str(value or "").strip().upper()
    if market == "us":
        symbol = to_db_symbol(symbol)
    if (
        not symbol
        or len(symbol) > 20
        or any(character.isspace() for character in symbol)
    ):
        raise ValueError(f"invalid {market.upper()} symbol: {value!r}")
    if market == "kr" and (len(symbol) != 6 or not symbol.isalnum()):
        raise ValueError(f"invalid KR symbol: {value!r}")
    return symbol


def assemble_cohort_members(
    *,
    market: Market,
    eligible: list[EligibleValuation],
    requested_size: int,
    forced_symbols: tuple[str, ...],
) -> tuple[CohortMember, ...]:
    if requested_size < 1:
        raise ValueError("requested_size must be >= 1")
    if any(row.market_cap <= 0 for row in eligible):
        raise KAssetResearchCohortError(
            "Eligible cohort rows must have positive market_cap"
        )
    if len({row.symbol for row in eligible}) != len(eligible):
        raise KAssetResearchCohortError("Eligible cohort symbols must be unique")
    eligible = sorted(
        eligible,
        key=lambda row: (-row.market_cap, row.symbol),
    )
    if len(eligible) < requested_size:
        raise KAssetResearchCohortError(
            f"Only {len(eligible)} eligible positive market-cap rows are available; "
            f"requested_size={requested_size}"
        )
    normalized_forced = tuple(
        dict.fromkeys(_normalize_symbol(market, symbol) for symbol in forced_symbols)
    )
    rank_by_symbol = {row.symbol: rank for rank, row in enumerate(eligible, start=1)}
    row_by_symbol = {row.symbol: row for row in eligible}
    missing_forced = [
        symbol for symbol in normalized_forced if symbol not in row_by_symbol
    ]
    if missing_forced:
        raise KAssetResearchCohortError(
            "Forced symbols lack an eligible positive valuation row: "
            + ", ".join(missing_forced)
        )

    core = eligible[:requested_size]
    core_symbols = {row.symbol for row in core}
    members: list[CohortMember] = [
        CohortMember(
            symbol=row.symbol,
            rank=rank,
            member_kind="active",
            market_cap=row.market_cap,
            eligibility_facts=dict(row.eligibility_facts),
        )
        for rank, row in enumerate(core, start=1)
    ]
    members.extend(
        CohortMember(
            symbol=symbol,
            rank=rank_by_symbol[symbol],
            member_kind="forced",
            market_cap=row_by_symbol[symbol].market_cap,
            eligibility_facts=dict(row_by_symbol[symbol].eligibility_facts),
        )
        for symbol in normalized_forced
        if symbol not in core_symbols
    )
    members.append(
        CohortMember(
            symbol=_BENCHMARKS[market],
            rank=1,
            member_kind="benchmark",
            market_cap=None,
            eligibility_facts={
                "benchmark": True,
                "universe_eligibility_applied": False,
            },
        )
    )
    return tuple(members)


async def _latest_snapshot_date(
    db: AsyncSession,
    *,
    market: Market,
    source: str,
    selection_date: date,
) -> date:
    snapshot = MarketValuationSnapshot
    if market == "kr":
        statement = (
            select(func.max(snapshot.snapshot_date))
            .join(KRSymbolUniverse, KRSymbolUniverse.symbol == snapshot.symbol)
            .where(
                snapshot.market == market,
                snapshot.source == source,
                snapshot.snapshot_date <= selection_date,
                snapshot.market_cap > 0,
                KRSymbolUniverse.is_active.is_(True),
                KRSymbolUniverse.security_type == "STOCK",
                KRSymbolUniverse.is_common_share.is_(True),
                KRSymbolUniverse.krx_trading_suspended.is_not(True),
            )
        )
    else:
        statement = (
            select(func.max(snapshot.snapshot_date))
            .join(USSymbolUniverse, USSymbolUniverse.symbol == snapshot.symbol)
            .where(
                snapshot.market == market,
                snapshot.source == source,
                snapshot.snapshot_date <= selection_date,
                snapshot.market_cap > 0,
                USSymbolUniverse.is_active.is_(True),
                USSymbolUniverse.is_common_stock.is_(True),
                USSymbolUniverse.exchange.in_(_SUPPORTED_US_EXCHANGES),
            )
        )
    latest = await db.scalar(statement)
    if latest is None:
        raise KAssetResearchCohortError(
            f"No eligible positive {market}/{source} valuation snapshot exists "
            f"on or before {selection_date.isoformat()}"
        )
    return latest


async def _eligible_rows(
    db: AsyncSession,
    *,
    market: Market,
    source: str,
    snapshot_date: date,
) -> list[EligibleValuation]:
    snapshot = MarketValuationSnapshot
    if market == "kr":
        statement = (
            select(
                snapshot.symbol,
                snapshot.market_cap,
                KRSymbolUniverse.is_active,
                KRSymbolUniverse.security_type,
                KRSymbolUniverse.is_common_share,
                KRSymbolUniverse.krx_trading_suspended,
                KRSymbolUniverse.exchange,
            )
            .join(KRSymbolUniverse, KRSymbolUniverse.symbol == snapshot.symbol)
            .where(
                snapshot.market == market,
                snapshot.source == source,
                snapshot.snapshot_date == snapshot_date,
                snapshot.market_cap > 0,
                KRSymbolUniverse.is_active.is_(True),
                KRSymbolUniverse.security_type == "STOCK",
                KRSymbolUniverse.is_common_share.is_(True),
                KRSymbolUniverse.krx_trading_suspended.is_not(True),
            )
            .order_by(snapshot.market_cap.desc(), snapshot.symbol)
        )
        rows = (await db.execute(statement)).all()
        return [
            EligibleValuation(
                symbol=row.symbol,
                market_cap=row.market_cap,
                eligibility_facts={
                    "is_active": row.is_active,
                    "security_type": row.security_type,
                    "is_common_share": row.is_common_share,
                    "krx_trading_suspended": row.krx_trading_suspended,
                    "exchange": row.exchange,
                },
            )
            for row in rows
        ]

    statement = (
        select(
            snapshot.symbol,
            snapshot.market_cap,
            USSymbolUniverse.is_active,
            USSymbolUniverse.is_common_stock,
            USSymbolUniverse.exchange,
        )
        .join(USSymbolUniverse, USSymbolUniverse.symbol == snapshot.symbol)
        .where(
            snapshot.market == market,
            snapshot.source == source,
            snapshot.snapshot_date == snapshot_date,
            snapshot.market_cap > 0,
            USSymbolUniverse.is_active.is_(True),
            USSymbolUniverse.is_common_stock.is_(True),
            USSymbolUniverse.exchange.in_(_SUPPORTED_US_EXCHANGES),
        )
        .order_by(snapshot.market_cap.desc(), snapshot.symbol)
    )
    rows = (await db.execute(statement)).all()
    return [
        EligibleValuation(
            symbol=row.symbol,
            market_cap=row.market_cap,
            eligibility_facts={
                "is_active": row.is_active,
                "is_common_stock": row.is_common_stock,
                "exchange": row.exchange,
                "supported_exchange": row.exchange in _SUPPORTED_US_EXCHANGES,
            },
        )
        for row in rows
    ]


def _cohort_id(
    *,
    market: Market,
    selection_date: date,
    effective_date: date,
    requested_size: int,
    source: str,
    snapshot_date: date,
    members: tuple[CohortMember, ...],
) -> str:
    material = {
        "market": market,
        "selection_date": selection_date.isoformat(),
        "effective_date": effective_date.isoformat(),
        "selection_method": "latest_market_cap",
        "requested_size": requested_size,
        "valuation_snapshot_date": snapshot_date.isoformat(),
        "valuation_snapshot_source": source,
        "members": [
            {
                "symbol": member.symbol,
                "rank": member.rank,
                "member_kind": member.member_kind,
                "market_cap": (
                    str(member.market_cap) if member.market_cap is not None else None
                ),
                "eligibility_facts": member.eligibility_facts,
            }
            for member in members
        ],
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def build_kasset_research_cohort(
    db: AsyncSession,
    *,
    market: str,
    valuation_source: str,
    requested_size: int = 100,
    forced_symbols: tuple[str, ...] = (),
    commit: bool = False,
    selection_as_of: datetime | None = None,
    effective_date: date | None = None,
) -> KAssetResearchCohortBuildResult:
    normalized_market = _normalize_market(market)
    source = str(valuation_source or "").strip()
    if source not in _ALLOWED_SOURCES[normalized_market]:
        allowed = ", ".join(sorted(_ALLOWED_SOURCES[normalized_market]))
        raise ValueError(
            f"valuation_source must be exactly one supported {normalized_market} "
            f"source: {allowed}"
        )
    if requested_size < 1:
        raise ValueError("requested_size must be >= 1")
    selected_at = selection_as_of or datetime.now(UTC)
    if selected_at.tzinfo is None or selected_at.utcoffset() is None:
        raise ValueError("selection_as_of must be timezone-aware")
    selection_timezone = ZoneInfo(
        "Asia/Seoul" if normalized_market == "kr" else "America/New_York"
    )
    selection_date = selected_at.astimezone(selection_timezone).date()
    cohort_effective_date = effective_date or selection_date

    snapshot_date = await _latest_snapshot_date(
        db,
        market=normalized_market,
        source=source,
        selection_date=selection_date,
    )
    if cohort_effective_date < snapshot_date:
        raise ValueError("effective_date cannot precede valuation_snapshot_date")
    eligible = await _eligible_rows(
        db,
        market=normalized_market,
        source=source,
        snapshot_date=snapshot_date,
    )
    members = assemble_cohort_members(
        market=normalized_market,
        eligible=eligible,
        requested_size=requested_size,
        forced_symbols=forced_symbols,
    )
    cohort_id = _cohort_id(
        market=normalized_market,
        selection_date=selection_date,
        effective_date=cohort_effective_date,
        requested_size=requested_size,
        source=source,
        snapshot_date=snapshot_date,
        members=members,
    )

    inserted = False
    rows_inserted = 0
    if commit:
        cohort_values = {
            "cohort_id": cohort_id,
            "market": normalized_market,
            "selection_as_of": selected_at,
            "selection_date": selection_date,
            "effective_date": cohort_effective_date,
            "selection_method": "latest_market_cap",
            "requested_size": requested_size,
            "active_member_count": requested_size,
            "valuation_snapshot_date": snapshot_date,
            "valuation_snapshot_source": source,
            "evidence_scope": "forward_paper",
        }
        result = await db.execute(
            pg_insert(KAssetResearchCohort)
            .values(cohort_values)
            .on_conflict_do_nothing(index_elements=["cohort_id"])
            .returning(KAssetResearchCohort.cohort_id)
        )
        inserted = result.scalar_one_or_none() is not None
        if inserted:
            await db.execute(
                pg_insert(KAssetResearchCohortMember).values(
                    [
                        {
                            "cohort_id": cohort_id,
                            "symbol": member.symbol,
                            "rank": member.rank,
                            "member_kind": member.member_kind,
                            "market_cap": member.market_cap,
                            "eligibility_facts": member.eligibility_facts,
                        }
                        for member in members
                    ]
                )
            )
            rows_inserted = 1 + len(members)

    return KAssetResearchCohortBuildResult(
        cohort_id=cohort_id,
        mode="commit" if commit else "dry-run",
        market=normalized_market,
        selection_as_of=selected_at.isoformat(),
        selection_date=selection_date.isoformat(),
        effective_date=cohort_effective_date.isoformat(),
        selection_method="latest_market_cap",
        requested_size=requested_size,
        valuation_snapshot_date=snapshot_date.isoformat(),
        valuation_snapshot_source=source,
        active_members=requested_size,
        forced_members=sum(member.member_kind == "forced" for member in members),
        benchmark_members=1,
        rows_inserted=rows_inserted,
        duplicate=commit and not inserted,
        members=members,
    )
