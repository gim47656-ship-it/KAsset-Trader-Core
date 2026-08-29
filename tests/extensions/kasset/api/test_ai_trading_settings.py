"""Focused request/response contracts for AI PAPER risk presets."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.extensions.kasset.api.router import _ai_trading_state_response
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingSnapshot,
    AITradingUsage,
    OperatingMode,
)
from app.schemas.ai_recommendations import AITradingStateUpdate


def _request_settings(**overrides: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "riskLevel": 4,
        "operatingBudget": "2500000",
        "dailyTargetRatePct": "0.7",
        "maxDailyLossRatePct": "1.8",
        "killSwitch": False,
        "currency": "KRW",
    }
    settings.update(overrides)
    return settings


def test_request_accepts_custom_percentages_as_decimal_strings() -> None:
    request = AITradingStateUpdate.model_validate(
        {"mode": "AUTO_PAPER", "settings": _request_settings()}
    )

    assert request.settings.daily_target_rate_pct == Decimal("0.7")
    assert request.settings.max_daily_loss_rate_pct == Decimal("1.8")
    assert request.model_dump(mode="json", by_alias=True)["settings"] == {
        "riskLevel": 4,
        "operatingBudget": "2500000",
        "dailyTargetRatePct": "0.7",
        "maxDailyLossRatePct": "1.8",
        "killSwitch": False,
        "currency": "KRW",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("riskLevel", 0),
        ("riskLevel", 6),
        ("dailyTargetRatePct", "-0.1"),
        ("dailyTargetRatePct", "10.1"),
        ("maxDailyLossRatePct", "0"),
        ("maxDailyLossRatePct", "20.1"),
    ],
)
def test_request_rejects_values_outside_safe_bounds(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        AITradingStateUpdate.model_validate(
            {
                "mode": "AUTO_PAPER",
                "settings": _request_settings(**{field: value}),
            }
        )


@pytest.mark.parametrize(
    "hidden_field",
    [
        "derivedLimits",
        "maxSymbolAllocationPct",
        "maxSymbolAllocation",
        "maxConcurrentHoldings",
        "maxBuysPerDay",
        "maxOrdersPerDay",
        "sameSymbolReentryLimit",
        "minAiConfidence",
        "conservativeDailyGoal",
        "dailyMaxLoss",
    ],
)
def test_request_rejects_client_hidden_limit_overrides(hidden_field: str) -> None:
    with pytest.raises(ValidationError):
        AITradingStateUpdate.model_validate(
            {
                "mode": "AUTO_PAPER",
                "settings": _request_settings(**{hidden_field: 999}),
            }
        )


def test_router_response_exposes_only_canonical_and_derived_settings() -> None:
    limits = AITradingLimits(
        risk_level=4,
        operating_budget=Decimal("2500000"),
        daily_target_rate_pct=Decimal("0.7"),
        max_daily_loss_rate_pct=Decimal("1.8"),
        currency="KRW",
    )
    response = _ai_trading_state_response(
        AITradingSnapshot(
            mode=OperatingMode.AUTO_PAPER,
            limits=limits,
            usage=AITradingUsage(),
            kill_switch=False,
            updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )

    settings = response.model_dump(mode="json", by_alias=True)["settings"]
    assert set(settings) == {
        "riskLevel",
        "operatingBudget",
        "dailyTargetRatePct",
        "maxDailyLossRatePct",
        "killSwitch",
        "currency",
        "derivedLimits",
    }
    assert settings["riskLevel"] == 4
    assert settings["dailyTargetRatePct"] == "0.7"
    assert settings["maxDailyLossRatePct"] == "1.8"
    derived = settings["derivedLimits"]
    assert Decimal(derived["dailyTargetAmount"]) == Decimal("17500")
    assert Decimal(derived["maxDailyLossAmount"]) == Decimal("45000")
    assert Decimal(derived["maxSymbolAllocationPct"]) == Decimal("25")
    assert derived["maxConcurrentHoldings"] == 5
    assert derived["maxBuysPerDay"] == 5
    assert derived["maxOrdersPerDay"] == 8
    assert derived["sameSymbolReentryLimit"] == 1
    assert derived["minAiConfidence"] == "0.50"
