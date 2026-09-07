"""ATR 근거가 없는 PAPER 보유분도 고정 손절선으로 관리하도록 initial_atr을 완화한다.

Revision ID: 20260907_kasset_optional_atr
Revises: 20260903_kasset_alert_events
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260907_kasset_optional_atr"
down_revision = "20260903_kasset_alert_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_paper_position_states"
_COLUMN = "initial_atr"


def upgrade() -> None:
    # 일봉 15봉이 없어 ATR을 만들 수 없는 보유분은 체결 평단 -3% 고정 손절선만으로
    # 관리한다. 기존 CHECK("initial_atr > 0")는 NULL에서 UNKNOWN이므로 그대로 둔다.
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Numeric(20, 8),
        nullable=True,
    )


def downgrade() -> None:
    # NULL 행은 "ATR 근거 없이 고정 손절선으로 보호 중인 실제 보유분"이다. 삭제하거나
    # 가짜 ATR로 채우면 손절선과 청산 근거가 조작되므로, 남아 있으면 거부한다.
    remaining = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NULL")
    )
    null_rows = int(remaining.scalar_one())
    if null_rows:
        raise RuntimeError(
            f"{_TABLE}.{_COLUMN} IS NULL 행이 {null_rows}건 남아 있어 downgrade를 "
            "거부합니다. 해당 보유 사이클을 정상 종료하거나 실제 ATR을 채운 뒤 "
            "다시 실행하세요."
        )
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Numeric(20, 8),
        nullable=False,
    )
