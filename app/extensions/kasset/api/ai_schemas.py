"""Android wire contracts for the read-only AI hub briefing.

Stored-data-only projection: no prompt, raw LLM response, article body, or
report portfolio snapshot reaches these schemas. Owner routine alerts are
read-only evidence.
"""

from typing import Literal

from pydantic import Field

from app.extensions.kasset.api.daily_routine_schemas import DailyRoutineAlert
from app.extensions.kasset.api.schemas import AndroidWireModel

AiSectionStatus = Literal["available", "empty"]
AiResearchStatus = Literal["available", "empty", "stale"]
AiBriefingStatus = Literal["available", "unavailable", "stale"]
AiBriefingDataStatus = Literal["fresh", "soft_stale", "partial", "unknown"]


class AiSymbolRef(AndroidWireModel):
    symbol: str
    market: str


class AiResearchSymbolRef(AndroidWireModel):
    symbol: str
    market: str
    source: str | None = None


class AiNewsItem(AndroidWireModel):
    id: str
    headline: str
    source: str | None = None
    published_at: str | None = None
    market: str
    symbols: list[AiSymbolRef] = Field(default_factory=list)
    canonical_url: str | None = None
    summary: str | None = None
    data_updated_at: str | None = None


class AiNewsSection(AndroidWireModel):
    status: AiSectionStatus
    refreshed_at: str | None = None
    items: list[AiNewsItem] = Field(default_factory=list)


class AiResearchItem(AndroidWireModel):
    id: str
    title: str | None = None
    provider: str | None = None
    published_at: str | None = None
    published_at_text: str | None = None
    market: str
    symbols: list[AiResearchSymbolRef] = Field(default_factory=list)
    canonical_url: str | None = None
    excerpt: str | None = None
    data_updated_at: str | None = None


class AiResearchSection(AndroidWireModel):
    status: AiResearchStatus
    refreshed_at: str | None = None
    items: list[AiResearchItem] = Field(default_factory=list)


class AiBriefingSection(AndroidWireModel):
    status: AiBriefingStatus
    id: str | None = None
    title: str | None = None
    summary: str | None = None
    provider: str | None = None
    market: str | None = None
    as_of: str | None = None
    valid_until: str | None = None
    data_status: AiBriefingDataStatus = "unknown"
    unavailable_reason: str | None = None


class AiBriefingResponse(AndroidWireModel):
    status: AiSectionStatus
    as_of: str
    news: AiNewsSection
    routine_alerts: list[DailyRoutineAlert] = Field(default_factory=list)
    research: AiResearchSection
    briefing: AiBriefingSection
