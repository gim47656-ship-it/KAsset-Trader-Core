"""KAsset 등락 FCM push 토큰 귀속과 발송 원장을 추가한다.

기기 세션에 FCM registration token을 nullable로 붙이고, 같은 token이 두
소유자에게 동시에 붙지 못하도록 fingerprint 부분 unique index를 건다. 원문
token은 index key로 쓰지 않는다. 발송 원장은 KST 일자 기준 dedupe로 같은
종목·방향을 하루 한 번만 밀어낸다.

전부 additive이며 기존 컬럼/제약을 바꾸지 않는다.

Revision ID: 20260901_kasset_fcm_push
Revises: 20260831_p0_currency
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260901_kasset_fcm_push"
down_revision = "20260831_p0_currency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSION_TABLE = "kasset_device_sessions"
_DELIVERY_TABLE = "kasset_push_deliveries"
_TOKEN_HASH_INDEX = "uq_kasset_device_session_fcm_token_hash"
_TOKEN_PAIRED_CHECK = "ck_kasset_device_session_fcm_token_paired"


def upgrade() -> None:
    op.add_column(_SESSION_TABLE, sa.Column("fcm_token", sa.Text(), nullable=True))
    op.add_column(_SESSION_TABLE, sa.Column("fcm_token_hash", sa.Text(), nullable=True))
    op.add_column(
        _SESSION_TABLE,
        sa.Column("fcm_token_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        _TOKEN_HASH_INDEX,
        _SESSION_TABLE,
        ["fcm_token_hash"],
        unique=True,
        postgresql_where=sa.text("fcm_token_hash IS NOT NULL"),
    )
    op.create_check_constraint(
        _TOKEN_PAIRED_CHECK,
        _SESSION_TABLE,
        "(fcm_token IS NULL) = (fcm_token_hash IS NULL)",
    )

    op.create_table(
        _DELIVERY_TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("device_session_id", sa.Text(), nullable=False),
        sa.Column("routine_date", sa.Date(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default="pending", nullable=False
        ),
        sa.Column("attempt_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'retry', 'sent', 'failed')",
            name="ck_kasset_push_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["device_session_id"],
            [f"{_SESSION_TABLE}.id"],
            name="fk_kasset_push_deliveries_device_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_push_deliveries"),
        sa.UniqueConstraint(
            "device_session_id",
            "dedupe_key",
            name="uq_kasset_push_delivery_session_dedupe",
        ),
    )
    op.create_index(
        "ix_kasset_push_delivery_due",
        _DELIVERY_TABLE,
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kasset_push_delivery_due", table_name=_DELIVERY_TABLE)
    op.drop_table(_DELIVERY_TABLE)
    op.drop_constraint(_TOKEN_PAIRED_CHECK, _SESSION_TABLE, type_="check")
    op.drop_index(_TOKEN_HASH_INDEX, table_name=_SESSION_TABLE)
    op.drop_column(_SESSION_TABLE, "fcm_token_updated_at")
    op.drop_column(_SESSION_TABLE, "fcm_token_hash")
    op.drop_column(_SESSION_TABLE, "fcm_token")
