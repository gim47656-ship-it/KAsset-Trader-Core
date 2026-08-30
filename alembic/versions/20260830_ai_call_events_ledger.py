"""AI 호출 원장 ``review.ai_call_events``를 추가한다 (append-only, expansion-only).

운영 대시보드가 "AI를 얼마나 썼고 실제로 성공했는지"를 정량으로 읽으려면 attempt 단위
원장이 필요하다. 기존 테이블은 하나도 건드리지 않는다.

Revision ID: 20260830_ai_call_events
Revises: 20260830_kasset_promotion_bypass
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_ai_call_events"
down_revision = "20260830_kasset_promotion_bypass"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_TABLE = "ai_call_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("logical_call_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("route_name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("cost_currency", sa.Text(), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        # Bare names: alembic renders these through the project's
        # ``ck_%(table_name)s_%(constraint_name)s`` convention, exactly like the
        # ORM model does, so create_all and this migration agree.
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        sa.CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL "
            "AND cost_source IS NULL) OR "
            "(cost_amount IS NOT NULL AND length(btrim(cost_currency)) > 0 "
            "AND length(btrim(cost_source)) > 0)",
            name="cost_coherent",
        ),
        sa.CheckConstraint("length(btrim(feature)) > 0", name="feature_nonempty"),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name="finished_after_started",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="latency_ms_nonnegative"),
        sa.CheckConstraint(
            "length(btrim(logical_call_id)) > 0",
            name="logical_call_id_nonempty",
        ),
        sa.CheckConstraint("length(btrim(model_name)) > 0", name="model_name_nonempty"),
        sa.CheckConstraint("length(btrim(provider)) > 0", name="provider_nonempty"),
        sa.CheckConstraint("length(btrim(route_name)) > 0", name="route_name_nonempty"),
        sa.CheckConstraint("status IN ('success', 'failure')", name="status"),
        sa.CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) AND "
            "(completion_tokens IS NULL OR completion_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0)",
            name="tokens_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_call_events"),
        sa.UniqueConstraint(
            "logical_call_id",
            "attempt_no",
            name="uq_ai_call_events_logical_call_attempt",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ai_call_events_started_at",
        _TABLE,
        [sa.text("started_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ai_call_events_feature_started_at",
        _TABLE,
        ["feature", sa.text("started_at DESC")],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ai_call_events_provider_started_at",
        _TABLE,
        ["provider", sa.text("started_at DESC")],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_call_events_provider_started_at", table_name=_TABLE, schema=_SCHEMA
    )
    op.drop_index(
        "ix_ai_call_events_feature_started_at", table_name=_TABLE, schema=_SCHEMA
    )
    op.drop_index("ix_ai_call_events_started_at", table_name=_TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)
