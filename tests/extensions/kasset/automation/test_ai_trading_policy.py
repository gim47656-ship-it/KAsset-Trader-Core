"""Focused persistence and priority contracts for AI PAPER trading policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    AITradingSnapshot,
    AITradingUsage,
    OperatingMode,
    _decode_setting,
)
from app.models.trading import User, UserRole
from app.models.user_settings import UserSetting

_NOW = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
_EXPECTED_PRIORITY = [
    "DAILY_MAX_LOSS",
    "BUDGET",
    "POSITION",
    "ORDER_COUNT",
    "AI",
    "DAILY_GOAL",
]


async def _owner(db_session: AsyncSession) -> tuple[int, str]:
    username = f"ai-policy-{uuid4().hex}"
    row = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("Ai-policy-secret-1!"),
        role=UserRole.trader,
        is_active=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row.id, username


async def _cleanup(db_session: AsyncSession, username: str) -> None:
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.username == username))
    await db_session.commit()


@pytest.mark.parametrize(
    (
        "risk_level",
        "target_rate",
        "loss_rate",
        "risk_rate",
        "allocation",
        "holdings",
        "buys",
        "sells",
        "orders",
        "max_custom_buys",
        "max_custom_sells",
        "reentry",
    ),
    [
        (1, "0.3", "0.5", "0.0025", "0.10", 3, 1, 1, 2, 6, 12, 1),
        (2, "0.5", "1.0", "0.005", "0.15", 4, 2, 1, 3, 8, 16, 1),
        (3, "0.8", "1.5", "0.0075", "0.20", 5, 3, 2, 5, 10, 20, 1),
        (4, "1.2", "2.5", "0.01", "0.25", 5, 5, 3, 8, 10, 20, 1),
        (5, "2.0", "4.0", "0.015", "0.30", 6, 8, 4, 12, 24, 30, 2),
    ],
)
def test_risk_level_uses_single_preset_for_all_hidden_limits(
    risk_level: int,
    target_rate: str,
    loss_rate: str,
    risk_rate: str,
    allocation: str,
    holdings: int,
    buys: int,
    sells: int,
    orders: int,
    max_custom_buys: int,
    max_custom_sells: int,
    reentry: int,
) -> None:
    limits = AITradingLimits(risk_level=risk_level)

    assert limits.daily_target_rate_pct == Decimal(target_rate)
    assert limits.max_daily_loss_rate_pct == Decimal(loss_rate)
    assert limits.risk_per_trade_rate == Decimal(risk_rate)
    assert limits.max_symbol_allocation == Decimal(allocation)
    assert limits.max_concurrent_holdings == holdings
    assert limits.max_buys_per_day == buys
    assert limits.max_sells_per_day == sells
    assert limits.max_orders_per_day == orders
    assert limits.max_custom_buys_per_day == max_custom_buys
    assert limits.max_custom_sells_per_day == max_custom_sells
    assert limits.max_custom_orders_per_day == max_custom_buys + max_custom_sells
    assert limits.same_symbol_reentry_limit == reentry
    assert limits.min_ai_confidence == Decimal("0.50")


def test_default_limits_are_stable_level_two() -> None:
    defaults = AITradingLimits()

    assert defaults.risk_level == 2
    assert defaults.daily_target_rate_pct == Decimal("0.5")
    assert defaults.max_daily_loss_rate_pct == Decimal("1.0")
    assert defaults.max_symbol_allocation == Decimal("0.15")
    assert defaults.risk_per_trade_rate == Decimal("0.005")
    assert defaults.max_buys_per_day == 2
    assert defaults.max_sells_per_day == 1
    assert defaults.max_orders_per_day == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("custom_max_buys_per_day", 0),
        ("custom_max_buys_per_day", 11),
        ("custom_max_buys_per_day", True),
        ("custom_max_sells_per_day", 0),
        ("custom_max_sells_per_day", 21),
        ("custom_max_sells_per_day", True),
    ],
)
def test_custom_daily_limits_reject_values_outside_server_hard_maximum(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        AITradingLimits(risk_level=4, **{field: value})  # type: ignore[arg-type]


def test_invalid_persisted_custom_daily_limit_fails_closed() -> None:
    with pytest.raises(
        ValueError,
        match="custom_max_sells_per_day must be between 1 and 20",
    ):
        _decode_setting(
            {
                "mode": "APPROVAL",
                "settings": {
                    "risk_level": 4,
                    "custom_max_sells_per_day": 21,
                },
            }
        )


@pytest.mark.asyncio
async def test_settings_round_trip_writes_only_canonical_fields(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    await runtime_state.update_policy(
        db_session,
        owner_id,
        max_symbol_ratio=Decimal("1"),
    )
    limits = AITradingLimits(
        risk_level=4,
        operating_budget=Decimal("9000000"),
        daily_target_rate_pct=Decimal("0.7"),
        max_daily_loss_rate_pct=Decimal("1.8"),
        custom_max_buys_per_day=7,
        custom_max_sells_per_day=18,
        kill_switch=False,
        currency="KRW",
    )
    try:
        saved = await service.put_snapshot(
            db_session,
            owner_id,
            mode=OperatingMode.AUTO_PAPER,
            limits=limits,
            now=_NOW,
        )
        loaded = await service.get_snapshot(
            db_session,
            owner_id,
            now=_NOW,
            execution_limit=0,
        )
        row = await db_session.scalar(
            select(UserSetting).where(
                UserSetting.user_id == owner_id,
                UserSetting.key == "kasset.ai_trading",
            )
        )
        state = await runtime_state.get(db_session, owner_id)

        assert saved.mode == loaded.mode == OperatingMode.AUTO_PAPER
        assert loaded.limits == limits
        assert loaded.limits.max_buys_per_day == 7
        assert loaded.limits.max_sells_per_day == 18
        assert loaded.limits.max_orders_per_day == 25
        assert Decimal(state.max_symbol_ratio) == Decimal("1")
        assert loaded.executions == ()
        assert row is not None
        assert row.value["settings"] == {
            "risk_level": 4,
            "operating_budget": "9000000",
            "daily_target_rate_pct": "0.7",
            "max_daily_loss_rate_pct": "1.8",
            "kill_switch": False,
            "currency": "KRW",
            "custom_max_buys_per_day": 7,
            "custom_max_sells_per_day": 18,
        }
    finally:
        await _cleanup(db_session, username)


@pytest.mark.asyncio
async def test_legacy_amounts_and_hidden_limits_migrate_on_next_put(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    db_session.add(
        UserSetting(
            user_id=owner_id,
            key="kasset.ai_trading",
            value={
                "mode": "AUTO_PAPER",
                "settings": {
                    "operating_budget": "9000000",
                    "conservative_daily_goal": "45000",
                    "daily_max_loss": "90000",
                    "max_buys_per_day": 2,
                    "max_orders_per_day": 3,
                    "max_symbol_allocation": "0.15",
                    "max_concurrent_holdings": 4,
                    "same_symbol_reentry_limit": 1,
                    "kill_switch": False,
                    "currency": "KRW",
                },
            },
        )
    )
    await db_session.commit()
    try:
        loaded = await service.get_snapshot(
            db_session,
            owner_id,
            now=_NOW,
            execution_limit=0,
        )

        assert loaded.limits.risk_level == 2
        assert loaded.limits.daily_target_rate_pct == Decimal("0.5")
        assert loaded.limits.max_daily_loss_rate_pct == Decimal("1")
        assert loaded.limits.max_symbol_allocation == Decimal("0.15")

        await service.put_snapshot(
            db_session,
            owner_id,
            mode=loaded.mode,
            limits=loaded.limits,
            now=_NOW,
        )
        row = await db_session.scalar(
            select(UserSetting).where(
                UserSetting.user_id == owner_id,
                UserSetting.key == "kasset.ai_trading",
            )
        )
        assert row is not None
        assert row.value["settings"] == {
            "risk_level": 2,
            "operating_budget": "9000000",
            "daily_target_rate_pct": "0.5",
            "max_daily_loss_rate_pct": "1",
            "kill_switch": False,
            "currency": "KRW",
        }
    finally:
        await _cleanup(db_session, username)


@pytest.mark.asyncio
async def test_hard_risk_order_is_fixed_and_daily_goal_never_forces_a_trade(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    try:
        limits = AITradingLimits(
            operating_budget=Decimal("1000000"),
            daily_target_rate_pct=Decimal("0.3"),
        )
        await service.put_snapshot(
            db_session,
            owner_id,
            mode=OperatingMode.APPROVAL,
            limits=limits,
            now=_NOW,
        )

        result = await service.evaluate_hard_risk(
            db_session,
            owner_id,
            action="BUY",
            market="KRX",
            symbol="005930",
            quantity=Decimal("1"),
            reference_price=Decimal("70000"),
            ai_confidence=Decimal("0.49"),
            now=_NOW,
        )

        assert [check.rule for check in result.checks] == _EXPECTED_PRIORITY
        assert result.passed is False
        assert (
            next(check for check in result.checks if check.rule == "AI").passed is False
        )
        goal = next(check for check in result.checks if check.rule == "DAILY_GOAL")
        assert goal.passed is True
        assert "referenceGoal=3000" in goal.detail
        assert "참고값" in goal.detail
    finally:
        await _cleanup(db_session, username)


@pytest.mark.asyncio
async def test_kill_switch_is_an_absolute_block_outside_ai_priority(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    try:
        await service.put_snapshot(
            db_session,
            owner_id,
            mode=OperatingMode.AUTO_PAPER,
            limits=replace(AITradingLimits(), kill_switch=True),
            now=_NOW,
        )

        result = await service.evaluate_hard_risk(
            db_session,
            owner_id,
            action="BUY",
            market="KRX",
            symbol="005930",
            quantity=Decimal("1"),
            reference_price=Decimal("70000"),
            ai_confidence=Decimal("0.90"),
            now=_NOW,
        )

        assert [check.rule for check in result.checks] == _EXPECTED_PRIORITY
        assert all(check.passed for check in result.checks)
        assert result.passed is False
        assert result.blocked_reason == "kill switch가 켜져 있습니다."
    finally:
        await _cleanup(db_session, username)


class _UsageDb:
    def __init__(self) -> None:
        self._values = iter((91, 10, 4, 6, 0, Decimal("0"), Decimal("0")))

    async def scalar(self, _statement: object) -> object:
        return next(self._values)


@pytest.mark.asyncio
async def test_usage_counts_buy_and_sell_orders_separately() -> None:
    usage = await AITradingPolicyService().usage(
        _UsageDb(),  # type: ignore[arg-type]
        101,
        limits=AITradingLimits(),
        now=_NOW,
    )

    assert usage.orders_today == 10
    assert usage.buys_today == 4
    assert usage.sells_today == 6


class _EmptyRiskDb:
    async def scalar(self, _statement: object) -> None:
        return None


@pytest.mark.asyncio
async def test_daily_loss_gate_uses_budget_derived_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = AITradingLimits(
        operating_budget=Decimal("2000000"),
        max_daily_loss_rate_pct=Decimal("1.5"),
    )
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=limits,
        usage=AITradingUsage(
            realized_pnl_today=Decimal("-30000"),
            realized_loss_today=Decimal("30000"),
        ),
        kill_switch=False,
        updated_at=_NOW,
    )
    service = AITradingPolicyService()
    monkeypatch.setattr(
        service,
        "get_snapshot",
        AsyncMock(return_value=snapshot),
    )

    result = await service.evaluate_hard_risk(
        _EmptyRiskDb(),  # type: ignore[arg-type]
        101,
        action="BUY",
        market="KRX",
        symbol="005930",
        quantity=Decimal("1"),
        reference_price=Decimal("70000"),
        ai_confidence=Decimal("0.90"),
        now=_NOW,
    )

    assert limits.max_daily_loss_amount == Decimal("30000")
    assert result.passed is False
    assert result.checks[0].rule == "DAILY_MAX_LOSS"
    assert result.checks[0].passed is False
    assert result.blocked_reason == "realizedLossToday=30000; limit=30000.0"


@pytest.mark.asyncio
async def test_daily_loss_gate_allows_risk_reducing_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = AITradingLimits(
        operating_budget=Decimal("2000000"),
        max_daily_loss_rate_pct=Decimal("1.5"),
    )
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=limits,
        usage=AITradingUsage(
            realized_pnl_today=Decimal("-30000"),
            realized_loss_today=Decimal("30000"),
        ),
        kill_switch=False,
        updated_at=_NOW,
    )
    service = AITradingPolicyService()
    monkeypatch.setattr(
        service,
        "get_snapshot",
        AsyncMock(return_value=snapshot),
    )

    class _SellRiskDb:
        def __init__(self) -> None:
            self.values = iter(
                (
                    1,
                    SimpleNamespace(
                        total_invested=Decimal("70000"),
                        quantity=Decimal("1"),
                    ),
                    0,
                )
            )

        async def scalar(self, _statement: object) -> object:
            return next(self.values)

    result = await service.evaluate_hard_risk(
        _SellRiskDb(),  # type: ignore[arg-type]
        101,
        action="SELL",
        market="KRX",
        symbol="005930",
        quantity=Decimal("1"),
        reference_price=Decimal("70000"),
        ai_confidence=Decimal("0.90"),
        now=_NOW,
    )

    daily_loss = next(
        check for check in result.checks if check.rule == "DAILY_MAX_LOSS"
    )
    assert daily_loss.passed is True
    assert result.passed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "usage", "expected"),
    [
        (
            "BUY",
            AITradingUsage(buys_today=2, sells_today=0, orders_today=2),
            False,
        ),
        (
            "SELL",
            AITradingUsage(buys_today=2, sells_today=0, orders_today=2),
            True,
        ),
        (
            "SELL",
            AITradingUsage(buys_today=0, sells_today=1, orders_today=1),
            False,
        ),
        (
            "BUY",
            AITradingUsage(buys_today=1, sells_today=1, orders_today=3),
            False,
        ),
    ],
)
async def test_order_count_gate_separates_buy_sell_and_total_limits(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    usage: AITradingUsage,
    expected: bool,
) -> None:
    limits = AITradingLimits(risk_level=2)
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=limits,
        usage=usage,
        kill_switch=False,
        updated_at=_NOW,
    )
    service = AITradingPolicyService()
    monkeypatch.setattr(
        service,
        "get_snapshot",
        AsyncMock(return_value=snapshot),
    )

    result = await service.evaluate_hard_risk(
        _EmptyRiskDb(),  # type: ignore[arg-type]
        101,
        action=action,
        market="KRX",
        symbol="005930",
        quantity=Decimal("1"),
        reference_price=Decimal("70000"),
        ai_confidence=Decimal("0.90"),
        now=_NOW,
    )

    order_count = next(check for check in result.checks if check.rule == "ORDER_COUNT")
    assert order_count.passed is expected
    assert "buysToday=" in order_count.detail
    assert "sellsToday=" in order_count.detail
    assert "hardMaxBuys=8" in order_count.detail
    assert "hardMaxSells=16" in order_count.detail
