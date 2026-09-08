"""PAPER stop/ATR snapshot activation history를 보존한다.

Revision ID: 20260908_kasset_stop_history
Revises: 20260907_kasset_optional_atr
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260908_kasset_stop_history"
down_revision = "20260907_kasset_optional_atr"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_paper_position_states"


def upgrade() -> None:
    # 기존 행은 둘 다 NULL인 legacy snapshot으로 남긴다. updated_at이나 migration
    # 실행 시각을 activation provenance로 추측하는 backfill은 하지 않는다.
    op.add_column(
        _TABLE,
        sa.Column("exit_levels_effective_at", sa.TIMESTAMP(timezone=True)),
    )
    op.add_column(
        _TABLE,
        sa.Column("exit_level_history", postgresql.JSONB(astext_type=sa.Text())),
    )


def downgrade() -> None:
    # 컬럼 삭제 자체는 기존 stop 값을 바꾸지 않지만 activation provenance를 잃는다.
    # 운영은 구버전 rollback 대신 이 revision을 이해하는 코드로 roll-forward한다.
    op.drop_column(_TABLE, "exit_level_history")
    op.drop_column(_TABLE, "exit_levels_effective_at")
