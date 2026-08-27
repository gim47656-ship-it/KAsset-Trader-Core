from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import paper_account_adapter
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.paper_schemas import (
    AmendRequest,
    OrderRequest,
    RiskAssessment,
)
from app.extensions.kasset.api.runtime_state import runtime_state


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


def _quote() -> SimpleNamespace:
    return SimpleNamespace(
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        price="70000",
    )


@pytest.mark.asyncio
async def test_paper_market_order_reaches_fill_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSession()
    monkeypatch.setattr(paper_orders, "_by_client_id", AsyncMock(return_value=None))
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

    async def fill(_db: object, order: object, price: Decimal) -> None:
        order.status = "FILLED"
        order.average_fill_price = price

    monkeypatch.setattr(paper_orders, "_fill", fill)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="filled"))

    envelope, replay = await paper_orders.submit(db, _request())  # type: ignore[arg-type]

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
    monkeypatch.setattr(paper_orders, "_by_client_id", AsyncMock(return_value=None))
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
    existing = SimpleNamespace(client_order_id="client-order-1", status="FILLED")
    monkeypatch.setattr(paper_orders, "_by_client_id", AsyncMock(return_value=existing))
    allowed = AsyncMock()
    monkeypatch.setattr(runtime_state, "assert_order_allowed", allowed)
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="same-order"))

    envelope, replay = await paper_orders.submit(db, _request())  # type: ignore[arg-type]

    assert envelope == "same-order"
    assert replay is True
    assert db.added == []
    allowed.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_cancel_and_kill_switch_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_db = FakeSession()
    open_order = SimpleNamespace(status="OPEN")
    monkeypatch.setattr(paper_orders, "get", AsyncMock(return_value=open_order))
    monkeypatch.setattr(paper_orders, "envelope", AsyncMock(return_value="cancelled"))

    cancelled = await paper_orders.cancel(cancel_db, "order-1")  # type: ignore[arg-type]

    assert cancelled == "cancelled"
    assert open_order.status == "CANCELLED"
    assert cancel_db.commit_count == 1

    submit_db = FakeSession()
    monkeypatch.setattr(paper_orders, "_by_client_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        runtime_state,
        "assert_order_allowed",
        AsyncMock(
            side_effect=MobileApiError(409, "KILL_SWITCH_ON", "거래 중지 상태입니다.")
        ),
    )

    with pytest.raises(MobileApiError) as exc_info:
        await paper_orders.submit(submit_db, _request())  # type: ignore[arg-type]

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
            "order-1",
            AmendRequest(quantity="6", limitPrice="80000"),
        )

    assert order.quantity == Decimal("6")
    assert order.limit_price == Decimal("80000")
    assert db.commit_count == 1
    fill.assert_awaited_once_with(
        db,
        order,
        Decimal("70000"),
        fill_quantity=Decimal("4"),
    )
