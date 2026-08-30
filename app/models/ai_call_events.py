"""Append-only ledger of individual AI provider attempts.

One row is one *attempt* against one provider/model. A single logical AI request
fans out into several attempts whenever availability routing falls back or the
tier router escalates, so ``logical_call_id`` groups the attempts that served
the same caller request and ``attempt_no`` orders them from 1.

Deliberate omissions:

* No prompt or response text. The ledger answers "how much AI did we use and
  did it work", never "what did we ask". Provider bodies routinely echo request
  headers, so storing them here would leak credentials into an operator screen.
* ``error_type`` holds a bounded classifier (the exception class name, or an
  ``http_<status class>`` bucket), never the provider's error message.
* Token and cost columns are nullable on purpose. MCP, subscription-CLI,
  Cloudflare and Hermes transports return no usage block; those rows keep
  ``NULL`` so a reader can tell "no tokens reported" apart from "zero tokens".
* ``owner_user_id`` carries no foreign key. Audit rows must outlive the user
  row they describe, and a ``CASCADE`` would silently erase usage history.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

type AiCallStatus = Literal["success", "failure"]

AI_CALL_EVENTS_SCHEMA: Final = "review"
AI_CALL_EVENTS_TABLE: Final = "ai_call_events"

#: Provider-reported cost is the only accepted source. There is no local price
#: table: model prices change without notice and a stale table would report
#: confident, wrong money.
COST_SOURCE_PROVIDER_REPORTED: Final = "provider_reported"


class AiCallEvent(Base):
    """One provider attempt. Rows are inserted once and never updated."""

    __tablename__ = AI_CALL_EVENTS_TABLE
    __table_args__ = (
        CheckConstraint(
            "length(btrim(logical_call_id)) > 0",
            name="logical_call_id_nonempty",
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint("latency_ms >= 0", name="latency_ms_nonnegative"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("length(btrim(feature)) > 0", name="feature_nonempty"),
        CheckConstraint("length(btrim(route_name)) > 0", name="route_name_nonempty"),
        CheckConstraint("length(btrim(provider)) > 0", name="provider_nonempty"),
        CheckConstraint("length(btrim(model_name)) > 0", name="model_name_nonempty"),
        CheckConstraint("status IN ('success', 'failure')", name="status"),
        CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) AND "
            "(completion_tokens IS NULL OR completion_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0)",
            name="tokens_nonnegative",
        ),
        # All three cost columns move together: a number without a currency and
        # a source is not a cost, it is a guess.
        CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL "
            "AND cost_source IS NULL) OR "
            "(cost_amount IS NOT NULL AND length(btrim(cost_currency)) > 0 "
            "AND length(btrim(cost_source)) > 0)",
            name="cost_coherent",
        ),
        UniqueConstraint(
            "logical_call_id",
            "attempt_no",
            name="uq_ai_call_events_logical_call_attempt",
        ),
        Index(
            "ix_ai_call_events_started_at",
            "started_at",
            postgresql_ops={"started_at": "DESC"},
        ),
        Index(
            "ix_ai_call_events_feature_started_at",
            "feature",
            "started_at",
            postgresql_ops={"started_at": "DESC"},
        ),
        Index(
            "ix_ai_call_events_provider_started_at",
            "provider",
            "started_at",
            postgresql_ops={"started_at": "DESC"},
        ),
        {"schema": AI_CALL_EVENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    logical_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    feature: Mapped[str] = mapped_column(Text, nullable=False)
    route_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10),
        nullable=True,
    )
    cost_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "AI_CALL_EVENTS_SCHEMA",
    "AI_CALL_EVENTS_TABLE",
    "COST_SOURCE_PROVIDER_REPORTED",
    "AiCallEvent",
    "AiCallStatus",
]
