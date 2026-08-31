"""Append-only operator audit for one KAsset recommendation owner cycle.

The row stores bounded counters and secret-free review outcomes. It never stores
AI prompts, raw provider responses, credentials, or broker payloads. Audit
writes are deliberately isolated from recommendation persistence so an
observability failure cannot change a trading decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

type KAssetAutomationCycleStatus = Literal["completed", "skipped", "failed"]

KASSET_AUTOMATION_CYCLE_EVENTS_SCHEMA: Final = "review"
KASSET_AUTOMATION_CYCLE_EVENTS_TABLE: Final = "kasset_automation_cycle_events"


class KAssetAutomationCycleEvent(Base):
    """One immutable owner result from the scheduled recommendation producer."""

    __tablename__ = KASSET_AUTOMATION_CYCLE_EVENTS_TABLE
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed', 'skipped', 'failed')",
            name="kasset_automation_cycle_status",
        ),
        CheckConstraint(
            "finished_at >= observed_at",
            name="kasset_automation_cycle_finished_after_observed",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND ranked_count >= 0 "
            "AND candidate_exclusion_count >= 0 "
            "AND strategy_evaluated_count >= 0 "
            "AND strategy_actionable_count >= 0 "
            "AND ai_reviewed_count >= 0 AND ai_failure_count >= 0 "
            "AND recommendation_count >= 0",
            name="kasset_automation_cycle_counts_nonnegative",
        ),
        Index(
            "ix_kasset_automation_cycle_observed_at",
            "observed_at",
            postgresql_ops={"observed_at": "DESC"},
        ),
        Index(
            "ix_kasset_automation_cycle_owner_observed_at",
            "owner_user_id",
            "observed_at",
            postgresql_ops={"observed_at": "DESC"},
        ),
        {"schema": KASSET_AUTOMATION_CYCLE_EVENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ranked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_exclusion_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    strategy_evaluated_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    strategy_actionable_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    ai_reviewed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    candidate_markets: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    candidate_sources: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    collection_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ranked_candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    candidate_exclusions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    ai_review_rejections: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    ai_review_outcomes: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "KASSET_AUTOMATION_CYCLE_EVENTS_SCHEMA",
    "KASSET_AUTOMATION_CYCLE_EVENTS_TABLE",
    "KAssetAutomationCycleEvent",
    "KAssetAutomationCycleStatus",
]
