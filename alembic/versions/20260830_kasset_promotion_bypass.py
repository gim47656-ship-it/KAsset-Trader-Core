"""소유자별 "승격 근거 없이 PAPER 자동실행 허용" override를 런타임 상태에 추가한다.

Revision ID: 20260830_kasset_promotion_bypass
Revises: 20260830_news_translation
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_kasset_promotion_bypass"
down_revision = "20260830_news_translation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_android_runtime_state"
_COLUMN = "promotion_bypass_enabled"


def upgrade() -> None:
    # fail-closed: 기존 소유자는 전부 false로 들어와 지금 동작이 그대로 유지된다.
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
