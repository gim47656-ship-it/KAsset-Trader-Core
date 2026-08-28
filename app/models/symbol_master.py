from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, CheckConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolMaster(Base):
    __tablename__ = "symbol_master"
    __table_args__ = (
        CheckConstraint("market IN ('KRX', 'US')", name="ck_symbol_master_market"),
        CheckConstraint(
            "security_type IN ('COMMON_STOCK', 'ETF')",
            name="ck_symbol_master_security_type",
        ),
        Index(
            "ix_symbol_master_market_active_symbol",
            "market",
            "is_active",
            "symbol",
        ),
    )

    market: Mapped[str] = mapped_column(String(3), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    security_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
