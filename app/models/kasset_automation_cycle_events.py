"""Append-only operator audit for one KAsset recommendation owner cycle.

The row stores bounded counters and secret-free review outcomes. It never stores
AI prompts, raw provider responses, credentials, or broker payloads. Audit
writes are deliberately isolated from recommendation persistence so an
observability failure cannot change a trading decision.

``cycle_trace_id`` is the join key between this row, the recommendations the
cycle produced, and the PAPER execution events those recommendations caused.
It is stored as a plain column with no foreign key so a missing or failed audit
row can never invalidate a recommendation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, Text, func, text
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
        CheckConstraint(
            "cycle_trace_id IS NULL OR length(btrim(cycle_trace_id)) > 0",
            name="kasset_automation_cycle_trace_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(recommendation_ids) = 'array'",
            name="kasset_automation_cycle_recommendation_ids_array",
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
        Index(
            "ix_kasset_automation_cycle_trace",
            "cycle_trace_id",
            unique=True,
            postgresql_where=text("cycle_trace_id IS NOT NULL"),
        ),
        {"schema": KASSET_AUTOMATION_CYCLE_EVENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 이 cycle을 추천·실행 원장과 잇는 추적 id. 열이 추가되기 전 행은 NULL이다.
    cycle_trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    #: 이 cycle이 만든 추천 id. 상한을 둔 목록이며 개수는
    #: ``recommendation_count``가 따로 보관한다.
    recommendation_ids: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
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
