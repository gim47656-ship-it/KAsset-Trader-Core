"""Allow depositary receipts (ADR) in symbol_master.

Revision ID: 20260926_symbol_master_adr
Revises: 20260926_symbol_search_aliases
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "20260926_symbol_master_adr"
down_revision = "20260926_symbol_search_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 원래 마이그레이션(20260828)과 같이 op.f()로 naming convention 접두사를 막는다.
_NAME = op.f("ck_symbol_master_security_type")


def upgrade() -> None:
    op.drop_constraint(_NAME, "symbol_master", type_="check")
    op.create_check_constraint(
        _NAME,
        "symbol_master",
        "security_type IN ('COMMON_STOCK', 'ETF', 'DEPOSITARY_RECEIPT')",
    )


def downgrade() -> None:
    # ADR 행이 남아 있으면 옛 CHECK를 다시 걸 수 없다. 되돌리기 전에 먼저 지운다.
    op.execute("DELETE FROM symbol_master WHERE security_type = 'DEPOSITARY_RECEIPT'")
    op.drop_constraint(_NAME, "symbol_master", type_="check")
    op.create_check_constraint(
        _NAME,
        "symbol_master",
        "security_type IN ('COMMON_STOCK', 'ETF')",
    )
