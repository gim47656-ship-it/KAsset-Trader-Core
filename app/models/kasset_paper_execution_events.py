"""Append-only operator audit for one PAPER execution attempt.

The row records who executed, which recommendation was executed, which
recommendation cycle produced it, and how the attempt ended. It never stores
broker payloads, credentials, AI prompts, or provider responses. There is no
foreign key to ``review.ai_recommendations`` or to the paper order tables on
purpose: an audit failure must never be able to change or block an execution,
and the recommendation row stays the single owner of execution state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

type KAssetPaperExecutionOrigin = Literal["AUTO_PAPER", "APPROVAL"]
type KAssetPaperExecutionStatus = Literal[
    "IDLE",
    "BLOCKED",
    "REJECTED",
    "SUBMITTED",
    "FAILED",
]

KASSET_PAPER_EXECUTION_EVENTS_SCHEMA: Final = "review"
KASSET_PAPER_EXECUTION_EVENTS_TABLE: Final = "kasset_paper_execution_events"

#: 무인 sweep과 사람이 누른 승인 실행을 구분하는 유일한 값.
KASSET_PAPER_EXECUTION_ORIGINS: Final[tuple[str, ...]] = ("AUTO_PAPER", "APPROVAL")
KASSET_PAPER_EXECUTION_STATUSES: Final[tuple[str, ...]] = (
    "IDLE",
    "BLOCKED",
    "REJECTED",
    "SUBMITTED",
    "FAILED",
)

_ORIGIN_LIST = ", ".join(f"'{value}'" for value in KASSET_PAPER_EXECUTION_ORIGINS)
_STATUS_LIST = ", ".join(f"'{value}'" for value in KASSET_PAPER_EXECUTION_STATUSES)


class KAssetPaperExecutionEvent(Base):
    """One immutable PAPER execution attempt result for one owner."""

    __tablename__ = KASSET_PAPER_EXECUTION_EVENTS_TABLE
    __table_args__ = (
        CheckConstraint(
            f"origin IN ({_ORIGIN_LIST})",
            name="kasset_paper_execution_origin",
        ),
        CheckConstraint(
            f"status IN ({_STATUS_LIST})",
            name="kasset_paper_execution_status",
        ),
        CheckConstraint(
            "length(btrim(recommendation_id)) > 0",
            name="kasset_paper_execution_recommendation_nonempty",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0",
            name="kasset_paper_execution_reason_nonempty",
        ),
        CheckConstraint(
            "cycle_trace_id IS NULL OR length(btrim(cycle_trace_id)) > 0",
            name="kasset_paper_execution_cycle_trace_nonempty",
        ),
        CheckConstraint(
            "paper_order_id IS NULL OR length(btrim(paper_order_id)) > 0",
            name="kasset_paper_execution_order_nonempty",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="kasset_paper_execution_attempt_nonnegative",
        ),
        CheckConstraint(
            "finished_at >= started_at",
            name="kasset_paper_execution_finished_after_started",
        ),
        Index(
            "ix_kasset_paper_execution_owner_observed_at",
            "owner_user_id",
            "observed_at",
            postgresql_ops={"observed_at": "DESC"},
        ),
        Index(
            "ix_kasset_paper_execution_owner_recommendation",
            "owner_user_id",
            "recommendation_id",
            "observed_at",
            postgresql_ops={"observed_at": "DESC"},
        ),
        Index(
            "ix_kasset_paper_execution_cycle_trace",
            "cycle_trace_id",
            postgresql_where=text("cycle_trace_id IS NOT NULL"),
        ),
        {"schema": KASSET_PAPER_EXECUTION_EVENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recommendation_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 이 추천을 만든 cycle의 추적 id. 열 추가 이전 추천이나 cycle 밖에서 만들어진
    #: 추천(포지션 청산 등)은 NULL로 남는다.
    cycle_trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    replayed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    paper_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    promotion_bypass_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "KASSET_PAPER_EXECUTION_EVENTS_SCHEMA",
    "KASSET_PAPER_EXECUTION_EVENTS_TABLE",
    "KASSET_PAPER_EXECUTION_ORIGINS",
    "KASSET_PAPER_EXECUTION_STATUSES",
    "KAssetPaperExecutionEvent",
    "KAssetPaperExecutionOrigin",
    "KAssetPaperExecutionStatus",
]
