"""Order Estimation Service — 주문 비용 추정 공통 로직"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

logger = logging.getLogger(__name__)


class PendingBuyCostUnavailableError(RuntimeError):
    """Toss 미체결 매수 비용을 증거 기반으로 계산할 수 없는 상태."""

    error_code = "pending_buy_cost_unavailable"

    def __init__(self, *, market: Literal["kr", "us"], reason: str) -> None:
        self.market = market
        self.reason = reason
        super().__init__(f"{self.error_code}:{market}:{reason}")


def _evidence_decimal(
    value: object,
    *,
    market: Literal["kr", "us"],
    missing_reason: str,
    invalid_reason: str,
) -> Decimal:
    if value is None:
        raise PendingBuyCostUnavailableError(market=market, reason=missing_reason)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PendingBuyCostUnavailableError(
            market=market, reason=invalid_reason
        ) from exc
    if not parsed.is_finite():
        raise PendingBuyCostUnavailableError(market=market, reason=invalid_reason)
    return parsed


def calculate_pending_toss_buy_cost(
    orders: list[Any],
    *,
    market: Literal["kr", "us"],
) -> float:
    """합산 가능한 Toss 지정가 매수만 잔량 × 지정가로 계산한다."""

    from app.services.current_orders_service import toss_order_market

    total = Decimal("0")
    for order in orders:
        if toss_order_market(str(order.symbol)) != market:
            continue
        if str(order.side).strip().lower() not in {"buy", "bid", "매수"}:
            continue
        if str(order.order_type).strip().lower() != "limit":
            raise PendingBuyCostUnavailableError(
                market=market, reason="open_buy_order_is_not_limit"
            )

        execution = order.execution
        if not isinstance(execution, dict) or "filledQuantity" not in execution:
            raise PendingBuyCostUnavailableError(
                market=market, reason="missing_filled_quantity"
            )
        quantity = _evidence_decimal(
            order.quantity,
            market=market,
            missing_reason="missing_order_quantity",
            invalid_reason="invalid_order_quantity",
        )
        filled = _evidence_decimal(
            execution.get("filledQuantity"),
            market=market,
            missing_reason="missing_filled_quantity",
            invalid_reason="invalid_filled_quantity",
        )
        limit_price = _evidence_decimal(
            order.price,
            market=market,
            missing_reason="missing_limit_price",
            invalid_reason="invalid_limit_price",
        )
        if quantity < 0 or filled < 0 or filled > quantity:
            raise PendingBuyCostUnavailableError(
                market=market, reason="inconsistent_order_quantities"
            )
        if limit_price <= 0:
            raise PendingBuyCostUnavailableError(
                market=market, reason="invalid_limit_price"
            )
        total += (quantity - filled) * limit_price
    return float(total)


async def _fetch_pending_equity_buy_cost(
    *,
    market: Literal["kr", "us"],
    toss_client_factory: Callable[[], Any] | None = None,
) -> float:
    from app.services.current_orders_service import fetch_toss_open_orders

    try:
        if toss_client_factory is None:
            orders = await fetch_toss_open_orders()
        else:
            orders = await fetch_toss_open_orders(client_factory=toss_client_factory)
    except Exception as exc:
        logger.warning(
            "Toss 미체결 주문 조회 실패 (%s)", type(exc).__name__, exc_info=True
        )
        raise PendingBuyCostUnavailableError(
            market=market, reason="provider_unavailable"
        ) from exc
    return calculate_pending_toss_buy_cost(orders, market=market)


_BUY_PRICE_FIELDS = [
    "appropriate_buy_min",
    "appropriate_buy_max",
    "buy_hope_min",
    "buy_hope_max",
]


def extract_buy_prices_from_analysis(analysis: Any) -> list[dict[str, Any]]:
    """분석 결과에서 매수 가격 목록 추출

    Args:
        analysis: StockAnalysisResult 객체 (appropriate_buy_min/max, buy_hope_min/max 속성)

    Returns:
        [{"price_name": "appropriate_buy_min", "price": 50000.0}, ...]
    """
    buy_prices: list[dict[str, Any]] = []
    for field in _BUY_PRICE_FIELDS:
        value = getattr(analysis, field, None)
        if value is not None:
            buy_prices.append({"price_name": field, "price": float(value)})
    return buy_prices


def calculate_estimated_order_cost(
    symbol: str,
    buy_prices: list[dict[str, float]],
    quantity_per_order: float,
    currency: str = "KRW",
    *,
    amount_based: bool = False,
) -> dict[str, Any]:
    """예상 주문 비용 계산

    Args:
        symbol: 종목 코드
        buy_prices: 매수 가격 목록 [{"price_name": "...", "price": 50000}, ...]
        quantity_per_order: 주문당 수량 (amount_based=True일 때는 주문당 금액)
        currency: 통화 (KRW, USD)
        amount_based: True이면 금액 기반 계산 (암호화폐용).
            각 가격대마다 동일 금액(quantity_per_order)을 매수하고,
            수량은 금액/가격으로 역산.

    Returns:
        {
            "symbol": "005930",
            "quantity_per_order": 2,
            "buy_prices": [{"price_name": ..., "price": ..., "quantity": ..., "cost": ...}],
            "total_orders": 2,
            "total_quantity": 4,
            "total_cost": 196000,
            "currency": "KRW"
        }
    """
    result_prices = []
    total_quantity = 0.0
    total_cost = 0.0

    for price_info in buy_prices:
        price = price_info["price"]
        price_name = price_info["price_name"]

        if amount_based:
            qty = quantity_per_order / price if price > 0 else 0
            cost = quantity_per_order
        elif currency == "KRW":
            qty = int(quantity_per_order)
            cost = price * qty
        else:
            qty = quantity_per_order
            cost = price * qty

        result_prices.append(
            {
                "price_name": price_name,
                "price": price,
                "quantity": qty,
                "cost": cost,
            }
        )

        total_quantity += qty
        total_cost += cost

    return {
        "symbol": symbol,
        "quantity_per_order": quantity_per_order,
        "buy_prices": result_prices,
        "total_orders": len(buy_prices),
        "total_quantity": total_quantity,
        "total_cost": total_cost,
        "currency": currency,
    }


async def fetch_pending_domestic_buy_cost(
    *,
    toss_client_factory: Callable[[], Any] | None = None,
) -> float:
    """Toss 국내 미체결 지정가 매수 주문의 증거 기반 잔여 금액."""

    return await _fetch_pending_equity_buy_cost(
        market="kr", toss_client_factory=toss_client_factory
    )


async def fetch_pending_overseas_buy_cost(
    *,
    toss_client_factory: Callable[[], Any] | None = None,
) -> float:
    """Toss 해외 미체결 지정가 매수 주문의 증거 기반 잔여 금액."""

    return await _fetch_pending_equity_buy_cost(
        market="us", toss_client_factory=toss_client_factory
    )


async def fetch_pending_crypto_buy_cost() -> float:
    """미체결 암호화폐 매수 주문 총액 조회

    Upbit API를 호출하여 미체결 매수 주문의 총 금액을 반환.
    시장가(price) 주문: price가 주문 금액.
    지정가(limit) 주문: price * remaining_volume.
    실패 시 0.0 반환 (warning 로그).
    """
    import app.services.brokers.upbit.client as upbit

    try:
        pending_orders = await upbit.fetch_open_orders()
        cost = 0.0
        for order in pending_orders:
            if order.get("side") == "bid":
                ord_type = order.get("ord_type", "")
                if ord_type == "price":
                    cost += float(order.get("price", 0))
                else:
                    price_val = float(order.get("price", 0))
                    remaining = float(order.get("remaining_volume", 0))
                    cost += price_val * remaining
        return cost
    except Exception as e:
        logger.warning(f"Upbit 미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0
