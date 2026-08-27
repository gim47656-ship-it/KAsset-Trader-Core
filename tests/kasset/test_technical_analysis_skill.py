from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.extensions.kasset.ai.models import SkillRequest, SkillResult
from app.extensions.kasset.skills.technical_analysis import (
    TechnicalAnalysisInput,
    TechnicalAnalysisSkill,
)


class _CaptureProvider:
    name = "subscription"

    def __init__(self) -> None:
        self.request: SkillRequest | None = None

    async def run_skill(self, request: SkillRequest) -> SkillResult:
        self.request = request
        return SkillResult(
            skill=request.skill,
            provider="subscription",
            summary="read-only technical result",
            signal="WATCH",
            confidence=0.6,
            correlation_id=request.correlation_id,
        )


@pytest.mark.asyncio
async def test_technical_skill_sends_only_normalized_read_only_evidence() -> None:
    provider = _CaptureProvider()
    skill = TechnicalAnalysisSkill()
    data = TechnicalAnalysisInput(
        symbol="AAPL",
        market="us",
        timeframe="1d",
        quote_asof=datetime(2026, 8, 26, 6, 0, tzinfo=UTC),
        indicators={"rsi14": 54.2, "sma20": 226.1},
        price_context={"last": 228.4},
        correlation_id="test-001",
    )

    result = await skill.run(data, provider=provider)

    assert result.skill == "technical_analysis"
    assert provider.request is not None
    assert provider.request.context == {
        "timeframe": "1d",
        "quote_asof": "2026-08-26T06:00:00+00:00",
        "indicators": {"rsi14": 54.2, "sma20": 226.1},
        "price_context": {"last": 228.4},
    }
    serialized = provider.request.model_dump_json().lower()
    assert "quantity" not in serialized
    assert "place_order" not in serialized
    assert "account_number" not in serialized
