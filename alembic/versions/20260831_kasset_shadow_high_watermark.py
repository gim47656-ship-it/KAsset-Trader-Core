"""KAsset 계좌 일별 고점 SHADOW 상태 테이블을 추가한다.

Revision ID: 20260831_kasset_shadow_hwm
Revises: 20260830_ai_runtime_config
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_kasset_shadow_hwm"
down_revision = "20260830_ai_runtime_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_shadow_daily_high_watermarks"
_INDEX = "ix_kasset_shadow_hwm_owner_valuation"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_key", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("session_opening_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("reference_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("peak_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("current_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("valuation_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("valuation_source", sa.Text(), nullable=False),
        sa.Column(
            "state_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("evidence_schema_version", sa.Text(), nullable=False),
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'SHADOW'"),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "account_key",
            "market",
            "trading_date",
            name="pk_kasset_shadow_daily_high_watermarks",
        ),
        sa.CheckConstraint(
            "btrim(account_key) <> ''",
            name="ck_kasset_shadow_hwm_account_key_nonempty",
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US')",
            name="ck_kasset_shadow_hwm_market_valid",
        ),
        sa.CheckConstraint(
            "mode = 'SHADOW'",
            name="ck_kasset_shadow_hwm_mode_shadow",
        ),
        sa.CheckConstraint(
            "session_opening_equity > 0 AND reference_equity > 0 "
            "AND peak_equity > 0 AND current_equity > 0",
            name="ck_kasset_shadow_hwm_equities_positive",
        ),
        sa.CheckConstraint(
            "peak_equity >= session_opening_equity "
            "AND peak_equity >= current_equity",
            name="ck_kasset_shadow_hwm_peak_monotonic",
        ),
        sa.CheckConstraint(
            "state_version > 0",
            name="ck_kasset_shadow_hwm_state_version_positive",
        ),
        sa.CheckConstraint(
            "btrim(valuation_source) <> ''",
            name="ck_kasset_shadow_hwm_valuation_source_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(evidence_schema_version) <> ''",
            name="ck_kasset_shadow_hwm_evidence_schema_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name="ck_kasset_shadow_hwm_evidence_object",
        ),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["owner_user_id", "valuation_at"],
        unique=False,
    )


def downgrade() -> None:
    # 기존 테이블이나 행은 해석·변경하지 않고 전용 SHADOW 테이블만 제거한다.
    op.drop_table(_TABLE)
