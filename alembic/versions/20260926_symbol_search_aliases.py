"""Add precomputed instrument search aliases.

Revision ID: 20260926_symbol_search_aliases
Revises: 20260908_kasset_stop_history
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260926_symbol_search_aliases"
down_revision = "20260908_kasset_stop_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_search_aliases",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("market", sa.String(3), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("alias", sa.String(30), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US')", name="ck_symbol_search_aliases_market"
        ),
        sa.UniqueConstraint(
            "market", "symbol", "alias", name="uq_symbol_search_aliases_key"
        ),
        schema="public",
    )
    op.create_index(
        "ix_symbol_search_aliases_alias",
        "symbol_search_aliases",
        ["alias"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("symbol_search_aliases", schema="public")
