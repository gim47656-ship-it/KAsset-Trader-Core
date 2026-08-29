"""Persisted, review-only AI recommendations for the Android API."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


class RecommendationAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"


class RecommendationDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RecommendationExecutionStatus(StrEnum):
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RecommendationMarket(StrEnum):
    KRX = "KRX"
    US = "US"


type RecommendationStatusGroup = Literal["PENDING", "RESOLVED"]
type TerminalRecommendationDecision = Literal["APPROVED", "REJECTED"]

_DECIMAL_TEXT_CHECK = "{column} ~ '^-?[0-9]+(\\.[0-9]+)?$'"


class AIRecommendation(Base):
    """Immutable recommendation facts plus one terminal review decision."""

    __tablename__ = "ai_recommendations"
    __table_args__ = (
        CheckConstraint("length(btrim(id)) > 0", name="id_nonempty"),
        CheckConstraint(
            "action IN ('BUY', 'SELL', 'HOLD', 'WATCH')",
            name="action",
        ),
        CheckConstraint(
            "decision IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="decision",
        ),
        CheckConstraint("market IN ('KRX', 'US')", name="market"),
        CheckConstraint("length(btrim(symbol)) > 0", name="symbol_nonempty"),
        CheckConstraint(
            "currency IS NULL OR currency IN ('KRW', 'USD')",
            name="currency",
        ),
        CheckConstraint("jsonb_typeof(rationale) = 'array'", name="rationale_array"),
        CheckConstraint("jsonb_typeof(risks) = 'array'", name="risks_array"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_array"),
        CheckConstraint(
            "confidence IS NULL OR " + _DECIMAL_TEXT_CHECK.format(column="confidence"),
            name="confidence_decimal_text",
        ),
        CheckConstraint(
            "reference_price IS NULL OR "
            + _DECIMAL_TEXT_CHECK.format(column="reference_price"),
            name="reference_price_decimal_text",
        ),
        CheckConstraint(
            "suggested_quantity IS NULL OR "
            + _DECIMAL_TEXT_CHECK.format(column="suggested_quantity"),
            name="suggested_quantity_decimal_text",
        ),
        CheckConstraint(
            "(decision = 'PENDING' AND decided_at IS NULL) OR "
            "(decision IN ('APPROVED', 'REJECTED') AND decided_at IS NOT NULL)",
            name="decision_timestamp",
        ),
        CheckConstraint(
            "paper_execution_status IS NULL OR "
            "paper_execution_status IN ('CLAIMED', 'SUCCEEDED', 'FAILED')",
            name="paper_execution_status",
        ),
        CheckConstraint(
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
            "AND length(btrim(paper_execution_error)) > 0)",
            name="paper_execution_coherent",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "paper_order_id"],
            [
                "kasset_android_paper_orders.owner_user_id",
                "kasset_android_paper_orders.id",
            ],
            name="fk_ai_recommendation_owner_paper_order",
        ),
        UniqueConstraint(
            "owner_user_id",
            "id",
            name="uq_ai_recommendations_owner_id",
        ),
        Index(
            "ix_ai_recommendations_owner_decision_created_at",
            "owner_user_id",
            "decision",
            "created_at",
            "id",
            postgresql_ops={"created_at": "DESC"},
        ),
        {"schema": "review"},
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        default=lambda: f"rec-{uuid.uuid4()}",
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=RecommendationDecision.PENDING.value,
        server_default=text("'PENDING'"),
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    risks: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    evidence: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_price: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_quantity: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    paper_execution_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    paper_execution_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    paper_execution_claimed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    paper_execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    paper_execution_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    paper_execution_completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    paper_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    paper_execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = [
    "AIRecommendation",
    "RecommendationAction",
    "RecommendationDecision",
    "RecommendationExecutionStatus",
    "RecommendationMarket",
    "RecommendationStatusGroup",
    "TerminalRecommendationDecision",
]
