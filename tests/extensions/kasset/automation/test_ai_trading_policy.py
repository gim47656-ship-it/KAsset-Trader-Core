"""Focused persistence and priority contracts for AI PAPER trading policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    OperatingMode,
)
from app.models.trading import User, UserRole

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


@pytest.mark.asyncio
async def test_settings_round_trip_as_paper_only_owner_policy(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    limits = AITradingLimits(
        operating_budget=Decimal("9000000"),
        conservative_daily_goal=Decimal("90000"),
        daily_max_loss=Decimal("450000"),
        max_buys_per_day=2,
        max_orders_per_day=4,
        max_symbol_allocation=Decimal("0.15"),
        max_concurrent_holdings=4,
        same_symbol_reentry_limit=1,
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

        assert saved.mode == loaded.mode == OperatingMode.AUTO_PAPER
        assert loaded.limits == limits
        assert loaded.limits.kill_switch is False
        assert loaded.executions == ()
    finally:
        await _cleanup(db_session, username)


@pytest.mark.asyncio
async def test_hard_risk_order_is_fixed_and_daily_goal_never_forces_a_trade(
    db_session: AsyncSession,
) -> None:
    owner_id, username = await _owner(db_session)
    service = AITradingPolicyService()
    try:
        await service.put_snapshot(
            db_session,
            owner_id,
            mode=OperatingMode.APPROVAL,
            limits=replace(
                AITradingLimits(),
                conservative_daily_goal=Decimal("999999999"),
            ),
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
