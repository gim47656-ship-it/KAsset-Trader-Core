"""KAsset PAPER position-manager state를 추가한다.

Revision ID: 20260829_kasset_position_manager
Revises: 20260829_kasset_routine_market_scope
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260829_kasset_position_manager"
down_revision = "20260829_kasset_routine_market_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "kasset_paper_position_states"


def upgrade() -> None:
    op.create_table(
        _TABLE_NAME,
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("initial_atr", sa.Numeric(20, 8), nullable=False),
        sa.Column("initial_stop", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_stop", sa.Numeric(20, 8), nullable=False),
        sa.Column("highest_close", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "partial_exit_completed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("entry_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_exit_signal_key", sa.Text()),
        sa.Column("strategy_version", sa.Text(), nullable=False),
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
        sa.CheckConstraint("market IN ('KRX', 'US')", name="ck_kasset_position_state_market_valid"),
        sa.CheckConstraint("entry_price > 0", name="ck_kasset_position_state_entry_price_positive"),
        sa.CheckConstraint("initial_atr > 0", name="ck_kasset_position_state_initial_atr_positive"),
        sa.CheckConstraint("initial_stop > 0", name="ck_kasset_position_state_initial_stop_positive"),
        sa.CheckConstraint("current_stop > 0", name="ck_kasset_position_state_current_stop_positive"),
        sa.CheckConstraint("highest_close > 0", name="ck_kasset_position_state_highest_close_positive"),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "paper_account_id"],
            [
                "kasset_android_paper_accounts.owner_user_id",
                "kasset_android_paper_accounts.paper_account_id",
            ],
            name="fk_kasset_position_state_owner_paper_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "paper_account_id",
            "symbol",
            name="pk_kasset_paper_position_states",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "last_exit_signal_key",
            name="uq_kasset_position_state_owner_exit_signal",
        ),
    )
    op.create_index(
        "ix_kasset_position_state_owner_updated",
        _TABLE_NAME,
        ["owner_user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kasset_position_state_owner_updated", table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)
