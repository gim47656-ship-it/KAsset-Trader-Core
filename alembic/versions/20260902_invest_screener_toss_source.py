"""invest_screener_snapshots에 Toss 운영 source를 추가한다.

기존 KIS/Yahoo 행은 그대로 보존하고 ``toss``만 허용 집합에 추가한다.
downgrade 시 Toss 행이 있으면 데이터 삭제나 재분류 없이 중단한다.

Revision ID: 20260902_screener_toss_source
Revises: 20260902_toss_report_scopes
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260902_screener_toss_source"
down_revision = "20260902_toss_report_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "invest_screener_snapshots"
_CHECK_NAME = "ck_invest_screener_snapshots_source"
# The table-creation migration supplied a convention-prefixed logical name, so
# PostgreSQL stored this convention-expanded and truncated identifier.
_GENERATED_CHECK_NAME = "ck_invest_screener_snapshots_ck_invest_screener_snapsho_3d05"
_CHECK_NAMES = (_CHECK_NAME, _GENERATED_CHECK_NAME)
_OLD_PREDICATE = "source IN ('kis', 'yahoo')"
_NEW_PREDICATE = "source IN ('kis', 'yahoo', 'toss')"


def _drop_source_check_if_exists() -> None:
    for check_name in _CHECK_NAMES:
        op.execute(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT IF EXISTS "{check_name}"')


def upgrade() -> None:
    _drop_source_check_if_exists()
    op.create_check_constraint(
        op.f(_CHECK_NAME),
        _TABLE,
        _NEW_PREDICATE,
    )


def downgrade() -> None:
    bind = op.get_bind()
    toss_row = bind.execute(
        sa.text(f'SELECT 1 FROM "{_TABLE}" WHERE source = :source LIMIT 1'),
        {"source": "toss"},
    ).scalar()
    if toss_row is not None:
        raise RuntimeError(
            "downgrade blocked: toss rows exist in invest_screener_snapshots; "
            "no data rewrite or deletion is permitted"
        )

    _drop_source_check_if_exists()
    op.create_check_constraint(
        op.f(_CHECK_NAME),
        _TABLE,
        _OLD_PREDICATE,
    )
