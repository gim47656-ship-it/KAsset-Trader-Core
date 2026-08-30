"""Add nullable news translation fields.

Revision ID: 20260830_news_translation
Revises: 20260830_kr_lifecycle_ca
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_news_translation"
down_revision = "20260830_kr_lifecycle_ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_analysis_results",
        sa.Column("translated_title", sa.Text(), nullable=True),
    )
    op.add_column(
        "news_analysis_results",
        sa.Column("translated_excerpt", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("news_analysis_results", "translated_excerpt")
    op.drop_column("news_analysis_results", "translated_title")
