"""Active Upbit live-order reconcile MCP registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

LIVE_RECONCILE_TOOL_NAMES: set[str] = {"live_reconcile_orders"}


def register_live_reconcile_tools(mcp: FastMCP) -> None:
    """Register the crypto/Upbit accepted-order settlement tool."""

    @mcp.tool(
        name="live_reconcile_orders",
        description=(
            "Reconcile accepted/pending Upbit crypto live orders against Upbit "
            "order-state evidence. Omitted market/broker default to the only "
            "supported pair; explicit non-crypto or non-Upbit requests return "
            "provider_unsupported before broker I/O. Books fills, journals, and "
            "realized_pnl only from confirmed fills (delta-idempotent). Explicit "
            "broker cancellation returns cancelled; missing evidence leaves the "
            "ledger open with requires_manual_review. dry_run=True by default."
        ),
    )
    async def live_reconcile_orders(
        market: str | None = None,
        broker: str | None = None,
        symbol: str | None = None,
        order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized_market = (market or "crypto").strip().lower()
        normalized_broker = (broker or "upbit").strip().lower()
        if normalized_market != "crypto" or normalized_broker != "upbit":
            return {
                "success": False,
                "error": "provider_unsupported",
                "provider_unsupported": True,
                "market": market,
                "broker": broker,
                "detail": (
                    "live_reconcile_orders supports only market='crypto' "
                    "with broker='upbit'"
                ),
            }

        from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

        return await live_reconcile_orders_impl(
            market="crypto",
            broker="upbit",
            symbol=symbol,
            order_id=order_id,
            dry_run=dry_run,
            limit=limit,
        )


__all__ = ["LIVE_RECONCILE_TOOL_NAMES", "register_live_reconcile_tools"]
