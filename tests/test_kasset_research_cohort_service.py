from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services import kasset_research_cohort_service as service


def _eligible(symbol: str, market_cap: str) -> service.EligibleValuation:
    return service.EligibleValuation(
        symbol=symbol,
        market_cap=Decimal(market_cap),
        eligibility_facts={"is_active": True, "eligible": True},
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
        forced_symbols=("000003", "000001"),
    )

    assert [(row.symbol, row.rank, row.member_kind) for row in members] == [
        ("000001", 1, "active"),
        ("000002", 2, "active"),
        ("000003", 3, "forced"),
        ("KOSPI", 1, "benchmark"),
    ]


def test_forced_symbol_must_pass_current_eligibility_and_positive_valuation() -> None:
    with pytest.raises(
        service.KAssetResearchCohortError,
        match="Forced symbols lack an eligible positive valuation row",
    ):
        service.assemble_cohort_members(
            market="kr",
            eligible=[_eligible("005930", "100")],
            requested_size=1,
            forced_symbols=("000660",),
        )


def test_requested_size_requires_complete_positive_eligible_core() -> None:
    with pytest.raises(service.KAssetResearchCohortError, match="requested_size=100"):
        service.assemble_cohort_members(
            market="us",
            eligible=[_eligible("AAPL", "1")],
            requested_size=100,
            forced_symbols=(),
        )


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

    monkeypatch.setattr(service, "_latest_snapshot_date", latest)
    monkeypatch.setattr(service, "_eligible_rows", rows)
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
