"""KAsset PAPER position state를 실제 보유 cycle에 결합한다.

Revision ID: 20260830_kasset_position_cycles
Revises: 20260829_kasset_promotion
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_kasset_position_cycles"
down_revision = "20260829_kasset_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "kasset_paper_position_states"
_ACTIVE_INDEX = "uq_kasset_position_state_owner_active_holding"


def upgrade() -> None:
    op.add_column(
        _TABLE_NAME,
        sa.Column("position_cycle_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("paper_position_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("entry_order_id", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("strategy_key", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("strategy_fingerprint", sa.Text(), nullable=True),
    )

    op.execute(
        sa.text(
            """
            UPDATE kasset_paper_position_states AS state
            SET position_cycle_id = position.id,
                paper_position_id = position.id,
                opened_at = state.entry_at,
                strategy_key = 'qullamaggie_breakout_portfolio'
            FROM paper.paper_positions AS position
            WHERE position.account_id = state.paper_account_id
              AND position.symbol = state.symbol
              AND (
                    (state.market = 'KRX'
                     AND position.instrument_type::text = 'equity_kr')
                 OR (state.market = 'US'
                     AND position.instrument_type::text = 'equity_us')
              )
            """
        )
    )
    # 기존 position에 안전하게 결합할 수 없는 state는 재진입 시 재사용하지 않는다.
    op.execute(
        sa.text(
            "DELETE FROM kasset_paper_position_states WHERE position_cycle_id IS NULL"
        )
    )

    op.drop_constraint(
        "pk_kasset_paper_position_states",
        _TABLE_NAME,
        type_="primary",
    )
    op.drop_column(_TABLE_NAME, "entry_at")
    op.alter_column(
        _TABLE_NAME,
        "position_cycle_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        _TABLE_NAME,
        "opened_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
    )
    op.alter_column(
        _TABLE_NAME,
        "strategy_version",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.create_primary_key(
        "pk_kasset_paper_position_states",
        _TABLE_NAME,
        ["position_cycle_id"],
    )
    op.create_foreign_key(
        "fk_kasset_position_state_paper_position",
        _TABLE_NAME,
        "paper_positions",
        ["paper_position_id"],
        ["id"],
        referent_schema="paper",
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_kasset_position_state_entry_order",
        _TABLE_NAME,
        "kasset_android_paper_orders",
        ["entry_order_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_kasset_position_state_active_position",
        _TABLE_NAME,
        ["paper_position_id"],
    )
    op.create_index(
        _ACTIVE_INDEX,
        _TABLE_NAME,
        ["owner_user_id", "paper_account_id", "market", "symbol"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_check_constraint(
        "ck_kasset_position_state_cycle_positive",
        _TABLE_NAME,
        "position_cycle_id > 0",
    )
    op.create_check_constraint(
        "ck_kasset_position_state_lifecycle",
        _TABLE_NAME,
        "(paper_position_id IS NOT NULL AND closed_at IS NULL) "
        "OR (paper_position_id IS NULL AND closed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_kasset_position_state_timestamp_order",
        _TABLE_NAME,
        "closed_at IS NULL OR closed_at >= opened_at",
    )


def downgrade() -> None:
    # 종료되어 current position이 없는 cycle은 predecessor schema로 표현할 수 없다.
    op.execute(
        sa.text(
            "DELETE FROM kasset_paper_position_states "
            "WHERE paper_position_id IS NULL OR strategy_version IS NULL"
        )
    )
    op.add_column(
        _TABLE_NAME,
        sa.Column("entry_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(sa.text("UPDATE kasset_paper_position_states SET entry_at = opened_at"))
    op.drop_constraint(
        "ck_kasset_position_state_timestamp_order",
        _TABLE_NAME,
        type_="check",
    )
    op.drop_constraint(
        "ck_kasset_position_state_lifecycle",
        _TABLE_NAME,
        type_="check",
    )
    op.drop_constraint(
        "ck_kasset_position_state_cycle_positive",
        _TABLE_NAME,
        type_="check",
    )
    op.drop_index(_ACTIVE_INDEX, table_name=_TABLE_NAME)
    op.drop_constraint(
        "uq_kasset_position_state_active_position",
        _TABLE_NAME,
        type_="unique",
    )
    op.drop_constraint(
        "fk_kasset_position_state_entry_order",
        _TABLE_NAME,
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_kasset_position_state_paper_position",
        _TABLE_NAME,
        type_="foreignkey",
    )
    op.drop_constraint(
        "pk_kasset_paper_position_states",
        _TABLE_NAME,
        type_="primary",
    )
    op.alter_column(
        _TABLE_NAME,
        "strategy_version",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_primary_key(
        "pk_kasset_paper_position_states",
        _TABLE_NAME,
        ["owner_user_id", "paper_account_id", "symbol"],
    )
    op.alter_column(
        _TABLE_NAME,
        "entry_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
    )
    op.drop_column(_TABLE_NAME, "strategy_fingerprint")
    op.drop_column(_TABLE_NAME, "strategy_key")
    op.drop_column(_TABLE_NAME, "closed_at")
    op.drop_column(_TABLE_NAME, "opened_at")
    op.drop_column(_TABLE_NAME, "entry_order_id")
    op.drop_column(_TABLE_NAME, "paper_position_id")
    op.drop_column(_TABLE_NAME, "position_cycle_id")
