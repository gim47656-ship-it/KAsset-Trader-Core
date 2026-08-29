"""Add durable KR lifecycle and corporate-action evidence.

Revision ID: 20260830_kr_lifecycle_ca
Revises: 20260830_kasset_claim_lease
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260830_kr_lifecycle_ca"
down_revision = "20260830_kasset_claim_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "kr_symbol_universe",
        "listing_status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.add_column(
        "kr_symbol_universe",
        sa.Column("std_pdno", sa.String(length=32), nullable=True),
    )

    op.create_table(
        "kr_stock_lifecycle_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_endpoint", sa.Text(), nullable=False),
        sa.Column("provider_tr_id", sa.String(length=32), nullable=False),
        sa.Column("pdno", sa.String(length=32), nullable=True),
        sa.Column("std_pdno", sa.String(length=32), nullable=True),
        sa.Column("isin", sa.String(length=32), nullable=True),
        sa.Column("listing_status", sa.String(length=64), nullable=True),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("fetch_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "raw_provider_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("canonical_raw_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_kr_lifecycle_obs_symbol_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_kr_lifecycle_obs_source_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0",
            name="ck_kr_lifecycle_obs_provider_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_endpoint)) > 0",
            name="ck_kr_lifecycle_obs_endpoint_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_tr_id)) > 0",
            name="ck_kr_lifecycle_obs_tr_nonblank",
        ),
        sa.CheckConstraint(
            "list_date IS NULL OR delist_date IS NULL OR list_date <= delist_date",
            name="ck_kr_lifecycle_obs_date_order",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"],
            ["kr_symbol_universe.symbol"],
            name="fk_kr_lifecycle_obs_symbol",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_kr_lifecycle_obs_idempotency_key"
        ),
    )
    op.create_index(
        "ix_kr_lifecycle_obs_symbol_observed",
        "kr_stock_lifecycle_observations",
        ["symbol", "observed_at"],
        unique=False,
    )

    op.create_table(
        "kr_corporate_action_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_endpoint", sa.Text(), nullable=False),
        sa.Column("provider_tr_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_kind", sa.String(length=64), nullable=False),
        sa.Column("provider_action_type", sa.String(length=128), nullable=True),
        sa.Column("std_pdno", sa.String(length=32), nullable=True),
        sa.Column("isin", sa.String(length=32), nullable=True),
        sa.Column("requested_from_date", sa.Date(), nullable=False),
        sa.Column("requested_to_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("fetch_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "raw_provider_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("canonical_raw_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_kr_action_ev_symbol_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0",
            name="ck_kr_action_ev_source_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0",
            name="ck_kr_action_ev_provider_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_endpoint)) > 0",
            name="ck_kr_action_ev_endpoint_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_tr_id)) > 0",
            name="ck_kr_action_ev_tr_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(evidence_kind)) > 0",
            name="ck_kr_action_ev_kind_nonblank",
        ),
        sa.CheckConstraint(
            "requested_from_date <= requested_to_date",
            name="ck_kr_action_ev_window_order",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"],
            ["kr_symbol_universe.symbol"],
            name="fk_kr_action_ev_symbol",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_kr_action_ev_idempotency_key"),
    )
    op.create_index(
        "ix_kr_action_ev_symbol_record",
        "kr_corporate_action_evidence",
        ["symbol", "record_date"],
        unique=False,
    )
    op.create_index(
        "ix_kr_action_ev_kind_observed",
        "kr_corporate_action_evidence",
        ["evidence_kind", "observed_at"],
        unique=False,
    )

    op.create_table(
        "kasset_corporate_action_fetch_coverage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_endpoint", sa.Text(), nullable=False),
        sa.Column("provider_tr_id", sa.String(length=32), nullable=False),
        sa.Column("action_kind", sa.String(length=64), nullable=False),
        sa.Column("requested_from_date", sa.Date(), nullable=False),
        sa.Column("requested_to_date", sa.Date(), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("fetch_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("last_cursor", sa.String(length=256), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_kasset_ca_coverage_symbol_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0 "
            "AND length(btrim(provider)) > 0 "
            "AND length(btrim(provider_endpoint)) > 0 "
            "AND length(btrim(provider_tr_id)) > 0 "
            "AND length(btrim(action_kind)) > 0",
            name="ck_kasset_ca_coverage_identity_nonblank",
        ),
        sa.CheckConstraint(
            "requested_from_date <= requested_to_date",
            name="ck_kasset_ca_coverage_window_order",
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND page_count >= 0",
            name="ck_kasset_ca_coverage_counts",
        ),
        sa.CheckConstraint(
            "(status = 'success' AND error_class IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND length(btrim(error_class)) > 0)",
            name="ck_kasset_ca_coverage_status",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"],
            ["kr_symbol_universe.symbol"],
            name="fk_kasset_ca_coverage_symbol",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_kasset_ca_coverage_idempotency",
        ),
        sa.UniqueConstraint(
            "fetch_run_id",
            "symbol",
            "provider_endpoint",
            "provider_tr_id",
            "requested_from_date",
            "requested_to_date",
            name="uq_kasset_ca_coverage_run_window",
        ),
    )
    op.create_index(
        "ix_kasset_ca_coverage_symbol_window",
        "kasset_corporate_action_fetch_coverage",
        ["symbol", "requested_from_date", "requested_to_date"],
        unique=False,
    )

    op.create_table(
        "kasset_research_cohorts",
        sa.Column("cohort_id", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("selection_as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("selection_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("selection_method", sa.String(length=32), nullable=False),
        sa.Column("requested_size", sa.Integer(), nullable=False),
        sa.Column("active_member_count", sa.Integer(), nullable=False),
        sa.Column("valuation_snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "valuation_snapshot_source",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("evidence_scope", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(cohort_id)) > 0",
            name="ck_kasset_research_cohort_id_nonblank",
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_kasset_research_cohort_market",
        ),
        sa.CheckConstraint(
            "selection_method = 'latest_market_cap'",
            name="ck_kasset_research_cohort_method",
        ),
        sa.CheckConstraint(
            "length(btrim(valuation_snapshot_source)) > 0",
            name="ck_kasset_research_cohort_source_nonblank",
        ),
        sa.CheckConstraint(
            "valuation_snapshot_source IN "
            "('naver_finance', 'yahoo', 'toss_openapi', 'tvscreener')",
            name="ck_kasset_research_cohort_source",
        ),
        sa.CheckConstraint(
            "requested_size > 0 AND active_member_count = requested_size",
            name="ck_kasset_research_cohort_size",
        ),
        sa.CheckConstraint(
            "selection_date >= valuation_snapshot_date "
            "AND effective_date >= valuation_snapshot_date",
            name="ck_kasset_research_cohort_date_order",
        ),
        sa.CheckConstraint(
            "evidence_scope = 'forward_paper'",
            name="ck_kasset_research_cohort_scope",
        ),
        sa.PrimaryKeyConstraint("cohort_id"),
    )
    op.create_index(
        "ix_kasset_research_cohort_market_date",
        "kasset_research_cohorts",
        ["market", "selection_date"],
        unique=False,
    )

    op.create_table(
        "kasset_research_cohort_members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cohort_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("member_kind", sa.String(length=16), nullable=False),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column(
            "eligibility_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_kasset_research_member_symbol_nonblank",
        ),
        sa.CheckConstraint(
            "rank > 0",
            name="ck_kasset_research_member_rank_positive",
        ),
        sa.CheckConstraint(
            "member_kind IN ('active', 'forced', 'benchmark')",
            name="ck_kasset_research_member_kind",
        ),
        sa.CheckConstraint(
            "(member_kind = 'active' AND market_cap > 0) OR "
            "(member_kind IN ('forced', 'benchmark') "
            "AND (market_cap IS NULL OR market_cap > 0))",
            name="ck_kasset_research_member_market_cap",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["kasset_research_cohorts.cohort_id"],
            name="fk_kasset_research_member_cohort",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cohort_id",
            "member_kind",
            "rank",
            name="uq_kasset_research_member_kind_rank",
        ),
        sa.UniqueConstraint(
            "cohort_id",
            "symbol",
            name="uq_kasset_research_member_symbol",
        ),
    )
    op.create_index(
        "ix_kasset_research_member_symbol",
        "kasset_research_cohort_members",
        ["symbol"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kasset_research_member_symbol",
        table_name="kasset_research_cohort_members",
    )
    op.drop_table("kasset_research_cohort_members")
    op.drop_index(
        "ix_kasset_research_cohort_market_date",
        table_name="kasset_research_cohorts",
    )
    op.drop_table("kasset_research_cohorts")
    op.drop_index(
        "ix_kasset_ca_coverage_symbol_window",
        table_name="kasset_corporate_action_fetch_coverage",
    )
    op.drop_table("kasset_corporate_action_fetch_coverage")
    op.drop_index(
        "ix_kr_action_ev_kind_observed",
        table_name="kr_corporate_action_evidence",
    )
    op.drop_index(
        "ix_kr_action_ev_symbol_record",
        table_name="kr_corporate_action_evidence",
    )
    op.drop_table("kr_corporate_action_evidence")
    op.drop_index(
        "ix_kr_lifecycle_obs_symbol_observed",
        table_name="kr_stock_lifecycle_observations",
    )
    op.drop_table("kr_stock_lifecycle_observations")
    op.drop_column("kr_symbol_universe", "std_pdno")
    op.alter_column(
        "kr_symbol_universe",
        "listing_status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
