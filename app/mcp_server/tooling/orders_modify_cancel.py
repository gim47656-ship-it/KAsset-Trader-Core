"""Order modify/cancel normalization helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling.order_execution import (
    _normalize_market_type_to_external,
)
from app.mcp_server.tooling.order_validation import (
    _get_holdings_for_order,
)
from app.mcp_server.tooling.shared import (
    parse_holdings_market_filter as _parse_holdings_market_filter,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import to_float as _to_float


def _kis_non_operational_result(
    *,
    order_id: str,
    symbol: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """등록 해제된 KIS 주문 mutation을 provider 접근 전에 거부한다."""
    result: dict[str, Any] = {
        "success": False,
        "error": "provider kis is not operational",
        "provider_unsupported": True,
        "order_id": order_id,
        "mutation_sent": False,
    }
    if symbol is not None:
        result["symbol"] = symbol
    if dry_run is not None:
        result["dry_run"] = dry_run
    return result


def _map_upbit_state(state: str, filled: float, remaining: float) -> str:
    if state == "wait":
        return "pending"
    if state == "done":
        if filled > 0:
            return "filled"
        return "cancelled"
    if state == "cancelled":
        return "cancelled"
    return "partial"


def _normalize_upbit_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = order.get("side", "")
    side = "buy" if side_code == "bid" else "sell"

    state = order.get("state", "")
    remaining = float(order.get("remaining_volume", 0) or 0)
    filled = float(order.get("executed_volume", 0) or 0)
    ordered = remaining + filled

    ordered_price = float(order.get("price", 0) or 0)
    filled_price = float(order.get("avg_price", 0) or 0)
    status = _map_upbit_state(state, filled, remaining)

    return {
        "order_id": order.get("uuid", ""),
        "symbol": order.get("market", ""),
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": order.get("created_at", ""),
        "filled_at": order.get("done_at", ""),
        "currency": "KRW",
    }


def _validate_cancel_inputs(
    order_id: str,
    symbol: str | None,
    market: str | None,
) -> tuple[str, str | None, str]:
    """Validate and resolve cancel order inputs.

    Returns:
        (order_id, symbol, market_type)
    """
    order_id = (order_id or "").strip()
    if not order_id:
        raise ValueError("order_id is required")

    symbol = (symbol or "").strip() if symbol else None
    market_type = _parse_holdings_market_filter(market)

    if market_type is None:
        if symbol:
            market_type, _ = _resolve_market_type(symbol, None)
        elif "-" in order_id and len(order_id) == 36:
            market_type = "crypto"
        else:
            raise ValueError(
                "market must be specified when symbol is not provided "
                "and order_id is not a UUID"
            )

    return order_id, symbol, market_type


async def _cancel_upbit(
    order_id: str,
    *,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Cancel an Upbit (crypto) order."""
    hook_kw = {"pre_send_hook": pre_send_hook} if pre_send_hook is not None else {}
    results = await upbit_service.cancel_orders(
        [order_id],
        **hook_kw,
    )
    if results and len(results) > 0:
        result = results[0]
        if "error" in result:
            return {
                "success": False,
                "order_id": order_id,
                "error": result.get("error"),
            }
        return {
            "success": True,
            "order_id": order_id,
            "cancelled_at": result.get("created_at", ""),
        }
    return {
        "success": False,
        "order_id": order_id,
        "error": "No result from Upbit",
    }


_KIS_MOCK_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "not available in mock",
    "not supported",
    "unsupported",
    "미지원",
    "tttc8036r",
)


def _is_kis_mock_unsupported(message: str) -> bool:
    """True when a broker error indicates the TR is unsupported in mock mode.

    Conservative: only soft-cancel on these markers. Any other broker error
    (e.g. already-filled, invalid order) is surfaced as a genuine failure.
    The exact marker set is refined after the operator VTTC0013U mock smoke.
    """
    lowered = message.lower()
    return any(marker in lowered for marker in _KIS_MOCK_UNSUPPORTED_MARKERS)


def _kis_mock_expected_claim(
    resolved: dict[str, Any],
) -> tuple[str, str, int | None, str | None] | None:
    """The claim 4-tuple this ledger row belongs to, when the row names one.

    Returns ``None`` when the row carries no J2B lineage — rows written before
    the coordinated lane existed do not, and inventing one here would
    manufacture the very ownership the follow-up gate is checking for.
    """

    scope, key = _kis_mock_claim_identity(resolved)
    if scope is None or key is None:
        return None
    row_id = resolved.get("claim_row_id")
    return (
        scope,
        key,
        row_id if isinstance(row_id, int) else None,
        resolved.get("side"),
    )


def _kis_mock_claim_identity(resolved: dict[str, Any]) -> tuple[str | None, str | None]:
    """The durable claim a follow-up would be amending, if the row names one.

    Read from the ledger row rather than assumed: a row written before the
    coordinated lane existed carries no J2B lineage, and inventing one here
    would manufacture exactly the ownership the follow-up gate is checking for.
    """

    scope = resolved.get("claim_account_scope")
    key = resolved.get("claim_idempotency_key")
    return (
        scope if isinstance(scope, str) and scope.strip() else None,
        key if isinstance(key, str) and key.strip() else None,
    )


async def _cancel_kis_mock_domestic(
    order_id: str,
    symbol: str | None,
) -> dict[str, Any]:
    return _kis_non_operational_result(order_id=order_id, symbol=symbol)


async def _cancel_kis_domestic(
    order_id: str,
    symbol: str | None,
    *,
    is_mock: bool = False,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    _ = (is_mock, pre_send_hook)
    return _kis_non_operational_result(order_id=order_id, symbol=symbol)


async def _cancel_kis_overseas(
    order_id: str,
    symbol: str | None,
    *,
    is_mock: bool = False,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    _ = (is_mock, pre_send_hook)
    return _kis_non_operational_result(order_id=order_id, symbol=symbol)


async def cancel_order_impl(
    order_id: str,
    symbol: str | None = None,
    market: str | None = None,
    *,
    is_mock: bool = False,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    order_id, symbol, market_type = _validate_cancel_inputs(order_id, symbol, market)
    # 레거시 구현은 Upbit 취소만 허용한다. 주식은 Toss 취소 도구를 거쳐야 한다.
    if market_type != "crypto":
        return {
            "success": False,
            "order_id": order_id,
            "symbol": symbol,
            "error": "provider kis is not operational",
            "mutation_sent": False,
        }

    try:
        if market_type == "crypto":
            return await _cancel_upbit(
                order_id,
                pre_send_hook=pre_send_hook,
            )
        if market_type == "equity_kr":
            result = await _cancel_kis_domestic(
                order_id,
                symbol,
                is_mock=is_mock,
                pre_send_hook=pre_send_hook,
            )
            # ROB-395: keep the live ledger truthful — a cancelled live order must
            # not stay accepted/pending (otherwise reconcile could still act on it).
            if not is_mock and result.get("success"):
                from app.mcp_server.tooling.kis_live_ledger import (
                    _mark_ledger_cancelled,
                )

                await _mark_ledger_cancelled(order_id)
            return result
        if market_type == "equity_us":
            return await _cancel_kis_overseas(
                order_id,
                symbol,
                is_mock=is_mock,
                pre_send_hook=pre_send_hook,
            )
        return {
            "success": False,
            "order_id": order_id,
            "error": "Unsupported market type",
        }
    except Exception as exc:
        return {
            "success": False,
            "order_id": order_id,
            "error": str(exc),
        }


def _validate_modify_inputs(
    order_id: str,
    symbol: str,
    market: str | None,
    new_price: float | None,
    new_quantity: float | None,
) -> tuple[str, str, str, str]:
    """Validate modify order inputs.

    Returns:
        (order_id, symbol, market_type, normalized_symbol)
    """
    if new_price is None and new_quantity is None:
        raise ValueError("At least one of new_price or new_quantity must be specified")
    if new_price is not None and new_price <= 0:
        raise ValueError("new_price must be a positive number")
    if new_quantity is not None and new_quantity <= 0:
        raise ValueError("new_quantity must be a positive number")

    order_id = order_id.strip()
    symbol = symbol.strip()
    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    return order_id, symbol, market_type, normalized_symbol


async def _live_sell_reprice_floor_error(
    *,
    normalized_symbol: str,
    market_type: str,
    side: str,
    new_price: float | None,
) -> str | None:
    """ROB-518: live sell orders must not be repriced below avg_buy * 1.01.

    Placement-time guards do not re-run on modify, so without this check a
    guarded live sell could be repriced into a loss. Callers are live-only
    (mock modifies delegate/return before reaching this). Quantity-only
    modifies (new_price=None) and buy orders introduce no new price risk.
    Unknown cost basis (no holdings row / avg<=0) stays fail-open, matching
    the placement-guard semantics.
    """
    if new_price is None or side != "sell":
        return None
    holdings = await _get_holdings_for_order(
        normalized_symbol, market_type, is_mock=False
    )
    if not holdings:
        return None
    avg_price = _to_float(holdings.get("avg_price"), default=0.0)
    if avg_price <= 0:
        return None
    min_sell_price = avg_price * 1.01
    if float(new_price) < min_sell_price:
        return (
            f"Live sell modify blocked: new price {new_price} below minimum "
            f"(avg_buy_price * 1.01 = {round(min_sell_price, 4)}). "
            "Loss-selling is disabled on live accounts (ROB-518)."
        )
    return None


def _build_modify_dry_run_response(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Build dry-run preview response for modify order."""
    changes: dict[str, Any] = {
        "price": {"from": None, "to": new_price} if new_price else None,
        "quantity": ({"from": None, "to": new_quantity} if new_quantity else None),
    }
    return {
        "success": True,
        "status": "simulated",
        "order_id": order_id,
        "symbol": normalized_symbol,
        "market": _normalize_market_type_to_external(market_type),
        "changes": changes,
        "method": "dry_run",
        "dry_run": dry_run,
        "message": f"Dry run - Preview changes for order {order_id}",
    }


async def _modify_upbit(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Modify an Upbit (crypto) order via cancel-and-reorder."""
    try:
        original_order = await upbit_service.fetch_order_detail(order_id)

        if original_order.get("state") != "wait":
            return {
                "success": False,
                "status": "failed",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "error": "Order not in wait state (cannot modify non-pending orders)",
                "dry_run": dry_run,
            }
        if original_order.get("ord_type") != "limit":
            return {
                "success": False,
                "status": "failed",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "error": "Only limit orders can be modified (not market orders)",
                "dry_run": dry_run,
            }

        original_price = float(original_order.get("price", 0) or 0)
        original_quantity = float(original_order.get("remaining_volume", 0) or 0)
        final_price = new_price if new_price is not None else original_price
        final_quantity = new_quantity if new_quantity is not None else original_quantity

        # ROB-518 — Upbit modifies are always live (real funds).
        side = "buy" if original_order.get("side") == "bid" else "sell"
        floor_error = await _live_sell_reprice_floor_error(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            side=side,
            new_price=new_price,
        )
        if floor_error is not None:
            return {
                "success": False,
                "status": "failed",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "error": floor_error,
                "method": "cancel_reorder",
                "dry_run": dry_run,
            }

        result = await upbit_service.cancel_and_reorder(
            order_id, final_price, final_quantity
        )
        changes = {
            "price": {"from": original_price, "to": final_price}
            if final_price != original_price
            else None,
            "quantity": {"from": original_quantity, "to": final_quantity}
            if final_quantity != original_quantity
            else None,
        }

        if result.get("new_order") and "uuid" in result["new_order"]:
            return {
                "success": True,
                "status": "modified",
                "order_id": order_id,
                "new_order_id": result["new_order"]["uuid"],
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "changes": changes,
                "method": "cancel_reorder",
                "dry_run": dry_run,
                "message": "Order modified via cancel and reorder",
            }
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "market": _normalize_market_type_to_external(market_type),
            "error": result.get("cancel_result", {}).get("error", "Unknown error"),
            "changes": changes,
            "method": "cancel_reorder",
            "dry_run": dry_run,
        }
    except Exception as exc:
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "market": _normalize_market_type_to_external(market_type),
            "error": str(exc),
            "changes": None,
            "method": "cancel_reorder",
            "dry_run": dry_run,
        }


async def _modify_kis_mock_domestic(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    _ = (market_type, new_price, new_quantity)
    return _kis_non_operational_result(
        order_id=order_id,
        symbol=normalized_symbol,
        dry_run=dry_run,
    )


async def _modify_kis_domestic(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
    *,
    is_mock: bool = False,
) -> dict[str, Any]:
    _ = (market_type, new_price, new_quantity, is_mock)
    return _kis_non_operational_result(
        order_id=order_id,
        symbol=normalized_symbol,
        dry_run=dry_run,
    )


async def _modify_kis_overseas(
    order_id: str,
    normalized_symbol: str,
    market_type: str,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
    *,
    is_mock: bool = False,
) -> dict[str, Any]:
    _ = (market_type, new_price, new_quantity, is_mock)
    return _kis_non_operational_result(
        order_id=order_id,
        symbol=normalized_symbol,
        dry_run=dry_run,
    )


async def modify_order_impl(
    order_id: str,
    symbol: str,
    market: str | None = None,
    new_price: float | None = None,
    new_quantity: float | None = None,
    dry_run: bool = True,
    *,
    is_mock: bool = False,
) -> dict[str, Any]:
    order_id, symbol, market_type, normalized_symbol = _validate_modify_inputs(
        order_id, symbol, market, new_price, new_quantity
    )
    # 레거시 구현은 Upbit 정정만 허용한다. 주식은 Toss 정정 도구를 거쳐야 한다.
    if market_type != "crypto":
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "market": _normalize_market_type_to_external(market_type),
            "error": "provider kis is not operational",
            "dry_run": dry_run,
            "mutation_sent": False,
        }

    if dry_run:
        return _build_modify_dry_run_response(
            order_id,
            normalized_symbol,
            market_type,
            new_price,
            new_quantity,
            dry_run,
        )

    if market_type == "crypto":
        return await _modify_upbit(
            order_id,
            normalized_symbol,
            market_type,
            new_price,
            new_quantity,
            dry_run,
        )
    if market_type == "equity_kr":
        result = await _modify_kis_domestic(
            order_id,
            normalized_symbol,
            market_type,
            new_price,
            new_quantity,
            dry_run,
            is_mock=is_mock,
        )
        # ROB-395: KIS 정정주문 issues a new odno; re-point the live ledger row so
        # reconcile tracks the replacement instead of orphaning it.
        if not is_mock and result.get("success"):
            from app.mcp_server.tooling.kis_live_ledger import (
                _repoint_ledger_after_modify,
            )

            await _repoint_ledger_after_modify(
                old_order_no=order_id,
                new_order_no=result.get("new_order_id"),
                new_price=new_price,
                new_quantity=new_quantity,
            )
        return result
    if market_type == "equity_us":
        return await _modify_kis_overseas(
            order_id,
            normalized_symbol,
            market_type,
            new_price,
            new_quantity,
            dry_run,
            is_mock=is_mock,
        )

    return {
        "success": False,
        "status": "failed",
        "order_id": order_id,
        "symbol": normalized_symbol,
        "market": _normalize_market_type_to_external(market_type),
        "error": f"modify_order is not supported for market '{market_type}'",
        "dry_run": dry_run,
    }


__all__ = [
    "cancel_order_impl",
    "modify_order_impl",
    "_normalize_upbit_order",
]
