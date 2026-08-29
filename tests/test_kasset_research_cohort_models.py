from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.kasset_research_cohorts import (
    KAssetResearchCohort,
    KAssetResearchCohortMember,
)


def _names(model: type[object], kind: type[object]) -> set[str | None]:
    return {
        constraint.name
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, kind)
    }


def test_cohort_model_locks_forward_method_source_size_and_date_contracts() -> None:
    columns = KAssetResearchCohort.__table__.c

    assert {
        "cohort_id",
        "market",
        "selection_as_of",
        "selection_date",
        "effective_date",
        "selection_method",
        "requested_size",
        "active_member_count",
        "valuation_snapshot_date",
        "valuation_snapshot_source",
        "evidence_scope",
        "created_at",
    } <= set(columns.keys())
    assert {
        "ck_kasset_research_cohort_id_nonblank",
        "ck_kasset_research_cohort_market",
        "ck_kasset_research_cohort_method",
        "ck_kasset_research_cohort_source_nonblank",
        "ck_kasset_research_cohort_source",
        "ck_kasset_research_cohort_size",
        "ck_kasset_research_cohort_date_order",
        "ck_kasset_research_cohort_scope",
    } <= _names(KAssetResearchCohort, CheckConstraint)


def test_member_model_enforces_positive_kind_scoped_rank_and_identity() -> None:
    assert {
        "ck_kasset_research_member_symbol_nonblank",
        "ck_kasset_research_member_rank_positive",
        "ck_kasset_research_member_kind",
        "ck_kasset_research_member_market_cap",
    } <= _names(KAssetResearchCohortMember, CheckConstraint)
    assert {
        "uq_kasset_research_member_kind_rank",
        "uq_kasset_research_member_symbol",
    } <= _names(KAssetResearchCohortMember, UniqueConstraint)
