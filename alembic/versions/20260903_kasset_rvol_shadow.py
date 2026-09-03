"""KAsset 장중 RVOL shadow 관측 원장을 추가한다.

Revision ID: 20260903_kasset_rvol_shadow
Revises: 20260902_screener_toss_source
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260903_kasset_rvol_shadow"
down_revision = "20260902_screener_toss_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_TABLE = "kasset_intraday_rvol_shadow"
_OBSERVED_AT_INDEX = "ix_review_kasset_intraday_rvol_shadow_observed_at"
_SYMBOL_OBSERVED_AT_INDEX = "ix_review_kasset_intraday_rvol_shadow_symbol_observed_at"
_CYCLE_TRACE_SYMBOL_INDEX = (
    "ix_review_kasset_intraday_rvol_shadow_cycle_trace_symbol"
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("cycle_trace_id", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("bucket_start_kst", sa.Time(), nullable=False),
        sa.Column("completed_bars", sa.Integer(), nullable=False),
        sa.Column("session_decision_status", sa.Text(), nullable=False),
        sa.Column("session_decision_reason", sa.Text(), nullable=True),
        sa.Column("same_time_baseline_median_5m", sa.Numeric(), nullable=True),
        sa.Column("same_time_baseline_median_20m", sa.Numeric(), nullable=True),
        sa.Column("session_rvol_5m", sa.Numeric(), nullable=True),
        sa.Column("session_status_5m", sa.Text(), nullable=False),
        sa.Column("session_rvol_20m", sa.Numeric(), nullable=True),
        sa.Column("session_status_20m", sa.Text(), nullable=False),
        sa.Column("same_time_rvol_5m", sa.Numeric(), nullable=True),
        sa.Column("same_time_status_5m", sa.Text(), nullable=False),
        sa.Column("same_time_sample_days_5m", sa.Integer(), nullable=False),
        sa.Column("same_time_rvol_20m", sa.Numeric(), nullable=True),
        sa.Column("same_time_status_20m", sa.Text(), nullable=False),
        sa.Column("same_time_sample_days_20m", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_intraday_rvol_shadow"),
        schema=_SCHEMA,
    )
    op.create_index(
        _OBSERVED_AT_INDEX,
        _TABLE,
        [sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        _SYMBOL_OBSERVED_AT_INDEX,
        _TABLE,
        ["symbol", sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        _CYCLE_TRACE_SYMBOL_INDEX,
        _TABLE,
        ["cycle_trace_id", "symbol"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("cycle_trace_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        _CYCLE_TRACE_SYMBOL_INDEX,
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        _SYMBOL_OBSERVED_AT_INDEX,
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        _OBSERVED_AT_INDEX,
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
