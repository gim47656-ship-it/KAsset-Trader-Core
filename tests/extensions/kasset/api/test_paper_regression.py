import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import paper_account_adapter
from app.extensions.kasset.api.paper_orders import PaperOrderFacade, paper_orders
from app.extensions.kasset.api.paper_schemas import (
    AmendRequest,
    OrderRequest,
    Quote,
    RiskAssessment,
)
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.models import AndroidPaperAccount, AndroidPaperOrder
from app.models.paper_trading import PaperAccount, PaperTrade
from app.models.trading import InstrumentType, User
from app.services.paper_trading_service import PaperTradingService


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def refresh(self, value: object) -> None:
        return None


def _request(
    *, client_order_id: str = "client-order-1", order_type: str = "MARKET"
) -> OrderRequest:
    return OrderRequest(
        clientOrderId=client_order_id,
        broker="PAPER",
        accountId=None,
        market="KRX",
        symbol="005930",
        side="BUY",
        orderType=order_type,
        quantity="2",
        limitPrice="60000" if order_type == "LIMIT" else None,
    )


def _approved() -> RiskAssessment:
    return RiskAssessment(
        decision="APPROVED",
        reasons=[],
        estimatedAmount="140000",
        estimatedFee="0",
        referencePrice="70000",
        currency="KRW",
    )


def _account() -> SimpleNamespace:
    return SimpleNamespace(id="paper-account", cash_krw=Decimal("10000000"))


def _quote() -> Quote:
    return Quote(
        broker="PAPER",
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        price="70000",
        as_of="2026-08-28T00:00:00Z",
        source="TEST",
    )


@pytest.mark.asyncio
async def test_paper_market_order_reaches_fill_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    monkeypatch.setattr(
        paper_orders,
        "get_by_client_order_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        runtime_state, "assert_order_allowed", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(paper_orders, "preview", AsyncMock(return_value=_approved()))
    monkeypatch.setattr(
        paper_account_adapter, "resolve_account", AsyncMock(return_value=_account())
    )
    monkeypatch.setattr(
        paper_account_adapter, "quote", AsyncMock(return_value=_quote())
    )

    async def fill(
        _db: object, owner_user_id: int, order: object, price: Decimal
    ) -> None:
        assert owner_user_id == 101
        order.status = "FILLED"
        order.average_fill_price = price

    monkeypatch.setattr(paper_orders, "_fill", fill)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="filled"))

    envelope, replay = await paper_orders.submit(  # type: ignore[arg-type]
        db, 101, _request()
    )

    assert envelope == "filled"
    assert replay is False
    assert len(db.added) == 1
    assert db.added[0].status == "FILLED"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_paper_non_crossing_limit_order_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    monkeypatch.setattr(
        paper_orders,
        "get_by_client_order_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        runtime_state, "assert_order_allowed", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(paper_orders, "preview", AsyncMock(return_value=_approved()))
    monkeypatch.setattr(
        paper_account_adapter, "resolve_account", AsyncMock(return_value=_account())
    )
    monkeypatch.setattr(
        paper_account_adapter, "quote", AsyncMock(return_value=_quote())
    )
    fill = AsyncMock()
    monkeypatch.setattr(paper_orders, "_fill", fill)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="open"))

    envelope, replay = await paper_orders.submit(
        db,
        101,
        _request(order_type="LIMIT"),  # type: ignore[arg-type]
    )

    assert envelope == "open"
    assert replay is False
    assert db.added[0].status == "OPEN"
    assert db.commit_count == 2
    fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_client_order_id_replays_without_second_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    existing = SimpleNamespace(
        owner_user_id=101,
        client_order_id="client-order-1",
        status="FILLED",
    )
    monkeypatch.setattr(
        paper_orders,
        "get_by_client_order_id",
        AsyncMock(return_value=existing),
    )
    allowed = AsyncMock()
    monkeypatch.setattr(runtime_state, "assert_order_allowed", allowed)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="same-order"))

    envelope, replay = await paper_orders.submit(  # type: ignore[arg-type]
        db, 101, _request()
    )

    assert envelope == "same-order"
    assert replay is True
    assert db.added == []
    allowed.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_order_with_correlation_trade_repairs_fill_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    order = SimpleNamespace(
        id="pending-order",
        owner_user_id=101,
        paper_account_id=11,
        market="KRX",
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("2"),
        limit_price=None,
        status="PENDING",
        filled_quantity=Decimal("0"),
        average_fill_price=None,
        paper_trade_id=None,
        reject_reason=None,
    )
    trade = SimpleNamespace(
        id=77,
        quantity=Decimal("2"),
        price=Decimal("70000"),
    )
    monkeypatch.setattr(
        paper_orders,
        "_trade_by_correlation",
        AsyncMock(return_value=trade),
    )
    fill = AsyncMock()
    monkeypatch.setattr(paper_orders, "_fill", fill)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="repaired"))

    result = await paper_orders.reconcile(  # type: ignore[arg-type]
        db,
        101,
        order,
    )

    assert result == "repaired"
    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("2")
    assert order.average_fill_price == Decimal("70000")
    assert order.paper_trade_id == 77
    assert db.commit_count == 1
    fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_pending_reconciliation_resumes_fill_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    order = SimpleNamespace(
        id="pending-race",
        owner_user_id=101,
        paper_account_id=11,
        market="KRX",
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("2"),
        limit_price=None,
        status="PENDING",
        filled_quantity=Decimal("0"),
        average_fill_price=None,
        paper_trade_id=None,
        reject_reason=None,
    )
    monkeypatch.setattr(
        paper_orders,
        "_trade_by_correlation",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        paper_account_adapter,
        "quote",
        AsyncMock(return_value=_quote()),
    )
    fill_calls = 0

    async def fill_once(
        _db: object,
        owner_user_id: int,
        pending: SimpleNamespace,
        market_price: Decimal,
    ) -> SimpleNamespace:
        nonlocal fill_calls
        assert owner_user_id == 101
        assert market_price == Decimal("70000")
        fill_calls += 1
        await asyncio.sleep(0)
        pending.status = "FILLED"
        return pending

    monkeypatch.setattr(paper_orders, "_fill", fill_once)
    monkeypatch.setattr(
        paper_orders,
        "envelope",
        AsyncMock(return_value="filled"),
    )

    first, second = await asyncio.gather(
        paper_orders.reconcile(db, 101, order),  # type: ignore[arg-type]
        paper_orders.reconcile(db, 101, order),  # type: ignore[arg-type]
    )

    assert first == second == "filled"
    assert fill_calls == 1
    assert order.status == "FILLED"


@pytest.mark.asyncio
async def test_paper_cancel_and_kill_switch_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_db = FakeSession()
    open_order = SimpleNamespace(status="OPEN")
    monkeypatch.setattr(paper_orders, "get", AsyncMock(return_value=open_order))
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="cancelled"))

    cancelled = await paper_orders.cancel(  # type: ignore[arg-type]
        cancel_db, 101, "order-1"
    )

    assert cancelled == "cancelled"
    assert open_order.status == "CANCELLED"
    assert cancel_db.commit_count == 1

    submit_db = FakeSession()
    monkeypatch.setattr(
        paper_orders,
        "get_by_client_order_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        runtime_state,
        "assert_order_allowed",
        AsyncMock(
            side_effect=MobileApiError(409, "KILL_SWITCH_ON", "거래 중지 상태입니다.")
        ),
    )

    with pytest.raises(MobileApiError) as exc_info:
        await paper_orders.submit(  # type: ignore[arg-type]
            submit_db, 101, _request()
        )

    assert exc_info.value.code == "KILL_SWITCH_ON"
    assert submit_db.added == []


@pytest.mark.asyncio
async def test_paper_crossing_amend_keeps_total_quantity_when_fill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    order = SimpleNamespace(
        client_order_id="client-order-1",
        order_type="LIMIT",
        status="PARTIALLY_FILLED",
        market="KRX",
        symbol="005930",
        side="BUY",
        quantity=Decimal("5"),
        filled_quantity=Decimal("2"),
        limit_price=Decimal("60000"),
    )
    monkeypatch.setattr(paper_orders, "get", AsyncMock(return_value=order))
    monkeypatch.setattr(
        paper_account_adapter,
        "resolve_account",
        AsyncMock(return_value=_account()),
    )
    monkeypatch.setattr(runtime_state, "assert_order_allowed", AsyncMock())
    monkeypatch.setattr(paper_orders, "preview", AsyncMock(return_value=_approved()))
    monkeypatch.setattr(
        paper_account_adapter, "quote", AsyncMock(return_value=_quote())
    )
    fill = AsyncMock(
        side_effect=MobileApiError(
            409,
            "BROKER_ERROR",
            "PAPER 주문을 실행하지 못했습니다.",
        )
    )
    monkeypatch.setattr(paper_orders, "_fill", fill)

    with pytest.raises(MobileApiError):
        await paper_orders.amend(
            db,  # type: ignore[arg-type]
            101,
            "order-1",
            AmendRequest(quantity="6", limitPrice="80000"),
        )

    assert order.quantity == Decimal("6")
    assert order.limit_price == Decimal("80000")
    assert db.commit_count == 1
    fill.assert_awaited_once_with(
        db,
        101,
        order,
        Decimal("70000"),
        fill_quantity=Decimal("4"),
    )


@pytest.mark.asyncio
async def test_old_and_new_worker_race_converges_to_one_order_and_trade(
    db_session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = user.id
    order_id = f"paper-race-{uuid4().hex}"
    client_order_id = f"ai-rec:race-{uuid4().hex}"
    account = PaperAccount(
        name=f"paper-reconcile-race-{uuid4().hex}",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    account_id = account.id
    order = AndroidPaperOrder(
        id=order_id,
        owner_user_id=owner_id,
        client_order_id=client_order_id,
        paper_account_id=account_id,
        broker_order_id=f"PAPER-RACE-{uuid4().hex}",
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
        status="PENDING",
        filled_quantity=Decimal("0"),
    )
    db_session.add_all(
        [
            AndroidPaperAccount(
                owner_user_id=owner_id,
                paper_account_id=account_id,
            ),
            order,
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(
        paper_account_adapter,
        "quote",
        AsyncMock(return_value=_quote()),
    )

    async def insert_trade_once(
        service: PaperTradingService,
        *,
        account_id: int,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        amount: Decimal | None = None,
        reason: str = "",
        correlation_id: str | None = None,
    ) -> dict[str, object]:
        del price, amount
        service.db.add(
            PaperTrade(
                account_id=account_id,
                symbol=symbol,
                instrument_type=InstrumentType.equity_kr,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=Decimal("70000"),
                total_amount=Decimal("70000"),
                fee=Decimal("0"),
                currency="KRW",
                reason=reason,
                correlation_id=correlation_id,
            )
        )
        await asyncio.sleep(0)
        await service.db.commit()
        return {"success": True}

    monkeypatch.setattr(PaperTradingService, "execute_order", insert_trade_once)

    async def reconcile_as_worker() -> str:
        async with AsyncSessionLocal() as session:
            facade = PaperOrderFacade()
            pending = await facade.get_by_client_order_id(
                session,
                owner_id,
                client_order_id,
            )
            assert pending is not None
            envelope = await facade.reconcile(session, owner_id, pending)
            return envelope.order.status

    try:
        statuses = await asyncio.gather(
            reconcile_as_worker(),
            reconcile_as_worker(),
        )
        assert statuses == ["FILLED", "FILLED"]
        db_session.expire_all()
        order_count = await db_session.scalar(
            select(func.count())
            .select_from(AndroidPaperOrder)
            .where(
                AndroidPaperOrder.owner_user_id == owner_id,
                AndroidPaperOrder.client_order_id == client_order_id,
            )
        )
        trade_count = await db_session.scalar(
            select(func.count())
            .select_from(PaperTrade)
            .where(
                PaperTrade.account_id == account_id,
                PaperTrade.correlation_id == order_id,
            )
        )
        repaired = await db_session.get(AndroidPaperOrder, order_id)
        assert order_count == 1
        assert trade_count == 1
        assert repaired is not None
        assert repaired.status == "FILLED"
        assert repaired.filled_quantity == Decimal("1")
        assert repaired.paper_trade_id is not None
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(AndroidPaperOrder).where(AndroidPaperOrder.id == order_id)
        )
        await db_session.execute(
            delete(AndroidPaperAccount).where(
                AndroidPaperAccount.owner_user_id == owner_id,
                AndroidPaperAccount.paper_account_id == account_id,
            )
        )
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.id == account_id)
        )
        await db_session.commit()
