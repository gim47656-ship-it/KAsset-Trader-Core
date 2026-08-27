"""Add persisted Android-facing AI recommendation reviews.

The table is review-only. It has no foreign keys, triggers, or imports into
broker, order, watch, ledger, reconcile, or scheduler surfaces.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_ai_recommendations"
down_revision: str | Sequence[str] | None = "20260824_s257_rung_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"


def upgrade() -> None:
    op.create_table(
        "ai_recommendations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "decision",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column(
            "rationale",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "risks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("reference_price", sa.Text(), nullable=True),
        sa.Column("suggested_quantity", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("length(btrim(id)) > 0", name="ck_ai_recommendations_id_nonempty"),
        sa.CheckConstraint(
            "action IN ('BUY', 'SELL', 'HOLD', 'WATCH')",
            name="ck_ai_recommendations_action",
        ),
        sa.CheckConstraint(
            "decision IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_ai_recommendations_decision",
        ),
        sa.CheckConstraint(
            "market IN ('KRX', 'US')",
            name="ck_ai_recommendations_market",
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="ck_ai_recommendations_symbol_nonempty",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency IN ('KRW', 'USD')",
            name="ck_ai_recommendations_currency",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rationale) = 'array'",
            name="ck_ai_recommendations_rationale_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(risks) = 'array'",
            name="ck_ai_recommendations_risks_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array'",
            name="ck_ai_recommendations_evidence_array",
        ),
        sa.CheckConstraint(
            f"confidence IS NULL OR confidence ~ '{_DECIMAL_PATTERN}'",
            name="ck_ai_recommendations_confidence_decimal_text",
        ),
        sa.CheckConstraint(
            f"reference_price IS NULL OR reference_price ~ '{_DECIMAL_PATTERN}'",
            name="ck_ai_recommendations_reference_price_decimal_text",
        ),
        sa.CheckConstraint(
            f"suggested_quantity IS NULL OR suggested_quantity ~ '{_DECIMAL_PATTERN}'",
            name="ck_ai_recommendations_suggested_quantity_decimal_text",
        ),
        sa.CheckConstraint(
            "(decision = 'PENDING' AND decided_at IS NULL) OR "
            "(decision IN ('APPROVED', 'REJECTED') AND decided_at IS NOT NULL)",
            name="ck_ai_recommendations_decision_timestamp",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_recommendations"),
        schema="review",
    )
    op.create_index(
        "ix_ai_recommendations_decision_created_at",
        "ai_recommendations",
        ["decision", sa.text("created_at DESC"), sa.text("id DESC")],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_recommendations_decision_created_at",
        table_name="ai_recommendations",
        schema="review",
    )
    op.drop_table("ai_recommendations", schema="review")
