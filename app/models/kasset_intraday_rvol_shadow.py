from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Index,
    Integer,
    Numeric,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.models.base import Base

KASSET_INTRADAY_RVOL_SHADOW_SCHEMA: Final = "review"
KASSET_INTRADAY_RVOL_SHADOW_TABLE: Final = "kasset_intraday_rvol_shadow"


class KAssetIntradayRvolShadow(Base):
    """기존 RVOL과 동시간대 RVOL을 함께 남기는 shadow 관측 행."""

    __tablename__ = KASSET_INTRADAY_RVOL_SHADOW_TABLE
    __table_args__ = (
        Index(
            conv("ix_review_kasset_intraday_rvol_shadow_observed_at"),
            text("observed_at DESC"),
        ),
        Index(
            conv("ix_review_kasset_intraday_rvol_shadow_symbol_observed_at"),
            "symbol",
            text("observed_at DESC"),
        ),
        Index(
            conv("ix_review_kasset_intraday_rvol_shadow_cycle_trace_symbol"),
            "cycle_trace_id",
            "symbol",
            unique=True,
            postgresql_where=text("cycle_trace_id IS NOT NULL"),
        ),
        {"schema": KASSET_INTRADAY_RVOL_SHADOW_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    cycle_trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    bucket_start_kst: Mapped[time] = mapped_column(Time, nullable=False)
    completed_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    session_decision_status: Mapped[str] = mapped_column(Text, nullable=False)
    session_decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    same_time_baseline_median_5m: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True
    )
    same_time_baseline_median_20m: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True
    )
    session_rvol_5m: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    session_status_5m: Mapped[str] = mapped_column(Text, nullable=False)
    session_rvol_20m: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    session_status_20m: Mapped[str] = mapped_column(Text, nullable=False)
    same_time_rvol_5m: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    same_time_status_5m: Mapped[str] = mapped_column(Text, nullable=False)
    same_time_sample_days_5m: Mapped[int] = mapped_column(Integer, nullable=False)
    same_time_rvol_20m: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    same_time_status_20m: Mapped[str] = mapped_column(Text, nullable=False)
    same_time_sample_days_20m: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "KASSET_INTRADAY_RVOL_SHADOW_SCHEMA",
    "KASSET_INTRADAY_RVOL_SHADOW_TABLE",
    "KAssetIntradayRvolShadow",
]
