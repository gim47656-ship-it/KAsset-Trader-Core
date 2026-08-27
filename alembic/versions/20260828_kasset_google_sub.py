"""Add Google subject identity to KAsset users.

Revision ID: 20260828_kasset_google_sub
Revises: 20260827_kasset_multi_user_core
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260828_kasset_google_sub"
down_revision = "20260827_kasset_multi_user_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("google_sub", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_users_google_sub",
        "users",
        ["google_sub"],
        unique=True,
        postgresql_where=sa.text("google_sub IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
