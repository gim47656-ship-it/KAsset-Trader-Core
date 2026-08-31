"""PAPER 계좌의 USD 초기 잔고를 영속화한다.

Revision ID: 20260831_paper_initial_usd
Revises: 20260831_kasset_cycle_audit
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260831_paper_initial_usd"
down_revision = "20260831_kasset_cycle_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_accounts",
        sa.Column(
            "initial_capital_usd",
            sa.Numeric(20, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema="paper",
    )


def downgrade() -> None:
    op.drop_column(
        "paper_accounts",
        "initial_capital_usd",
        schema="paper",
    )
