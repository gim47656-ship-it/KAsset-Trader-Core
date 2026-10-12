"""같은 종목 반복 매수·같은 발행사(보통주·우선주) 쏠림을 막는 Hard Risk 계약.

2026-10-08 PAPER 계좌는 보유 중인 051915를 10분마다 다시 사고, 같은 날 청산 9분
뒤 재진입했으며, 같은 발행사 009150·009155를 함께 들고 있었다. BUY는 위험등급과
무관하게 같은 발행사 보유 중이거나 오늘 그 발행사 BUY 주문이 이미 있으면 막힌다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    AITradingSnapshot,
    AITradingUsage,
    HardRiskResult,
    OperatingMode,
    issuer_group_key,
)
from app.extensions.kasset.models import AndroidPaperAccount, AndroidPaperOrder
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.trading import InstrumentType, User, UserRole

# 2026-10-12 10:00 KST. KRW 거래일은 2026-10-11 15:00 UTC에 시작한다.
_NOW = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
_ISSUER_RULES = {"SAME_ISSUER_HELD", "SAME_ISSUER_DAILY_BUY_LIMIT"}


@dataclass(frozen=True, slots=True)
class _Account:
    owner_user_id: int
    paper_account_id: int


@pytest_asyncio.fixture
async def account(db_session: AsyncSession) -> AsyncIterator[_Account]:
    username = f"issuer-limit-{uuid4().hex}"
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("Issuer-limit-secret-1!"),
        role=UserRole.trader,
        is_active=True,
    )
    paper = PaperAccount(
        name=f"KAsset issuer limit {uuid4().hex}",
        initial_capital=Decimal("100000000"),
        cash_krw=Decimal("100000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )
    db_session.add_all([user, paper])
    await db_session.flush()
    owner_user_id = user.id
    paper_account_id = paper.id
    db_session.add(
        AndroidPaperAccount(
            owner_user_id=owner_user_id,
            paper_account_id=paper_account_id,
        )
    )
    await db_session.commit()
    try:
        yield _Account(owner_user_id, paper_account_id)
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(AndroidPaperOrder).where(
                AndroidPaperOrder.owner_user_id == owner_user_id
            )
        )
        await db_session.execute(
            delete(PaperPosition).where(PaperPosition.account_id == paper_account_id)
        )
        await db_session.execute(
            delete(AndroidPaperAccount).where(
                AndroidPaperAccount.owner_user_id == owner_user_id
            )
        )
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.id == paper_account_id)
        )
        await db_session.execute(delete(User).where(User.username == username))
        await db_session.commit()


async def _hold(
    db_session: AsyncSession, account: _Account, symbol: str, quantity: str = "8"
) -> None:
    db_session.add(
        PaperPosition(
            account_id=account.paper_account_id,
            symbol=symbol,
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal(quantity),
            avg_price=Decimal("1000"),
            total_invested=Decimal(quantity) * Decimal("1000"),
        )
    )
    await db_session.commit()


async def _order(
    db_session: AsyncSession,
    account: _Account,
    symbol: str,
    *,
    side: str,
    created_at: datetime,
    status: str = "FILLED",
) -> None:
    db_session.add(
        AndroidPaperOrder(
            id=str(uuid4()),
            owner_user_id=account.owner_user_id,
            client_order_id=f"ai-rec:{uuid4().hex}",
            paper_account_id=account.paper_account_id,
            broker_order_id=f"PAPER-{uuid4().hex}",
            market="KRX",
            symbol=symbol,
            name=None,
            currency="KRW",
            side=side,
            order_type="MARKET",
            quantity=Decimal("8"),
            status=status,
            filled_quantity=Decimal("8") if status == "FILLED" else Decimal("0"),
            average_fill_price=Decimal("1000") if status == "FILLED" else None,
            created_at=created_at,
        )
    )
    await db_session.commit()


async def _evaluate(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str,
    action: str = "BUY",
    risk_level: int = 5,
) -> HardRiskResult:
    # 3~5단계는 주문 횟수·동시보유·같은 종목 재진입 한도가 없다. 실패가 새 발행사
    # 관문에서만 나오는지 보려고 기본은 가장 느슨한 5단계다.
    usage = AITradingUsage()
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=AITradingLimits(
            risk_level=risk_level, operating_budget_krw=Decimal("100000000")
        ),
        usage=usage,
        usage_by_currency={"KRW": usage, "USD": AITradingUsage()},
        kill_switch=False,
        updated_at=_NOW,
    )
    service = AITradingPolicyService()
    monkeypatch.setattr(service, "get_snapshot", AsyncMock(return_value=snapshot))
    return await service.evaluate_hard_risk(
        db_session,
        account.owner_user_id,
        action=action,
        market="KRX",
        symbol=symbol,
        quantity=Decimal("1"),
        reference_price=Decimal("1000"),
        ai_confidence=Decimal("0.9"),
        now=_NOW,
    )


def _failed(result: HardRiskResult) -> set[str]:
    return {check.rule for check in result.checks if not check.passed}


def _check(result: HardRiskResult, rule: str):
    return next(check for check in result.checks if check.rule == rule)


@pytest.mark.parametrize(
    ("market", "symbol", "expected"),
    [
        ("KRX", "009150", "00915"),
        ("KRX", "009155", "00915"),
        ("KRX", "051910", "05191"),
        ("KRX", "051915", "05191"),
        ("US", "AAPL", "AAPL"),
    ],
)
def test_issuer_group_key_groups_common_and_preferred_shares(
    market: str, symbol: str, expected: str
) -> None:
    assert issuer_group_key(market, symbol) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_level", [1, 5])
async def test_buy_preferred_while_holding_common_of_same_issuer_is_blocked(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
    risk_level: int,
) -> None:
    # (a) 009150 보유 중 009155 BUY.
    await _hold(db_session, account, "009150")

    result = await _evaluate(
        db_session, account, monkeypatch, symbol="009155", risk_level=risk_level
    )

    held = _check(result, "SAME_ISSUER_HELD")
    assert result.passed is False
    assert held.passed is False
    assert "issuer=00915" in held.detail
    assert "issuerHeld=1/0" in held.detail
    assert _check(result, "SAME_ISSUER_DAILY_BUY_LIMIT").passed is True
    assert result.blocked_reason == held.detail


@pytest.mark.asyncio
async def test_adding_to_a_held_symbol_is_blocked(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (b) 051915 보유 중 051915 추가 BUY(물타기). 오늘 BUY 기록이 없어도 막힌다.
    await _hold(db_session, account, "051915")

    result = await _evaluate(db_session, account, monkeypatch, symbol="051915")

    assert _failed(result) == {"SAME_ISSUER_HELD"}
    assert "issuer=05191" in _check(result, "SAME_ISSUER_HELD").detail


@pytest.mark.asyncio
async def test_same_day_reentry_after_full_exit_is_blocked(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (c) 오늘 051915를 사고 전량 판 뒤(보유 없음) 다시 BUY.
    await _order(
        db_session,
        account,
        "051915",
        side="BUY",
        created_at=_NOW - timedelta(minutes=40),
    )
    await _order(
        db_session,
        account,
        "051915",
        side="SELL",
        created_at=_NOW - timedelta(minutes=9),
    )

    result = await _evaluate(db_session, account, monkeypatch, symbol="051915")

    limit = _check(result, "SAME_ISSUER_DAILY_BUY_LIMIT")
    assert _failed(result) == {"SAME_ISSUER_DAILY_BUY_LIMIT"}
    assert "sameIssuerBuys=1/1" in limit.detail
    assert result.blocked_reason == limit.detail


@pytest.mark.asyncio
async def test_unfilled_buy_order_of_same_issuer_today_blocks_a_second_buy(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hard Risk는 이번 BUY의 주문 행을 만들기 전에 판정한다. 첫 BUY는 통과하고,
    # 그 주문 행이 생긴 뒤(아직 미체결이라 보유 없음) 같은 발행사 BUY는 막힌다.
    first = await _evaluate(db_session, account, monkeypatch, symbol="051910")
    assert first.passed is True

    await _order(
        db_session,
        account,
        "051910",
        side="BUY",
        created_at=_NOW - timedelta(minutes=1),
        status="PENDING",
    )

    second = await _evaluate(db_session, account, monkeypatch, symbol="051915")
    assert _failed(second) == {"SAME_ISSUER_DAILY_BUY_LIMIT"}


@pytest.mark.asyncio
async def test_first_buy_of_the_day_without_holding_passes(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (d) 보유 없음·오늘 BUY 없음. 다른 발행사 보유와 어제 같은 종목 BUY는 세지 않는다.
    await _hold(db_session, account, "005930")
    await _order(
        db_session,
        account,
        "051915",
        side="BUY",
        created_at=_NOW - timedelta(days=1),
    )

    result = await _evaluate(db_session, account, monkeypatch, symbol="051915")

    assert result.passed is True
    assert _failed(result) == set()
    assert "issuerHeld=0/0" in _check(result, "SAME_ISSUER_HELD").detail
    limit = _check(result, "SAME_ISSUER_DAILY_BUY_LIMIT")
    assert "sameIssuerBuys=0/1" in limit.detail


@pytest.mark.asyncio
async def test_sell_of_held_symbol_ignores_issuer_limits(
    db_session: AsyncSession,
    account: _Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _hold(db_session, account, "051915")
    await _order(
        db_session,
        account,
        "051915",
        side="BUY",
        created_at=_NOW - timedelta(minutes=30),
    )

    result = await _evaluate(
        db_session, account, monkeypatch, symbol="051915", action="SELL"
    )

    assert result.passed is True
    assert all(_check(result, rule).passed for rule in _ISSUER_RULES)
