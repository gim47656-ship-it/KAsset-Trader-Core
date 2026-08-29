from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation import promotion_evidence
from app.models.kasset_research_cohorts import (
    KAssetResearchCohort,
    KAssetResearchCohortMember,
)
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services import kr_lifecycle_action_service as lifecycle
from app.services.daily_candles import readiness

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_bootstrap_creates_lifecycle_schema_and_explicit_symbols_are_unbounded(
    db_session: AsyncSession,
) -> None:
    symbols = [f"Z7{index:04d}" for index in range(1, 101)]
    status = "S" * 64
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=symbol,
                name=f"PostgreSQL fixture {symbol}",
                exchange="KOSPI",
                nxt_eligible=False,
                is_active=True,
                is_common_share=True,
                listing_status=status,
                std_pdno=f"KR{symbol}",
            )
            for symbol in symbols
        ]
    )
    cohort_id = f"pg-null-forced-{uuid.uuid4().hex}"
    db_session.add(
        KAssetResearchCohort(
            cohort_id=cohort_id,
            market="kr",
            selection_as_of=datetime(2026, 1, 31, 12, 0, tzinfo=UTC),
            selection_date=date(2026, 1, 31),
            effective_date=date(2026, 1, 31),
            selection_method="latest_market_cap",
            requested_size=1,
            active_member_count=1,
            valuation_snapshot_date=date(2026, 1, 31),
            valuation_snapshot_source="naver_finance",
            evidence_scope="forward_paper",
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            KAssetResearchCohortMember(
                cohort_id=cohort_id,
                symbol=symbols[0],
                rank=1,
                member_kind="active",
                market_cap=Decimal("1000000"),
                eligibility_facts={"source": "postgresql-fixture"},
            ),
            KAssetResearchCohortMember(
                cohort_id=cohort_id,
                symbol=symbols[1],
                rank=1,
                member_kind="forced",
                market_cap=None,
                eligibility_facts={"forced_source": "watchlist"},
            ),
        ]
    )
    await db_session.flush()

    expected_tables = {
        "kr_stock_lifecycle_observations",
        "kr_corporate_action_evidence",
        "kasset_corporate_action_fetch_coverage",
        "kasset_research_cohorts",
        "kasset_research_cohort_members",
    }
    actual_tables = set(
        (
            await db_session.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_name = ANY(CAST(:table_names AS text[]))"
                ),
                {"table_names": sorted(expected_tables)},
            )
        ).all()
    )
    lengths = dict(
        (
            await db_session.execute(
                text(
                    "SELECT table_name || '.' || column_name, "
                    "character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND ("
                    "(table_name = 'kr_symbol_universe' "
                    "AND column_name IN ('listing_status', 'std_pdno')) OR "
                    "(table_name = 'kr_stock_lifecycle_observations' "
                    "AND column_name = 'listing_status'))"
                )
            )
        ).all()
    )

    selected = await lifecycle.select_kr_symbols(
        db_session,
        explicit_symbols=symbols,
    )
    forced_market_cap = await db_session.scalar(
        text(
            "SELECT market_cap FROM public.kasset_research_cohort_members "
            "WHERE cohort_id = :cohort_id AND member_kind = 'forced'"
        ),
        {"cohort_id": cohort_id},
    )

    assert actual_tables == expected_tables
    assert lengths == {
        "kr_stock_lifecycle_observations.listing_status": 64,
        "kr_symbol_universe.listing_status": 64,
        "kr_symbol_universe.std_pdno": 32,
    }
    assert selected == symbols
    assert forced_market_cap is None


async def test_postgresql_executes_readiness_promotion_and_lifecycle_contracts(
    db_session: AsyncSession,
) -> None:
    symbol = "Y90001"
    benchmark_symbol = "Y90002"
    cohort_id = f"pg-contract-{uuid.uuid4().hex}"
    selection_as_of = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    query_as_of = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)
    expected_day = date(2026, 1, 5)
    eligibility_facts = {
        "security_type": "STOCK",
        "is_common_share": True,
        "source": "postgresql-fixture",
    }

    db_session.add(
        KRSymbolUniverse(
            symbol=symbol,
            name="PostgreSQL contract symbol",
            exchange="KOSPI",
            nxt_eligible=False,
            is_active=True,
            is_common_share=True,
            listing_status="L" * 64,
            list_date=date(2020, 1, 2),
            std_pdno=f"KR{symbol}",
        )
    )
    db_session.add(
        KAssetResearchCohort(
            cohort_id=cohort_id,
            market="kr",
            selection_as_of=selection_as_of,
            selection_date=selection_as_of.date(),
            effective_date=selection_as_of.date(),
            selection_method="latest_market_cap",
            requested_size=1,
            active_member_count=1,
            valuation_snapshot_date=selection_as_of.date(),
            valuation_snapshot_source="naver_finance",
            evidence_scope="forward_paper",
            created_at=selection_as_of,
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            KAssetResearchCohortMember(
                cohort_id=cohort_id,
                symbol=symbol,
                rank=1,
                member_kind="active",
                market_cap=Decimal("1000000"),
                eligibility_facts=eligibility_facts,
            ),
            KAssetResearchCohortMember(
                cohort_id=cohort_id,
                symbol=benchmark_symbol,
                rank=1,
                member_kind="benchmark",
                market_cap=None,
                eligibility_facts={"role": "benchmark"},
            ),
        ]
    )
    await db_session.execute(
        text(
            "INSERT INTO public.kr_candles_1d "
            "(time, symbol, venue, open, high, low, close, volume, value, source) "
            "VALUES (:time, :symbol, 'KRX', :open, :high, :low, :close, "
            ":volume, :value, :source)"
        ),
        [
            {
                "time": datetime(2026, 1, 5, 6, 0, tzinfo=UTC),
                "symbol": symbol,
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 105,
                "volume": 1000,
                "value": 105000,
                "source": "kis",
            },
            {
                "time": datetime(2026, 1, 5, 6, 0, tzinfo=UTC),
                "symbol": benchmark_symbol,
                "open": 200,
                "high": 210,
                "low": 190,
                "close": 205,
                "volume": 2000,
                "value": 410000,
                "source": "toss",
            },
            {
                "time": datetime(2026, 1, 6, 6, 0, tzinfo=UTC),
                "symbol": benchmark_symbol,
                "open": 205,
                "high": 215,
                "low": 195,
                "close": 210,
                "volume": 2100,
                "value": 441000,
                "source": "kis",
            },
        ],
    )

    fetch_run_id = uuid.uuid4()
    lifecycle_row = lifecycle.build_lifecycle_evidence(
        symbol=symbol,
        row={
            "pdno": symbol,
            "std_pdno": f"KR{symbol}",
            "listing_status": "L" * 64,
            "scts_mket_lstg_dt": "20200102",
        },
        observed_at=selection_as_of,
        fetch_run_id=fetch_run_id,
    )
    action_row = lifecycle.build_action_evidence(
        symbol=symbol,
        row={"sht_cd": symbol, "record_date": "20260105"},
        spec=lifecycle._ACTION_SPECS[0],
        window=lifecycle.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 31)),
        observed_at=selection_as_of,
        fetch_run_id=fetch_run_id,
    )
    coverage_rows = [
        lifecycle.build_fetch_coverage(
            symbol=symbol,
            spec=spec,
            window=window,
            fetch_run_id=fetch_run_id,
            status="success",
            row_count=0,
            page_count=1,
            last_cursor=None,
            completed_at=selection_as_of,
        )
        for spec in lifecycle._ACTION_SPECS
        for window in (
            lifecycle.MonthlyWindow(date(2026, 1, 1), date(2026, 1, 15)),
            lifecycle.MonthlyWindow(date(2026, 1, 16), date(2026, 1, 31)),
        )
    ]

    assert await lifecycle.upsert_lifecycle_evidence(db_session, [lifecycle_row]) == 1
    assert await lifecycle.upsert_action_evidence(db_session, [action_row]) == 1
    assert await lifecycle.upsert_fetch_coverage(db_session, coverage_rows) == 8

    cohort = (
        (
            await db_session.execute(
                readiness._cohort_query("kr"),
                {"market": "kr", "as_of": query_as_of, "cohort_id": cohort_id},
            )
        )
        .mappings()
        .one()
    )
    market_rows = (
        (
            await db_session.execute(
                readiness._market_query("kr", readiness._CONFIG["kr"]),
                {
                    "cohort_id": cohort_id,
                    "as_of": query_as_of,
                    "expected_sessions": [expected_day],
                },
            )
        )
        .mappings()
        .all()
    )
    benchmark = (
        (
            await db_session.execute(
                readiness._benchmark_query("kr", readiness._CONFIG["kr"]),
                {"cohort_id": cohort_id, "as_of": query_as_of},
            )
        )
        .mappings()
        .one()
    )
    expanded_rows = (
        (
            await db_session.execute(
                promotion_evidence._candle_query(promotion_evidence._STORAGE["kr"]),
                {
                    "symbols": [symbol, benchmark_symbol],
                    "as_of": query_as_of,
                    "history_bars": 10,
                },
            )
        )
        .mappings()
        .all()
    )
    persisted_coverage = (
        (
            await db_session.execute(
                readiness._kr_coverage_query(),
                {
                    "cohort_id": cohort_id,
                    "window_start": date(2026, 1, 1),
                    "window_end": date(2026, 1, 31),
                    "as_of": query_as_of,
                },
            )
        )
        .mappings()
        .all()
    )

    assert cohort["cohort_id"] == cohort_id
    assert len(market_rows) == 1
    assert market_rows[0]["symbol"] == symbol
    assert market_rows[0]["eligibility_facts"] == eligibility_facts
    assert market_rows[0]["observed_expected_session_count"] == 1
    assert benchmark["sources"] == "kis,toss"
    assert benchmark["bar_count"] == 2
    assert [row["symbol"] for row in expanded_rows] == [
        symbol,
        benchmark_symbol,
        benchmark_symbol,
    ]
    assert len(persisted_coverage) == 8
    assert {row["row_count"] for row in persisted_coverage} == {0}
    assert {row["status"] for row in persisted_coverage} == {"success"}
    assert readiness._kr_covered_symbols(
        persisted_coverage,
        [symbol],
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 31),
    ) == frozenset({symbol})

    rows_with_gap = [
        row
        for row in persisted_coverage
        if not (
            row["action_kind"] == lifecycle._ACTION_SPECS[0].evidence_kind
            and row["requested_from_date"] == date(2026, 1, 16)
        )
    ]
    assert (
        readiness._kr_covered_symbols(
            rows_with_gap,
            [symbol],
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 31),
        )
        == frozenset()
    )
