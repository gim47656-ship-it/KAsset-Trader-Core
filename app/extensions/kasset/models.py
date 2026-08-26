"""Persistence owned by the KAsset Android compatibility facade."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AndroidPaperOrder(Base):
    __tablename__ = "kasset_android_paper_orders"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    paper_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    broker_order_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default="0"
    )
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    paper_trade_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_trades.id", ondelete="SET NULL"),
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AndroidRuntimeState(Base):
    __tablename__ = "kasset_android_runtime_state"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    trading_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="PAPER", server_default="PAPER"
    )
    max_order_ratio: Mapped[Decimal] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        default=Decimal("0.1000"),
        server_default="0.1000",
    )
    max_symbol_ratio: Mapped[Decimal] = mapped_column(
        Numeric(8, 4),
        nullable=False,
        default=Decimal("0.2500"),
        server_default="0.2500",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BrokerCredential(Base):
    __tablename__ = "kasset_broker_credentials"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    encrypted_app_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_app_secret: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_account_no: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
