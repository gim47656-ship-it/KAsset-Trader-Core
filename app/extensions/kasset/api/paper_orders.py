"""PAPER order facade with durable idempotency and Core service execution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import decimal_text, iso_z, paper_account_adapter
from app.extensions.kasset.api.paper_schemas import (
    AmendRequest,
    Fill,
    FillsResponse,
    Order,
    OrderDetail,
    OrderEnvelope,
    OrderRequest,
    OrdersResponse,
    RiskAssessment,
    RiskReason,
)
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.models import AndroidPaperOrder
from app.models.paper_trading import PaperTrade
from app.services.paper_trading_service import PaperTradingService, calculate_fee


class PaperOrderFacade:
    def __init__(self) -> None:
        self._transition_lock = asyncio.Lock()

    async def preview(
        self,
        db: AsyncSession,
        owner_user_id: int,
        request: OrderRequest,
    ) -> RiskAssessment:
        self._assert_paper(request)
        account = await paper_account_adapter.resolve_account(
            db, owner_user_id, request.account_id
        )
        quote = await krx_quotes.quote_for_market(
            db, market=request.market, symbol=request.symbol
        )
        state = await runtime_state.get(db, owner_user_id)
        global_state = await runtime_state.get_global(db)
        price = Decimal(quote.price)
        estimated_amount = request.quantity * (
            request.limit_price if request.order_type == "LIMIT" else price
        )
        fee = calculate_fee(
            "equity_us" if quote.currency == "USD" else "equity_kr",
            request.side.lower(),
            estimated_amount,
        )
        reasons: list[RiskReason] = []
        if not settings.TRADING_ENABLED:
            reasons.append(
                RiskReason(
                    code="TRADING_DISABLED", message="서버에서 거래가 꺼져 있습니다."
                )
            )
        if global_state.kill_switch_enabled or state.kill_switch_enabled:
            reasons.append(
                RiskReason(code="KILL_SWITCH_ON", message="거래 중지 상태입니다.")
            )
        available = (
            Decimal(account.cash_usd)
            if quote.currency == "USD"
            else Decimal(account.cash_krw)
        )
        if request.side == "BUY" and estimated_amount + fee > available:
            reasons.append(
                RiskReason(
                    code="INSUFFICIENT_CASH", message="주문 가능 금액이 부족합니다."
                )
            )
        if available > 0 and estimated_amount > available * Decimal(
            state.max_order_ratio
        ):
            reasons.append(
                RiskReason(
                    code="MAX_ORDER_RATIO",
                    message="한 주문의 최대 자산 비율을 초과했습니다.",
                )
            )
        return RiskAssessment(
            decision="REJECTED" if reasons else "APPROVED",
            reasons=reasons,
            estimated_amount=decimal_text(estimated_amount),
            estimated_fee=decimal_text(fee),
            reference_price=quote.price,
            currency=quote.currency,
        )

    async def submit(
        self,
        db: AsyncSession,
        owner_user_id: int,
        request: OrderRequest,
    ) -> tuple[OrderEnvelope, bool]:
        self._assert_paper(request)
        if not request.client_order_id:
            raise MobileApiError(422, "VALIDATION_ERROR", "clientOrderId가 필요합니다.")
        async with self._transition_lock:
            existing = await self.get_by_client_order_id(
                db, owner_user_id, request.client_order_id
            )
            if existing is not None:
                return (
                    await self._reconcile_existing(
                        db,
                        owner_user_id,
                        existing,
                    ),
                    True,
                )

            await runtime_state.assert_order_allowed(db, owner_user_id)
            risk = await self.preview(db, owner_user_id, request)
            if risk.decision != "APPROVED":
                raise MobileApiError(
                    409,
                    "RISK_REJECTED",
                    "위험 관리 정책에 따라 주문이 거절되었습니다.",
                    {
                        "reasons": [
                            reason.model_dump(by_alias=True) for reason in risk.reasons
                        ]
                    },
                )
            account = await paper_account_adapter.resolve_account(
                db, owner_user_id, request.account_id
            )
            quote = await krx_quotes.quote_for_market(
                db, market=request.market, symbol=request.symbol
            )
            order = AndroidPaperOrder(
                id=str(uuid4()),
                owner_user_id=owner_user_id,
                client_order_id=request.client_order_id,
                paper_account_id=account.id,
                broker_order_id=f"PAPER-{uuid4().hex[:16].upper()}",
                market=quote.market,
                symbol=quote.symbol,
                name=quote.name,
                currency=quote.currency,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                limit_price=request.limit_price,
                status="PENDING",
                filled_quantity=Decimal("0"),
            )
            db.add(order)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await self.get_by_client_order_id(
                    db, owner_user_id, request.client_order_id
                )
                if existing is None:
                    raise
                return (
                    await self._reconcile_existing(
                        db,
                        owner_user_id,
                        existing,
                    ),
                    True,
                )
            await db.refresh(order)

            market_price = Decimal(quote.price)
            crosses = request.order_type == "MARKET" or self._crosses(
                request.side, request.limit_price, market_price
            )
            if not crosses:
                order.status = "OPEN"
                await db.commit()
                await db.refresh(order)
                return (
                    await self.envelope(db, owner_user_id, order, risk=risk),
                    False,
                )

            filled_order = await self._fill(db, owner_user_id, order, market_price)
            return (
                await self.envelope(
                    db,
                    owner_user_id,
                    filled_order or order,
                    risk=risk,
                ),
                False,
            )

    async def cancel(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order_id: str,
    ) -> OrderEnvelope:
        async with self._transition_lock:
            order = await self.get(db, owner_user_id, order_id, for_update=True)
            if order.status not in {"OPEN", "PARTIALLY_FILLED"}:
                raise MobileApiError(
                    409,
                    "ORDER_STATE_CONFLICT",
                    "이미 체결되었거나 취소된 주문은 취소할 수 없습니다.",
                )
            order.status = "CANCELLED"
            await db.commit()
            await db.refresh(order)
            return await self.envelope(db, owner_user_id, order)

    async def amend(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order_id: str,
        amendment: AmendRequest,
    ) -> OrderEnvelope:
        async with self._transition_lock:
            order = await self.get(db, owner_user_id, order_id, for_update=True)
            if order.order_type != "LIMIT" or order.status not in {
                "OPEN",
                "PARTIALLY_FILLED",
            }:
                raise MobileApiError(
                    409,
                    "ORDER_STATE_CONFLICT",
                    "미체결 지정가 주문만 정정할 수 있습니다.",
                )
            quantity = amendment.quantity or Decimal(order.quantity)
            if quantity < Decimal(order.filled_quantity):
                raise MobileApiError(
                    409,
                    "ORDER_STATE_CONFLICT",
                    "이미 체결된 수량보다 적게 정정할 수 없습니다.",
                )
            limit_price = amendment.limit_price or order.limit_price
            if limit_price is None:
                raise MobileApiError(
                    422, "VALIDATION_ERROR", "limitPrice가 필요합니다."
                )
            request = OrderRequest(
                clientOrderId=order.client_order_id,
                broker="PAPER",
                accountId=paper_account_adapter.account_id(
                    await paper_account_adapter.resolve_account(db, owner_user_id, None)
                ),
                market=order.market,
                symbol=order.symbol,
                side=order.side,
                orderType="LIMIT",
                quantity=quantity,
                limitPrice=limit_price,
            )
            await runtime_state.assert_order_allowed(db, owner_user_id)
            risk = await self.preview(db, owner_user_id, request)
            if risk.decision != "APPROVED":
                raise MobileApiError(
                    409,
                    "RISK_REJECTED",
                    "위험 관리 정책에 따라 주문이 거절되었습니다.",
                    {
                        "reasons": [
                            reason.model_dump(by_alias=True) for reason in risk.reasons
                        ]
                    },
                )
            order.quantity = quantity
            order.limit_price = limit_price
            quote = await paper_account_adapter.quote(
                db, market=order.market, symbol=order.symbol
            )
            if self._crosses(order.side, limit_price, Decimal(quote.price)):
                remaining = quantity - Decimal(order.filled_quantity)
                if remaining == 0:
                    order.status = "FILLED"
                    await db.commit()
                    await db.refresh(order)
                else:
                    await db.commit()
                    filled_order = await self._fill(
                        db,
                        owner_user_id,
                        order,
                        Decimal(quote.price),
                        fill_quantity=remaining,
                    )
                    if filled_order is not None:
                        order = filled_order
            else:
                await db.commit()
                await db.refresh(order)
            return await self.envelope(db, owner_user_id, order, risk=risk)

    async def get_by_client_order_id(
        self,
        db: AsyncSession,
        owner_user_id: int,
        client_order_id: str,
    ) -> AndroidPaperOrder | None:
        result = await db.execute(
            select(AndroidPaperOrder).where(
                AndroidPaperOrder.owner_user_id == owner_user_id,
                AndroidPaperOrder.client_order_id == client_order_id,
            )
        )
        return result.scalar_one_or_none()

    async def reconcile(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order: AndroidPaperOrder,
    ) -> OrderEnvelope:
        async with self._transition_lock:
            return await self._reconcile_existing(db, owner_user_id, order)

    async def get(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order_id: str,
        *,
        for_update: bool = False,
    ) -> AndroidPaperOrder:
        stmt = select(AndroidPaperOrder).where(
            AndroidPaperOrder.owner_user_id == owner_user_id,
            AndroidPaperOrder.id == order_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        order = result.scalar_one_or_none()
        if order is None:
            raise MobileApiError(404, "NOT_FOUND", "주문을 찾을 수 없습니다.")
        return order

    async def list_orders(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        statuses: set[str] | None,
        limit: int,
    ) -> OrdersResponse:
        stmt = select(AndroidPaperOrder).where(
            AndroidPaperOrder.owner_user_id == owner_user_id
        )
        if statuses:
            stmt = stmt.where(AndroidPaperOrder.status.in_(statuses))
        result = await db.execute(
            stmt.order_by(AndroidPaperOrder.created_at.desc()).limit(limit)
        )
        return OrdersResponse(
            orders=[self.serialize_order(order) for order in result.scalars().all()]
        )

    async def list_fills(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        limit: int,
    ) -> FillsResponse:
        result = await db.execute(
            select(AndroidPaperOrder)
            .where(
                AndroidPaperOrder.owner_user_id == owner_user_id,
                AndroidPaperOrder.paper_trade_id.is_not(None),
            )
            .order_by(AndroidPaperOrder.updated_at.desc())
            .limit(limit)
        )
        fills: list[Fill] = []
        for order in result.scalars().all():
            fill = await self._fill_for_order(db, owner_user_id, order)
            if fill is not None:
                fills.append(fill)
        return FillsResponse(fills=fills)

    async def detail(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order_id: str,
    ) -> OrderDetail:
        order = await self.get(db, owner_user_id, order_id)
        fill = await self._fill_for_order(db, owner_user_id, order)
        return OrderDetail(
            order=self.serialize_order(order), fills=[fill] if fill is not None else []
        )

    async def envelope(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order: AndroidPaperOrder,
        *,
        risk: RiskAssessment | None = None,
        replay: bool = False,
    ) -> OrderEnvelope:
        self._assert_owned(order, owner_user_id)
        fill = await self._fill_for_order(db, owner_user_id, order)
        return OrderEnvelope(
            order=self.serialize_order(order),
            risk=risk,
            fills=[fill] if fill is not None else [],
            idempotent_replay=replay,
        )

    async def _fill(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order: AndroidPaperOrder,
        market_price: Decimal,
        *,
        fill_quantity: Decimal | None = None,
    ) -> AndroidPaperOrder:
        self._assert_owned(order, owner_user_id)
        order_id = order.id
        account_id = order.paper_account_id
        prior_filled = Decimal(order.filled_quantity)
        correlation_id = (
            order_id
            if prior_filled == 0
            else f"{order_id}:fill:{format(prior_filled, 'f')}"
        )
        existing_trade = await self._trade_by_correlation(
            db,
            account_id,
            correlation_id,
        )
        if existing_trade is not None:
            return await self._apply_trade_metadata(db, order, existing_trade)

        execution_quantity = (
            fill_quantity
            if fill_quantity is not None
            else Decimal(order.quantity) - prior_filled
        )
        service = PaperTradingService(db)
        try:
            await service.execute_order(
                account_id=account_id,
                symbol=order.symbol,
                side=order.side.lower(),
                order_type=("market" if order.order_type == "MARKET" else "limit"),
                quantity=execution_quantity,
                price=(market_price if order.order_type == "LIMIT" else None),
                reason="KAsset Android PAPER",
                correlation_id=correlation_id,
            )
        except IntegrityError:
            await db.rollback()
            trade = await self._trade_by_correlation(
                db,
                account_id,
                correlation_id,
            )
            if trade is None:
                raise
            order = await self.get(db, owner_user_id, order_id)
            return await self._apply_trade_metadata(db, order, trade)
        except ValueError as err:
            order.status = "REJECTED"
            order.reject_reason = "PAPER 주문 조건을 충족하지 못했습니다."
            await db.commit()
            raise MobileApiError(
                409, "BROKER_ERROR", "PAPER 주문을 실행하지 못했습니다."
            ) from err

        trade = await self._trade_by_correlation(db, account_id, correlation_id)
        if trade is None:
            raise MobileApiError(
                500, "BROKER_ERROR", "PAPER 체결 결과를 확인하지 못했습니다."
            )
        return await self._apply_trade_metadata(db, order, trade)

    @staticmethod
    async def _trade_by_correlation(
        db: AsyncSession,
        account_id: int,
        correlation_id: str,
    ) -> PaperTrade | None:
        result = await db.execute(
            select(PaperTrade).where(
                PaperTrade.account_id == account_id,
                PaperTrade.correlation_id == correlation_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _apply_trade_metadata(
        db: AsyncSession,
        order: AndroidPaperOrder,
        trade: PaperTrade,
    ) -> AndroidPaperOrder:
        if order.paper_trade_id == trade.id and order.status in {
            "FILLED",
            "PARTIALLY_FILLED",
        }:
            return order
        prior_filled = Decimal(order.filled_quantity)
        execution_quantity = Decimal(trade.quantity)
        total_filled = prior_filled + execution_quantity
        if total_filled > Decimal(order.quantity):
            raise MobileApiError(
                500,
                "BROKER_ERROR",
                "PAPER 체결 수량이 주문 수량을 초과했습니다.",
            )
        previous_average = order.average_fill_price
        order.status = (
            "FILLED" if total_filled == Decimal(order.quantity) else "PARTIALLY_FILLED"
        )
        order.filled_quantity = total_filled
        order.average_fill_price = (
            (
                (Decimal(previous_average) * prior_filled)
                + (Decimal(trade.price) * execution_quantity)
            )
            / total_filled
            if prior_filled > 0 and previous_average is not None
            else trade.price
        )
        order.paper_trade_id = trade.id
        order.reject_reason = None
        await db.commit()
        await db.refresh(order)
        return order

    async def _fill_for_order(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order: AndroidPaperOrder,
    ) -> Fill | None:
        self._assert_owned(order, owner_user_id)
        if order.paper_trade_id is None:
            return None
        result = await db.execute(
            select(PaperTrade).where(
                PaperTrade.account_id == order.paper_account_id,
                PaperTrade.id == order.paper_trade_id,
            )
        )
        trade = result.scalar_one_or_none()
        if trade is None:
            return None
        return Fill(
            id=f"PAPER-FILL-{trade.id}",
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            market=order.market,
            symbol=order.symbol,
            side=order.side,
            quantity=decimal_text(trade.quantity),
            price=decimal_text(trade.price),
            fee=decimal_text(trade.fee),
            filled_at=iso_z(trade.executed_at),
        )

    async def _reconcile_existing(
        self,
        db: AsyncSession,
        owner_user_id: int,
        order: AndroidPaperOrder,
    ) -> OrderEnvelope:
        self._assert_owned(order, owner_user_id)
        if order.status == "PENDING":
            trade = await self._trade_by_correlation(
                db,
                order.paper_account_id,
                order.id,
            )
            if trade is not None:
                order = await self._apply_trade_metadata(db, order, trade)
            else:
                quote = await paper_account_adapter.quote(
                    db,
                    market=order.market,
                    symbol=order.symbol,
                )
                market_price = Decimal(quote.price)
                crosses = order.order_type == "MARKET" or self._crosses(
                    order.side,
                    order.limit_price,
                    market_price,
                )
                if crosses:
                    filled_order = await self._fill(
                        db,
                        owner_user_id,
                        order,
                        market_price,
                    )
                    if filled_order is not None:
                        order = filled_order
                else:
                    order.status = "OPEN"
                    await db.commit()
                    await db.refresh(order)
        return await self.envelope(db, owner_user_id, order, replay=True)

    @staticmethod
    def serialize_order(order: AndroidPaperOrder) -> Order:
        created = order.created_at or datetime.now(UTC)
        updated = order.updated_at or created
        return Order(
            id=order.id,
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            account_id=f"PAPER-{order.paper_account_id}",
            market=order.market,
            symbol=order.symbol,
            name=order.name,
            currency=order.currency,
            side=order.side,
            order_type=order.order_type,
            quantity=decimal_text(order.quantity),
            limit_price=(
                decimal_text(order.limit_price)
                if order.limit_price is not None
                else None
            ),
            status=order.status,
            filled_quantity=decimal_text(order.filled_quantity),
            average_fill_price=(
                decimal_text(order.average_fill_price)
                if order.average_fill_price is not None
                else None
            ),
            reject_reason=order.reject_reason,
            created_at=iso_z(created),
            updated_at=iso_z(updated),
        )

    @staticmethod
    def _assert_owned(order: AndroidPaperOrder, owner_user_id: int) -> None:
        if order.owner_user_id != owner_user_id:
            raise MobileApiError(404, "NOT_FOUND", "주문을 찾을 수 없습니다.")

    @staticmethod
    def _crosses(side: str, limit_price: Decimal | None, market_price: Decimal) -> bool:
        if limit_price is None:
            return False
        return (
            limit_price >= market_price
            if side == "BUY"
            else limit_price <= market_price
        )

    @staticmethod
    def _assert_paper(request: OrderRequest) -> None:
        if request.broker != "PAPER":
            raise MobileApiError(
                409, "BROKER_NOT_CONNECTED", "선택한 브로커가 연결되지 않았습니다."
            )


paper_orders = PaperOrderFacade()
