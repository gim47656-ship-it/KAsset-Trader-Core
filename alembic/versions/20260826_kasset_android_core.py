"""Add KAsset Android PAPER order and runtime state.

Revision ID: 20260826_kasset_android_core
Revises: 20260824_s257_rung_reason
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260826_kasset_android_core"
down_revision = "20260824_s257_rung_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kasset_android_paper_orders",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(20, 8), server_default="0", nullable=False),
        sa.Column("average_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("paper_trade_id", sa.BigInteger(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_kasset_android_order_side"),
        sa.CheckConstraint(
            "order_type IN ('MARKET','LIMIT')",
            name="ck_kasset_android_order_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','OPEN','FILLED','PARTIALLY_FILLED','CANCELLED','REJECTED')",
            name="ck_kasset_android_order_status",
        ),
        sa.ForeignKeyConstraint(
            ["paper_account_id"],
            ["paper.paper_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paper_trade_id"],
            ["paper.paper_trades.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_order_id", name="uq_kasset_android_broker_order_id"),
        sa.UniqueConstraint("client_order_id", name="uq_kasset_android_client_order_id"),
    )
    op.create_index(
        "ix_kasset_android_paper_orders_paper_account_id",
        "kasset_android_paper_orders",
        ["paper_account_id"],
    )
    op.create_table(
        "kasset_android_runtime_state",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kill_switch_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("trading_mode", sa.Text(), server_default="PAPER", nullable=False),
        sa.Column(
            "max_order_ratio",
            sa.Numeric(8, 4),
            server_default="0.1000",
            nullable=False,
        ),
        sa.Column(
            "max_symbol_ratio",
            sa.Numeric(8, 4),
            server_default="0.2500",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trading_mode IN ('PAPER')", name="ck_kasset_android_trading_mode"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("kasset_android_runtime_state")
    op.drop_index(
        "ix_kasset_android_paper_orders_paper_account_id",
        table_name="kasset_android_paper_orders",
    )
    op.drop_table("kasset_android_paper_orders")
