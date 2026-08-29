"""종목 뉴스·공시 keyset 조회용 복합 인덱스를 추가한다.

Revision ID: 20260829_market_news_keyset
Revises: 20260828_nhplug_symbol_master
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260829_market_news_keyset"
down_revision = "20260828_nhplug_symbol_master"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_news_articles_symbol_published_id_feed"


def upgrade() -> None:
    """종목 선두의 시간순 keyset 인덱스를 만든다."""

    op.create_index(
        _INDEX_NAME,
        "news_articles",
        [
            "stock_symbol",
            sa.text("article_published_at DESC NULLS LAST"),
            sa.text("id DESC"),
            "feed_source",
        ],
        unique=False,
    )


def downgrade() -> None:
    """종목 뉴스·공시 keyset 인덱스를 제거한다."""

    op.drop_index(_INDEX_NAME, table_name="news_articles")
