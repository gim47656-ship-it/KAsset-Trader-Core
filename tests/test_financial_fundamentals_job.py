from __future__ import annotations

import datetime as dt
from contextlib import AbstractAsyncContextManager

import pandas as pd
import pytest

from app.jobs import financial_fundamentals_snapshots as job
from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
)


class _SessionFactory(AbstractAsyncContextManager):
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def bind_job_session(monkeypatch, db_session):
    monkeypatch.setattr(job, "AsyncSessionLocal", lambda: _SessionFactory(db_session))
    return db_session


async def _fake_fetcher(
    symbol: str, *, include_quarterly: bool, plan=None
) -> RawFundamentalsBundle:
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "1,000",
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "100",
            },
        ]
    )
    return RawFundamentalsBundle(
        symbol=symbol,
        annual=(RawAnnualFiling(bsns_year=2024, rcept_no="r1", income_statement=df),),
        filing_dates={"r1": dt.date(2025, 3, 20)},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_builds_but_writes_nothing(bind_job_session, monkeypatch):
    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=False
        ),
        fetcher=_fake_fetcher,
    )
    assert result.committed is False
    assert result.snapshots_built == 1
    assert result.symbols_resolved == 1
    assert any(s.fiscal_period == "2024A" for s in result.samples)


@pytest.mark.asyncio
async def test_job_budget_exceeded_fail_stops_and_does_not_commit(
    bind_job_session, monkeypatch
):
    from decimal import Decimal

    from app.services.financial_fundamentals_snapshots.builder import (
        DartDailyRequestBudgetExceeded,
        FinancialFundamentalsUpsert,
    )

    dummy_payload = FinancialFundamentalsUpsert(
        market="kr",
        symbol="005930",
        fiscal_period="2024A",
        period_type="annual",
        period_end_date=dt.date(2024, 12, 31),
        filing_date=dt.date(2025, 3, 20),
        effective_at=dt.date(2025, 3, 20),
        source="dart",
        source_collected_at=dt.datetime.now(dt.UTC),
        currency="KRW",
        revenue=Decimal("1000"),
        net_income=Decimal("100"),
        gross_profit=None,
        cost_of_sales=None,
        payout_ratio=None,
        dividend_per_share=None,
        discrete_revenue=Decimal("1000"),
        discrete_net_income=Decimal("100"),
        data_state="fresh",
        raw_payload=None,
    )

    async def mock_build(*args, **kwargs):
        raise DartDailyRequestBudgetExceeded(
            "Budget Exceeded", payloads=(dummy_payload,), warnings=("Budget limit hit",)
        )

    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))
    monkeypatch.setattr(job, "build_financial_fundamentals_for_symbols", mock_build)

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=True, allow_partial=True
        ),
        fetcher=_fake_fetcher,
    )

    assert result.committed is False
    assert result.budget_exhausted is True
    assert result.snapshots_built == 1
    assert any(s.fiscal_period == "2024A" for s in result.samples)
    assert any(
        "Budget Exceeded" in w or "Budget limit hit" in w for w in result.warnings
    )


def _async_return(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro


@pytest.mark.asyncio
async def test_estimate_only_does_not_fetch_or_commit():
    # No bind_job_session fixture on purpose: estimate-only must short-circuit
    # BEFORE any AsyncSessionLocal use, so this test proves it never touches DB.
    calls: list[str] = []

    async def _spy_fetcher(symbol: str, *, include_quarterly: bool, plan=None):
        calls.append(symbol)
        raise AssertionError("fetcher must not be called in estimate-only mode")

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr",
            symbols=("005930",),
            estimate_only=True,
            include_quarterly=False,
            collected_at=dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.UTC),
        ),
        fetcher=_spy_fetcher,
    )
    assert calls == []
    # 5 annual years x (CFS + OFS + 배당) + 1 disclosure list
    assert result.projected_requests == 16
    assert result.committed is False
    assert result.snapshots_built == 0
    assert any("estimate-only" in w for w in result.warnings)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skip_existing_budget_split(bind_job_session, db_session, monkeypatch):
    # ROB-441 budget-split: --skip-existing drops already-collected symbols so daily
    # re-runs advance through the uncollected universe within the DART daily budget.
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    syms = ["900001", "900002", "900003"]
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    # 900001 already collected → must be skipped.
    db_session.add(
        FinancialFundamentalsSnapshot(
            market="kr",
            symbol="900001",
            fiscal_period="2024A",
            period_type="annual",
            period_end_date=dt.date(2024, 12, 31),
            source="dart",
            source_collected_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            data_state="fresh",
        )
    )
    await db_session.commit()

    async def _fake_universe(market):  # noqa: ANN001
        return list(syms)

    monkeypatch.setattr(job, "resolve_active_universe", _fake_universe)

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr",
            all_symbols=True,
            estimate_only=True,
            skip_existing=True,
            collected_at=dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.UTC),
        )
    )
    assert result.symbols_resolved == 2  # 900001 skipped (already collected)
    assert result.projected_requests == 2 * 16  # only uncollected projected
    assert any(
        "skip_existing" in w and "1 already-collected" in w for w in result.warnings
    )

    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_classify_idempotency_handles_keys_beyond_bind_arg_limit(
    bind_job_session, db_session
):
    # 전 종목 분기 백필은 고유 키가 8,192개를 넘는다. 키당 인자 4개라 한 쿼리로
    # 조회하면 asyncpg의 32767 인자 한도를 넘어 커밋 전체가 실패했다(2026-09-26).
    from types import SimpleNamespace

    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    period = "2099A"
    symbols = [f"8{i:05d}" for i in range(8200)]
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.fiscal_period == period
        )
    )
    db_session.add(
        FinancialFundamentalsSnapshot(
            market="kr",
            symbol=symbols[-1],
            fiscal_period=period,
            period_type="annual",
            period_end_date=dt.date(2099, 12, 31),
            source="dart",
            source_collected_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            data_state="fresh",
        )
    )
    await db_session.commit()
    payloads = [
        SimpleNamespace(market="kr", symbol=s, fiscal_period=period, source="dart")
        for s in symbols
    ]

    try:
        result = await job._classify_idempotency(payloads)  # type: ignore[arg-type]
        assert result == {
            "wouldInsert": 8199,
            "wouldUpdate": 1,
            "duplicatePayloadKeys": 0,
        }
    finally:
        await db_session.execute(
            sa.delete(FinancialFundamentalsSnapshot).where(
                FinancialFundamentalsSnapshot.fiscal_period == period
            )
        )
        await db_session.commit()


def test_kr_dart_common_symbol_filter_excludes_non_dart_universe_rows() -> None:
    assert job._is_kr_dart_common_symbol("005930", "삼성전자") is True
    assert job._is_kr_dart_common_symbol("035420", "NAVER") is True

    # The failed 2026-06-09 backfill chunk hit rows like these before the
    # OpenDART fetch loop. They should be removed from the default universe.
    assert job._is_kr_dart_common_symbol("0000H0", "비표준코드") is False
    assert job._is_kr_dart_common_symbol("000087", "하이트진로2우B") is False
    assert job._is_kr_dart_common_symbol("000145", "하이트진로홀딩스우") is False
    assert job._is_kr_dart_common_symbol("999970", "KODEX 테스트") is False
    assert job._is_kr_dart_common_symbol("999980", "테스트스팩") is False
    assert job._is_kr_dart_common_symbol("999960", "테스트리츠") is False

    # Toss master classification wins over the name rules: an ETF whose brand
    # carries no ETF token (2026-10 ETF 118 candidates) and a 우선주 flagged by
    # is_common_share stay out; classified common stocks stay in.
    assert job._is_kr_dart_common_symbol("999940", "삼성 미국테크", "ETF") is False
    assert job._is_kr_dart_common_symbol("999930", "어떤증권", "ETN") is False
    assert job._is_kr_dart_common_symbol("005935", "삼성전자", "STOCK", False) is False
    assert job._is_kr_dart_common_symbol("005930", "삼성전자", "STOCK", True) is True
    # A master-confirmed common share whose code ends in 5/7/9 is not a 우선주.
    assert job._is_kr_dart_common_symbol("123455", "테스트지주", "STOCK", True) is True
    assert job._is_kr_dart_common_symbol("123455", "테스트지주", None, None) is False
    assert job._is_kr_dart_common_symbol("999985", "테스트스팩", "STOCK", True) is False
    # Not yet classified by the master: name rules decide (no common-stock loss).
    assert job._is_kr_dart_common_symbol("005380", "현대차", None, None) is True


_NOW = dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.UTC)  # 18:30 KST cron slot


def _ev(last: dt.datetime, *, has_latest: bool, partial_end=None):
    return job.SymbolCollectionEvidence(
        last_collected_at=last,
        has_latest_period=has_latest,
        earliest_partial_period_end=partial_end,
    )


def test_select_refresh_due_orders_reasons_and_skips_fresh_symbols() -> None:
    utc = dt.UTC
    evidence = {
        # Collected before 2026Q3 ended (2026-10-01 KST): due now.
        "000001": _ev(dt.datetime(2026, 9, 28, tzinfo=utc), has_latest=False),
        "000002": _ev(dt.datetime(2026, 10, 2, tzinfo=utc), has_latest=True),
        # Looked after Q3 ended and it was unfiled: wait for the weekly recheck.
        "000003": _ev(dt.datetime(2026, 10, 2, tzinfo=utc), has_latest=False),
        "000004": _ev(dt.datetime(2026, 9, 25, tzinfo=utc), has_latest=False),
        # Partial inside the 5-year window but before the recent 2 years.
        "000005": _ev(
            dt.datetime(2026, 9, 20, tzinfo=utc),
            has_latest=True,
            partial_end=dt.date(2022, 12, 31),
        ),
        "000006": _ev(dt.datetime(2026, 8, 1, tzinfo=utc), has_latest=True),
        # Partial but re-fetched yesterday: not yet.
        "000009": _ev(
            dt.datetime(2026, 10, 2, tzinfo=utc),
            has_latest=True,
            partial_end=dt.date(2026, 6, 30),
        ),
    }
    pool = sorted([*evidence, "000007", "000008"])

    alloc = job.select_refresh_due(
        pool, evidence, now=_NOW, include_quarterly=True, limit=None, budget=18000
    )
    assert [(t.symbol, t.reason) for t in alloc.selected] == [
        ("000004", "missing_latest"),
        ("000001", "missing_latest"),
        ("000005", "partial"),
        ("000007", "uncollected"),
        ("000008", "uncollected"),
        ("000006", "stale"),
    ]
    plans = {t.symbol: t.plan for t in alloc.selected}
    # Collected symbols re-fetch only 2025 + 2026's ended quarters (16 calls max);
    # a partial row older than that window and new symbols get the full window.
    assert [y.year for y in plans["000001"].years] == [2026, 2025]
    assert plans["000001"].max_requests == 16
    assert plans["000005"].max_requests == 52
    assert plans["000007"].max_requests == 52
    assert alloc.spent == 16 * 3 + 52 * 3
    assert alloc.deferred == 0


def test_select_refresh_due_fits_daily_budget_in_priority_order() -> None:
    evidence = {
        s: _ev(dt.datetime(2026, 9, 1, tzinfo=dt.UTC), has_latest=False)
        for s in ("000001", "000002", "000003")
    }
    alloc = job.select_refresh_due(
        [*evidence, "000004"],
        evidence,
        now=_NOW,
        include_quarterly=True,
        limit=None,
        budget=16 * 2 + 10,
    )
    assert [t.symbol for t in alloc.selected] == ["000001", "000002"]
    assert alloc.spent <= 16 * 2 + 10
    assert alloc.deferred == 2  # one refresh + the 52-call new symbol wait


def _seen_over_days(pool, evidence, *, days: int, limit, budget) -> list[set[str]]:
    # Every fetch keeps failing: evidence never changes between days.
    return [
        {
            t.symbol
            for t in job.select_refresh_due(
                pool,
                evidence,
                now=_NOW + dt.timedelta(days=day),
                include_quarterly=True,
                limit=limit,
                budget=budget,
            ).selected
        }
        for day in range(days)
    ]


def test_refresh_fairness_reaches_lower_tiers_when_a_failure_fills_the_run() -> None:
    # A failing missing_latest symbol alone fills --limit 1 every day; the
    # uncollected and stale symbols behind it must still get their turn.
    evidence = {
        "000001": _ev(dt.datetime(2026, 9, 1, tzinfo=dt.UTC), has_latest=False),
        "000003": _ev(dt.datetime(2026, 8, 1, tzinfo=dt.UTC), has_latest=True),
    }
    pool = ["000001", "000002", "000003"]
    daily = _seen_over_days(pool, evidence, days=3, limit=1, budget=18000)
    assert all(len(day) == 1 for day in daily)
    assert set().union(*daily) == set(pool)


def test_refresh_fairness_survives_many_failing_high_tier_symbols() -> None:
    # 20 failing missing_latest symbols already exceed the daily budget; the
    # 52-call uncollected symbol and the stale one still come up in time.
    failing = [f"2000{i:02d}" for i in range(20)]
    evidence = {
        s: _ev(dt.datetime(2026, 9, 1, tzinfo=dt.UTC), has_latest=False)
        for s in failing
    }
    evidence["300001"] = _ev(dt.datetime(2026, 8, 1, tzinfo=dt.UTC), has_latest=True)
    pool = [*failing, "300000", "300001"]
    budget = 52 * 2  # fairness slice of 2 symbols/day over a ring of 22
    daily = _seen_over_days(pool, evidence, days=11, limit=None, budget=budget)
    assert set().union(*daily) == set(pool)


@pytest.mark.asyncio
async def test_run_flags_fetches_that_all_fail_as_no_rows() -> None:
    async def _failing(symbol, *, include_quarterly, plan=None):
        raise RuntimeError("DART 013")

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930", "000660"), collected_at=_NOW
        ),
        fetcher=_failing,
    )
    assert result.symbols_resolved == 2
    assert result.snapshots_built == 0
    assert result.no_rows_collected is True
    assert result.committed is False
    assert result.budget_exhausted is False


async def _seed_rows(db_session, rows) -> None:
    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    for symbol, period, period_end, collected, state in rows:
        db_session.add(
            FinancialFundamentalsSnapshot(
                market="kr",
                symbol=symbol,
                fiscal_period=period,
                period_type="annual" if period.endswith("A") else "quarterly",
                period_end_date=period_end,
                source="dart",
                source_collected_at=collected,
                data_state=state,
            )
        )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_due_refetches_stale_symbols_without_faking_failures(
    bind_job_session, db_session, monkeypatch
):
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    syms = ["900011", "900012", "900013", "900014"]
    old = dt.datetime(2026, 9, 27, 9, 30, tzinfo=dt.UTC)
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await _seed_rows(
        db_session,
        [
            # Stopped at 2025Q4 like production on 2026-10-02.
            ("900011", "2025A", dt.date(2025, 12, 31), old, "fresh"),
            ("900012", "2025A", dt.date(2025, 12, 31), old, "fresh"),
            # Already has the newest ended period and is recent: not due.
            ("900013", "2026Q3", dt.date(2026, 9, 30), _NOW, "fresh"),
        ],
    )
    monkeypatch.setattr(job, "resolve_active_universe", _async_return(list(syms)))

    async def _fetcher(symbol, *, include_quarterly, plan=None):
        assert plan is not None and plan.years[0].year == 2026
        if symbol == "900012":
            raise RuntimeError("DART 013 no data")
        base = await _fake_fetcher(symbol, include_quarterly=include_quarterly)
        return RawFundamentalsBundle(
            symbol=symbol,
            annual=(
                RawAnnualFiling(
                    bsns_year=2025,
                    rcept_no="r1",
                    income_statement=base.annual[0].income_statement,
                ),
            ),
            filing_dates=base.filing_dates,
        )

    try:
        estimate = await job.run_financial_fundamentals_snapshot_build(
            job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr",
                all_symbols=True,
                include_quarterly=True,
                refresh_due=True,
                estimate_only=True,
                collected_at=_NOW,
            ),
            fetcher=_fetcher,
        )
        assert estimate.selection == {"missing_latest": 2, "uncollected": 1}
        assert estimate.projected_requests == 16 * 2 + 52
        assert estimate.snapshots_built == 0

        result = await job.run_financial_fundamentals_snapshot_build(
            job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr",
                all_symbols=True,
                include_quarterly=True,
                refresh_due=True,
                commit=True,
                allow_partial=True,
                collected_at=_NOW,
            ),
            fetcher=_fetcher,
        )
        assert result.committed is True
        assert result.budget_exhausted is False
        assert any("900012: fetch failed" in w for w in result.warnings)

        rows = (
            await db_session.execute(
                sa.select(
                    FinancialFundamentalsSnapshot.symbol,
                    sa.func.max(FinancialFundamentalsSnapshot.source_collected_at),
                )
                .where(FinancialFundamentalsSnapshot.symbol.in_(syms))
                .group_by(FinancialFundamentalsSnapshot.symbol)
            )
        ).all()
        last = dict(rows)
        assert last["900011"] == _NOW
        assert last["900014"] == _NOW  # first backfill landed
        assert last["900012"] == old  # failure never stamps a success time

        # Next day: the failed symbol is still due; the refreshed ones wait.
        again = await job.run_financial_fundamentals_snapshot_build(
            job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr",
                all_symbols=True,
                include_quarterly=True,
                refresh_due=True,
                estimate_only=True,
                collected_at=_NOW + dt.timedelta(days=1),
            ),
            fetcher=_fetcher,
        )
        assert again.selection == {"missing_latest": 1}
        assert again.symbols_resolved == 1
    finally:
        await db_session.execute(
            sa.delete(FinancialFundamentalsSnapshot).where(
                FinancialFundamentalsSnapshot.symbol.in_(syms)
            )
        )
        await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collection_evidence_ignores_partials_outside_fetch_window(
    bind_job_session, db_session
):
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    syms = ["900021", "900022"]
    # Within the 30-day sweep but past the 7-day partial recheck.
    collected = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await _seed_rows(
        db_session,
        [
            # 2019 is outside the 2021+ window: no plan can fix it, so not due.
            ("900021", "2019A", dt.date(2019, 12, 31), collected, "partial"),
            ("900021", "2026Q3", dt.date(2026, 9, 30), collected, "fresh"),
            # The older out-of-window partial must not hide the 2023 one.
            ("900022", "2019A", dt.date(2019, 12, 31), collected, "partial"),
            ("900022", "2023A", dt.date(2023, 12, 31), collected, "partial"),
            ("900022", "2026Q3", dt.date(2026, 9, 30), collected, "fresh"),
        ],
    )
    try:
        evidence = await job._collection_evidence(
            "kr", "2026Q3", partial_since=dt.date(2021, 1, 1)
        )
        assert evidence["900021"].earliest_partial_period_end is None
        assert evidence["900022"].earliest_partial_period_end == dt.date(2023, 12, 31)
        alloc = job.select_refresh_due(
            syms, evidence, now=_NOW, include_quarterly=True, limit=None, budget=18000
        )
        picked = [(t.symbol, t.reason, t.plan.max_requests) for t in alloc.selected]
        assert picked == [("900022", "partial", 52)]
    finally:
        await db_session.execute(
            sa.delete(FinancialFundamentalsSnapshot).where(
                FinancialFundamentalsSnapshot.symbol.in_(syms)
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_refresh_due_rejects_skip_existing() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        await job.run_financial_fundamentals_snapshot_build(
            job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr", refresh_due=True, skip_existing=True
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_active_universe_filters_to_dart_common_stocks(
    bind_job_session, db_session
):
    import sqlalchemy as sa

    from app.models.kr_symbol_universe import KRSymbolUniverse

    symbols = [
        "999990",
        "999995",
        "99A990",
        "999970",
        "999980",
        "999960",
        "999950",
        "999940",
        "999920",
    ]
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(symbols))
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="999990", name="테스트보통", exchange="STK", is_active=True
            ),
            KRSymbolUniverse(
                symbol="999995", name="테스트우", exchange="STK", is_active=True
            ),
            KRSymbolUniverse(
                symbol="99A990", name="비표준코드", exchange="STK", is_active=True
            ),
            KRSymbolUniverse(
                symbol="999970", name="KODEX 테스트", exchange="STK", is_active=True
            ),
            KRSymbolUniverse(
                symbol="999980", name="테스트스팩", exchange="KSQ", is_active=True
            ),
            KRSymbolUniverse(
                symbol="999960", name="테스트리츠", exchange="STK", is_active=True
            ),
            KRSymbolUniverse(
                symbol="999950", name="비활성보통", exchange="STK", is_active=False
            ),
            KRSymbolUniverse(
                symbol="999940",
                name="삼성 미국테크",
                exchange="STK",
                is_active=True,
                security_type="ETF",
            ),
            KRSymbolUniverse(
                symbol="999920",
                name="테스트지주",
                exchange="STK",
                is_active=True,
                security_type="STOCK",
                is_common_share=True,
            ),
        ]
    )
    await db_session.commit()

    try:
        resolved = await job.resolve_active_universe("kr")
        assert "999990" in resolved
        assert "999920" in resolved
        assert "999995" not in resolved
        assert "99A990" not in resolved
        assert "999970" not in resolved
        assert "999980" not in resolved
        assert "999960" not in resolved
        assert "999950" not in resolved
        assert "999940" not in resolved  # ETF by master, no ETF token in name
    finally:
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(symbols))
        )
        await db_session.commit()
