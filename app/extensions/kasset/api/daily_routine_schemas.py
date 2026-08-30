"""Android wire contracts for owner-scoped daily AI monitoring routines."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator

from app.extensions.kasset.api.schemas import AndroidWireModel

RoutineKey = Literal[
    "RAPID_RISE",
    "RAPID_FALL",
    "TRUMP_POLICY",
    "GLOBAL_FINANCIAL_NEWS",
]

RecommendationMarketScope = Literal["KR_ONLY", "US_ONLY", "KR_US"]

ROUTINE_KEYS: tuple[RoutineKey, ...] = (
    "RAPID_RISE",
    "RAPID_FALL",
    "TRUMP_POLICY",
    "GLOBAL_FINANCIAL_NEWS",
)


class AvailableRoutine(AndroidWireModel):
    key: RoutineKey
    label: str
    description: str


class DailyRoutineAlert(AndroidWireModel):
    id: str
    kind: RoutineKey
    headline: str
    summary: str | None = None
    translated_title: str | None = None
    translated_excerpt: str | None = None
    symbol: str | None = None
    source: str | None = None
    url: str | None = None
    occurred_at: datetime


class DailyRoutineResponse(AndroidWireModel):
    date: date
    inherited_from: date | None = None
    enabled_routines: list[RoutineKey]
    recommendation_market_scope: RecommendationMarketScope
    available_routines: list[AvailableRoutine]
    alerts: list[DailyRoutineAlert]
    updated_at: datetime


class DailyRoutineUpdateRequest(AndroidWireModel):
    enabled_routines: list[RoutineKey] = Field(max_length=len(ROUTINE_KEYS))
    recommendation_market_scope: RecommendationMarketScope | None = None

    @field_validator("enabled_routines")
    @classmethod
    def reject_duplicates(cls, value: list[RoutineKey]) -> list[RoutineKey]:
        if len(value) != len(set(value)):
            raise ValueError("enabledRoutines must not contain duplicates")
        return value


__all__ = [
    "AvailableRoutine",
    "DailyRoutineAlert",
    "DailyRoutineResponse",
    "DailyRoutineUpdateRequest",
    "ROUTINE_KEYS",
    "RecommendationMarketScope",
    "RoutineKey",
]
