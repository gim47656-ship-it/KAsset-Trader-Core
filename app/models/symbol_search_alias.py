from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolSearchAlias(Base):
    __tablename__ = "symbol_search_aliases"
    __table_args__ = (
        CheckConstraint(
            "market IN ('KRX', 'US')", name="ck_symbol_search_aliases_market"
        ),
        UniqueConstraint(
            "market", "symbol", "alias", name="uq_symbol_search_aliases_key"
        ),
        Index("ix_symbol_search_aliases_alias", "alias"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market: Mapped[str] = mapped_column(String(3), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    alias: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
