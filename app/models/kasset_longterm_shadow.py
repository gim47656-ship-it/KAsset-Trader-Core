"""KRX 장기 추세·재무성장 SHADOW 전향 관측 원장(주문·추천·승격과 무관).

* ``kasset_longterm_shadow_runs`` — 관측 실행 한 번마다 한 행(append). 거부·실패와
  주 미완료(``not_applicable``) 세션도 남기고, 코호트 세션의 completed run은 벤치마크
  분모인 ``evaluated_symbols``(대상·품질 필터를 통과해 평가된 전 종목)를 담는다.
* ``kasset_longterm_shadow_signals`` — 후보 신호 한 건. 같은 후보·설정·종목·코호트
  세션은 한 번만 저장되고(재실행 멱등) 처음 넣은 run과 관측 시점 근거를 보존한다.

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

KASSET_LONGTERM_SHADOW_SCHEMA: Final = "review"
KASSET_LONGTERM_SHADOW_RUNS_TABLE: Final = "kasset_longterm_shadow_runs"
KASSET_LONGTERM_SHADOW_SIGNALS_TABLE: Final = "kasset_longterm_shadow_signals"

LONGTERM_SHADOW_RUN_STATUSES: Final = (
    "completed",
    "rejected",
    "failed",
    "not_applicable",
)
LONGTERM_SHADOW_TRIGGER_SOURCES: Final = ("cli", "daily_candles_task")
LONGTERM_SHADOW_CANDIDATE_KEYS: Final = ("trend_momentum", "quality_growth_trend")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class KAssetLongtermShadowRun(Base):
    __tablename__ = KASSET_LONGTERM_SHADOW_RUNS_TABLE
    __table_args__ = (
        CheckConstraint(
            _in_list("status", LONGTERM_SHADOW_RUN_STATUSES), name="status"
        ),
        CheckConstraint(
            _in_list("trigger_source", LONGTERM_SHADOW_TRIGGER_SOURCES),
            name="trigger_source",
        ),
        CheckConstraint(
            "status <> 'completed' OR (signal_session_date IS NOT NULL "
            "AND evaluation_as_of IS NOT NULL AND universe_count IS NOT NULL "
            "AND evaluated_count IS NOT NULL AND evaluated_symbols IS NOT NULL)",
            name="completed_complete",
        ),
        CheckConstraint(
            "status <> 'not_applicable' OR (signal_session_date IS NOT NULL "
            "AND evaluation_as_of IS NOT NULL)",
            name="not_applicable_complete",
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
        CheckConstraint(
            "evaluated_symbols IS NULL OR jsonb_typeof(evaluated_symbols) = 'array'",
            name="evaluated_symbols_array",
        ),
        Index(
            "ix_review_kasset_longterm_shadow_runs_session",
            "signal_session_date",
            "config_fingerprint",
        ),
        {"schema": KASSET_LONGTERM_SHADOW_SCHEMA},
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
    evaluated_symbols: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class KAssetLongtermShadowSignal(Base):
    __tablename__ = KASSET_LONGTERM_SHADOW_SIGNALS_TABLE
    __table_args__ = (
        UniqueConstraint(
            "candidate",
            "config_fingerprint",
            "symbol",
            "signal_session_date",
            name="uq_kasset_longterm_shadow_signals_identity",
        ),
        CheckConstraint(
            _in_list("candidate", LONGTERM_SHADOW_CANDIDATE_KEYS), name="candidate"
        ),
        CheckConstraint("market = 'KRX'", name="market"),
        CheckConstraint("btrim(symbol) <> ''", name="symbol_nonempty"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("momentum_12_1 > 0", name="momentum_positive"),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint"
        ),
        CheckConstraint("btrim(schema_version) <> ''", name="schema_version"),
        CheckConstraint(
            "signal_open > 0 AND signal_high > 0 AND signal_low > 0 "
            "AND signal_close > 0 AND signal_volume >= 0",
            name="prices_positive",
        ),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
        Index(
            "ix_review_kasset_longterm_shadow_signals_session",
            "signal_session_date",
            "candidate",
        ),
        Index("ix_review_kasset_longterm_shadow_signals_run", "run_id"),
        {"schema": KASSET_LONGTERM_SHADOW_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{KASSET_LONGTERM_SHADOW_SCHEMA}.{KASSET_LONGTERM_SHADOW_RUNS_TABLE}.id"
        ),
        nullable=False,
    )
    candidate: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    signal_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    evaluation_as_of: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    momentum_12_1: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
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
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "KASSET_LONGTERM_SHADOW_RUNS_TABLE",
    "KASSET_LONGTERM_SHADOW_SCHEMA",
    "KASSET_LONGTERM_SHADOW_SIGNALS_TABLE",
    "LONGTERM_SHADOW_CANDIDATE_KEYS",
    "LONGTERM_SHADOW_RUN_STATUSES",
    "LONGTERM_SHADOW_TRIGGER_SOURCES",
    "KAssetLongtermShadowRun",
    "KAssetLongtermShadowSignal",
]
