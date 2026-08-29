"""Immutable forward/PAPER research cohort evidence."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.models.base import Base


class KAssetResearchCohort(Base):
    __tablename__ = "kasset_research_cohorts"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(cohort_id)) > 0",
            name=conv("ck_kasset_research_cohort_id_nonblank"),
        ),
        CheckConstraint(
            "market IN ('kr', 'us')", name=conv("ck_kasset_research_cohort_market")
        ),
        CheckConstraint(
            "selection_method = 'latest_market_cap'",
            name=conv("ck_kasset_research_cohort_method"),
        ),
        CheckConstraint(
            "length(btrim(valuation_snapshot_source)) > 0",
            name=conv("ck_kasset_research_cohort_source_nonblank"),
        ),
        CheckConstraint(
            "valuation_snapshot_source IN "
            "('naver_finance', 'yahoo', 'toss_openapi', 'tvscreener')",
            name=conv("ck_kasset_research_cohort_source"),
        ),
        CheckConstraint(
            "requested_size > 0 AND active_member_count = requested_size",
            name=conv("ck_kasset_research_cohort_size"),
        ),
        CheckConstraint(
            "selection_date >= valuation_snapshot_date "
            "AND effective_date >= valuation_snapshot_date",
            name=conv("ck_kasset_research_cohort_date_order"),
        ),
        CheckConstraint(
            "evidence_scope = 'forward_paper'",
            name=conv("ck_kasset_research_cohort_scope"),
        ),
        Index(
            "ix_kasset_research_cohort_market_date",
            "market",
            "selection_date",
        ),
    )

    cohort_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    selection_as_of: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    selection_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    selection_method: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_size: Mapped[int] = mapped_column(Integer, nullable=False)
    active_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    valuation_snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    valuation_snapshot_source: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class KAssetResearchCohortMember(Base):
    __tablename__ = "kasset_research_cohort_members"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(symbol)) > 0",
            name=conv("ck_kasset_research_member_symbol_nonblank"),
        ),
        CheckConstraint(
            "rank > 0", name=conv("ck_kasset_research_member_rank_positive")
        ),
        CheckConstraint(
            "member_kind IN ('active', 'forced', 'benchmark')",
            name=conv("ck_kasset_research_member_kind"),
        ),
        CheckConstraint(
            "(member_kind = 'active' AND market_cap > 0) OR "
            "(member_kind IN ('forced', 'benchmark') "
            "AND (market_cap IS NULL OR market_cap > 0))",
            name=conv("ck_kasset_research_member_market_cap"),
        ),
        UniqueConstraint(
            "cohort_id",
            "member_kind",
            "rank",
            name="uq_kasset_research_member_kind_rank",
        ),
        UniqueConstraint(
            "cohort_id",
            "symbol",
            name="uq_kasset_research_member_symbol",
        ),
        Index("ix_kasset_research_member_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cohort_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "kasset_research_cohorts.cohort_id",
            name="fk_kasset_research_member_cohort",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    member_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    eligibility_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
