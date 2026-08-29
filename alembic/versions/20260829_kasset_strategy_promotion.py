"""KAsset strategy promotion 전역 상태를 추가한다.

Revision ID: 20260829_kasset_promotion
Revises: 20260829_kasset_position_manager
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260829_kasset_promotion"
down_revision = "20260829_kasset_position_manager"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_TABLE = "kasset_strategy_promotions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("strategy_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("metrics_hash", sa.Text()),
        sa.Column(
            "threshold_evaluation",
            postgresql.JSONB(astext_type=sa.Text()),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("suspended_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(strategy_key) <> '' AND btrim(version) <> ''",
            name="ck_kasset_strategy_promotion_identity",
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT','BACKTESTED','PAPER_APPROVED',"
            "'PAPER_SUSPENDED','RETIRED')",
            name="ck_kasset_strategy_promotion_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metrics) = 'object'",
            name="ck_kasset_strategy_promotion_metrics_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array'",
            name="ck_kasset_strategy_promotion_evidence_array",
        ),
        sa.CheckConstraint(
            "threshold_evaluation IS NULL "
            "OR jsonb_typeof(threshold_evaluation) = 'object'",
            name="ck_kasset_strategy_promotion_threshold_object",
        ),
        sa.CheckConstraint(
            "metrics_hash IS NULL OR metrics_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kasset_strategy_promotion_hash_format",
        ),
        sa.CheckConstraint(
            "(state = 'DRAFT' AND metrics = '{}'::jsonb AND metrics_hash IS NULL)"
            " OR (state IN ('BACKTESTED','PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND metrics <> '{}'::jsonb AND metrics_hash IS NOT NULL)"
            " OR (state = 'RETIRED' AND ((metrics = '{}'::jsonb"
            " AND metrics_hash IS NULL) OR (metrics <> '{}'::jsonb"
            " AND metrics_hash IS NOT NULL)))",
            name="ck_kasset_strategy_promotion_metrics_state",
        ),
        sa.CheckConstraint(
            "(state IN ('PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND threshold_evaluation IS NOT NULL)"
            " OR state IN ('DRAFT','BACKTESTED','RETIRED')",
            name="ck_kasset_strategy_promotion_threshold_state",
        ),
        sa.CheckConstraint(
            "(state IN ('PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND approved_at IS NOT NULL)"
            " OR (state IN ('DRAFT','BACKTESTED') AND approved_at IS NULL)"
            " OR state = 'RETIRED'",
            name="ck_kasset_strategy_promotion_approved_at",
        ),
        sa.CheckConstraint(
            "(state = 'PAPER_SUSPENDED' AND suspended_at IS NOT NULL)"
            " OR (state IN ('DRAFT','BACKTESTED','PAPER_APPROVED')"
            " AND suspended_at IS NULL) OR state = 'RETIRED'",
            name="ck_kasset_strategy_promotion_suspended_at",
        ),
        sa.CheckConstraint(
            "(state = 'RETIRED' AND retired_at IS NOT NULL)"
            " OR (state <> 'RETIRED' AND retired_at IS NULL)",
            name="ck_kasset_strategy_promotion_retired_at",
        ),
        sa.CheckConstraint(
            "suspended_at IS NULL OR approved_at IS NOT NULL",
            name="ck_kasset_strategy_promotion_suspend_after_approve",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at"
            " AND (approved_at IS NULL OR approved_at >= created_at)"
            " AND (suspended_at IS NULL OR suspended_at >= approved_at)"
            " AND (retired_at IS NULL OR retired_at >= created_at)"
            " AND (retired_at IS NULL OR approved_at IS NULL"
            " OR retired_at >= approved_at)"
            " AND (retired_at IS NULL OR suspended_at IS NULL"
            " OR retired_at >= suspended_at)",
            name="ck_kasset_strategy_promotion_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_strategy_promotions"),
        sa.UniqueConstraint(
            "strategy_key",
            "version",
            name="uq_kasset_strategy_promotion_key_version",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_strategy_promotion_state_updated",
        _TABLE,
        ["state", "updated_at"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kasset_strategy_promotion_state_updated",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
