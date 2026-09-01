from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.models.base import Base

TOSS_MINUTE_SOURCE = "TOSS"
TOSS_MINUTE_VALUE_SEMANTICS = "CLOSE_X_VOLUME_SYNTHETIC"
TOSS_MINUTE_SESSION_SEGMENTS = ("NXT_PRE", "KRX_REGULAR", "NXT_POST")


class KRTossMinuteCandle(Base):
    """Toss combined KRX/NXT minute candle stored in the research schema.

    ``session_segment`` is a KST clock-time classification, not a venue claim.
    The production relation deliberately has a UNIQUE key instead of a primary
    key; ``__mapper_args__`` supplies that identity to SQLAlchemy without
    changing the database DDL contract.
    """

    __tablename__ = "kr_candles_1m_toss"
    __table_args__ = (
        UniqueConstraint(
            "time_utc",
            "symbol",
            name=conv("uq_research_kr_candles_1m_toss_time_symbol"),
        ),
        CheckConstraint(
            "session_segment IN ('NXT_PRE', 'KRX_REGULAR', 'NXT_POST')",
            name=conv("ck_research_kr_candles_1m_toss_session_segment"),
        ),
        CheckConstraint(
            "source = 'TOSS'",
            name=conv("ck_research_kr_candles_1m_toss_source"),
        ),
        CheckConstraint(
            "value_semantics = 'CLOSE_X_VOLUME_SYNTHETIC'",
            name=conv("ck_research_kr_candles_1m_toss_value_semantics"),
        ),
        Index(
            "ix_research_kr_candles_1m_toss_symbol_time_desc",
            "symbol",
            text("time_utc DESC"),
        ),
        Index(
            "ix_research_kr_candles_1m_toss_session_date",
            "session_date_kst",
            "symbol",
        ),
        Index("ix_research_kr_candles_1m_toss_batch", "batch_id"),
        {"schema": "research"},
    )

    time_utc: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    session_date_kst: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    session_segment: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default=TOSS_MINUTE_SOURCE
    )
    open: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    value_semantics: Mapped[str] = mapped_column(
        Text, nullable=False, default=TOSS_MINUTE_VALUE_SEMANTICS
    )
    is_padding: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pre_nxt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)

    __mapper_args__ = {"primary_key": (time_utc, symbol)}
