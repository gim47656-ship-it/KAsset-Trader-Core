"""KRX 장기 추세·재무성장 SHADOW 관측 run·신호 원장을 추가한다.

Revision ID: 20261005_kasset_longterm_shadow
Revises: 20261003_kasset_swing_shadow
Create Date: 2026-10-05

Additive DDL only. 기존 테이블·행은 건드리지 않는다. 기록기는
``KASSET_LONGTERM_SHADOW_ENABLED``(기본 false) 또는 운영자 CLI로만 쓴다.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261005_kasset_longterm_shadow"
down_revision = "20261003_kasset_swing_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_RUNS = "kasset_longterm_shadow_runs"
_SIGNALS = "kasset_longterm_shadow_signals"
_RUNS_SESSION_INDEX = "ix_review_kasset_longterm_shadow_runs_session"
_SIGNALS_SESSION_INDEX = "ix_review_kasset_longterm_shadow_signals_session"
_SIGNALS_RUN_INDEX = "ix_review_kasset_longterm_shadow_signals_run"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        _RUNS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trigger_source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("signal_session_date", sa.Date(), nullable=True),
        sa.Column("evaluation_as_of", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("universe_count", sa.Integer(), nullable=True),
        sa.Column("evaluated_count", sa.Integer(), nullable=True),
        sa.Column(
            "exclusions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "candidate_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evaluated_symbols",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'rejected', 'failed', 'not_applicable')",
            name="status",
        ),
        sa.CheckConstraint(
            "trigger_source IN ('cli', 'daily_candles_task')",
            name="trigger_source",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR (signal_session_date IS NOT NULL "
            "AND evaluation_as_of IS NOT NULL AND universe_count IS NOT NULL "
            "AND evaluated_count IS NOT NULL AND evaluated_symbols IS NOT NULL)",
            name="completed_complete",
        ),
        sa.CheckConstraint(
            "status <> 'not_applicable' OR (signal_session_date IS NOT NULL "
            "AND evaluation_as_of IS NOT NULL)",
            name="not_applicable_complete",
        ),
        sa.CheckConstraint(
            "status = 'completed' OR (reason IS NOT NULL AND btrim(reason) <> '')",
            name="reason_required",
        ),
        sa.CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint"
        ),
        sa.CheckConstraint("btrim(schema_version) <> ''", name="schema_version"),
        sa.CheckConstraint("jsonb_typeof(config) = 'object'", name="config_object"),
        sa.CheckConstraint(
            "jsonb_typeof(exclusions) = 'object'", name="exclusions_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidate_counts) = 'object'",
            name="candidate_counts_object",
        ),
        sa.CheckConstraint(
            "evaluated_symbols IS NULL OR jsonb_typeof(evaluated_symbols) = 'array'",
            name="evaluated_symbols_array",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=_SCHEMA,
    )
    op.create_index(
        _RUNS_SESSION_INDEX,
        _RUNS,
        ["signal_session_date", "config_fingerprint"],
        schema=_SCHEMA,
    )
    op.create_table(
        _SIGNALS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("signal_session_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("evaluation_as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("momentum_12_1", sa.Numeric(), nullable=False),
        sa.Column("signal_open", sa.Numeric(), nullable=False),
        sa.Column("signal_high", sa.Numeric(), nullable=False),
        sa.Column("signal_low", sa.Numeric(), nullable=False),
        sa.Column("signal_close", sa.Numeric(), nullable=False),
        sa.Column("signal_volume", sa.Numeric(), nullable=False),
        sa.Column("signal_value", sa.Numeric(), nullable=True),
        sa.Column("signal_bar_source", sa.Text(), nullable=True),
        sa.Column("signal_bar_ingested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "candidate IN ('trend_momentum', 'quality_growth_trend')",
            name="candidate",
        ),
        sa.CheckConstraint("market = 'KRX'", name="market"),
        sa.CheckConstraint("btrim(symbol) <> ''", name="symbol_nonempty"),
        sa.CheckConstraint("rank >= 1", name="rank_positive"),
        sa.CheckConstraint("momentum_12_1 > 0", name="momentum_positive"),
        sa.CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint"
        ),
        sa.CheckConstraint("btrim(schema_version) <> ''", name="schema_version"),
        sa.CheckConstraint(
            "signal_open > 0 AND signal_high > 0 AND signal_low > 0 "
            "AND signal_close > 0 AND signal_volume >= 0",
            name="prices_positive",
        ),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
        sa.ForeignKeyConstraint(["run_id"], [f"{_SCHEMA}.{_RUNS}.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate",
            "config_fingerprint",
            "symbol",
            "signal_session_date",
            name="uq_kasset_longterm_shadow_signals_identity",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        _SIGNALS_SESSION_INDEX,
        _SIGNALS,
        ["signal_session_date", "candidate"],
        schema=_SCHEMA,
    )
    op.create_index(_SIGNALS_RUN_INDEX, _SIGNALS, ["run_id"], schema=_SCHEMA)


def downgrade() -> None:
    op.drop_index(_SIGNALS_RUN_INDEX, table_name=_SIGNALS, schema=_SCHEMA)
    op.drop_index(_SIGNALS_SESSION_INDEX, table_name=_SIGNALS, schema=_SCHEMA)
    op.drop_table(_SIGNALS, schema=_SCHEMA)
    op.drop_index(_RUNS_SESSION_INDEX, table_name=_RUNS, schema=_SCHEMA)
    op.drop_table(_RUNS, schema=_SCHEMA)
