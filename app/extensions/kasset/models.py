"""User-owned persistence for the KAsset Android product surface."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Date,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AndroidPaperAccount(Base):
    __tablename__ = "kasset_android_paper_accounts"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_kasset_android_paper_account_owner"),
        UniqueConstraint(
            "paper_account_id", name="uq_kasset_android_paper_account_link"
        ),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    paper_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class AndroidPaperOrder(Base):
    __tablename__ = "kasset_android_paper_orders"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "id",
            name="uq_kasset_android_paper_order_owner_id",
        ),
        UniqueConstraint(
            "owner_user_id",
            "client_order_id",
            name="uq_kasset_android_paper_order_owner_client",
        ),
        UniqueConstraint(
            "owner_user_id",
            "broker_order_id",
            name="uq_kasset_android_paper_order_owner_broker",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "paper_account_id"],
            [
                "kasset_android_paper_accounts.owner_user_id",
                "kasset_android_paper_accounts.paper_account_id",
            ],
            name="fk_kasset_android_order_owner_paper_account",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_kasset_android_paper_order_owner_created",
            "owner_user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    paper_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    broker_order_id: Mapped[str] = mapped_column(Text, nullable=False)
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

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
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


class GlobalRuntimeState(Base):
    __tablename__ = "kasset_global_runtime_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KAssetDeviceSession(Base):
    __tablename__ = "kasset_device_sessions"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "device_id",
            name="uq_kasset_device_session_owner_device",
        ),
        UniqueConstraint(
            "refresh_token_hash",
            name="uq_kasset_device_session_refresh_hash",
        ),
        Index(
            "ix_kasset_device_session_owner_active",
            "owner_user_id",
            "revoked_at",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BrokerCredential(Base):
    __tablename__ = "kasset_broker_credentials"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "id",
            name="uq_kasset_broker_credential_owner_id",
        ),
        UniqueConstraint(
            "owner_user_id",
            "provider",
            name="uq_kasset_broker_credential_owner_provider",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
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


class KAssetDailyRoutineSetting(Base):
    """One owner-scoped routine selection for one KST calendar date."""

    __tablename__ = "kasset_ai_daily_routine_settings"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(enabled_routines) = 'array'",
            name="enabled_routines_array",
        ),
        CheckConstraint(
            "jsonb_array_length(enabled_routines) <= 4",
            name="enabled_routines_bounded",
        ),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    routine_date: Mapped[date] = mapped_column(Date, primary_key=True)
    enabled_routines: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
