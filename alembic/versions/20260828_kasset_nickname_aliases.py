"""Add KAsset nickname bounds and searchable instrument aliases.

Revision ID: 20260828_kasset_ux_names
Revises: 20260828_kasset_google_sub
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260828_kasset_ux_names"
down_revision = "20260828_kasset_google_sub"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "nickname",
        existing_type=sa.Text(),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
    op.add_column(
        "instruments",
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE instruments
            SET aliases = CASE symbol
                WHEN '005930' THEN ARRAY['삼성', 'samsung', '젬스']::text[]
                WHEN '000660' THEN ARRAY['하이닉스', 'sk', '에스케이', 'hynix']::text[]
                WHEN '035420' THEN ARRAY['네이버', 'naver', '엔버']::text[]
            END
            WHERE symbol IN ('005930', '000660', '035420')
            """
        )
    )


def downgrade() -> None:
    op.drop_column("instruments", "aliases")
    op.alter_column(
        "users",
        "nickname",
        existing_type=sa.String(length=32),
        type_=sa.Text(),
        existing_nullable=True,
    )
