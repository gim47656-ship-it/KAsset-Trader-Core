"""KAsset 사용자별 KST 날짜 AI routine 설정을 추가한다.

Revision ID: 20260829_kasset_daily_routines
Revises: 20260829_market_news_keyset
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260829_kasset_daily_routines"
down_revision = "20260829_market_news_keyset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "kasset_ai_daily_routine_settings"


def upgrade() -> None:
    op.create_table(
        _TABLE_NAME,
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("routine_date", sa.Date(), nullable=False),
        sa.Column(
            "enabled_routines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(enabled_routines) = 'array'",
            name="ck_kasset_ai_daily_routine_settings_enabled_routines_array",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(enabled_routines) <= 4",
            name="ck_kasset_ai_daily_routine_settings_enabled_routines_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_kasset_ai_daily_routine_settings_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "routine_date",
            name="pk_kasset_ai_daily_routine_settings",
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE_NAME)
