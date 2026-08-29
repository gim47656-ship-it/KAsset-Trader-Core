"""Durable AI shadow evidence and read-only persisted-selection statistics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

AI_SHADOW_SCHEMA_VERSION = "kasset.ai-shadow.v1"
PERSISTED_FINAL_SELECTIONS_ONLY = "persisted final selections only"
AI_SHADOW_SELECTION_REASON = "ranked_final_selection_after_strategy_ai_agreement"


class _ValidatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    action: Literal["BUY", "SELL", "HOLD", "IGNORE", "REVIEW"]
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    bullish_score: int = Field(ge=0, le=100, alias="bullishScore")
    bearish_score: int = Field(ge=0, le=100, alias="bearishScore")
    rationale_tags: list[str] = Field(max_length=12, alias="rationaleTags")

    @field_validator("rationale_tags")
    @classmethod
    def validate_rationale_tags(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or value != value.strip() or len(value) > 64:
                raise ValueError("rationaleTags must contain normalized short tags")
        return values


class _SelectedAiShadowEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    title: Literal["AI shadow final selection"]
    source: Literal["kasset_ai_shadow"]
    kind: Literal["ai_shadow"]
    schema_version: str = Field(alias="schemaVersion")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$", alias="inputHash")
    provider: str = Field(min_length=1, max_length=128)
    tier: Literal["luna", "terra", "sol"]
    model_id: str = Field(min_length=1, max_length=256, alias="modelId")
    validated_response: _ValidatedResponse = Field(alias="validatedResponse")
    confidence: str
    selected: Literal[True]
    selection_reason: str = Field(
        min_length=1,
        max_length=128,
        alias="selectionReason",
    )
    observed_at: str = Field(alias="observedAt")

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != AI_SHADOW_SCHEMA_VERSION:
            raise ValueError("unsupported AI shadow schemaVersion")
        return value

    @field_validator("provider", "model_id", "selection_reason")
    @classmethod
    def validate_normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("AI shadow text fields must be normalized")
        return value

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: str) -> str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("confidence must be decimal text") from exc
        if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
            raise ValueError("confidence must be finite and in [0, 1]")
        return str(parsed)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observedAt must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("observedAt must include a timezone")
        return _timestamp_text(parsed)


@dataclass(frozen=True, slots=True)
class AiShadowObservation:
    """Validated facts observed from the exact provider route that returned a verdict."""

    input_hash: str
    provider: str
    tier: str
    model_id: str
    action: str
    risk: str
    bullish_score: int
    bearish_score: int
    rationale_tags: tuple[str, ...]
    confidence: str
    observed_at: str

    def as_selected_evidence(
        self,
        *,
        selection_reason: str = AI_SHADOW_SELECTION_REASON,
    ) -> dict[str, object]:
        return validate_selected_ai_shadow_evidence(
            {
                "title": "AI shadow final selection",
                "source": "kasset_ai_shadow",
                "kind": "ai_shadow",
                "schemaVersion": AI_SHADOW_SCHEMA_VERSION,
                "inputHash": self.input_hash,
                "provider": self.provider,
                "tier": self.tier,
                "modelId": self.model_id,
                "validatedResponse": {
                    "action": self.action,
                    "risk": self.risk,
                    "bullishScore": self.bullish_score,
                    "bearishScore": self.bearish_score,
                    "rationaleTags": list(self.rationale_tags),
                },
                "confidence": self.confidence,
                "selected": True,
                "selectionReason": selection_reason,
                "observedAt": self.observed_at,
            }
        )


def build_ai_shadow_observation(
    verdict: object,
    *,
    observed_at: datetime,
) -> AiShadowObservation:
    """Project a router verdict into a secret-free in-memory audit observation."""

    timestamp = _timestamp_text(observed_at)
    input_hash = _required_text_attribute(verdict, "input_hash")
    provider = _required_text_attribute(verdict, "provider")
    tier = _required_text_attribute(verdict, "tier")
    model_id = _required_text_attribute(verdict, "model_id")
    action = _required_text_attribute(verdict, "action")
    risk = _required_text_attribute(verdict, "risk")
    confidence = _confidence_text(getattr(verdict, "confidence", None))
    bullish_score = _score_attribute(verdict, "bullish_score")
    bearish_score = _score_attribute(verdict, "bearish_score")
    raw_tags = getattr(verdict, "rationale_tags", None)
    if not isinstance(raw_tags, list | tuple):
        raise ValueError("AI shadow verdict requires rationale_tags")
    rationale_tags = tuple(cast(str, value) for value in raw_tags)

    observation = AiShadowObservation(
        input_hash=input_hash,
        provider=provider,
        tier=tier,
        model_id=model_id,
        action=action,
        risk=risk,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        rationale_tags=rationale_tags,
        confidence=confidence,
        observed_at=timestamp,
    )
    # Validate every persisted field now; selection is added only for a durable row.
    observation.as_selected_evidence()
    return observation


def validate_selected_ai_shadow_evidence(
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Return the closed, JSON-safe ai_shadow shape or fail closed."""

    validated = _SelectedAiShadowEvidence.model_validate(evidence)
    return cast(dict[str, object], validated.model_dump(mode="json", by_alias=True))


def derive_persisted_final_selections_only_stats(
    evidence_rows: Iterable[Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Aggregate valid ai_shadow evidence, one final selection per persisted row."""

    selected: list[_SelectedAiShadowEvidence] = []
    for evidence_items in evidence_rows:
        valid_for_row: list[_SelectedAiShadowEvidence] = []
        for item in evidence_items:
            if item.get("kind") != "ai_shadow":
                continue
            try:
                valid_for_row.append(_SelectedAiShadowEvidence.model_validate(item))
            except ValidationError:
                continue
        if len(valid_for_row) == 1:
            selected.append(valid_for_row[0])

    model_counts = Counter(item.model_id for item in selected)
    action_counts = Counter(item.validated_response.action for item in selected)
    confidence_total = sum(
        (Decimal(item.confidence) for item in selected),
        start=Decimal("0"),
    )
    average_confidence = (
        str(
            (confidence_total / Decimal(len(selected))).quantize(
                Decimal("0.000001"),
                rounding=ROUND_HALF_EVEN,
            )
        )
        if selected
        else None
    )
    count = len(selected)
    return {
        "name": f"AI shadow stats — {PERSISTED_FINAL_SELECTIONS_ONLY}",
        "scope": PERSISTED_FINAL_SELECTIONS_ONLY,
        "count": count,
        "modelCounts": dict(sorted(model_counts.items())),
        "actionCounts": dict(sorted(action_counts.items())),
        "averageConfidence": average_confidence,
        "selectionCount": count,
    }


async def load_persisted_final_selections_only_stats(
    db: AsyncSession,
    *,
    owner_user_id: int | None = None,
) -> dict[str, object]:
    """Read recommendation JSONB and derive scoped stats without writing tables."""

    from sqlalchemy import select

    from app.models.ai_recommendations import AIRecommendation

    statement = select(AIRecommendation.evidence)
    if owner_user_id is not None:
        statement = statement.where(AIRecommendation.owner_user_id == owner_user_id)
    evidence_rows = (await db.scalars(statement)).all()
    return derive_persisted_final_selections_only_stats(evidence_rows)


def _required_text_attribute(value: object, name: str) -> str:
    raw = getattr(value, name, None)
    if isinstance(raw, Enum):
        raw = raw.value
    if type(raw) is not str or not raw or raw != raw.strip():
        raise ValueError(f"AI shadow verdict requires exact {name}")
    return raw


def _score_attribute(value: object, name: str) -> int:
    raw = getattr(value, name, None)
    if type(raw) is not int or not 0 <= raw <= 100:
        raise ValueError(f"AI shadow verdict requires bounded {name}")
    return raw


def _confidence_text(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("AI shadow verdict requires numeric confidence")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("AI shadow verdict requires numeric confidence") from exc
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        raise ValueError("AI shadow verdict requires bounded confidence")
    return str(parsed)


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AI shadow observed_at must include a timezone")
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


__all__ = [
    "AI_SHADOW_SCHEMA_VERSION",
    "AI_SHADOW_SELECTION_REASON",
    "PERSISTED_FINAL_SELECTIONS_ONLY",
    "AiShadowObservation",
    "build_ai_shadow_observation",
    "derive_persisted_final_selections_only_stats",
    "load_persisted_final_selections_only_stats",
    "validate_selected_ai_shadow_evidence",
]
