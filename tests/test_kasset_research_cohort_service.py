from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import kasset_research_cohort_service as service


def _eligible(symbol: str, market_cap: str) -> service.EligibleValuation:
    return service.EligibleValuation(
        symbol=symbol,
        market_cap=Decimal(market_cap),
        eligibility_facts={"is_active": True, "eligible": True},
    )


def _forced(
    symbol: str,
    market_cap: str | None,
    *reasons: str,
    security_type: str = "STOCK",
    leverage_factor: str | None = None,
    exchange: str = "NASD",
) -> service.ForcedValuation:
    return service.ForcedValuation(
        symbol=symbol,
        market_cap=Decimal(market_cap) if market_cap is not None else None,
        reasons=reasons or ("active_watchlist",),
        eligibility_facts={
            "is_active": True,
            "security_type": security_type,
            "leverage_factor": leverage_factor,
            "exchange": exchange,
        },
    )


def test_forced_members_do_not_displace_ranked_core_and_benchmark_is_separate() -> None:
    eligible = [
        _eligible("000003", "100"),
        _eligible("000001", "300"),
        _eligible("000002", "200"),
    ]

    members = service.assemble_cohort_members(
        market="kr",
        eligible=eligible,
        requested_size=2,
        forced=[
            _forced("000003", "100", "active_watchlist"),
            _forced("000001", "300", "explicit_force"),
        ],
    )

    assert [(row.symbol, row.rank, row.member_kind) for row in members] == [
        ("000001", 1, "active"),
        ("000002", 2, "active"),
        ("000003", 3, "forced"),
        ("KOSPI", 1, "benchmark"),
    ]


def test_forced_etfs_allow_null_valuation_without_changing_core_or_benchmark() -> None:
    members = service.assemble_cohort_members(
        market="us",
        eligible=[
            _eligible("AAPL", "300"),
            _eligible("MSFT", "200"),
            _eligible("NVDA", "100"),
        ],
        requested_size=2,
        forced=[
            _forced(
                "SOXL",
                None,
                "active_watchlist",
                security_type="ETF",
                leverage_factor="3.000000",
                exchange="AMEX",
            ),
            _forced(
                "TQQQ",
                None,
                "active_watchlist",
                security_type="ETF",
                leverage_factor="3.000000",
            ),
            _forced("SPY", None, "explicit_force", security_type="ETF"),
        ],
    )

    assert [(row.symbol, row.rank, row.member_kind) for row in members] == [
        ("AAPL", 1, "active"),
        ("MSFT", 2, "active"),
        ("SOXL", 3, "forced"),
        ("TQQQ", 4, "forced"),
        ("SPY", 1, "benchmark"),
    ]
    assert [row.symbol for row in members if row.member_kind == "active"] == [
        "AAPL",
        "MSFT",
    ]
    for row in members:
        if row.member_kind != "forced":
            continue
        assert row.market_cap is None
        assert row.eligibility_facts["forced_reasons"] == ["active_watchlist"]
        assert row.eligibility_facts["promotion_sample"] is False
        assert row.eligibility_facts["data_continuity_only"] is True
        assert row.eligibility_facts["security_type"] == "ETF"
        assert row.eligibility_facts["leverage_factor"] == "3.000000"


def test_positive_valuation_forced_rank_is_deterministic_and_after_core() -> None:
    members = service.assemble_cohort_members(
        market="us",
        eligible=[_eligible("AAPL", "300"), _eligible("MSFT", "200")],
        requested_size=2,
        forced=[
            _forced("ZZZ", None),
            _forced("LOW", "10"),
            _forced("AAA", None),
        ],
    )

    assert [
        (row.symbol, row.rank) for row in members if row.member_kind == "forced"
    ] == [("LOW", 3), ("AAA", 4), ("ZZZ", 5)]


def test_requested_size_requires_complete_positive_eligible_core() -> None:
    with pytest.raises(service.KAssetResearchCohortError, match="requested_size=100"):
        service.assemble_cohort_members(
            market="us",
            eligible=[_eligible("AAPL", "1")],
            requested_size=100,
            forced=[],
        )


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class _StatementDB:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _RowsResult:
        self.statements.append(statement)
        return _RowsResult(self.rows)


@pytest.mark.asyncio
async def test_automatic_forced_reasons_merge_all_market_scoped_sources() -> None:
    db = _StatementDB(
        [
            SimpleNamespace(symbol="SOXL", reason="active_watchlist"),
            SimpleNamespace(symbol="SOXL", reason="positive_paper_position"),
            SimpleNamespace(symbol="TQQQ", reason="positive_manual_holding"),
        ]
    )

    reasons = await service._automatic_forced_reasons(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        market="us",
    )

    assert reasons == {
        "SOXL": {"active_watchlist", "positive_paper_position"},
        "TQQQ": {"positive_manual_holding"},
    }
    statement = db.statements[0]
    sql = str(statement)
    assert "user_watch_items" in sql
    assert "instruments" in sql
    assert "manual_holdings" in sql
    assert "broker_accounts" in sql
    assert "paper.paper_positions" in sql
    assert "quantity >" in sql
    params = {
        getattr(value, "value", value) for value in statement.compile().params.values()
    }
    assert "equity_us" in params
    assert "US" in params


@pytest.mark.asyncio
async def test_invalid_automatic_forced_symbol_fails_closed() -> None:
    db = _StatementDB([SimpleNamespace(symbol="", reason="positive_manual_holding")])

    with pytest.raises(
        service.KAssetResearchCohortError,
        match=r"Automatic forced symbols are invalid for US: '' "
        r"\(positive_manual_holding\)",
    ):
        await service._automatic_forced_reasons(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            market="us",
        )


@pytest.mark.asyncio
async def test_forced_rows_keep_known_active_leveraged_etfs_with_null_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reasons(*args: Any, **kwargs: Any) -> dict[str, set[str]]:
        return {
            "SOXL": {"active_watchlist"},
            "TQQQ": {"active_watchlist", "positive_paper_position"},
        }

    monkeypatch.setattr(service, "_automatic_forced_reasons", reasons)
    db = _StatementDB(
        [
            SimpleNamespace(
                symbol=symbol,
                is_active=True,
                is_common_stock=True,
                security_type="ETF",
                leverage_factor=Decimal("3"),
                exchange=exchange,
                market_cap=None,
            )
            for symbol, exchange in (("SOXL", "AMEX"), ("TQQQ", "NASD"))
        ]
    )

    rows = await service._forced_rows(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        market="us",
        source="yahoo",
        snapshot_date=date(2026, 8, 29),
        explicit_symbols=(),
    )

    assert [row.symbol for row in rows] == ["SOXL", "TQQQ"]
    assert all(row.market_cap is None for row in rows)
    assert rows[0].eligibility_facts["exchange"] == "AMEX"
    assert rows[0].eligibility_facts["security_type"] == "ETF"
    assert rows[0].eligibility_facts["leverage_factor"] == "3"
    assert rows[1].reasons == (
        "active_watchlist",
        "positive_paper_position",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("automatic", "explicit", "expected"),
    [
        (
            {"SOXL": {"active_watchlist", "positive_paper_position"}},
            (),
            r"SOXL \(active_watchlist/positive_paper_position\)",
        ),
        (
            {},
            ("UNKNOWN",),
            r"UNKNOWN \(explicit_force\)",
        ),
    ],
)
async def test_unresolved_forced_symbol_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    automatic: dict[str, set[str]],
    explicit: tuple[str, ...],
    expected: str,
) -> None:
    async def reasons(*args: Any, **kwargs: Any) -> dict[str, set[str]]:
        return automatic

    monkeypatch.setattr(service, "_automatic_forced_reasons", reasons)
    db = _StatementDB([])

    with pytest.raises(
        service.KAssetResearchCohortError,
        match=expected,
    ):
        await service._forced_rows(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            market="us",
            source="yahoo",
            snapshot_date=date(2026, 8, 29),
            explicit_symbols=explicit,
        )


class _CoreQueryDB(_StatementDB):
    async def scalar(self, statement: Any) -> date:
        self.statements.append(statement)
        return date(2026, 8, 29)


@pytest.mark.asyncio
async def test_us_core_requires_direct_unleveraged_stock_and_records_facts() -> None:
    db = _CoreQueryDB(
        [
            SimpleNamespace(
                symbol="AAPL",
                market_cap=Decimal("100"),
                is_active=True,
                is_common_stock=True,
                security_type="STOCK",
                leverage_factor=None,
                exchange="NASD",
            )
        ]
    )

    snapshot_date = await service._latest_snapshot_date(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        market="us",
        source="yahoo",
        selection_date=date(2026, 8, 30),
    )
    rows = await service._eligible_rows(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        market="us",
        source="yahoo",
        snapshot_date=snapshot_date,
    )

    for statement in db.statements:
        sql = str(statement)
        assert "us_symbol_universe.security_type =" in sql
        assert "us_symbol_universe.leverage_factor IS NULL" in sql
        assert "us_symbol_universe.leverage_factor =" in sql
    assert rows[0].eligibility_facts["security_type"] == "STOCK"
    assert rows[0].eligibility_facts["leverage_factor"] is None


class _InsertResult:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class _FakeDB:
    def __init__(self) -> None:
        self.cohort_ids: set[str] = set()
        self.member_insert_count = 0

    async def execute(self, statement: Any) -> _InsertResult:
        table_name = statement.table.name
        if table_name == "kasset_research_cohorts":
            cohort_id = statement.compile().params["cohort_id"]
            if cohort_id in self.cohort_ids:
                return _InsertResult(None)
            self.cohort_ids.add(cohort_id)
            return _InsertResult(cohort_id)
        if table_name == "kasset_research_cohort_members":
            self.member_insert_count += 1
            return _InsertResult(None)
        raise AssertionError(f"unexpected insert table: {table_name}")


@pytest.mark.asyncio
async def test_dry_run_has_no_insert_and_commit_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_at = datetime(2026, 8, 30, 0, 30, tzinfo=UTC)
    eligible = [
        _eligible("AAPL", "300"),
        _eligible("MSFT", "200"),
        _eligible("TSLA", "100"),
    ]

    async def latest(*args: Any, **kwargs: Any) -> date:
        return date(2026, 8, 29)

    async def rows(*args: Any, **kwargs: Any) -> list[service.EligibleValuation]:
        return eligible

    async def forced_rows(*args: Any, **kwargs: Any) -> list[service.ForcedValuation]:
        return [_forced("TSLA", "100", "explicit_force")]

    monkeypatch.setattr(service, "_latest_snapshot_date", latest)
    monkeypatch.setattr(service, "_eligible_rows", rows)
    monkeypatch.setattr(service, "_forced_rows", forced_rows)
    db = _FakeDB()

    dry_run = await service.build_kasset_research_cohort(
        db,  # type: ignore[arg-type]
        market="us",
        valuation_source="yahoo",
        requested_size=2,
        forced_symbols=("TSLA",),
        commit=False,
        selection_as_of=selected_at,
    )
    first = await service.build_kasset_research_cohort(
        db,  # type: ignore[arg-type]
        market="us",
        valuation_source="yahoo",
        requested_size=2,
        forced_symbols=("TSLA",),
        commit=True,
        selection_as_of=selected_at,
    )
    duplicate = await service.build_kasset_research_cohort(
        db,  # type: ignore[arg-type]
        market="us",
        valuation_source="yahoo",
        requested_size=2,
        forced_symbols=("TSLA",),
        commit=True,
        selection_as_of=selected_at,
    )

    assert dry_run.mode == "dry-run"
    assert dry_run.rows_inserted == 0
    assert dry_run.historical_point_in_time_available is False
    assert dry_run.selection_date == "2026-08-29"
    assert first.cohort_id == dry_run.cohort_id == duplicate.cohort_id
    assert first.rows_inserted == 5
    assert first.duplicate is False
    assert duplicate.rows_inserted == 0
    assert duplicate.duplicate is True
    assert db.member_insert_count == 1
