"""KAsset 손실 연속 SHADOW 관찰 상태 테이블을 추가한다.

Revision ID: 20260831_kasset_shadow_loss_lock
Revises: 20260831_kasset_shadow_hwm
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_kasset_shadow_loss_lock"
down_revision = "20260831_kasset_shadow_hwm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_shadow_loss_locks"
_INDEX = "ix_kasset_shadow_loss_lock_owner_expiration"


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
        sa.Column("lock_scope", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "evaluated_at", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("streak_count", sa.BigInteger(), nullable=False),
        sa.Column("loss_limit", sa.BigInteger(), nullable=False),
        sa.Column("newest_loss_id", sa.BigInteger(), nullable=True),
        sa.Column("newest_loss_transaction_id", sa.Text(), nullable=True),
        sa.Column("newest_loss_trade_id", sa.Text(), nullable=True),
        sa.Column(
            "newest_loss_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "buy_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("lock_reason", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("evidence_version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'valid'"),
        ),
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
            "lock_scope",
            "symbol",
            name="pk_kasset_shadow_loss_locks",
        ),
        sa.CheckConstraint(
            "btrim(account_key) <> ''",
            name="ck_kasset_shadow_loss_lock_account_key_nonempty",
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US')",
            name="ck_kasset_shadow_loss_lock_market_valid",
        ),
        sa.CheckConstraint(
            "lock_scope IN ('GLOBAL', 'SYMBOL')",
            name="ck_kasset_shadow_loss_lock_scope_valid",
        ),
        sa.CheckConstraint(
            "(lock_scope = 'GLOBAL' AND symbol = '') OR "
            "(lock_scope = 'SYMBOL' AND btrim(symbol) <> '')",
            name="ck_kasset_shadow_loss_lock_symbol_scope",
        ),
        sa.CheckConstraint(
            "mode = 'SHADOW'",
            name="ck_kasset_shadow_loss_lock_mode_shadow",
        ),
        sa.CheckConstraint(
            "status = 'valid'",
            name="ck_kasset_shadow_loss_lock_status_valid",
        ),
        sa.CheckConstraint(
            "streak_count >= 0 AND loss_limit > 0",
            name="ck_kasset_shadow_loss_lock_counts_valid",
        ),
        sa.CheckConstraint(
            "("
            "streak_count = 0 "
            "AND newest_loss_id IS NULL "
            "AND newest_loss_transaction_id IS NULL "
            "AND newest_loss_trade_id IS NULL "
            "AND newest_loss_at IS NULL "
            "AND expires_at IS NULL"
            ") OR ("
            "streak_count > 0 "
            "AND newest_loss_id IS NOT NULL "
            "AND newest_loss_transaction_id IS NOT NULL "
            "AND newest_loss_trade_id IS NOT NULL "
            "AND newest_loss_at IS NOT NULL "
            "AND expires_at IS NOT NULL"
            ")",
            name="ck_kasset_shadow_loss_lock_newest_consistent",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > newest_loss_at",
            name="ck_kasset_shadow_loss_lock_expiration_valid",
        ),
        sa.CheckConstraint(
            "buy_locked = ("
            "streak_count >= loss_limit "
            "AND expires_at IS NOT NULL "
            "AND expires_at > evaluated_at"
            ")",
            name="ck_kasset_shadow_loss_lock_buy_state_consistent",
        ),
        sa.CheckConstraint(
            "btrim(lock_reason) <> '' "
            "AND btrim(config_fingerprint) <> '' "
            "AND btrim(schema_version) <> '' "
            "AND btrim(evidence_version) <> ''",
            name="ck_kasset_shadow_loss_lock_metadata_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name="ck_kasset_shadow_loss_lock_evidence_object",
        ),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["owner_user_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    # 다른 상태나 거래 원장은 건드리지 않고 전용 SHADOW 테이블만 제거한다.
    op.drop_table(_TABLE)
