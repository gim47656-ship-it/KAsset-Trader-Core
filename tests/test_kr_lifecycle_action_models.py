from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.kr_lifecycle_actions import (
    KAssetCorporateActionFetchCoverage,
    KRCorporateActionEvidence,
    KRStockLifecycleObservation,
)
from app.models.kr_symbol_universe import KRSymbolUniverse


def _constraint_names(model: type[object], kind: type[object]) -> set[str | None]:
    return {
        constraint.name
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, kind)
    }


def test_lifecycle_model_preserves_identity_raw_evidence_and_observation_time() -> None:
    columns = KRStockLifecycleObservation.__table__.c

    assert {
        "symbol",
        "source",
        "provider",
        "provider_endpoint",
        "provider_tr_id",
        "pdno",
        "std_pdno",
        "isin",
        "listing_status",
        "list_date",
        "delist_date",
        "observed_at",
        "fetch_run_id",
        "raw_provider_fields",
        "canonical_raw_hash",
        "idempotency_key",
    } <= set(columns.keys())
    assert not columns.observed_at.nullable
    assert not columns.fetch_run_id.nullable
    assert not columns.raw_provider_fields.nullable
    assert "uq_kr_lifecycle_obs_idempotency_key" in _constraint_names(
        KRStockLifecycleObservation, UniqueConstraint
    )
    assert {
        "ck_kr_lifecycle_obs_symbol_nonblank",
        "ck_kr_lifecycle_obs_source_nonblank",
        "ck_kr_lifecycle_obs_provider_nonblank",
        "ck_kr_lifecycle_obs_endpoint_nonblank",
        "ck_kr_lifecycle_obs_tr_nonblank",
        "ck_kr_lifecycle_obs_date_order",
    } <= _constraint_names(KRStockLifecycleObservation, CheckConstraint)
    assert columns.listing_status.type.length == 64
    assert KRSymbolUniverse.__table__.c.listing_status.type.length == 64


def test_action_model_has_window_and_provider_identity_guards() -> None:
    columns = KRCorporateActionEvidence.__table__.c

    assert {
        "symbol",
        "source",
        "provider",
        "provider_endpoint",
        "provider_tr_id",
        "evidence_kind",
        "provider_action_type",
        "std_pdno",
        "isin",
        "requested_from_date",
        "requested_to_date",
        "effective_date",
        "record_date",
        "list_date",
        "payment_date",
        "observed_at",
        "fetch_run_id",
        "raw_provider_fields",
        "canonical_raw_hash",
        "idempotency_key",
    } <= set(columns.keys())
    assert "uq_kr_action_ev_idempotency_key" in _constraint_names(
        KRCorporateActionEvidence, UniqueConstraint
    )
    assert {
        "ck_kr_action_ev_symbol_nonblank",
        "ck_kr_action_ev_source_nonblank",
        "ck_kr_action_ev_provider_nonblank",
        "ck_kr_action_ev_endpoint_nonblank",
        "ck_kr_action_ev_tr_nonblank",
        "ck_kr_action_ev_kind_nonblank",
        "ck_kr_action_ev_window_order",
    } <= _constraint_names(KRCorporateActionEvidence, CheckConstraint)


def test_fetch_coverage_model_records_zero_rows_and_failures_without_overwrite() -> (
    None
):
    columns = KAssetCorporateActionFetchCoverage.__table__.c

    assert {
        "symbol",
        "source",
        "provider",
        "provider_endpoint",
        "provider_tr_id",
        "action_kind",
        "requested_from_date",
        "requested_to_date",
        "completed_at",
        "row_count",
        "status",
        "fetch_run_id",
        "error_class",
        "error_message",
        "last_cursor",
        "page_count",
        "idempotency_key",
    } <= set(columns.keys())
    assert {
        "uq_kasset_ca_coverage_idempotency",
        "uq_kasset_ca_coverage_run_window",
    } <= _constraint_names(KAssetCorporateActionFetchCoverage, UniqueConstraint)
    assert {
        "ck_kasset_ca_coverage_symbol_nonblank",
        "ck_kasset_ca_coverage_identity_nonblank",
        "ck_kasset_ca_coverage_window_order",
        "ck_kasset_ca_coverage_counts",
        "ck_kasset_ca_coverage_status",
    } <= _constraint_names(KAssetCorporateActionFetchCoverage, CheckConstraint)
