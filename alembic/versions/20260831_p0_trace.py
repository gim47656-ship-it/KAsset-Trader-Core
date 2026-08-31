"""추천 cycle → 추천 → PAPER 실행을 잇는 추적 원장을 추가한다.

확장 전용(expansion-only) migration이다. 기존 행은 새 열이 NULL이거나 빈
배열로 남고, 어떤 기존 열도 삭제·변경하지 않는다. 추천 테이블에는 감사
원장으로 향하는 외래키를 만들지 않는다. 감사 쓰기 실패가 추천이나 체결을
막을 수 없어야 하기 때문이다.

Revision ID: 20260831_p0_trace
Revises: 20260831_paper_initial_usd
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_p0_trace"
down_revision = "20260831_paper_initial_usd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_CYCLE_TABLE = "kasset_automation_cycle_events"
_RECOMMENDATION_TABLE = "ai_recommendations"
_EXECUTION_TABLE = "kasset_paper_execution_events"

_ORIGINS = "'AUTO_PAPER', 'APPROVAL'"
_STATUSES = "'IDLE', 'BLOCKED', 'REJECTED', 'SUBMITTED', 'FAILED'"


def upgrade() -> None:
    op.add_column(
        _CYCLE_TABLE,
        sa.Column("cycle_trace_id", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _CYCLE_TABLE,
        sa.Column(
            "recommendation_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "kasset_automation_cycle_trace_nonempty",
        _CYCLE_TABLE,
        "cycle_trace_id IS NULL OR length(btrim(cycle_trace_id)) > 0",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "kasset_automation_cycle_recommendation_ids_array",
        _CYCLE_TABLE,
        "jsonb_typeof(recommendation_ids) = 'array'",
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_automation_cycle_trace",
        _CYCLE_TABLE,
        ["cycle_trace_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("cycle_trace_id IS NOT NULL"),
    )

    op.add_column(
        _RECOMMENDATION_TABLE,
        sa.Column("cycle_trace_id", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "cycle_trace_nonempty",
        _RECOMMENDATION_TABLE,
        "cycle_trace_id IS NULL OR length(btrim(cycle_trace_id)) > 0",
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ai_recommendations_owner_cycle_trace",
        _RECOMMENDATION_TABLE,
        ["owner_user_id", "cycle_trace_id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("cycle_trace_id IS NOT NULL"),
    )

    op.create_table(
        _EXECUTION_TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("recommendation_id", sa.Text(), nullable=False),
        sa.Column("cycle_trace_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "replayed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("paper_order_id", sa.Text(), nullable=True),
        sa.Column("promotion_bypass_reason", sa.Text(), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("observed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"origin IN ({_ORIGINS})",
            name="kasset_paper_execution_origin",
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUSES})",
            name="kasset_paper_execution_status",
        ),
        sa.CheckConstraint(
            "length(btrim(recommendation_id)) > 0",
            name="kasset_paper_execution_recommendation_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0",
            name="kasset_paper_execution_reason_nonempty",
        ),
        sa.CheckConstraint(
            "cycle_trace_id IS NULL OR length(btrim(cycle_trace_id)) > 0",
            name="kasset_paper_execution_cycle_trace_nonempty",
        ),
        sa.CheckConstraint(
            "paper_order_id IS NULL OR length(btrim(paper_order_id)) > 0",
            name="kasset_paper_execution_order_nonempty",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="kasset_paper_execution_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name="kasset_paper_execution_finished_after_started",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_paper_execution_events"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_paper_execution_owner_observed_at",
        _EXECUTION_TABLE,
        ["owner_user_id", sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_paper_execution_owner_recommendation",
        _EXECUTION_TABLE,
        ["owner_user_id", "recommendation_id", sa.text("observed_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kasset_paper_execution_cycle_trace",
        _EXECUTION_TABLE,
        ["cycle_trace_id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("cycle_trace_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kasset_paper_execution_cycle_trace",
        table_name=_EXECUTION_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_kasset_paper_execution_owner_recommendation",
        table_name=_EXECUTION_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_kasset_paper_execution_owner_observed_at",
        table_name=_EXECUTION_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_EXECUTION_TABLE, schema=_SCHEMA)

    op.drop_index(
        "ix_ai_recommendations_owner_cycle_trace",
        table_name=_RECOMMENDATION_TABLE,
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "cycle_trace_nonempty",
        _RECOMMENDATION_TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_column(_RECOMMENDATION_TABLE, "cycle_trace_id", schema=_SCHEMA)

    op.drop_index(
        "ix_kasset_automation_cycle_trace",
        table_name=_CYCLE_TABLE,
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "kasset_automation_cycle_recommendation_ids_array",
        _CYCLE_TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "kasset_automation_cycle_trace_nonempty",
        _CYCLE_TABLE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_column(_CYCLE_TABLE, "recommendation_ids", schema=_SCHEMA)
    op.drop_column(_CYCLE_TABLE, "cycle_trace_id", schema=_SCHEMA)
