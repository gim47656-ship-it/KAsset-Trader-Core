"""Add NH PLUG-backed integrated symbol master.

Revision ID: 20260828_nhplug_symbol_master
Revises: 20260828_kasset_ux_names
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260828_nhplug_symbol_master"
down_revision = "20260828_kasset_ux_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_master",
        sa.Column("market", sa.String(length=3), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("security_type", sa.String(length=20), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US')",
            name=op.f("ck_symbol_master_market"),
        ),
        sa.CheckConstraint(
            "security_type IN ('COMMON_STOCK', 'ETF')",
            name=op.f("ck_symbol_master_security_type"),
        ),
        sa.PrimaryKeyConstraint("market", "symbol", name=op.f("pk_symbol_master")),
    )
    op.create_index(
        "ix_symbol_master_market_active_symbol",
        "symbol_master",
        ["market", "is_active", "symbol"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_symbol_master_market_active_symbol",
        table_name="symbol_master",
    )
    op.drop_table("symbol_master")
