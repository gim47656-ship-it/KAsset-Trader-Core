from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.account_routing_tools import suggest_order_account_impl

if TYPE_CHECKING:
    from fastmcp import FastMCP


ACCOUNT_ROUTING_TOOL_NAMES: set[str] = {"suggest_order_account"}


def register_account_routing_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="suggest_order_account",
        description=(
            "KR/US 주식 매수의 Toss 계정 비용·주문가능 현금·notional 한도와 "
            "기존 Toss 보유 통합 여부를 읽기 전용으로 검토합니다. "
            "주문을 제출하거나 자동 route하지 않으며 운영자 최종 판단이 필요합니다."
        ),
    )
    async def suggest_order_account(
        symbol: str,
        market: str | None = None,
        side: str = "buy",
        quantity: float = 0,
        price: float | None = None,
        usd_krw: float | None = None,
    ) -> dict[str, Any]:
        return await suggest_order_account_impl(
            symbol=symbol,
            market=market,
            side=side,
            quantity=quantity,
            price=price,
            usd_krw=usd_krw,
        )


__all__ = ["ACCOUNT_ROUTING_TOOL_NAMES", "register_account_routing_tools"]
