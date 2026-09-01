"""Orders MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.mcp_server.tooling import order_execution, orders_history, orders_toss_variants
from app.mcp_server.tooling.account_modes import (
    AccountRouting,
    apply_account_routing_metadata,
    normalize_account_mode,
)
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)
from app.mcp_server.tooling.paper_order_handler import (
    _get_paper_order_history,
    _place_paper_order,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
    "sell_ladder_fill_preview",
    "buy_ladder_fill_preview",
}

_KIS_NOT_OPERATIONAL = "provider kis is not operational"


def _selector_was_explicit(
    account_mode: str | None,
    account_type: str | None,
) -> bool:
    return account_mode is not None or account_type is not None


def _kis_not_operational_response(
    routing: AccountRouting,
    **context: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error": _KIS_NOT_OPERATIONAL,
        "account_mode": routing.account_mode,
        **context,
    }


def _toss_crypto_mismatch_response(**context: Any) -> dict[str, Any]:
    return {
        "success": False,
        "error": "account_mode='toss_live' does not support crypto; omit the selector for Upbit",
        "account_mode": "toss_live",
        **context,
    }


def _routing_for_implicit_crypto(
    *,
    routing: AccountRouting,
    account_mode: str | None,
    account_type: str | None,
) -> AccountRouting | None:
    if _selector_was_explicit(account_mode, account_type):
        return None
    return AccountRouting(account_mode="upbit")


def _looks_like_upbit_order_id(order_id: str) -> bool:
    value = str(order_id or "").strip()
    return len(value) == 36 and value.count("-") == 4


def _ladder_fill_preview_response(
    *,
    side: str,
    symbol: str,
    anchor_price: float,
    rungs: list[dict[str, Any]],
    atr: float | None,
    anchor_source: str | None,
    anchor_as_of: str | None,
) -> dict[str, Any]:
    """Shared body for sell_ladder_fill_preview / buy_ladder_fill_preview."""
    try:
        parsed_rungs = [
            LadderRung(
                limit_price=float(rung["limit_price"]),
                quantity=(
                    float(rung["quantity"])
                    if rung.get("quantity") is not None
                    else None
                ),
            )
            for rung in rungs
        ]
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": f"invalid rungs payload (need 'limit_price'): {exc!r}",
            "expected": "[{'limit_price': float, 'quantity': float|null}, ...]",
        }
    warnings, fill_safety = evaluate_ladder_fill_safety(
        side=side,
        rungs=parsed_rungs,
        anchor_price=anchor_price,
        anchor_source=anchor_source,
        atr=atr,
    )
    if fill_safety is None:
        return {
            "success": False,
            "error": (
                "nothing to analyze: anchor_price must be > 0 and at least "
                "one rung needs limit_price > 0"
            ),
            "symbol": symbol,
        }
    result: dict[str, Any] = {
        "success": True,
        "symbol": symbol,
        "read_only": True,
        "warnings": warnings,
        "fill_safety": fill_safety,
    }
    if anchor_as_of is not None:
        result["anchor_as_of"] = anchor_as_of
    return result


def register_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_order_history",
        description=(
            "주문 이력을 조회합니다. 주식은 Toss, 암호화폐는 Upbit를 사용하며 "
            "account_mode='db_simulated'는 가상계좌 이력을 조회합니다. "
            "KIS account_mode는 운영 경로가 아니므로 명시적으로 거부합니다."
        ),
    )
    async def get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled", "expired"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
        paper_account: str | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_kis_live or routing.is_kis_mock:
            return _kis_not_operational_response(routing, order_id=order_id)
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                await _get_paper_order_history(
                    symbol=symbol,
                    status=status,
                    order_id=order_id,
                    market=market,
                    side=side,
                    days=days,
                    limit=limit,
                    paper_account_name=paper_account,
                ),
                routing,
            )

        normalized_market = _normalize_market(market)
        if market is not None and normalized_market is None:
            return {
                "success": False,
                "error": f"Unsupported market: {market}",
                "account_mode": routing.account_mode,
            }
        if symbol is not None:
            try:
                normalized_market, symbol = _resolve_market_type(symbol, market)
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "account_mode": routing.account_mode,
                }
        if normalized_market == "crypto":
            crypto_routing = _routing_for_implicit_crypto(
                routing=routing,
                account_mode=account_mode,
                account_type=account_type,
            )
            if crypto_routing is None:
                return _toss_crypto_mismatch_response(order_id=order_id)
            routing = crypto_routing

        return apply_account_routing_metadata(
            await orders_history.get_order_history_impl(
                symbol=symbol,
                status=status,
                order_id=order_id,
                market=market,
                side=side,
                days=days,
                limit=limit,
            ),
            routing,
        )

    @mcp.tool(
        name="place_order",
        description=(
            "LIMIT 주문을 미리보기 또는 제출합니다. 주식은 Toss, 암호화폐는 "
            "Upbit를 사용합니다. dry_run=True가 기본이며 Toss 실주문은 "
            "confirm=True와 승인 해시 등 기존 Toss 안전 경계를 그대로 적용합니다. "
            "KIS account_mode는 운영 경로가 아니므로 명시적으로 거부합니다."
        ),
    )
    async def place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        confirm_high_value_order: bool = False,
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
        account_mode: str | None = None,
        account_type: str | None = None,
        paper_account: str | None = None,
        report_item_uuid: str | None = None,
        approval_hash: str | None = None,
        rung: str | int | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_kis_live or routing.is_kis_mock:
            return _kis_not_operational_response(routing, symbol=symbol)
        if exit_intent == "loss_cut":
            return {
                "success": False,
                "error": "loss_cut_direct_path_disabled_use_order_proposal_create",
                "source": "mcp",
                "symbol": symbol,
            }
        if defensive_trim:
            return {
                "success": False,
                "error": (
                    "defensive_trim_direct_path_disabled_use_order_proposal_create"
                ),
                "source": "mcp",
                "symbol": symbol,
            }
        # 오래된 클라이언트가 스키마를 우회하더라도 시장가 주문은 거부한다.
        if str(order_type).lower().strip() != "limit":
            return {
                "success": False,
                "error": (
                    "MCP place_order only supports limit orders; "
                    "market orders are not allowed."
                ),
                "source": "mcp",
                "symbol": symbol,
                "order_type": order_type,
            }
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                await _place_paper_order(
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
                    paper_account_name=paper_account,
                ),
                routing,
            )

        try:
            market_type, normalized_symbol = _resolve_market_type(symbol, None)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account_mode": routing.account_mode,
                "symbol": symbol,
            }
        if market_type == "crypto":
            crypto_routing = _routing_for_implicit_crypto(
                routing=routing,
                account_mode=account_mode,
                account_type=account_type,
            )
            if crypto_routing is None:
                return _toss_crypto_mismatch_response(symbol=normalized_symbol)
            return apply_account_routing_metadata(
                await order_execution._place_order_impl(
                    symbol=normalized_symbol,
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
                    is_mock=False,
                    report_item_uuid=report_item_uuid,
                    approval_hash=approval_hash,
                    rung=rung,
                ),
                crypto_routing,
            )

        toss_market: Literal["kr", "us"] = "kr" if market_type == "equity_kr" else "us"
        if dry_run:
            result = await orders_toss_variants.toss_preview_order(
                symbol=normalized_symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                order_amount=amount,
                market=toss_market,
                account_mode="toss_live",
                rung=rung,
                exit_intent=exit_intent,
                exit_reason=exit_reason,
                retrospective_id=retrospective_id,
                approval_issue_id=approval_issue_id,
            )
        else:
            result = await orders_toss_variants.toss_place_order(
                symbol=normalized_symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                order_amount=amount,
                market=toss_market,
                dry_run=False,
                confirm=confirm,
                confirm_high_value_order=confirm_high_value_order,
                reason=reason,
                exit_intent=exit_intent,
                exit_reason=exit_reason,
                retrospective_id=retrospective_id,
                approval_issue_id=approval_issue_id,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price,
                stop_loss=stop_loss,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
                report_item_uuid=report_item_uuid,
                account_mode="toss_live",
                approval_hash=approval_hash,
                rung=rung,
            )
        return apply_account_routing_metadata(result, routing)

    @mcp.tool(
        name="sell_ladder_fill_preview",
        description=(
            "[ROB-477] Read-only fill-safety analysis for a multi-rung SELL "
            "limit ladder. No broker calls, no order mutation. Pass "
            "anchor_price (current price or best bid from get_quote) and the "
            "FULL ladder as rungs=[{'limit_price': 64.0, 'quantity': 2.0}, ...]"
            "; atr optional (widens the near-market threshold to "
            "max(0.3% of anchor, 0.3*ATR)). Returns warnings: "
            "ladder_all_above_market (zero-fill tail risk on reversal - "
            "2026-06-09 incident: 8/8 all-above-market sell ladders filled "
            "nothing) and ladder_missing_near_market_anchor (no rung at or "
            "near the anchor), plus per-rung distance pct / ATR multiples and "
            "a suggested anchor rung. anchor_as_of (optional ISO timestamp "
            "of the anchor quote) is echoed back so a later reviewer can "
            "judge anchor staleness. Run this BEFORE submitting multi-rung "
            "sell ladders via place_order / toss_place_order."
        ),
    )
    async def sell_ladder_fill_preview(
        symbol: str,
        anchor_price: float,
        rungs: list[dict[str, Any]],
        atr: float | None = None,
        anchor_source: str | None = None,
        anchor_as_of: str | None = None,
    ):
        return _ladder_fill_preview_response(
            side="sell",
            symbol=symbol,
            anchor_price=anchor_price,
            rungs=rungs,
            atr=atr,
            anchor_source=anchor_source,
            anchor_as_of=anchor_as_of,
        )

    @mcp.tool(
        name="buy_ladder_fill_preview",
        description=(
            "[ROB-507] Read-only fill-safety analysis for a multi-rung BUY "
            "limit ladder (mirror of sell_ladder_fill_preview). No broker "
            "calls, no order mutation. Pass anchor_price (current price or "
            "best ask from get_quote) and the FULL ladder as "
            "rungs=[{'limit_price': 165.5, 'quantity': 10.0}, ...]; atr "
            "optional (widens the near-market threshold to max(0.3% of "
            "anchor, 0.3*ATR)). Returns warnings: ladder_all_below_market "
            "(zero-fill tail risk in a rally — the mirror of the 2026-06-09 "
            "all-above-market sell incident) and "
            "ladder_missing_near_market_anchor (no rung at or near the "
            "anchor), plus per-rung distance pct / ATR multiples and a "
            "suggested anchor rung. anchor_as_of (optional ISO timestamp of "
            "the anchor quote) is echoed back so a later reviewer can judge "
            "anchor staleness (2026-06-10: 5+ stale anchors drifted 1-3% "
            "between analysis and submission). Run this BEFORE submitting "
            "multi-rung buy ladders via place_order / toss_place_order. "
            "Note: a buy rung ABOVE the anchor is marketable and place_order "
            "rejects it outright (buy limit > current is a hard error), so "
            "the actionable risk here is the all-below zero-fill tail."
        ),
    )
    async def buy_ladder_fill_preview(
        symbol: str,
        anchor_price: float,
        rungs: list[dict[str, Any]],
        atr: float | None = None,
        anchor_source: str | None = None,
        anchor_as_of: str | None = None,
    ):
        return _ladder_fill_preview_response(
            side="buy",
            symbol=symbol,
            anchor_price=anchor_price,
            rungs=rungs,
            atr=atr,
            anchor_source=anchor_source,
            anchor_as_of=anchor_as_of,
        )

    @mcp.tool(
        name="cancel_order",
        description=(
            "대기 주문을 취소하거나 취소 미리보기를 반환합니다. 주식은 Toss, "
            "암호화폐는 Upbit를 사용합니다. Toss 취소는 dry_run=True가 기본이며 "
            "실제 취소에는 confirm=True가 필요합니다."
        ),
    )
    async def cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        account_mode: str | None = None,
        account_type: str | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_kis_live or routing.is_kis_mock:
            return _kis_not_operational_response(routing, order_id=order_id)
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                {
                    "success": False,
                    "error": "cancel_order is not supported for db_simulated",
                    "order_id": order_id,
                },
                routing,
            )

        market_type = _normalize_market(market)
        if market is not None and market_type is None:
            return {
                "success": False,
                "error": f"Unsupported market: {market}",
                "account_mode": routing.account_mode,
                "order_id": order_id,
            }
        if symbol is not None:
            try:
                market_type, symbol = _resolve_market_type(symbol, market)
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "account_mode": routing.account_mode,
                    "order_id": order_id,
                }
        is_crypto = market_type == "crypto" or (
            market_type is None
            and symbol is None
            and _looks_like_upbit_order_id(order_id)
        )
        if is_crypto:
            crypto_routing = _routing_for_implicit_crypto(
                routing=routing,
                account_mode=account_mode,
                account_type=account_type,
            )
            if crypto_routing is None:
                return _toss_crypto_mismatch_response(order_id=order_id)
            return apply_account_routing_metadata(
                await cancel_order_impl(
                    order_id=order_id,
                    symbol=symbol,
                    market=market,
                    is_mock=False,
                ),
                crypto_routing,
            )

        return apply_account_routing_metadata(
            await orders_toss_variants.toss_cancel_order(
                order_id=order_id,
                dry_run=dry_run,
                confirm=confirm,
                account_mode="toss_live",
            ),
            routing,
        )

    @mcp.tool(
        name="modify_order",
        description=(
            "대기 주문의 가격 또는 수량을 변경합니다. 주식은 Toss, 암호화폐는 "
            "Upbit를 사용합니다. Toss 변경은 dry_run=True가 기본이며 실제 변경에는 "
            "confirm=True가 필요합니다."
        ),
    )
    async def modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        confirm_high_value_order: bool = False,
        reason: str = "",
        account_mode: str | None = None,
        account_type: str | None = None,
    ):
        del reason
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_kis_live or routing.is_kis_mock:
            return _kis_not_operational_response(
                routing,
                order_id=order_id,
                symbol=symbol,
            )
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                {
                    "success": False,
                    "error": "modify_order is not supported for db_simulated",
                    "order_id": order_id,
                    "symbol": symbol,
                },
                routing,
            )
        try:
            market_type, normalized_symbol = _resolve_market_type(symbol, market)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account_mode": routing.account_mode,
                "order_id": order_id,
                "symbol": symbol,
            }
        if market_type == "crypto":
            crypto_routing = _routing_for_implicit_crypto(
                routing=routing,
                account_mode=account_mode,
                account_type=account_type,
            )
            if crypto_routing is None:
                return _toss_crypto_mismatch_response(
                    order_id=order_id,
                    symbol=normalized_symbol,
                )
            return apply_account_routing_metadata(
                await modify_order_impl(
                    order_id=order_id,
                    symbol=normalized_symbol,
                    market=market,
                    new_price=new_price,
                    new_quantity=new_quantity,
                    dry_run=dry_run,
                    is_mock=False,
                ),
                crypto_routing,
            )

        toss_market: Literal["kr", "us"] = "kr" if market_type == "equity_kr" else "us"
        return apply_account_routing_metadata(
            await orders_toss_variants.toss_modify_order(
                order_id=order_id,
                new_price=new_price,
                new_quantity=new_quantity,
                market=toss_market,
                dry_run=dry_run,
                confirm=confirm,
                confirm_high_value_order=confirm_high_value_order,
                account_mode="toss_live",
            ),
            routing,
        )


__all__ = ["ORDER_TOOL_NAMES", "register_order_tools"]
