"""KAsset 관심종목 ±5% 알림의 하루 단위 포착 기록을 추가한다.

Revision ID: 20260903_kasset_alert_events
Revises: 20260903_kasset_rvol_shadow
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260903_kasset_alert_events"
down_revision = "20260903_kasset_rvol_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kasset_routine_price_alert_events"
_OWNER_DATE_INDEX = "ix_kasset_routine_price_alert_event_owner_date"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("routine_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("detected_rate_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("last_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('RAPID_RISE', 'RAPID_FALL')",
            name="ck_kasset_routine_price_alert_event_kind",
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US', 'CRYPTO')",
            name="ck_kasset_routine_price_alert_event_market",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_kasset_routine_price_alert_events_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_routine_price_alert_events"),
        sa.UniqueConstraint(
            "owner_user_id",
            "routine_date",
            "kind",
            "market",
            "symbol",
            name="uq_kasset_routine_price_alert_event_day_key",
        ),
    )
    op.create_index(_OWNER_DATE_INDEX, _TABLE, ["owner_user_id", "routine_date"])


def downgrade() -> None:
    op.drop_index(_OWNER_DATE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
