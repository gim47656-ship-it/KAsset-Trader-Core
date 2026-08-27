"""Read-only technical-analysis Skill.

This Skill consumes normalized evidence supplied by the runtime. It does not
fetch broker credentials, call broker APIs, or submit orders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.extensions.kasset.ai.base import ExternalSkillRunner
from app.extensions.kasset.ai.models import SkillRequest, SkillResult


class TechnicalAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64)
    market: Literal["kr", "us", "crypto"]
    timeframe: str = Field(default="1d", min_length=1, max_length=32)
    quote_asof: datetime | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)
    price_context: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=128)


class TechnicalAnalysisSkill:
    name = "technical_analysis"

    async def run(
        self,
        data: TechnicalAnalysisInput,
        *,
        provider: ExternalSkillRunner,
    ) -> SkillResult:
        request = SkillRequest(
            skill=self.name,
            symbol=data.symbol,
            market=data.market,
            correlation_id=data.correlation_id,
            instruction=(
                "Analyze the supplied technical evidence only. Identify trend, "
                "momentum, volatility, support/resistance, and contradictory "
                "signals. Return BUY/SELL/HOLD/WATCH only as advisory evidence. "
                "Do not propose order sizing, brokers, accounts, leverage, or "
                "execution instructions. Explicitly note stale or insufficient "
                "evidence."
            ),
            context={
                "timeframe": data.timeframe,
                "quote_asof": data.quote_asof.isoformat() if data.quote_asof else None,
                "indicators": data.indicators,
                "price_context": data.price_context,
            },
        )
        return await provider.run_skill(request)
