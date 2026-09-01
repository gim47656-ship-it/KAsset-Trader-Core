"""Order history helpers and shared order execution aliases."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import orders_toss_variants
from app.mcp_server.tooling.order_execution import _normalize_market_type_to_external
from app.mcp_server.tooling.orders_modify_cancel import _normalize_upbit_order
from app.mcp_server.tooling.shared import (
    logger,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)

# ROB-466: status='all' without a symbol returns the symbol-free subset (open
# orders across all markets). Broker fill/cancel history is keyed by symbol, so
# it is omitted in that case; surface a warning rather than silently dropping it.
ALL_SYMBOLS_CLOSED_HISTORY_WARNING = (
    "status='all' without a symbol returns open/pending orders across markets "
    "only; filled/cancelled history is broker-symbol-keyed — pass a symbol to "
    "include it."
)


def _calculate_order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
    total_orders = len(orders)
    filled = sum(1 for o in orders if o.get("status") == "filled")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    partial = sum(1 for o in orders if o.get("status") == "partial")
    cancelled = sum(1 for o in orders if o.get("status") == "cancelled")
    expired = sum(1 for o in orders if o.get("status") == "expired")

    return {
        "total_orders": total_orders,
        "filled": filled,
        "pending": pending,
        "partial": partial,
        "cancelled": cancelled,
        "expired": expired,
    }


def _validate_history_inputs(
    symbol: str | None,
    status: str,
    order_id: str | None,
    market: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
    float,
    list[str],
    str | None,
]:
    """Validate and normalize history query inputs.

    Returns:
        (symbol, order_id, market_hint, side, effective_days,
         limit_val, market_types, normalized_symbol)
    """
    if status in ("filled", "cancelled") and not symbol:
        raise ValueError(
            f"symbol is required when status='{status}' "
            "(broker fill/cancel history is keyed by symbol). "
            "Use status='pending' or status='all' for symbol-free queries "
            "(open orders across markets), "
            "or provide a symbol (e.g. symbol='KRW-BTC')."
        )

    symbol = (symbol or "").strip() or None
    order_id = (order_id or "").strip() or None
    market_hint = (market or "").strip().lower() or None
    side = (side or "").strip().lower() or None

    if side and side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    if limit is None:
        limit = 50
    elif limit < -1:
        raise ValueError("limit must be >= -1")

    limit_val = limit if limit not in (0, -1) else float("inf")
    effective_days = days

    market_types: list[str] = []
    normalized_symbol: str | None = None

    if symbol:
        market_type, normalized_symbol = _resolve_market_type(symbol, market_hint)
        market_types = [market_type]
    elif market_hint:
        norm = _normalize_market(market_hint)
        if norm is None:
            raise ValueError(f"Unsupported market: {market_hint}")
        market_types = [norm]
    if not market_types and status in ("pending", "all", "expired"):
        # ROB-665 item 3: expired (dead day orders) surface via the live
        # KR/US inquiries just like pending, so scan all markets when unscoped.
        market_types = ["crypto", "equity_kr", "equity_us"]

    if not market_types and order_id:
        if "-" in order_id and len(order_id) == 36:
            market_types = ["crypto"]
        else:
            market_types = ["crypto", "equity_kr", "equity_us"]

    return (
        symbol,
        order_id,
        market_hint,
        side,
        effective_days,
        limit_val,
        market_types,
        normalized_symbol,
    )


async def _fetch_crypto_orders(
    normalized_symbol: str | None,
    status: str,
    limit_val: float,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch and normalize Upbit (crypto) orders."""
    fetched: list[dict[str, Any]] = []

    if status in ("all", "pending"):
        open_ops = await upbit_service.fetch_open_orders(market=normalized_symbol)
        fetched.extend([_normalize_upbit_order(o) for o in open_ops])

    if status in ("all", "filled", "cancelled") and normalized_symbol:
        fetch_limit = 100 if limit_val == float("inf") else max(limit, 20)
        closed_ops = await upbit_service.fetch_closed_orders(
            market=normalized_symbol,
            limit=fetch_limit,
        )
        fetched.extend([_normalize_upbit_order(o) for o in closed_ops])

    return fetched


_TOSS_STATUS_MAP = {
    "PENDING": "pending",
    "PENDING_CANCEL": "pending",
    "PENDING_REPLACE": "pending",
    "CANCEL_REJECTED": "pending",
    "REPLACE_REJECTED": "pending",
    "PARTIAL_FILLED": "partial",
    "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "CANCELLED": "cancelled",
    "REPLACED": "cancelled",
    "REJECTED": "expired",
    "EXPIRED": "expired",
}


def _remaining_quantity(
    quantity: Any,
    filled_quantity: Any,
) -> str | None:
    if quantity is None or filled_quantity is None:
        return None
    try:
        return str(
            max(Decimal(str(quantity)) - Decimal(str(filled_quantity)), Decimal(0))
        )
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_toss_order(order: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(order.get("symbol") or "").strip()
    if not symbol:
        return None
    try:
        market_type, normalized_symbol = _resolve_market_type(symbol, None)
    except ValueError:
        return None
    if market_type not in {"equity_kr", "equity_us"}:
        return None

    execution = order.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    filled_quantity = execution.get("filledQuantity")
    raw_status = str(order.get("status") or "").strip().upper()
    status = _TOSS_STATUS_MAP.get(raw_status, raw_status.lower())
    return {
        "order_id": str(order.get("order_id") or ""),
        "symbol": normalized_symbol,
        "side": str(order.get("side") or "").lower(),
        "order_type": str(order.get("order_type") or "").lower(),
        "status": status,
        "is_live": status in {"pending", "partial"},
        "ordered_qty": order.get("quantity"),
        "filled_qty": filled_quantity,
        "remaining_qty": _remaining_quantity(
            order.get("quantity"),
            filled_quantity,
        ),
        "ordered_price": order.get("price"),
        "filled_avg_price": execution.get("averageFilledPrice"),
        "ordered_at": order.get("ordered_at"),
        "cancelled_at": order.get("canceled_at"),
        "currency": order.get("currency"),
        "source": "toss",
        "_market_type": market_type,
    }


async def _fetch_toss_orders(
    normalized_symbol: str | None,
    status: str,
    effective_days: int | None,
    limit_val: float,
) -> list[dict[str, Any]]:
    """Toss 주식 주문을 조회해 공통 주문 이력 형식으로 정규화한다."""
    requested_statuses = ["open"]
    if status in {"filled", "cancelled", "expired"}:
        requested_statuses = ["closed"]
    elif status == "all" and normalized_symbol is not None:
        requested_statuses.append("closed")

    from_date = None
    to_date = None
    if effective_days is not None:
        today = date.today()
        from_date = (today - timedelta(days=effective_days)).isoformat()
        to_date = today.isoformat()

    fetched: list[dict[str, Any]] = []
    api_limit = None if limit_val == float("inf") else max(int(limit_val), 1)
    for requested_status in requested_statuses:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            response = await orders_toss_variants.toss_get_order_history(
                status=requested_status,
                symbol=normalized_symbol,
                from_date=from_date,
                to_date=to_date,
                cursor=cursor,
                limit=api_limit,
                account_mode="toss_live",
            )
            if not response.get("success"):
                raise RuntimeError(
                    str(response.get("error") or "Toss order history request failed")
                )
            for row in response.get("orders") or []:
                if not isinstance(row, dict):
                    continue
                normalized = _normalize_toss_order(row)
                if normalized is not None:
                    fetched.append(normalized)
            next_cursor = response.get("next_cursor")
            if not response.get("has_next") or not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise RuntimeError("Toss order history returned a repeated cursor")
            seen_cursors.add(cursor)
    return fetched


def _dedupe_orders(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate orders, preserving last occurrence."""
    original_count = len(orders)
    unique_orders: dict[tuple[Any, ...], dict[str, Any]] = {}
    for o in orders:
        oid = str(o.get("order_id") or "").strip()
        source_market = o.get("_source_market") or o.get("market") or "unknown"
        if oid:
            key = (source_market, oid)
            unique_orders[key] = o
        else:
            key = (
                source_market,
                o.get("symbol"),
                o.get("side"),
                o.get("ordered_price"),
                o.get("ordered_qty"),
                o.get("ordered_at"),
                o.get("status"),
                o.get("currency"),
            )
            unique_orders[key] = o

    result = list(unique_orders.values())
    removed = original_count - len(result)
    if removed > 0:
        logger.info("Removed %s duplicate orders", removed)
    return result


def _filter_and_sort_orders(
    orders: list[dict[str, Any]],
    status: str,
    order_id: str | None,
    side: str | None,
    limit_val: float,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Filter, sort, and truncate orders.

    Returns:
        (response_orders, total_available, truncated)
    """
    filtered: list[dict[str, Any]] = []
    for o in orders:
        o_status = o.get("status")
        if status == "pending":
            if o_status not in ("pending", "partial"):
                continue
        elif status == "filled":
            if o_status != "filled":
                continue
        elif status == "cancelled":
            if o_status != "cancelled":
                continue
        elif status == "expired":
            if o_status != "expired":
                continue

        if order_id and o.get("order_id") != order_id:
            continue

        if side and o.get("side") != side:
            continue

        filtered.append(o)

    def _get_sort_key(o: dict[str, Any]) -> str:
        val = o.get("ordered_at") or o.get("created_at") or ""
        return str(val)

    filtered.sort(key=_get_sort_key, reverse=True)

    total_available = len(filtered)
    truncated = False
    if limit_val != float("inf") and total_available > limit_val:
        filtered = filtered[: int(limit_val)]
        truncated = True

    response_orders: list[dict[str, Any]] = []
    for o in filtered:
        cleaned = dict(o)
        cleaned.pop("_source_market", None)
        response_orders.append(cleaned)

    return response_orders, total_available, truncated


def _build_history_response(
    *,
    response_orders: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    market_types: list[str],
    normalized_symbol: str | None,
    symbol: str | None,
    status: str,
    order_id: str | None,
    market_hint: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
    truncated: bool,
    total_available: int,
) -> dict[str, Any]:
    """Build the final order history response dict."""
    summary = _calculate_order_summary(response_orders)

    warnings: list[str] = []
    if status == "all" and normalized_symbol is None:
        warnings.append(ALL_SYMBOLS_CLOSED_HISTORY_WARNING)

    ret_market = "mixed"
    if len(market_types) == 1:
        ret_market = _normalize_market_type_to_external(market_types[0])
    elif normalized_symbol:
        m, _ = _resolve_market_type(normalized_symbol, None)
        ret_market = _normalize_market_type_to_external(m)

    return {
        "success": bool(response_orders) or not errors,
        "symbol": normalized_symbol,
        "market": ret_market,
        "status": status,
        "filters": {
            "symbol": symbol,
            "status": status,
            "order_id": order_id,
            "market": market_hint,
            "side": side,
            "days": days,
            "limit": limit,
        },
        "orders": response_orders,
        "summary": summary,
        "truncated": truncated,
        "total_available": total_available,
        "errors": errors,
        "warnings": warnings,
    }


async def get_order_history_impl(
    symbol: str | None = None,
    status: Literal["all", "pending", "filled", "cancelled", "expired"] = "all",
    order_id: str | None = None,
    market: str | None = None,
    side: str | None = None,
    days: int | None = None,
    limit: int | None = 50,
    is_mock: bool = False,
) -> dict[str, Any]:
    if is_mock:
        return {
            "success": False,
            "account_mode": "kis_mock",
            "error": "provider kis is not operational",
            "orders": [],
            "errors": [
                {
                    "market": market or "mixed",
                    "error": "provider kis is not operational",
                }
            ],
        }

    (
        symbol,
        order_id,
        market_hint,
        side,
        effective_days,
        limit_val,
        market_types,
        normalized_symbol,
    ) = _validate_history_inputs(symbol, status, order_id, market, side, days, limit)

    orders: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if "crypto" in market_types:
        try:
            fetched = await _fetch_crypto_orders(
                normalized_symbol if market_types == ["crypto"] else None,
                status,
                limit_val,
                limit or 50,
            )
            for order in fetched:
                order["_source_market"] = "crypto"
            orders.extend(fetched)
        except Exception as exc:
            errors.append({"market": "crypto", "error": str(exc)})

    equity_market_types = {
        market_type
        for market_type in market_types
        if market_type in {"equity_kr", "equity_us"}
    }
    if equity_market_types:
        try:
            fetched = await _fetch_toss_orders(
                normalized_symbol if len(market_types) == 1 else None,
                status,
                effective_days,
                limit_val,
            )
            for order in fetched:
                market_type = order.pop("_market_type", None)
                if market_type not in equity_market_types:
                    continue
                order["_source_market"] = _normalize_market_type_to_external(
                    market_type
                )
                orders.append(order)
        except Exception as exc:
            for market_type in sorted(equity_market_types):
                errors.append({"market": market_type, "error": str(exc)})

    orders = _dedupe_orders(orders)
    response_orders, total_available, truncated = _filter_and_sort_orders(
        orders,
        status,
        order_id,
        side,
        limit_val,
    )

    return _build_history_response(
        response_orders=response_orders,
        errors=errors,
        market_types=market_types,
        normalized_symbol=normalized_symbol,
        symbol=symbol,
        status=status,
        order_id=order_id,
        market_hint=market_hint,
        side=side,
        days=days,
        limit=limit,
        truncated=truncated,
        total_available=total_available,
    )


__all__ = [
    "get_order_history_impl",
]
