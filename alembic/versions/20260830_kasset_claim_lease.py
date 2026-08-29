"""KAsset PAPER 추천 claim에 lease/fencing과 체결 복구 키를 추가한다.

Revision ID: 20260830_kasset_claim_lease
Revises: 20260830_kasset_promotion_trust
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_kasset_claim_lease"
down_revision = "20260830_kasset_promotion_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECOMMENDATION_SCHEMA = "review"
_RECOMMENDATION_TABLE = "ai_recommendations"
_COHERENT_CONSTRAINT = "ck_ai_recommendations_paper_execution_coherent"
_OWNER_ORDER_FK = "fk_ai_recommendation_owner_paper_order"
_TRADE_CORRELATION_INDEX = "uq_paper_trades_account_correlation"

_NEW_COHERENT_CHECK = (
    "(paper_execution_status IS NULL "
    "AND paper_execution_token IS NULL "
    "AND paper_execution_claimed_at IS NULL "
    "AND paper_execution_lease_expires_at IS NULL "
    "AND paper_execution_attempt_count = 0 "
    "AND paper_execution_completed_at IS NULL "
    "AND paper_order_id IS NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'CLAIMED' "
    "AND length(btrim(paper_execution_token)) > 0 "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_lease_expires_at > paper_execution_claimed_at "
    "AND paper_execution_attempt_count > 0 "
    "AND paper_execution_completed_at IS NULL "
    "AND paper_order_id IS NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'SUCCEEDED' "
    "AND paper_execution_token IS NULL "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_lease_expires_at IS NULL "
    "AND paper_execution_attempt_count > 0 "
    "AND paper_execution_completed_at IS NOT NULL "
    "AND paper_order_id IS NOT NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'FAILED' "
    "AND paper_execution_token IS NULL "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_lease_expires_at IS NULL "
    "AND paper_execution_attempt_count > 0 "
    "AND paper_execution_completed_at IS NOT NULL "
    "AND paper_order_id IS NULL "
    "AND length(btrim(paper_execution_error)) > 0)"
)

_OLD_COHERENT_CHECK = (
    "(paper_execution_status IS NULL "
    "AND paper_execution_claimed_at IS NULL "
    "AND paper_execution_completed_at IS NULL "
    "AND paper_order_id IS NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'CLAIMED' "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_completed_at IS NULL "
    "AND paper_order_id IS NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'SUCCEEDED' "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_completed_at IS NOT NULL "
    "AND paper_order_id IS NOT NULL "
    "AND paper_execution_error IS NULL) OR "
    "(paper_execution_status = 'FAILED' "
    "AND paper_execution_claimed_at IS NOT NULL "
    "AND paper_execution_completed_at IS NOT NULL "
    "AND paper_order_id IS NULL "
    "AND length(btrim(paper_execution_error)) > 0)"
)


def upgrade() -> None:
    op.add_column(
        _RECOMMENDATION_TABLE,
        sa.Column("paper_execution_token", sa.Text(), nullable=True),
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.add_column(
        _RECOMMENDATION_TABLE,
        sa.Column(
            "paper_execution_lease_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.add_column(
        _RECOMMENDATION_TABLE,
        sa.Column(
            "paper_execution_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=_RECOMMENDATION_SCHEMA,
    )

    # 기존 CLAIMED는 즉시 재claim할 수 있는 만료 lease로 옮긴다. 이미 terminal인
    # row는 token/lease 없이 attempt 이력만 1로 보존한다.
    op.execute(
        sa.text(
            "UPDATE review.ai_recommendations "
            "SET paper_execution_token = CASE "
            "        WHEN paper_execution_status = 'CLAIMED' "
            "        THEN 'legacy:' || owner_user_id::text || ':' || id "
            "        ELSE NULL END, "
            "    paper_execution_lease_expires_at = CASE "
            "        WHEN paper_execution_status = 'CLAIMED' "
            "        THEN paper_execution_claimed_at + INTERVAL '1 microsecond' "
            "        ELSE NULL END, "
            "    paper_execution_attempt_count = CASE "
            "        WHEN paper_execution_status IS NULL THEN 0 ELSE 1 END"
        )
    )
    op.drop_constraint(
        _COHERENT_CONSTRAINT,
        _RECOMMENDATION_TABLE,
        type_="check",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.create_check_constraint(
        _COHERENT_CONSTRAINT,
        _RECOMMENDATION_TABLE,
        _NEW_COHERENT_CHECK,
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.create_foreign_key(
        _OWNER_ORDER_FK,
        _RECOMMENDATION_TABLE,
        "kasset_android_paper_orders",
        ["owner_user_id", "paper_order_id"],
        ["owner_user_id", "id"],
        source_schema=_RECOMMENDATION_SCHEMA,
    )
    op.create_index(
        _TRADE_CORRELATION_INDEX,
        "paper_trades",
        ["account_id", "correlation_id"],
        unique=True,
        schema="paper",
        postgresql_where=sa.text("correlation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        _TRADE_CORRELATION_INDEX,
        table_name="paper_trades",
        schema="paper",
    )
    op.drop_constraint(
        _OWNER_ORDER_FK,
        _RECOMMENDATION_TABLE,
        type_="foreignkey",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.drop_constraint(
        _COHERENT_CONSTRAINT,
        _RECOMMENDATION_TABLE,
        type_="check",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.drop_column(
        _RECOMMENDATION_TABLE,
        "paper_execution_attempt_count",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.drop_column(
        _RECOMMENDATION_TABLE,
        "paper_execution_lease_expires_at",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.drop_column(
        _RECOMMENDATION_TABLE,
        "paper_execution_token",
        schema=_RECOMMENDATION_SCHEMA,
    )
    op.create_check_constraint(
        _COHERENT_CONSTRAINT,
        _RECOMMENDATION_TABLE,
        _OLD_COHERENT_CHECK,
        schema=_RECOMMENDATION_SCHEMA,
    )
