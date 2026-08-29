"""Durable KIS evidence for known KR symbols.

Rows are append-only observations in normal operation. Exact duplicate provider
payloads share an idempotency key and are ignored by the writer; corrected
provider payloads retain a separate evidence row.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.models.base import Base


class KRStockLifecycleObservation(Base):
    """One exact KIS stock-info response observed for a known KR symbol."""

    __tablename__ = "kr_stock_lifecycle_observations"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(symbol)) > 0",
            name=conv("ck_kr_lifecycle_obs_symbol_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(source)) > 0",
            name=conv("ck_kr_lifecycle_obs_source_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0",
            name=conv("ck_kr_lifecycle_obs_provider_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(provider_endpoint)) > 0",
            name=conv("ck_kr_lifecycle_obs_endpoint_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(provider_tr_id)) > 0",
            name=conv("ck_kr_lifecycle_obs_tr_nonblank"),
        ),
        CheckConstraint(
            "list_date IS NULL OR delist_date IS NULL OR list_date <= delist_date",
            name=conv("ck_kr_lifecycle_obs_date_order"),
        ),
        UniqueConstraint("idempotency_key", name="uq_kr_lifecycle_obs_idempotency_key"),
        Index("ix_kr_lifecycle_obs_symbol_observed", "symbol", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey(
            "kr_symbol_universe.symbol",
            name="fk_kr_lifecycle_obs_symbol",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    provider_tr_id: Mapped[str] = mapped_column(String(32), nullable=False)

    pdno: Mapped[str | None] = mapped_column(String(32), nullable=True)
    std_pdno: Mapped[str | None] = mapped_column(String(32), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    listing_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    fetch_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    raw_provider_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class KRCorporateActionEvidence(Base):
    """Exact KIS corporate-action evidence without inferred action semantics."""

    __tablename__ = "kr_corporate_action_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(symbol)) > 0", name=conv("ck_kr_action_ev_symbol_nonblank")
        ),
        CheckConstraint(
            "length(btrim(source)) > 0", name=conv("ck_kr_action_ev_source_nonblank")
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0",
            name=conv("ck_kr_action_ev_provider_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(provider_endpoint)) > 0",
            name=conv("ck_kr_action_ev_endpoint_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(provider_tr_id)) > 0",
            name=conv("ck_kr_action_ev_tr_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(evidence_kind)) > 0",
            name=conv("ck_kr_action_ev_kind_nonblank"),
        ),
        CheckConstraint(
            "requested_from_date <= requested_to_date",
            name=conv("ck_kr_action_ev_window_order"),
        ),
        UniqueConstraint("idempotency_key", name="uq_kr_action_ev_idempotency_key"),
        Index("ix_kr_action_ev_symbol_record", "symbol", "record_date"),
        Index("ix_kr_action_ev_kind_observed", "evidence_kind", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey(
            "kr_symbol_universe.symbol",
            name="fk_kr_action_ev_symbol",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    provider_tr_id: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_action_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    std_pdno: Mapped[str | None] = mapped_column(String(32), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_from_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_to_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    fetch_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    raw_provider_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class KAssetCorporateActionFetchCoverage(Base):
    """Durable proof that one endpoint/window completed or failed."""

    __tablename__ = "kasset_corporate_action_fetch_coverage"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(symbol)) > 0",
            name=conv("ck_kasset_ca_coverage_symbol_nonblank"),
        ),
        CheckConstraint(
            "length(btrim(source)) > 0 "
            "AND length(btrim(provider)) > 0 "
            "AND length(btrim(provider_endpoint)) > 0 "
            "AND length(btrim(provider_tr_id)) > 0 "
            "AND length(btrim(action_kind)) > 0",
            name=conv("ck_kasset_ca_coverage_identity_nonblank"),
        ),
        CheckConstraint(
            "requested_from_date <= requested_to_date",
            name=conv("ck_kasset_ca_coverage_window_order"),
        ),
        CheckConstraint(
            "row_count >= 0 AND page_count >= 0",
            name=conv("ck_kasset_ca_coverage_counts"),
        ),
        CheckConstraint(
            "(status = 'success' AND error_class IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND length(btrim(error_class)) > 0)",
            name=conv("ck_kasset_ca_coverage_status"),
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_kasset_ca_coverage_idempotency",
        ),
        UniqueConstraint(
            "fetch_run_id",
            "symbol",
            "provider_endpoint",
            "provider_tr_id",
            "requested_from_date",
            "requested_to_date",
            name="uq_kasset_ca_coverage_run_window",
        ),
        Index(
            "ix_kasset_ca_coverage_symbol_window",
            "symbol",
            "requested_from_date",
            "requested_to_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey(
            "kr_symbol_universe.symbol",
            name="fk_kasset_ca_coverage_symbol",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    provider_tr_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_from_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_to_date: Mapped[date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    fetch_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_cursor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
