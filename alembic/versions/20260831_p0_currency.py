"""PAPER 일간 스냅샷에 통화별 자산을 기록하고 혼합 통화 컬럼을 은퇴시킨다.

기존 positions_value/total_equity/daily_return_pct는 KRW와 USD를 그대로 더한
값이라 성과 지표로 쓸 수 없다. 과거 데이터는 감사용으로 남기고 nullable로
바꾼 뒤, 새 코드는 통화별 컬럼만 쓴다. downgrade는 통화별 컬럼에서 예전 의미
(원시 KRW+USD 합)를 그대로 복원하고 NOT NULL을 되돌린다.

Revision ID: 20260831_p0_currency
Revises: 20260831_p0_trace
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260831_p0_currency"
down_revision = "20260831_p0_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "paper"
_TABLE = "paper_daily_snapshots"

# 통화별 자산 컬럼. 포지션 평가액은 equity_<cur> - cash_<cur>로 정확히 유도되므로
# 별도 컬럼을 두지 않는다. Column 인스턴스는 add_column이 임시 Table에 귀속시키니
# 재사용하지 않고 호출 시점에 새로 만든다.
CURRENCY_COLUMN_SPECS: tuple[tuple[str, str], ...] = (
    ("equity_krw", "money"),
    ("equity_usd", "money"),
    ("daily_return_krw_pct", "pct"),
    ("daily_return_usd_pct", "pct"),
    ("valuation_complete_krw", "flag"),
    ("valuation_complete_usd", "flag"),
)

# nullable로 바뀌는 혼합 통화 컬럼. 데이터는 삭제하지 않는다.
LEGACY_MIXED_COLUMNS: tuple[str, ...] = ("positions_value", "total_equity")

_COLUMN_TYPES: dict[str, sa.types.TypeEngine] = {
    "money": sa.Numeric(20, 4),
    "pct": sa.Numeric(10, 4),
    "flag": sa.Boolean(),
}


def upgrade() -> None:
    for name, kind in CURRENCY_COLUMN_SPECS:
        op.add_column(
            _TABLE,
            sa.Column(name, _COLUMN_TYPES[kind], nullable=True),
            schema=_SCHEMA,
        )

    # 새 스냅샷은 혼합 통화 값을 더 이상 쓰지 않는다. 기존 행의 값은 그대로
    # 보존하고, 앞으로 기록되는 행에서 비워 둘 수 있게 nullable로만 바꾼다.
    for column_name in LEGACY_MIXED_COLUMNS:
        op.alter_column(
            _TABLE,
            column_name,
            existing_type=sa.Numeric(20, 4),
            nullable=True,
            schema=_SCHEMA,
        )


def downgrade() -> None:
    # NOT NULL을 되돌리기 전에 P0 이후 행의 혼합 통화 컬럼을 복원한다.
    # cash_krw/cash_usd는 이 마이그레이션이 건드리지 않으므로
    # positions_value = (equity_krw - cash_krw) + (equity_usd - cash_usd)는
    # 예전 컬럼의 의미(원시 KRW+USD 합)와 정확히 일치한다.
    op.execute(
        f"""
        UPDATE {_SCHEMA}.{_TABLE}
        SET total_equity = COALESCE(total_equity, equity_krw + equity_usd),
            positions_value = COALESCE(
                positions_value,
                (equity_krw - cash_krw) + (equity_usd - cash_usd)
            )
        WHERE total_equity IS NULL OR positions_value IS NULL
        """
    )
    # 자산 근거가 전혀 없는 행(legacy 값도, 통화별 값도 없음)까지 NOT NULL을
    # 복원할 수 있어야 한다. 행을 버리지 않고 0으로 채운다.
    op.execute(
        f"""
        UPDATE {_SCHEMA}.{_TABLE}
        SET total_equity = COALESCE(total_equity, 0),
            positions_value = COALESCE(positions_value, 0)
        WHERE total_equity IS NULL OR positions_value IS NULL
        """
    )
    for column_name in LEGACY_MIXED_COLUMNS:
        op.alter_column(
            _TABLE,
            column_name,
            existing_type=sa.Numeric(20, 4),
            nullable=False,
            schema=_SCHEMA,
        )

    for name, _kind in reversed(CURRENCY_COLUMN_SPECS):
        op.drop_column(_TABLE, name, schema=_SCHEMA)
