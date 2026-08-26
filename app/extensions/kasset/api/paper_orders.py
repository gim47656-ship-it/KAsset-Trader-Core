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
        self, db: AsyncSession, request: OrderRequest
    ) -> RiskAssessment:
        self._assert_paper(request)
        account = await paper_account_adapter.resolve_account(db, request.account_id)
        quote = await paper_account_adapter.quote(
            db, market=request.market, symbol=request.symbol
        )
        state = await runtime_state.get(db)
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
        if state.kill_switch_enabled:
            reasons.append(
                RiskReason(
                    code="KILL_SWITCH_ON", message="거래 중지 상태입니다."
                )
            )
        available = (
            Decimal(account.cash_usd)
            if quote.currency == "USD"
            else Decimal(account.cash_krw)
        )
        if request.side == "BUY" and estimated_amount + fee > available:
            reasons.append(
                RiskReason(code="INSUFFICIENT_CASH", message="주문 가능 금액이 부족합니다.")
            )
        if available > 0 and estimated_amount > available * Decimal(state.max_order_ratio):
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
        self, db: AsyncSession, request: OrderRequest
    ) -> tuple[OrderEnvelope, bool]:
        self._assert_paper(request)
        if not request.client_order_id:
            raise MobileApiError(
                422, "VALIDATION_ERROR", "clientOrderId가 필요합니다."
            )
        async with self._transition_lock:
            existing = await self._by_client_id(db, request.client_order_id)
            if existing is not None:
                return await self.envelope(db, existing, replay=True), True

            await runtime_state.assert_order_allowed(db)
            risk = await self.preview(db, request)
            if risk.decision != "APPROVED":
                raise MobileApiError(
                    409,
                    "RISK_REJECTED",
                    "위험 관리 정책에 따라 주문이 거절되었습니다.",
                    {"reasons": [reason.model_dump(by_alias=True) for reason in risk.reasons]},
                )
            account = await paper_account_adapter.resolve_account(db, request.account_id)
            quote = await paper_account_adapter.quote(
                db, market=request.market, symbol=request.symbol
            )
            order = AndroidPaperOrder(
                id=str(uuid4()),
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
                existing = await self._by_client_id(db, request.client_order_id)
                if existing is None:
                    raise
                return await self.envelope(db, existing, replay=True), True
            await db.refresh(order)

            market_price = Decimal(quote.price)
            crosses = request.order_type == "MARKET" or self._crosses(
                request.side, request.limit_price, market_price
            )
            if not crosses:
                order.status = "OPEN"
                await db.commit()
                await db.refresh(order)
                return await self.envelope(db, order, risk=risk), False

            await self._fill(db, order, market_price)
            return await self.envelope(db, order, risk=risk), False

    async def cancel(self, db: AsyncSession, order_id: str) -> OrderEnvelope:
        async with self._transition_lock:
            order = await self.get(db, order_id, for_update=True)
            if order.status not in {"OPEN", "PARTIALLY_FILLED"}:
                raise MobileApiError(
                    409,
                    "ORDER_STATE_CONFLICT",
                    "이미 체결되었거나 취소된 주문은 취소할 수 없습니다.",
                )
            order.status = "CANCELLED"
            await db.commit()
            await db.refresh(order)
            return await self.envelope(db, order)

    async def amend(
        self, db: AsyncSession, order_id: str, amendment: AmendRequest
    ) -> OrderEnvelope:
        async with self._transition_lock:
            order = await self.get(db, order_id, for_update=True)
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
                    await paper_account_adapter.resolve_account(db, None)
                ),
                market=order.market,
                symbol=order.symbol,
                side=order.side,
                orderType="LIMIT",
                quantity=quantity,
                limitPrice=limit_price,
            )
            await runtime_state.assert_order_allowed(db)
            risk = await self.preview(db, request)
            if risk.decision != "APPROVED":
                raise MobileApiError(
                    409,
                    "RISK_REJECTED",
                    "위험 관리 정책에 따라 주문이 거절되었습니다.",
                    {"reasons": [reason.model_dump(by_alias=True) for reason in risk.reasons]},
                )
            order.quantity = quantity
            order.limit_price = limit_price
            quote = await paper_account_adapter.quote(
                db, market=order.market, symbol=order.symbol
            )
            if self._crosses(order.side, limit_price, Decimal(quote.price)):
                remaining = quantity - Decimal(order.filled_quantity)
                order.quantity = remaining
                await db.commit()
                await self._fill(db, order, Decimal(quote.price))
                order.quantity = quantity
                await db.commit()
                await db.refresh(order)
            else:
                await db.commit()
                await db.refresh(order)
            return await self.envelope(db, order, risk=risk)

    async def get(
        self, db: AsyncSession, order_id: str, *, for_update: bool = False
    ) -> AndroidPaperOrder:
        stmt = select(AndroidPaperOrder).where(AndroidPaperOrder.id == order_id)
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
        *,
        statuses: set[str] | None,
        limit: int,
    ) -> OrdersResponse:
        stmt = select(AndroidPaperOrder)
        if statuses:
            stmt = stmt.where(AndroidPaperOrder.status.in_(statuses))
        result = await db.execute(
            stmt.order_by(AndroidPaperOrder.created_at.desc()).limit(limit)
        )
        return OrdersResponse(
            orders=[self.serialize_order(order) for order in result.scalars().all()]
        )

    async def list_fills(self, db: AsyncSession, *, limit: int) -> FillsResponse:
        result = await db.execute(
            select(AndroidPaperOrder)
            .where(AndroidPaperOrder.paper_trade_id.is_not(None))
            .order_by(AndroidPaperOrder.updated_at.desc())
            .limit(limit)
        )
        fills: list[Fill] = []
        for order in result.scalars().all():
            fill = await self._fill_for_order(db, order)
            if fill is not None:
                fills.append(fill)
        return FillsResponse(fills=fills)

    async def detail(self, db: AsyncSession, order_id: str) -> OrderDetail:
        order = await self.get(db, order_id)
        fill = await self._fill_for_order(db, order)
        return OrderDetail(
            order=self.serialize_order(order), fills=[fill] if fill is not None else []
        )

    async def envelope(
        self,
        db: AsyncSession,
        order: AndroidPaperOrder,
        *,
        risk: RiskAssessment | None = None,
        replay: bool = False,
    ) -> OrderEnvelope:
        fill = await self._fill_for_order(db, order)
        return OrderEnvelope(
            order=self.serialize_order(order),
            risk=risk,
            fills=[fill] if fill is not None else [],
            idempotent_replay=replay,
        )

    async def _fill(
        self, db: AsyncSession, order: AndroidPaperOrder, market_price: Decimal
    ) -> None:
        service = PaperTradingService(db)
        try:
            await service.execute_order(
                account_id=order.paper_account_id,
                symbol=order.symbol,
                side=order.side.lower(),
                order_type=("market" if order.order_type == "MARKET" else "limit"),
                quantity=order.quantity,
                price=(market_price if order.order_type == "LIMIT" else None),
                reason="KAsset Android PAPER",
                correlation_id=order.id,
            )
        except ValueError as err:
            order.status = "REJECTED"
            order.reject_reason = "PAPER 주문 조건을 충족하지 못했습니다."
            await db.commit()
            raise MobileApiError(
                409, "BROKER_ERROR", "PAPER 주문을 실행하지 못했습니다."
            ) from err
        result = await db.execute(
            select(PaperTrade).where(PaperTrade.correlation_id == order.id)
        )
        trade = result.scalar_one_or_none()
        if trade is None:
            order.status = "REJECTED"
            order.reject_reason = "PAPER 체결 기록을 확인하지 못했습니다."
            await db.commit()
            raise MobileApiError(
                500, "BROKER_ERROR", "PAPER 체결 결과를 확인하지 못했습니다."
            )
        order.status = "FILLED"
        order.filled_quantity = order.quantity
        order.average_fill_price = trade.price
        order.paper_trade_id = trade.id
        await db.commit()
        await db.refresh(order)

    async def _fill_for_order(
        self, db: AsyncSession, order: AndroidPaperOrder
    ) -> Fill | None:
        if order.paper_trade_id is None:
            return None
        result = await db.execute(
            select(PaperTrade).where(PaperTrade.id == order.paper_trade_id)
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

    async def _by_client_id(
        self, db: AsyncSession, client_order_id: str
    ) -> AndroidPaperOrder | None:
        result = await db.execute(
            select(AndroidPaperOrder).where(
                AndroidPaperOrder.client_order_id == client_order_id
            )
        )
        return result.scalar_one_or_none()

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
    def _crosses(side: str, limit_price: Decimal | None, market_price: Decimal) -> bool:
        if limit_price is None:
            return False
        return limit_price >= market_price if side == "BUY" else limit_price <= market_price

    @staticmethod
    def _assert_paper(request: OrderRequest) -> None:
        if request.broker != "PAPER":
            raise MobileApiError(
                409, "BROKER_NOT_CONNECTED", "선택한 브로커가 연결되지 않았습니다."
            )


paper_orders = PaperOrderFacade()
