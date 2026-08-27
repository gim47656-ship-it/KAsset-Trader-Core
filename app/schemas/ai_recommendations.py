"""Android transport schemas for persisted AI recommendation review."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from app.models.ai_recommendations import (
    RecommendationAction,
    RecommendationDecision,
    RecommendationMarket,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)

DecimalText = Annotated[str, Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]
_DECIMAL_TEXT = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


def _validate_decimal_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise ValueError("must be a plain decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("must be a valid decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("must be a finite decimal string")
    return value


def _validate_aware_timestamp(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamp must include a timezone")
    return value


def _serialize_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


class RecommendationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str = Field(min_length=1)
    source: str | None = None
    published_at: datetime | None = Field(default=None, alias="publishedAt")

    _published_at_timezone = field_validator("published_at")(_validate_aware_timestamp)

    @field_serializer("published_at", when_used="json")
    def serialize_published_at(self, value: datetime | None) -> str | None:
        return _serialize_timestamp(value)


class RecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    action: RecommendationAction
    decision: RecommendationDecision
    market: RecommendationMarket
    symbol: str
    name: str | None = None
    currency: Literal["KRW", "USD"] | None = None
    headline: str | None = None
    rationale: list[str]
    risks: list[str]
    evidence: list[RecommendationEvidence]
    confidence: DecimalText | None = None
    reference_price: DecimalText | None = Field(default=None, alias="referencePrice")
    suggested_quantity: DecimalText | None = Field(
        default=None,
        alias="suggestedQuantity",
    )
    source: str | None = None
    created_at: datetime = Field(alias="createdAt")
    valid_until: datetime | None = Field(default=None, alias="validUntil")
    decided_at: datetime | None = Field(default=None, alias="decidedAt")

    _decimal_strings = field_validator(
        "confidence",
        "reference_price",
        "suggested_quantity",
        mode="before",
    )(_validate_decimal_text)
    _timestamp_timezones = field_validator(
        "created_at",
        "valid_until",
        "decided_at",
    )(_validate_aware_timestamp)

    @field_serializer(
        "created_at",
        "valid_until",
        "decided_at",
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return _serialize_timestamp(value)


class RecommendationListResponse(BaseModel):
    recommendations: list[RecommendationResponse]


class RecommendationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: TerminalRecommendationDecision


class RecommendationError(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class RecommendationErrorEnvelope(BaseModel):
    error: RecommendationError


__all__ = [
    "RecommendationDecisionRequest",
    "RecommendationErrorEnvelope",
    "RecommendationEvidence",
    "RecommendationListResponse",
    "RecommendationResponse",
    "RecommendationStatusGroup",
    "TerminalRecommendationDecision",
]
