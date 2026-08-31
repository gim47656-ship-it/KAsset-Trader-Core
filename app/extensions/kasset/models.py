"""User-owned persistence for the KAsset Android product surface."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
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
    # 소유자가 명시적으로 켠 "승격 근거 없이 PAPER 자동실행 허용". 기본은 차단이고
    # 켜도 kill switch·PAPER 판정은 그대로 남는다.
    promotion_bypass_enabled: Mapped[bool] = mapped_column(
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
            name="ck_kasset_ai_daily_routine_settings_enabled_routines_array",
        ),
        CheckConstraint(
            "jsonb_array_length(enabled_routines) <= 4",
            name="ck_kasset_ai_daily_routine_settings_enabled_routines_bounded",
        ),
        CheckConstraint(
            "recommendation_market_scope IN ('KR_ONLY', 'US_ONLY', 'KR_US')",
            name=(
                "ck_kasset_ai_daily_routine_settings_recommendation_market_scope_valid"
            ),
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
    recommendation_market_scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="KR_US",
        server_default="KR_US",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KAssetPaperPositionState(Base):
    """Deterministic lifecycle state for one concrete PAPER position cycle."""

    __tablename__ = "kasset_paper_position_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "paper_account_id"],
            [
                "kasset_android_paper_accounts.owner_user_id",
                "kasset_android_paper_accounts.paper_account_id",
            ],
            name="fk_kasset_position_state_owner_paper_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "market IN ('KRX', 'US')",
            name="ck_kasset_position_state_market_valid",
        ),
        CheckConstraint(
            "entry_price > 0",
            name="ck_kasset_position_state_entry_price_positive",
        ),
        CheckConstraint(
            "initial_atr > 0",
            name="ck_kasset_position_state_initial_atr_positive",
        ),
        CheckConstraint(
            "initial_stop > 0",
            name="ck_kasset_position_state_initial_stop_positive",
        ),
        CheckConstraint(
            "current_stop > 0",
            name="ck_kasset_position_state_current_stop_positive",
        ),
        CheckConstraint(
            "highest_close > 0",
            name="ck_kasset_position_state_highest_close_positive",
        ),
        CheckConstraint(
            "position_cycle_id > 0",
            name="ck_kasset_position_state_cycle_positive",
        ),
        CheckConstraint(
            "(paper_position_id IS NOT NULL AND closed_at IS NULL) "
            "OR (paper_position_id IS NULL AND closed_at IS NOT NULL)",
            name="ck_kasset_position_state_lifecycle",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= opened_at",
            name="ck_kasset_position_state_timestamp_order",
        ),
        UniqueConstraint(
            "paper_position_id",
            name="uq_kasset_position_state_active_position",
        ),
        Index(
            "uq_kasset_position_state_owner_active_holding",
            "owner_user_id",
            "paper_account_id",
            "market",
            "symbol",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
        UniqueConstraint(
            "owner_user_id",
            "last_exit_signal_key",
            name="uq_kasset_position_state_owner_exit_signal",
        ),
        Index(
            "ix_kasset_position_state_owner_updated",
            "owner_user_id",
            "updated_at",
        ),
    )

    position_cycle_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    paper_position_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "paper.paper_positions.id",
            name="fk_kasset_position_state_paper_position",
            ondelete="SET NULL",
        ),
    )
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    paper_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    initial_atr: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    initial_stop: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_stop: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    highest_close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    partial_exit_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    opened_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_exit_signal_key: Mapped[str | None] = mapped_column(Text)
    strategy_key: Mapped[str | None] = mapped_column(Text)
    strategy_version: Mapped[str | None] = mapped_column(Text)
    strategy_fingerprint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KAssetStrategyPromotion(Base):
    """Owner-independent lifecycle for one exact PAPER strategy version."""

    __tablename__ = "kasset_strategy_promotions"
    __table_args__ = (
        CheckConstraint(
            "btrim(strategy_key) <> '' AND btrim(version) <> ''",
            name="ck_kasset_strategy_promotion_identity",
        ),
        CheckConstraint(
            "state IN ('DRAFT','BACKTESTED','PAPER_APPROVED',"
            "'PAPER_SUSPENDED','RETIRED')",
            name="ck_kasset_strategy_promotion_state",
        ),
        CheckConstraint(
            "jsonb_typeof(metrics) = 'object'",
            name="ck_kasset_strategy_promotion_metrics_object",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'array'",
            name="ck_kasset_strategy_promotion_evidence_array",
        ),
        CheckConstraint(
            "threshold_evaluation IS NULL "
            "OR jsonb_typeof(threshold_evaluation) = 'object'",
            name="ck_kasset_strategy_promotion_threshold_object",
        ),
        CheckConstraint(
            "metrics_hash IS NULL OR metrics_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kasset_strategy_promotion_hash_format",
        ),
        CheckConstraint(
            "strategy_artifact_fingerprint IS NULL "
            "OR strategy_artifact_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kasset_strategy_promotion_artifact_fingerprint",
        ),
        CheckConstraint(
            "source_commit IS NULL OR source_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name="ck_kasset_strategy_promotion_source_commit",
        ),
        CheckConstraint(
            "evidence_schema_version IS NULL OR btrim(evidence_schema_version) <> ''",
            name="ck_kasset_strategy_promotion_evidence_schema",
        ),
        CheckConstraint(
            "num_nonnulls(promotion_candidate_id, "
            "strategy_artifact_fingerprint, source_commit, "
            "evidence_schema_version) IN (0, 4)",
            name="ck_kasset_strategy_promotion_trust_bundle",
        ),
        CheckConstraint(
            "(state = 'DRAFT' AND metrics = '{}'::jsonb AND metrics_hash IS NULL)"
            " OR (state IN ('BACKTESTED','PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND metrics <> '{}'::jsonb AND metrics_hash IS NOT NULL)"
            " OR (state = 'RETIRED' AND ((metrics = '{}'::jsonb"
            " AND metrics_hash IS NULL) OR (metrics <> '{}'::jsonb"
            " AND metrics_hash IS NOT NULL)))",
            name="ck_kasset_strategy_promotion_metrics_state",
        ),
        CheckConstraint(
            "(state IN ('PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND threshold_evaluation IS NOT NULL)"
            " OR state IN ('DRAFT','BACKTESTED','RETIRED')",
            name="ck_kasset_strategy_promotion_threshold_state",
        ),
        CheckConstraint(
            "(state IN ('PAPER_APPROVED','PAPER_SUSPENDED')"
            " AND approved_at IS NOT NULL)"
            " OR (state IN ('DRAFT','BACKTESTED') AND approved_at IS NULL)"
            " OR state = 'RETIRED'",
            name="ck_kasset_strategy_promotion_approved_at",
        ),
        CheckConstraint(
            "(state = 'PAPER_SUSPENDED' AND suspended_at IS NOT NULL)"
            " OR (state IN ('DRAFT','BACKTESTED','PAPER_APPROVED')"
            " AND suspended_at IS NULL) OR state = 'RETIRED'",
            name="ck_kasset_strategy_promotion_suspended_at",
        ),
        CheckConstraint(
            "(state = 'RETIRED' AND retired_at IS NOT NULL)"
            " OR (state <> 'RETIRED' AND retired_at IS NULL)",
            name="ck_kasset_strategy_promotion_retired_at",
        ),
        CheckConstraint(
            "suspended_at IS NULL OR approved_at IS NOT NULL",
            name="ck_kasset_strategy_promotion_suspend_after_approve",
        ),
        CheckConstraint(
            "updated_at >= created_at"
            " AND (approved_at IS NULL OR approved_at >= created_at)"
            " AND (suspended_at IS NULL OR suspended_at >= approved_at)"
            " AND (retired_at IS NULL OR retired_at >= created_at)"
            " AND (retired_at IS NULL OR approved_at IS NULL"
            " OR retired_at >= approved_at)"
            " AND (retired_at IS NULL OR suspended_at IS NULL"
            " OR retired_at >= suspended_at)",
            name="ck_kasset_strategy_promotion_timestamp_order",
        ),
        UniqueConstraint(
            "strategy_key",
            "version",
            name="uq_kasset_strategy_promotion_key_version",
        ),
        Index(
            "ix_kasset_strategy_promotion_state_updated",
            "state",
            "updated_at",
        ),
        Index(
            "ix_kasset_strategy_promotion_candidate",
            "promotion_candidate_id",
            unique=True,
            postgresql_where=text("promotion_candidate_id IS NOT NULL"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="DRAFT",
        server_default="DRAFT",
    )
    metrics: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    metrics_hash: Mapped[str | None] = mapped_column(Text)
    promotion_candidate_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "research.promotion_candidates.id",
            name="fk_kasset_strategy_promotion_candidate",
            ondelete="RESTRICT",
        ),
    )
    strategy_artifact_fingerprint: Mapped[str | None] = mapped_column(Text)
    source_commit: Mapped[str | None] = mapped_column(Text)
    evidence_schema_version: Mapped[str | None] = mapped_column(Text)
    threshold_evaluation: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    evidence: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KAssetShadowDailyHighWatermark(Base):
    """소유자·계좌·시장·거래일별 SHADOW 계좌 자산 고점 상태."""

    __tablename__ = "kasset_shadow_daily_high_watermarks"
    __table_args__ = (
        CheckConstraint(
            "btrim(account_key) <> ''",
            name="ck_kasset_shadow_hwm_account_key_nonempty",
        ),
        CheckConstraint(
            "market IN ('KRX', 'US')",
            name="ck_kasset_shadow_hwm_market_valid",
        ),
        CheckConstraint(
            "mode = 'SHADOW'",
            name="ck_kasset_shadow_hwm_mode_shadow",
        ),
        CheckConstraint(
            "session_opening_equity > 0 AND reference_equity > 0 "
            "AND peak_equity > 0 AND current_equity > 0",
            name="ck_kasset_shadow_hwm_equities_positive",
        ),
        CheckConstraint(
            "peak_equity >= session_opening_equity "
            "AND peak_equity >= current_equity",
            name="ck_kasset_shadow_hwm_peak_monotonic",
        ),
        CheckConstraint(
            "state_version > 0",
            name="ck_kasset_shadow_hwm_state_version_positive",
        ),
        CheckConstraint(
            "btrim(valuation_source) <> ''",
            name="ck_kasset_shadow_hwm_valuation_source_nonempty",
        ),
        CheckConstraint(
            "btrim(evidence_schema_version) <> ''",
            name="ck_kasset_shadow_hwm_evidence_schema_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name="ck_kasset_shadow_hwm_evidence_object",
        ),
        Index(
            "ix_kasset_shadow_hwm_owner_valuation",
            "owner_user_id",
            "valuation_at",
        ),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_key: Mapped[str] = mapped_column(Text, primary_key=True)
    market: Mapped[str] = mapped_column(Text, primary_key=True)
    trading_date: Mapped[date] = mapped_column(Date, primary_key=True)
    session_opening_equity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
    )
    reference_equity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
    )
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    current_equity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    valuation_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    valuation_source: Mapped[str] = mapped_column(Text, nullable=False)
    state_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    evidence_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="SHADOW",
        server_default="SHADOW",
    )
    evidence: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


