"""KRX 스윙 SHADOW 전향 관측 원장(주문·추천·승격과 무관).

* ``kasset_swing_shadow_runs`` — 관측 실행 한 번마다 한 행(append). 성과 리포트의
  분모와 미관측 세션을 판단하는 근거다. 거부·실패 실행도 남긴다.
* ``kasset_swing_shadow_signals`` — 후보 신호 한 건. 같은 후보·설정·종목·anchor는
  한 번만 저장되고(재실행 멱등) 처음 넣은 run과 관측 시점 신호봉을 보존한다.

성과는 저장하지 않는다. 리포트가 조회 시점 일봉으로 계산한다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

KASSET_SWING_SHADOW_SCHEMA: Final = "review"
KASSET_SWING_SHADOW_RUNS_TABLE: Final = "kasset_swing_shadow_runs"
KASSET_SWING_SHADOW_SIGNALS_TABLE: Final = "kasset_swing_shadow_signals"

SWING_SHADOW_RUN_STATUSES: Final = ("completed", "rejected", "failed")
SWING_SHADOW_TRIGGER_SOURCES: Final = ("cli", "daily_candles_task")
SWING_SHADOW_CANDIDATE_KEYS: Final = (
    "weekly_compression_breakout",
    "uptrend_first_pullback",
    "box_breakout_retest",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class KAssetSwingShadowRun(Base):
    __tablename__ = KASSET_SWING_SHADOW_RUNS_TABLE
    __table_args__ = (
        CheckConstraint(_in_list("status", SWING_SHADOW_RUN_STATUSES), name="status"),
        CheckConstraint(
            _in_list("trigger_source", SWING_SHADOW_TRIGGER_SOURCES),
            name="trigger_source",
        ),
        CheckConstraint(
            "status <> 'completed' OR (signal_session_date IS NOT NULL "
            "AND evaluation_as_of IS NOT NULL AND universe_count IS NOT NULL "
            "AND evaluated_count IS NOT NULL)",
            name="completed_complete",
        ),
        CheckConstraint(
            "status = 'completed' OR (reason IS NOT NULL AND btrim(reason) <> '')",
            name="reason_required",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint"
        ),
        CheckConstraint("btrim(schema_version) <> ''", name="schema_version"),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_object"),
        CheckConstraint(
            "jsonb_typeof(exclusions) = 'object'", name="exclusions_object"
        ),
        CheckConstraint(
            "jsonb_typeof(candidate_counts) = 'object'",
            name="candidate_counts_object",
        ),
        Index(
            "ix_review_kasset_swing_shadow_runs_session",
            "signal_session_date",
            "config_fingerprint",
        ),
        {"schema": KASSET_SWING_SHADOW_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    evaluation_as_of: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    universe_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluated_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exclusions: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    candidate_counts: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class KAssetSwingShadowSignal(Base):
    __tablename__ = KASSET_SWING_SHADOW_SIGNALS_TABLE
    __table_args__ = (
        UniqueConstraint(
            "candidate",
            "config_fingerprint",
            "symbol",
            "anchor_session_date",
            name="uq_kasset_swing_shadow_signals_identity",
        ),
        CheckConstraint(
            _in_list("candidate", SWING_SHADOW_CANDIDATE_KEYS), name="candidate"
        ),
        CheckConstraint("market = 'KRX'", name="market"),
        CheckConstraint("btrim(symbol) <> ''", name="symbol_nonempty"),
        CheckConstraint(
            "anchor_session_date <= signal_session_date", name="anchor_not_future"
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint"
        ),
        CheckConstraint("btrim(schema_version) <> ''", name="schema_version"),
        CheckConstraint(
            "signal_open > 0 AND signal_high > 0 AND signal_low > 0 "
            "AND signal_close > 0 AND signal_volume >= 0 AND trigger_price > 0",
            name="prices_positive",
        ),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
        Index(
            "ix_review_kasset_swing_shadow_signals_session",
            "signal_session_date",
            "candidate",
        ),
        Index("ix_review_kasset_swing_shadow_signals_run", "run_id"),
        {"schema": KASSET_SWING_SHADOW_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{KASSET_SWING_SHADOW_SCHEMA}.{KASSET_SWING_SHADOW_RUNS_TABLE}.id"),
        nullable=False,
    )
    candidate: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    signal_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    anchor_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    evaluation_as_of: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    signal_open: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_high: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_low: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_close: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_volume: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    signal_bar_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_bar_ingested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    trigger_price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    stop_reference: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "KASSET_SWING_SHADOW_RUNS_TABLE",
    "KASSET_SWING_SHADOW_SCHEMA",
    "KASSET_SWING_SHADOW_SIGNALS_TABLE",
    "SWING_SHADOW_CANDIDATE_KEYS",
    "SWING_SHADOW_RUN_STATUSES",
    "SWING_SHADOW_TRIGGER_SOURCES",
    "KAssetSwingShadowRun",
    "KAssetSwingShadowSignal",
]
