import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api import krx_quotes
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
from app.models.paper_trading import PaperAccount, PaperPosition, PaperTrade
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


def _quote(*, price: str = "70000") -> Quote:
    return Quote(
        broker="PAPER",
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        price=price,
        as_of="2026-08-28T00:00:00Z",
        source="TEST",
    )


class PreviewRows:
    def __init__(self, rows: list[tuple[str, Decimal, Decimal]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, Decimal, Decimal]]:
        return self._rows


class PreviewSession:
    def __init__(self, positions: list[PaperPosition] | None = None) -> None:
        self.positions = positions or []
        self.last_statement: object | None = None

    async def execute(self, statement: object) -> PreviewRows:
        self.last_statement = statement
        return PreviewRows(
            [
                (position.symbol, position.quantity, position.avg_price)
                for position in self.positions
            ]
        )


class ScalarRows:
    def __init__(self, values: list[Decimal]) -> None:
        self._values = values

    def scalars(self) -> "ScalarRows":
        return self

    def all(self) -> list[Decimal]:
        return self._values


class BalanceSession:
    def __init__(self, realized: list[Decimal]) -> None:
        self.realized = realized
        self.last_statement: object | None = None

    async def execute(self, statement: object) -> ScalarRows:
        self.last_statement = statement
        return ScalarRows(self.realized)


class EmptyAccountRows:
    def scalar_one_or_none(self) -> None:
        return None


class NewAccountSession(FakeSession):
    async def execute(self, _statement: object) -> EmptyAccountRows:
        return EmptyAccountRows()

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, PaperAccount) and value.id is None:
                value.id = 1


def _risk_request(
    *,
    side: str = "BUY",
    market: str = "KRX",
    symbol: str = "005930",
    quantity: str = "1",
) -> OrderRequest:
    return OrderRequest(
        clientOrderId="risk-preview",
        broker="PAPER",
        accountId=None,
        market=market,
        symbol=symbol,
        side=side,
        orderType="MARKET",
        quantity=quantity,
        limitPrice=None,
    )


def _risk_quote(
    *,
    price: str = "100",
    market: str = "KRX",
    symbol: str = "005930",
    currency: str = "KRW",
) -> Quote:
    return Quote(
        broker="PAPER",
        market=market,
        symbol=symbol,
        name=symbol,
        currency=currency,
        price=price,
        as_of="2026-08-28T00:00:00Z",
        source="TEST",
    )


def _position(
    *,
    symbol: str,
    instrument_type: InstrumentType,
    quantity: str,
    avg_price: str,
) -> PaperPosition:
    cost_basis = Decimal(quantity) * Decimal(avg_price)
    return PaperPosition(
        account_id=1,
        symbol=symbol,
        instrument_type=instrument_type,
        quantity=Decimal(quantity),
        avg_price=Decimal(avg_price),
        total_invested=cost_basis,
    )


def _configure_preview(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account: object,
    quote: Quote,
    max_order_ratio: str,
    max_symbol_ratio: str,
) -> None:
    monkeypatch.setattr(settings, "TRADING_ENABLED", True)
    monkeypatch.setattr(
        paper_account_adapter, "resolve_account", AsyncMock(return_value=account)
    )
    monkeypatch.setattr(krx_quotes, "quote_for_market", AsyncMock(return_value=quote))
    monkeypatch.setattr(
        runtime_state,
        "get",
        AsyncMock(
            return_value=SimpleNamespace(
                kill_switch_enabled=False,
                max_order_ratio=Decimal(max_order_ratio),
                max_symbol_ratio=Decimal(max_symbol_ratio),
            )
        ),
    )
    monkeypatch.setattr(
        runtime_state,
        "get_global",
        AsyncMock(return_value=SimpleNamespace(kill_switch_enabled=False)),
    )


@pytest.mark.asyncio
async def test_new_android_paper_account_starts_with_parallel_usd_cash() -> None:
    db = NewAccountSession()
    account = await paper_account_adapter.default_account(
        db,
        owner_user_id=101,  # type: ignore[arg-type]
    )
    assert account.initial_capital == Decimal("10000000")
    assert account.initial_capital_usd == Decimal("10000")
    assert account.cash_krw == Decimal("10000000")
    assert account.cash_usd == Decimal("10000")
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_balance_keeps_usd_cash_separate_from_krw_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = BalanceSession([Decimal("1500")])
    account = SimpleNamespace(
        id=1, cash_krw=Decimal("10000000"), cash_usd=Decimal("10000")
    )
    monkeypatch.setattr(
        paper_account_adapter, "default_account", AsyncMock(return_value=account)
    )
    monkeypatch.setattr(
        PaperTradingService,
        "get_positions",
        AsyncMock(
            return_value=[
                {
                    "instrument_type": "equity_kr",
                    "evaluation_amount": Decimal("500000"),
                    "unrealized_pnl": Decimal("25000"),
                },
                {
                    "instrument_type": "equity_us",
                    "evaluation_amount": Decimal("3000"),
                    "unrealized_pnl": Decimal("200"),
                },
            ]
        ),
    )
    balance = await paper_account_adapter.balance(
        db,
        owner_user_id=101,  # type: ignore[arg-type]
    )
    assert balance.base_currency == "KRW"
    assert balance.evaluation_amount == "500000"
    assert balance.total_assets == "10500000"
    assert balance.unrealized_pnl == "25000"
    assert balance.realized_pnl == "1500"
    assert [(line.currency, line.cash) for line in balance.cash] == [
        ("KRW", "10000000"),
        ("USD", "10000"),
    ]
    assert db.last_statement is not None
    assert InstrumentType.equity_kr in db.last_statement.compile().params.values()


@pytest.mark.asyncio
async def test_balance_does_not_report_false_zero_when_krw_quote_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = BalanceSession([])
    account = SimpleNamespace(
        id=1, cash_krw=Decimal("10000000"), cash_usd=Decimal("10000")
    )
    monkeypatch.setattr(
        paper_account_adapter, "default_account", AsyncMock(return_value=account)
    )
    monkeypatch.setattr(
        PaperTradingService,
        "get_positions",
        AsyncMock(
            return_value=[
                {
                    "instrument_type": "equity_kr",
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                }
            ]
        ),
    )

    balance = await paper_account_adapter.balance(
        db,
        owner_user_id=101,  # type: ignore[arg-type]
    )

    assert balance.evaluation_amount is None
    assert balance.total_assets is None
    assert balance.unrealized_pnl is None


@pytest.mark.asyncio
async def test_buy_rejects_order_ratio_against_orderable_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession()
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(
            id=1, cash_krw=Decimal("1000"), cash_usd=Decimal("10000")
        ),
        quote=_risk_quote(),
        max_order_ratio="0.5",
        max_symbol_ratio="1",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(quantity="6"),  # type: ignore[arg-type]
    )
    assert [(reason.code, reason.message) for reason in risk.reasons] == [
        (
            "MAX_ORDER_RATIO",
            "한 주문 금액이 주문가능 현금 비율 한도를 초과했습니다.",
        )
    ]


@pytest.mark.asyncio
async def test_buy_rejects_projected_symbol_cost_basis_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession(
        [
            _position(
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                quantity="4",
                avg_price="100",
            )
        ]
    )
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(
            id=1, cash_krw=Decimal("1000"), cash_usd=Decimal("10000")
        ),
        quote=_risk_quote(),
        max_order_ratio="1",
        max_symbol_ratio="0.25",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(quantity="2"),  # type: ignore[arg-type]
    )
    assert [reason.code for reason in risk.reasons] == ["MAX_SYMBOL_RATIO"]


@pytest.mark.asyncio
async def test_buy_allows_both_concentration_ratios_at_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession(
        [
            _position(
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                quantity="4",
                avg_price="100",
            )
        ]
    )
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(
            id=1, cash_krw=Decimal("1000"), cash_usd=Decimal("10000")
        ),
        quote=_risk_quote(),
        max_order_ratio="1",
        max_symbol_ratio="1",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(quantity="5"),  # type: ignore[arg-type]
    )
    assert risk.decision == "APPROVED"
    assert risk.reasons == []


@pytest.mark.asyncio
async def test_ratios_at_one_keep_insufficient_cash_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession()
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(
            id=1, cash_krw=Decimal("100"), cash_usd=Decimal("10000")
        ),
        quote=_risk_quote(),
        max_order_ratio="1",
        max_symbol_ratio="1",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(),  # type: ignore[arg-type]
    )
    assert [reason.code for reason in risk.reasons] == ["INSUFFICIENT_CASH"]


@pytest.mark.asyncio
async def test_sell_ignores_buy_concentration_ratios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession()
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(id=1, cash_krw=Decimal("1"), cash_usd=Decimal("1")),
        quote=_risk_quote(),
        max_order_ratio="0.01",
        max_symbol_ratio="0.01",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(side="SELL", quantity="100"),  # type: ignore[arg-type]
    )
    assert risk.decision == "APPROVED"
    assert risk.reasons == []
    assert db.last_statement is None


@pytest.mark.asyncio
async def test_usd_symbol_ratio_excludes_krw_cash_and_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PreviewSession(
        [
            _position(
                symbol="AAPL",
                instrument_type=InstrumentType.equity_us,
                quantity="20",
                avg_price="100",
            )
        ]
    )
    _configure_preview(
        monkeypatch,
        account=SimpleNamespace(
            id=1,
            cash_krw=Decimal("1000000000"),
            cash_usd=Decimal("1000"),
        ),
        quote=_risk_quote(market="US", symbol="AAPL", currency="USD"),
        max_order_ratio="0.5",
        max_symbol_ratio="0.5",
    )
    risk = await PaperOrderFacade().preview(
        db,
        101,
        _risk_request(market="US", symbol="AAPL"),  # type: ignore[arg-type]
    )
    assert [reason.code for reason in risk.reasons] == ["MAX_SYMBOL_RATIO"]
    assert db.last_statement is not None
    assert InstrumentType.equity_us in db.last_statement.compile().params.values()


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
        resolved_market_price: Decimal | None = None,
        reason: str = "",
        correlation_id: str | None = None,
    ) -> dict[str, object]:
        del amount
        # MARKET은 기준가를 `resolved_market_price`로, LIMIT은 `price`로 받는다.
        assert price is None
        assert resolved_market_price == Decimal("70000")
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


@pytest.mark.asyncio
async def test_market_order_fills_at_submit_reference_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MARKET 체결가는 제출 시점 기준가여야 한다.

    회귀 방지(2026-08-30): `_fill()`이 MARKET에서 기준가를 버려 Core가
    `_fetch_current_price()`로 가격을 다시 만들었다. KRX 재조회 경로는 KIS
    전용이라 (1) KIS 403이면 토스가 정상이어도 체결 단계에서 주문이 실패하고,
    (2) KIS가 살아 있어도 리스크·교차 판정 가격과 체결가가 서로 다른 공급자에서
    나온 다른 값이 된다.

    세션 I/O만 가짜다. `submit -> preview -> _fill -> PaperTradingService.
    execute_order -> preview_order -> PaperTrade`는 실제 코드가 돈다.
    """
    monkeypatch.setattr(settings, "TRADING_ENABLED", True)
    db = FakeSession()
    owner_id = 101
    reference_price = Decimal("71234")
    account = PaperAccount(
        id=1,
        name="paper-refprice",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )

    async def only_quote_source(_db: object, *, market: str, symbol: str) -> Quote:
        # 이 경로의 유일한 시세 공급자. 기준가는 여기서만 나온다.
        assert (market, symbol) == ("KRX", "005930")
        return _quote(price=format(reference_price, "f"))

    async def forbidden_refetch(*_args: object, **_kwargs: object) -> Decimal:
        raise AssertionError(
            "PaperTradingService._fetch_current_price must not run for a KAsset "
            "PAPER MARKET fill: the submit-time reference price is authoritative"
        )

    async def trade_by_correlation(
        _db: object, _account_id: int, correlation_id: str
    ) -> PaperTrade | None:
        return next(
            (
                added
                for added in db.added
                if isinstance(added, PaperTrade)
                and added.correlation_id == correlation_id
            ),
            None,
        )

    monkeypatch.setattr(krx_quotes, "quote_for_market", only_quote_source)
    monkeypatch.setattr(PaperTradingService, "_fetch_current_price", forbidden_refetch)
    monkeypatch.setattr(
        PaperTradingService, "get_account", AsyncMock(return_value=account)
    )
    monkeypatch.setattr(
        PaperTradingService, "_get_position", AsyncMock(return_value=None)
    )
    # 이 파일의 다른 테스트가 `paper_orders` 인스턴스 속성을 남기므로 클래스가
    # 아니라 싱글턴을 직접 덮는다.
    monkeypatch.setattr(paper_orders, "_trade_by_correlation", trade_by_correlation)
    monkeypatch.setattr(paper_orders, "_fill_for_order", AsyncMock(return_value=None))
    monkeypatch.setattr(
        paper_orders, "get_by_client_order_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        runtime_state, "assert_order_allowed", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        runtime_state,
        "get",
        AsyncMock(
            return_value=SimpleNamespace(
                kill_switch_enabled=False,
                max_order_ratio=Decimal("0.1000"),
                max_symbol_ratio=Decimal("0.2500"),
            )
        ),
    )
    monkeypatch.setattr(
        runtime_state,
        "get_global",
        AsyncMock(return_value=SimpleNamespace(kill_switch_enabled=False)),
    )
    monkeypatch.setattr(
        paper_account_adapter, "resolve_account", AsyncMock(return_value=account)
    )

    envelope, replay = await paper_orders.submit(  # type: ignore[arg-type]
        db, owner_id, _request()
    )

    trade = next(added for added in db.added if isinstance(added, PaperTrade))
    position = next(added for added in db.added if isinstance(added, PaperPosition))

    assert replay is False
    assert envelope.order.status == "FILLED"
    # 재조회 없이 제출 시점 기준가로 체결된다.
    assert trade.price == reference_price
    assert trade.order_type == "market"
    assert trade.quantity == Decimal("2")
    assert trade.total_amount == Decimal("142468.0000")
    assert position.avg_price == reference_price
    # 리스크·교차 판정에 쓴 가격과 기록된 체결가가 같은 값이다.
    assert envelope.risk is not None
    assert envelope.risk.decision == "APPROVED"
    assert Decimal(envelope.risk.reference_price) == trade.price
    assert Decimal(envelope.risk.estimated_amount) == trade.total_amount
    assert Decimal(envelope.order.average_fill_price or "0") == trade.price
