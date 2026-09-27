"""Focused request/response contracts for AI PAPER risk presets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.router import (
    _ai_trading_state_response,
    ai_trading_state,
)
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingSnapshot,
    AITradingUsage,
    OperatingMode,
    PaperExecutionView,
)
from app.models.kasset_automation_cycle_events import KAssetAutomationCycleEvent
from app.models.trading import User
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
        "customMaxBuysPerDay": None,
        "customMaxSellsPerDay": None,
    }


def test_request_accepts_custom_buy_and_sell_daily_limits() -> None:
    request = AITradingStateUpdate.model_validate(
        {
            "mode": "AUTO_PAPER",
            "settings": _request_settings(
                customMaxBuysPerDay=10,
                customMaxSellsPerDay=18,
            ),
        }
    )
    assert request.settings.custom_max_buys_per_day == 10
    assert request.settings.custom_max_sells_per_day == 18


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
        operating_budget_krw=Decimal("2500000"),
        operating_budget_usd=Decimal("12500"),
        daily_target_rate_pct=Decimal("0.7"),
        max_daily_loss_rate_pct=Decimal("1.8"),
        custom_max_buys_per_day=10,
        custom_max_sells_per_day=18,
        currency="KRW",
    )
    response = _ai_trading_state_response(
        AITradingSnapshot(
            mode=OperatingMode.AUTO_PAPER,
            limits=limits,
            usage=AITradingUsage(sells_today=3),
            usage_by_currency={
                "KRW": AITradingUsage(sells_today=3),
                "USD": AITradingUsage(),
            },
            kill_switch=False,
            updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )

    payload = response.model_dump(mode="json", by_alias=True)
    settings = payload["settings"]
    assert set(settings) == {
        "riskLevel",
        "operatingBudget",
        "operatingBudgetKrw",
        "operatingBudgetUsd",
        "dailyTargetRatePct",
        "maxDailyLossRatePct",
        "killSwitch",
        "currency",
        "customMaxBuysPerDay",
        "customMaxSellsPerDay",
        "derivedLimits",
    }
    assert settings["riskLevel"] == 4
    assert settings["operatingBudget"] == "2500000"
    assert settings["operatingBudgetKrw"] == "2500000"
    assert settings["operatingBudgetUsd"] == "12500"
    assert settings["customMaxBuysPerDay"] == 10
    assert settings["customMaxSellsPerDay"] == 18
    assert settings["dailyTargetRatePct"] == "0.7"
    assert settings["maxDailyLossRatePct"] == "1.8"
    derived = settings["derivedLimits"]
    assert Decimal(derived["dailyTargetAmount"]) == Decimal("17500")
    assert Decimal(derived["maxDailyLossAmount"]) == Decimal("45000")
    assert Decimal(derived["maxSymbolAllocationPct"]) == Decimal("25")
    assert derived["maxConcurrentHoldings"] == 5
    assert derived["maxBuysPerDay"] == 10
    assert derived["maxSellsPerDay"] == 18
    assert derived["maxOrdersPerDay"] == 28
    assert derived["maxCustomBuysPerDay"] == 10
    assert derived["maxCustomSellsPerDay"] == 20
    assert derived["maxCustomOrdersPerDay"] == 30
    assert derived["riskPerTradeRate"] == "0.01"
    assert derived["sameSymbolReentryLimit"] == 1
    assert derived["minAiConfidence"] == "0.50"
    assert payload["usage"]["sellsToday"] == 3


def test_router_response_exposes_order_execution_origin() -> None:
    moment = datetime(2026, 9, 3, tzinfo=UTC)
    response = _ai_trading_state_response(
        AITradingSnapshot(
            mode=OperatingMode.AUTO_PAPER,
            limits=AITradingLimits(),
            usage=AITradingUsage(),
            usage_by_currency={"KRW": AITradingUsage(), "USD": AITradingUsage()},
            kill_switch=False,
            updated_at=moment,
            executions=(
                PaperExecutionView(
                    id="paper-1",
                    recommendation_id="rec-1",
                    execution_origin="AUTO_PAPER",
                    market="KRX",
                    symbol="005930",
                    name="삼성전자",
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("70100"),
                    currency="KRW",
                    status="FILLED",
                    at=moment,
                    reject_reason=None,
                ),
            ),
        )
    )

    assert (
        response.model_dump(mode="json", by_alias=True)["executions"][0][
            "executionOrigin"
        ]
        == "AUTO_PAPER"
    )


@pytest.mark.asyncio
async def test_trading_state_reads_latest_owner_cycle_without_rewriting_settings_time(
    db_session: AsyncSession,
    user: User,
    other_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_time = datetime(2026, 9, 1, tzinfo=UTC)
    cycle_time = datetime(2026, 9, 22, 1, tzinfo=UTC)
    db_session.add_all(
        [
            KAssetAutomationCycleEvent(
                owner_user_id=user.id,
                observed_at=cycle_time - timedelta(hours=1),
                finished_at=cycle_time - timedelta(hours=1),
                status="completed",
                candidate_count=8,
                recommendation_count=2,
            ),
            KAssetAutomationCycleEvent(
                owner_user_id=user.id,
                observed_at=cycle_time,
                finished_at=cycle_time + timedelta(seconds=18),
                status="skipped",
                skipped_reason="no_regular_market_open",
                candidate_count=2,
                recommendation_count=0,
            ),
            KAssetAutomationCycleEvent(
                owner_user_id=other_user.id,
                observed_at=cycle_time + timedelta(hours=1),
                finished_at=cycle_time + timedelta(hours=1),
                status="failed",
                candidate_count=99,
                recommendation_count=0,
            ),
        ]
    )
    await db_session.flush()
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=AITradingLimits(),
        usage=AITradingUsage(),
        usage_by_currency={"KRW": AITradingUsage(), "USD": AITradingUsage()},
        kill_switch=False,
        updated_at=settings_time,
    )
    get_snapshot = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(
        "app.extensions.kasset.api.router.AITradingPolicyService.get_snapshot",
        get_snapshot,
    )
    try:
        response = await ai_trading_state(
            SimpleNamespace(user=user),
            db_session,  # type: ignore[arg-type]
        )
        payload = response.model_dump(mode="json", by_alias=True)
        assert payload["updatedAt"] == "2026-09-01T00:00:00Z"
        assert payload["observedAt"].endswith("Z")
        assert payload["latestAutomationCycle"] == {
            "observedAt": "2026-09-22T01:00:00Z",
            "finishedAt": "2026-09-22T01:00:18Z",
            "status": "skipped",
            "skippedReason": "no_regular_market_open",
            "candidateCount": 2,
            "recommendationCount": 0,
        }
        get_snapshot.assert_awaited_once()
    finally:
        await db_session.rollback()


@pytest.mark.asyncio
async def test_trading_state_missing_cycle_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.extensions.kasset.api.router.AITradingPolicyService.get_snapshot",
        AsyncMock(
            return_value=AITradingSnapshot(
                mode=OperatingMode.APPROVAL,
                limits=AITradingLimits(),
                usage=AITradingUsage(),
                usage_by_currency={"KRW": AITradingUsage(), "USD": AITradingUsage()},
                kill_switch=False,
                updated_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        ),
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    response = await ai_trading_state(
        SimpleNamespace(user=SimpleNamespace(id=102)),
        db,  # type: ignore[arg-type]
    )
    assert response.latest_automation_cycle is None
    db.scalar.assert_awaited_once()
