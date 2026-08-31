"""KAsset 추천 cycle 운영 원장을 추가한다 (append-only, expansion-only).

Revision ID: 20260831_kasset_cycle_audit
Revises: 20260831_kasset_shadow_loss_lock
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_kasset_cycle_audit"
down_revision = "20260831_kasset_shadow_loss_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_TABLE = "kasset_automation_cycle_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skipped_reason", sa.Text(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("ranked_count", sa.Integer(), nullable=False),
        sa.Column("candidate_exclusion_count", sa.Integer(), nullable=False),
        sa.Column("strategy_evaluated_count", sa.Integer(), nullable=False),
        sa.Column("strategy_actionable_count", sa.Integer(), nullable=False),
        sa.Column("ai_reviewed_count", sa.Integer(), nullable=False),
        sa.Column("ai_failure_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_count", sa.Integer(), nullable=False),
        sa.Column("candidate_markets", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_sources", postgresql.JSONB(), nullable=False),
        sa.Column("collection_policy", postgresql.JSONB(), nullable=False),
        sa.Column("ranked_candidates", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_exclusions", postgresql.JSONB(), nullable=False),
        sa.Column("ai_review_rejections", postgresql.JSONB(), nullable=False),
        sa.Column("ai_review_outcomes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'skipped', 'failed')",
            name="kasset_automation_cycle_status",
        ),
        sa.CheckConstraint(
            "finished_at >= observed_at",
            name="kasset_automation_cycle_finished_after_observed",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0 AND ranked_count >= 0 "
            "AND candidate_exclusion_count >= 0 "
            "AND strategy_evaluated_count >= 0 "
            "AND strategy_actionable_count >= 0 "
            "AND ai_reviewed_count >= 0 AND ai_failure_count >= 0 "
            "AND recommendation_count >= 0",
            name="kasset_automation_cycle_counts_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_automation_cycle_events"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_automation_cycle_observed_at",
        _TABLE,
        [sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_automation_cycle_owner_observed_at",
        _TABLE,
        ["owner_user_id", sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kasset_automation_cycle_owner_observed_at",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_kasset_automation_cycle_observed_at",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
