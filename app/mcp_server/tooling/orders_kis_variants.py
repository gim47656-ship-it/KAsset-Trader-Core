"""Typed KIS order tool variants: kis_live_* and kis_mock_*.

Each variant is a thin wrapper that:
- Hard-pins is_mock (live=False, mock=True).
- Validates any supplied account_mode/account_type matches the pinned mode.
- For mock variants: fails closed via _mock_config_error() before delegating.
- Delegates to existing order implementation functions.
- Wraps response in apply_account_routing_metadata for a consistent envelope.

The original ambiguous place_order/cancel_order/modify_order/get_order_history
tools in orders_registration.py are unchanged; these are additive.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import validate_kis_mock_config
from app.core.symbol import to_db_symbol
from app.mcp_server.tooling import order_execution, orders_history
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_KIS_LIVE,
    ACCOUNT_MODE_KIS_MOCK,
    AccountRouting,
    apply_account_routing_metadata,
)
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)
from app.services.brokers.kis.live_order_expiry import (
    kr_day_order_expiry,
    parse_kis_ordered_at,
    row_has_cancel_evidence,
)
from app.services.brokers.kis.overseas_orders import _normalize_kis_exchange_code
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.warnings_guard import (
    WarningsGuardResult,
    check_warnings_guard,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

KIS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "kis_live_place_order",
    "kis_live_cancel_order",
    "kis_live_modify_order",
    "kis_live_get_order_history",
    "kis_live_reconcile_orders",
}

KIS_MOCK_ORDER_TOOL_NAMES: set[str] = {
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kis_mock_get_order_history",
}


# ---------------------------------------------------------------------------
# Shared guard/delegation helpers
# ---------------------------------------------------------------------------


def _pinned_routing(account_mode: str) -> AccountRouting:
    return AccountRouting(account_mode=account_mode)


def _is_mock_mode(pinned_mode: str) -> bool:
    return pinned_mode == ACCOUNT_MODE_KIS_MOCK


def _mock_config_error() -> dict[str, Any] | None:
    missing = validate_kis_mock_config()
    if not missing:
        return None
    return {
        "success": False,
        "error": (
            "KIS mock account is disabled or missing required configuration: "
            + ", ".join(missing)
        ),
        "source": "kis",
        "account_mode": ACCOUNT_MODE_KIS_MOCK,
    }


def _check_mode_arg(
    tool_name: str,
    pinned_mode: str,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any] | None:
    """Return a structured rejection if account_mode or account_type mismatches pinned_mode."""
    for param_name, value in (
        ("account_mode", account_mode),
        ("account_type", account_type),
    ):
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized and normalized != pinned_mode:
            return {
                "success": False,
                "error": (
                    f"{tool_name} does not accept {param_name}='{value}'; "
                    f"this tool is pinned to account_mode='{pinned_mode}'"
                ),
                "source": "mcp",
                "account_mode": pinned_mode,
            }
    return None


def _warning_payload(result: WarningsGuardResult) -> list[dict[str, str | None]]:
    return [
        {
            "warning_type": warning.warning_type,
            "exchange": warning.exchange,
            "start_date": warning.start_date,
            "end_date": warning.end_date,
        }
        for warning in result.warnings
    ]


async def _check_toss_warnings_for_kis_buy(symbol: str) -> WarningsGuardResult:
    client = None
    try:
        client = TossReadClient.from_settings()
        # ROB-550: market=None lets the guard auto-detect KR by the 6-digit
        # symbol pattern, so a US KIS buy (e.g. AAPL) skips the Toss warnings
        # fetch instead of issuing a wasted lookup.
        return await check_warnings_guard(client, symbol, market=None)
    except Exception as exc:
        logger.warning(
            "Failed to check Toss warnings for KIS live order symbol=%s; proceeding fail-open: %s",
            symbol,
            exc,
            exc_info=True,
        )
        return WarningsGuardResult(
            ok=True,
            warnings=[],
            error_message=f"Warnings check failed: {exc} (fail-open)",
        )
    finally:
        if client is not None:
            await client.aclose()


def _prepare_variant_call(
    tool_name: str,
    pinned_mode: str,
    account_mode: str | None,
    account_type: str | None,
) -> tuple[AccountRouting, dict[str, Any] | None]:
    routing = _pinned_routing(pinned_mode)
    rejection = _check_mode_arg(tool_name, pinned_mode, account_mode, account_type)
    if rejection:
        return routing, rejection
    if _is_mock_mode(pinned_mode):
        config_error = _mock_config_error()
        if config_error:
            return routing, apply_account_routing_metadata(config_error, routing)
    return routing, None


def _limit_order_error(tool_name: str, symbol: str, order_type: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"{tool_name} only supports limit orders.",
        "source": "mcp",
        "symbol": symbol,
        "order_type": order_type,
    }


# ROB-463: NXT venue, TIF/order-validity, and 예약주문 are requested capabilities
# but require operator confirmation of the exact KIS wire codes
# (EXCG_ID_DVSN_CD='NXT', ORD_COND_DVSN_CD, RSV_ORD_TIME) before any live order
# can carry them. Until then these knobs are surfaced (so callers get an explicit,
# actionable response instead of silent non-support) but fail closed — no live
# order is placed, even in dry_run. Day orders auto-route via SOR (NXT-eligible) /
# KRX exactly as before. See docs/superpowers/specs for the gated questions.
_SUPPORTED_VENUES = {None, "auto"}
_SUPPORTED_ORDER_VALIDITIES = {None, "day"}


def _venue_tif_gate(
    tool_name: str,
    symbol: str,
    *,
    venue: str | None,
    order_validity: str | None,
    reserved_time: str | None,
) -> dict[str, Any] | None:
    """Return a fail-closed error payload for not-yet-enabled venue/TIF knobs.

    None means the request uses only the supported (auto-route, day) behaviour
    and may proceed unchanged.
    """
    norm_venue = (venue or "").strip().lower() or None
    norm_validity = (order_validity or "").strip().lower() or None
    norm_reserved = (reserved_time or "").strip() or None

    blocked: str | None = None
    if norm_venue not in _SUPPORTED_VENUES:
        blocked = f"venue={venue!r} (explicit KRX/NXT/unified routing)"
    elif norm_validity not in _SUPPORTED_ORDER_VALIDITIES:
        blocked = f"order_validity={order_validity!r} (TIF / 예약주문 / gtc)"
    elif norm_reserved is not None:
        blocked = f"reserved_time={reserved_time!r} (예약주문 / scheduled order)"

    if blocked is None:
        return None

    return {
        "success": False,
        "error": "venue_tif_pending_operator_confirmation",
        "source": "mcp",
        "tool": tool_name,
        "symbol": symbol,
        "blocked": blocked,
        "linear": "ROB-463",
        "reason": (
            f"{blocked} is not yet enabled for KIS live orders. NXT venue, "
            "order validity/TIF, and 예약주문 require operator confirmation of "
            "the exact KIS wire codes (EXCG_ID_DVSN_CD='NXT', ORD_COND_DVSN_CD, "
            "RSV_ORD_TIME) before a live order can carry them — see ROB-463. "
            "Until then orders auto-route via SOR (NXT-eligible) / KRX as day "
            "orders; leave venue/order_validity/reserved_time unset (or "
            "venue='auto', order_validity='day')."
        ),
    }


async def _place_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit"],
    quantity: float | None,
    price: float | None,
    amount: float | None,
    dry_run: bool,
    reason: str,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    defensive_trim: bool,
    approval_issue_id: str | None,
    exit_intent: str | None = None,
    retrospective_id: int | None = None,
    account_mode: str | None,
    account_type: str | None,
    report_item_uuid: str | None = None,
    approval_hash: str | None = None,
    rung: str | int | None = None,
) -> dict[str, Any]:  # NOSONAR - mirrors the public MCP order contract.
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    if str(order_type).lower().strip() != "limit":
        return _limit_order_error(tool_name, symbol, order_type)

    warning_result: WarningsGuardResult | None = None
    is_live_buy = pinned_mode == ACCOUNT_MODE_KIS_LIVE and str(side).lower() == "buy"
    if is_live_buy:
        warning_result = await _check_toss_warnings_for_kis_buy(symbol)
        if not dry_run and not warning_result.ok:
            return {
                "success": False,
                "source": "kis",
                "account_mode": ACCOUNT_MODE_KIS_LIVE,
                "dry_run": dry_run,
                "mutation_sent": False,
                "error": warning_result.error_message,
                "warnings": _warning_payload(warning_result),
            }

    result = apply_account_routing_metadata(
        await order_execution._place_order_impl(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim=defensive_trim,
            approval_issue_id=approval_issue_id,
            exit_intent=exit_intent,
            retrospective_id=retrospective_id,
            is_mock=_is_mock_mode(pinned_mode),
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash,
            rung=rung,
        ),
        routing,
    )
    if warning_result is not None:
        result["warnings"] = _warning_payload(warning_result)
        if warning_result.error_message:
            result["warnings_check_message"] = warning_result.error_message
    return result


async def _cancel_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    order_id: str,
    symbol: str | None,
    market: str | None,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await cancel_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _modify_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    order_id: str,
    symbol: str,
    market: str | None,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await modify_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _get_order_history_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    symbol: str | None,
    status: Literal["all", "pending", "filled", "cancelled", "expired"],
    order_id: str | None,
    market: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await orders_history.get_order_history_impl(
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _reconcile_orders_variant(
    *,
    symbol: str | None,
    order_id: str | None,
    dry_run: bool,
    limit: int,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        "kis_live_reconcile_orders", ACCOUNT_MODE_KIS_LIVE, account_mode, account_type
    )
    if early_response:
        return early_response
    from app.mcp_server.tooling.kis_live_ledger import (
        kis_live_reconcile_orders_impl,
    )

    return apply_account_routing_metadata(
        await kis_live_reconcile_orders_impl(
            symbol=symbol, order_id=order_id, dry_run=dry_run, limit=limit
        ),
        routing,
    )


# ---------------------------------------------------------------------------
# Live variants (is_mock=False hard-pinned)
# ---------------------------------------------------------------------------


def register_kis_live_order_tools(mcp: FastMCP) -> None:
    """Register kis_live_* typed order tools (is_mock=False hard-pinned)."""
    _PINNED = ACCOUNT_MODE_KIS_LIVE

    @mcp.tool(
        name="kis_live_place_order",
        description=(
            "Place a LIMIT buy/sell order on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "dry_run=True by default for safety. "
            "For buy orders (dry_run=False), thesis and strategy are required. "
            "Normal weight-management trims do NOT need defensive_trim — leave "
            "it False. defensive_trim=True is disabled on this direct tool; use "
            "order_proposal_create for the human-confirmed proposal path. "
            "Orders auto-route via SOR (NXT-eligible) / KRX as day orders. "
            "venue (krx|nxt|unified), order_validity (day|예약|gtc), and "
            "reserved_time are accepted but NOT yet enabled — NXT/TIF/예약주문 "
            "require operator confirmation of the exact KIS wire codes "
            "(ROB-463) and currently fail closed with an explicit error (no live "
            "order, even in dry_run); leave them unset for normal day orders. "
            "report item에서 비롯된 주문이면 investment_report_get의 item_uuid를 report_item_uuid로 넘겨 감사 링크(ROB-473). "
            "Fills are NOT recorded at send time; run "
            "kis_live_reconcile_orders (or enable the operator-gated "
            "kis_live.reconcile_periodic task, ROB-475) to book "
            "fill/journal/realized_pnl. reconcile is the LOCAL bookkeeping "
            "layer; the live-account truth is get_holdings / "
            "get_available_capital. "
            "For multi-rung limit ladders, run sell_ladder_fill_preview "
            "(sells, ROB-477) or buy_ladder_fill_preview (buys, ROB-507) "
            "first to check zero-fill risk. Pass rung (ladder level) so sibling "
            "ladder orders on the same trading day stay distinct. "
            "Approval-hash binding (ORDER_APPROVAL_HASH_MODE, default optional): the "
            "dry_run=True preview mints approval_hash (self-contained token, 5-minute "
            "TTL) + idempotency_key; pass approval_hash back (same rung) so live send "
            "re-derives the canonical order and fail-closes on mismatch/expiry. "
            "off=ignored; optional=verified only when supplied; warn=logs a hash-less "
            "live send; required=mandatory (this live tool must supply a hash). "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
            ' ROB-864 exit_intent="loss_cut" is disabled on this direct tool. Use '
            "order_proposal_create; Telegram performs two-click confirmation with a "
            "single-use nonce and second-click full revalidation; that proposal "
            "flow requires approval_issue_id."
        ),
    )
    async def kis_live_place_order(  # NOSONAR - public MCP order schema mirrors legacy tool.
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        exit_reason: str | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: float | None = None,
        stop_loss: float | None = None,
        min_hold_days: int | None = None,
        notes: str | None = None,
        indicators_snapshot: dict[str, Any] | None = None,
        defensive_trim: bool = False,
        approval_issue_id: str | None = None,
        exit_intent: str | None = None,
        retrospective_id: int | None = None,
        venue: str | None = None,
        order_validity: str | None = None,
        reserved_time: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
        report_item_uuid: str | None = None,
        approval_hash: str | None = None,
        rung: str | int | None = None,
    ) -> dict[str, Any]:
        gate = _venue_tif_gate(
            "kis_live_place_order",
            symbol,
            venue=venue,
            order_validity=order_validity,
            reserved_time=reserved_time,
        )
        if gate is not None:
            return gate
        return await _place_order_variant(
            tool_name="kis_live_place_order",
            pinned_mode=_PINNED,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim=defensive_trim,
            approval_issue_id=approval_issue_id,
            exit_intent=exit_intent,
            retrospective_id=retrospective_id,
            account_mode=account_mode,
            account_type=account_type,
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash,
            rung=rung,
        )

    @mcp.tool(
        name="kis_live_cancel_order",
        description=(
            "Cancel a pending order on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _cancel_order_variant(
            tool_name="kis_live_cancel_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_modify_order",
        description=(
            "Modify a pending order (price/quantity) on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. dry_run=True by default for safety. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        del reason
        return await _modify_order_variant(
            tool_name="kis_live_modify_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_get_order_history",
        description=(
            "Get order history on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "status='expired' returns dead day orders (nothing filled, nothing "
            "left to modify/cancel — EOD expiry/reject), distinct from an "
            "operator cancel (status='cancelled'). Each order carries is_live "
            "(true only for pending/partial). "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled", "expired"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _get_order_history_variant(
            tool_name="kis_live_get_order_history",
            pinned_mode=_PINNED,
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_reconcile_orders",
        description=(
            "Reconcile accepted/pending KIS live (real-money) KR orders against "
            "order-id-keyed broker fill evidence (inquire_daily_order_domestic). "
            "Books fills/journals/realized_pnl ONLY from confirmed fills "
            "(delta-idempotent). Missing evidence is fail-closed: rows are left "
            "open with requires_manual_review instead of being marked cancelled. "
            "Stale unfilled day orders are resolved to 'expired' only after "
            "NXT close (20:00 KST) AND broker evidence (rjct_qty == ord_qty); "
            "cancel-confirm rows resolve to 'cancelled'. Evidence is queried "
            "from each order's send date through today (90-day cap), so "
            "next-day reconciles still book prior-day fills. "
            "dry_run=True by default for safety. KR domestic only. "
            "realized_pnl_pct (alias journal_pnl_pct, labeled "
            "realized_pnl_basis='journal_entry') is the per-lot / journal-entry "
            "(FIFO oldest-first) basis, NOT the account-average; "
            "place_order preview / get_holdings / get_available_capital remain "
            "the account-average (pchs_avg_pric) truth. "
            "This is the LOCAL bookkeeping layer (trade/journal/"
            "realized_pnl); the live-account truth is get_holdings / "
            "get_available_capital. An operator-gated periodic auto-"
            "reconcile task exists (kis_live.reconcile_periodic, ROB-475)."
        ),
    )
    async def kis_live_reconcile_orders(
        symbol: str | None = None,
        order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _reconcile_orders_variant(
            symbol=symbol,
            order_id=order_id,
            dry_run=dry_run,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )


# ---------------------------------------------------------------------------
# Mock variants (is_mock=True hard-pinned)
# ---------------------------------------------------------------------------


def register_kis_mock_order_tools(mcp: FastMCP) -> None:
    """Register kis_mock_* typed order tools (is_mock=True hard-pinned)."""
    _PINNED = ACCOUNT_MODE_KIS_MOCK

    @mcp.tool(
        name="kis_mock_place_order",
        description=(
            "Place a LIMIT buy/sell order on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config "
            "(KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, "
            "KIS_MOCK_ACCOUNT_NO) is missing. "
            "dry_run=True by default for safety. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_place_order(  # NOSONAR - public MCP order schema mirrors legacy tool.
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        exit_reason: str | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: float | None = None,
        stop_loss: float | None = None,
        min_hold_days: int | None = None,
        notes: str | None = None,
        indicators_snapshot: dict[str, Any] | None = None,
        defensive_trim: bool = False,
        approval_issue_id: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
        report_item_uuid: str | None = None,
    ) -> dict[str, Any]:
        return await _place_order_variant(
            tool_name="kis_mock_place_order",
            pinned_mode=_PINNED,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim=defensive_trim,
            approval_issue_id=approval_issue_id,
            account_mode=account_mode,
            account_type=account_type,
            report_item_uuid=report_item_uuid,
        )

    @mcp.tool(
        name="kis_mock_cancel_order",
        description=(
            "Cancel a pending order on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _cancel_order_variant(
            tool_name="kis_mock_cancel_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_mock_modify_order",
        description=(
            "Modify a pending order (price/quantity) on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "dry_run=True by default for safety. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        del reason
        return await _modify_order_variant(
            tool_name="kis_mock_modify_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_mock_get_order_history",
        description=(
            "Get order history on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "status='expired' returns dead day orders (nothing filled, nothing "
            "left to modify/cancel), distinct from an operator cancel "
            "(status='cancelled'). Each order carries is_live (true only for "
            "pending/partial). "
            "Note: some KR order history endpoints (e.g. TTTC8036R) are unsupported "
            "in KIS mock and return mock_unsupported-tagged errors. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled", "expired"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _get_order_history_variant(
            tool_name="kis_mock_get_order_history",
            pinned_mode=_PINNED,
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )


def _get_kis_field(order: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = order.get(key)
        if value:
            return value
    return default


def _extract_kis_order_number(order: dict[str, Any]) -> str:
    value = _get_kis_field(
        order,
        "odno",
        "ODNO",
        "ord_no",
        "ORD_NO",
        "orgn_odno",
        "ORGN_ODNO",
        default="",
    )
    if value is None:
        return ""
    return str(value).strip()


def _build_temp_kr_order_id(
    *,
    symbol: str,
    side: str,
    ordered_price: int,
    ordered_qty: int,
    ordered_at: str,
) -> str:
    raw = "|".join(
        [symbol, side, str(ordered_price), str(ordered_qty), ordered_at.strip()]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
    return f"TEMP_KR_{digest}"


def _map_kis_status(
    ordered: int,
    filled: int,
    remaining: int,
    status_name: str | None,
    *,
    cancel_evidence: bool = False,
) -> str:
    normalized_name = str(status_name or "").strip()

    # Explicit cancel evidence is authoritative at any point. ROB-665: the real
    # broker signal is cancel_evidence (cncl_yn / '취소' side name); the legacy
    # `prcs_stat_name == "주문취소"` key does not exist on live responses.
    if cancel_evidence or normalized_name == "주문취소":
        return "cancelled"
    # ROB-657: nothing filled and nothing left to modify/cancel
    # (정정취소가능수량 0) means the order is dead (EOD expiry / reject).
    # KIS ledger truth is "alive iff rmn_qty > 0", so this wins over a
    # stale '접수' status name that TTTC8036R may still carry.
    if ordered > 0 and filled == 0 and remaining <= 0:
        return "expired"
    if normalized_name in ("접수", "주문접수"):
        return "pending"
    if normalized_name == "체결":
        if filled > 0 and remaining > 0:
            return "partial"
        return "filled"
    if normalized_name == "미체결":
        return "pending"

    if filled > 0 and remaining <= 0:
        return "filled"
    if filled > 0 and remaining > 0:
        return "partial"
    return "pending"


_US_DAY_ORDER_REASON = "us_day_order"


def _kr_history_expiry_reason(*, ordered_at: str, side: str) -> str | None:
    """Categorical session×side expiry reason for a KR order-history row.

    Read-path classification only (no 15:30 downgrade — that is a live send-path
    decision). Returns None when ``ordered_at`` cannot be parsed.
    """
    accepted_at = parse_kis_ordered_at(ordered_at)
    if accepted_at is None:
        return None
    return kr_day_order_expiry(accepted_at=accepted_at, side=side)[1]


def _normalize_kis_domestic_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(float(_get_kis_field(order, "ord_qty", "ORD_QTY", default=0) or 0))
    filled = int(
        float(
            _get_kis_field(
                order,
                "ccld_qty",
                "CCLD_QTY",
                "tot_ccld_qty",
                "TOT_CCLD_QTY",
                default=0,
            )
            or 0
        )
    )

    remaining = int(
        float(
            _get_kis_field(order, "rmn_qty", "RMN_QTY", default=ordered - filled) or 0
        )
    )

    ordered_price = int(
        float(_get_kis_field(order, "ord_unpr", "ORD_UNPR", default=0) or 0)
    )
    filled_price = int(
        float(
            _get_kis_field(
                order,
                "ccld_unpr",
                "CCLD_UNPR",
                "avg_prvs",
                "AVG_PRVS",
                default=0,
            )
            or 0
        )
    )

    status = _map_kis_status(
        ordered,
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
        cancel_evidence=row_has_cancel_evidence(order),
    )
    symbol = str(_get_kis_field(order, "pdno", "PDNO"))
    ordered_at = (
        f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
        f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
    )
    order_id = _extract_kis_order_number(order)
    if not order_id:
        order_id = _build_temp_kr_order_id(
            symbol=symbol,
            side=side,
            ordered_price=ordered_price,
            ordered_qty=ordered,
            ordered_at=ordered_at,
        )
        logger.warning(
            "Missing order_id for KR order (symbol=%s, side=%s, qty=%s, price=%s, ordered_at=%s), generated %s",
            symbol,
            side,
            ordered,
            ordered_price,
            ordered_at,
            order_id,
        )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "is_live": status in ("pending", "partial"),
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": ordered_at,
        "filled_at": "",
        "expiry_reason": _kr_history_expiry_reason(ordered_at=ordered_at, side=side),
        "currency": "KRW",
    }


_DEFAULT_US_CANCEL_EXCHANGES = ["NASD", "NYSE", "AMEX"]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def _build_us_exchange_candidates(symbol: str | None) -> list[str]:
    """Build a list of exchange candidates for US cancel lookups.

    Prioritizes DB lookup result if symbol is provided, then adds
    default exchanges as fallbacks. Results are deduplicated.
    """
    candidates: list[str] = []
    if symbol:
        try:
            db_exchange = await get_us_exchange_by_symbol(symbol)
            candidates.append(_normalize_kis_exchange_code(db_exchange))
        except Exception as exc:
            logger.warning(
                "US exchange lookup failed for cancel: symbol=%s error=%s",
                symbol,
                exc,
            )
    candidates.extend(_DEFAULT_US_CANCEL_EXCHANGES)
    return _dedupe_preserve_order(candidates)


async def _find_us_open_order_by_id(
    kis: Any,
    order_id: str,
    symbol: str | None,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Find an open order by ID across candidate exchanges.

    Returns:
        Tuple of (order_dict, order_exchange, exchange_candidates).
        order_exchange is extracted from the order payload (ovrs_excg_cd)
        and normalized, falling back to the queried exchange if not present.
    """
    exchange_candidates = await _build_us_exchange_candidates(symbol)
    for exchange in exchange_candidates:
        try:
            open_orders = await kis.inquire_overseas_orders(exchange)
        except Exception as exc:
            logger.warning(
                "US open-order lookup failed: order_id=%s symbol=%s exchange=%s error=%s",
                order_id,
                symbol,
                exchange,
                exc,
            )
            continue

        for order in open_orders:
            if _extract_kis_order_number(order) == order_id:
                # Prefer order payload's exchange over queried exchange
                order_exchange = str(
                    order.get("ovrs_excg_cd") or order.get("OVRS_EXCG_CD") or exchange
                ).strip()
                return order, order_exchange, exchange_candidates

    return None, None, exchange_candidates


async def _find_us_order_in_recent_history(
    kis: Any,
    order_id: str,
    symbol: str,
    exchange_candidates: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find an order in recent daily order history when not in open orders.

    Searches a narrow recent window (last 7 days) across exchange candidates.
    Post-filters by order_id and symbol since the KIS API's order_number
    parameter is not supported for overseas orders.

    Returns:
        Tuple of (order_dict, order_exchange) or (None, None) if not found.
    """
    from datetime import datetime, timedelta

    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)

    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    for exchange in exchange_candidates:
        try:
            history = await kis.inquire_daily_order_overseas(
                start_date=start_str,
                end_date=end_str,
                symbol="%",  # Get all and filter client-side
                exchange_code=exchange,
            )
        except Exception as exc:
            logger.warning(
                "US history lookup failed: order_id=%s symbol=%s exchange=%s error=%s",
                order_id,
                symbol,
                exchange,
                exc,
            )
            continue

        for order in history:
            if _extract_kis_order_number(order) == order_id:
                # Verify symbol matches to avoid false positives
                order_symbol = _get_kis_field(order, "pdno", "PDNO", default="")
                if to_db_symbol(str(order_symbol)) != to_db_symbol(symbol):
                    continue
                # Prefer order payload's exchange
                order_exchange = str(
                    order.get("ovrs_excg_cd") or order.get("OVRS_EXCG_CD") or exchange
                ).strip()
                return order, order_exchange

    return None, None


def _normalize_kis_overseas_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(
        float(_get_kis_field(order, "ft_ord_qty", "FT_ORD_QTY", default=0) or 0)
    )
    filled = int(
        float(_get_kis_field(order, "ft_ccld_qty", "FT_CCLD_QTY", default=0) or 0)
    )
    # ROB-665 item 4: prefer the broker's 미체결수량 (nccs_qty) — a cancelled
    # unfilled order reports nccs_qty=0, whereas synthesizing ordered-filled
    # kept it pending+is_live. Fall back to ordered-filled when absent.
    nccs_raw = _get_kis_field(order, "nccs_qty", "NCCS_QTY")
    if nccs_raw is not None and str(nccs_raw).strip() != "":
        remaining = int(float(nccs_raw))
    else:
        remaining = ordered - filled

    ordered_price = float(
        _get_kis_field(order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0) or 0
    )
    filled_price = float(
        _get_kis_field(order, "ft_ccld_unpr3", "FT_CCLD_UNPR3", default=0) or 0
    )

    status = _map_kis_status(
        ordered,
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
        cancel_evidence=row_has_cancel_evidence(order),
    )

    return {
        "order_id": _extract_kis_order_number(order),
        "symbol": _get_kis_field(order, "pdno", "PDNO"),
        "side": side,
        "status": status,
        "is_live": status in ("pending", "partial"),
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": (
            f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
            f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
        ),
        "filled_at": "",
        "expiry_reason": _US_DAY_ORDER_REASON,
        "currency": "USD",
    }
