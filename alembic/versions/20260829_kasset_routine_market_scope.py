"""KAsset 일일 AI 추천 시장 범위를 추가한다.

Revision ID: 20260829_kasset_routine_market_scope
Revises: 20260829_kasset_daily_routines
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260829_kasset_routine_market_scope"
down_revision = "20260829_kasset_daily_routines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "kasset_ai_daily_routine_settings"
_CONSTRAINT_NAME = "ck_kasset_ai_daily_routine_settings_recommendation_market_scope_valid"


def upgrade() -> None:
    op.add_column(
        _TABLE_NAME,
        sa.Column(
            "recommendation_market_scope",
            sa.Text(),
            server_default="KR_US",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        "recommendation_market_scope IN ('KR_ONLY', 'US_ONLY', 'KR_US')",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.drop_column(_TABLE_NAME, "recommendation_market_scope")
