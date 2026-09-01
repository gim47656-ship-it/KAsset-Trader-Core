"""Cross-asset portfolio allocation MCP tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.account_modes import (
    apply_account_routing_metadata,
    normalize_account_mode,
)
from app.mcp_server.tooling.portfolio_cash import (
    get_cash_balance_impl,
    get_usd_krw_rate,
)
from app.mcp_server.tooling.portfolio_holdings import _collect_portfolio_positions
from app.services.krx import fetch_etf_all_cached
from app.services.portfolio_allocation_service import build_portfolio_allocation

if TYPE_CHECKING:
    from fastmcp import FastMCP


ALLOCATION_TOOL_NAMES: set[str] = {"get_portfolio_allocation"}


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _load_etf_rows(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        rows = await _maybe_await(fetch_etf_all_cached())
    except Exception as exc:
        errors.append({"source": "krx_etf", "error": str(exc), "degraded": True})
        return []
    return rows if isinstance(rows, list) else []


async def get_portfolio_allocation_impl(
    *,
    account: str | None = None,
    market: str | None = None,
    include_cash: bool = True,
    include_positions: bool = False,
    target_weights: dict[str, float] | None = None,
    drift_threshold_pct: float = 5.0,
    is_mock: bool = False,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    (
        positions,
        position_errors,
        resolved_market,
        resolved_account,
    ) = await _collect_portfolio_positions(
        account=account,
        market=market,
        include_current_price=True,
        is_mock=is_mock,
    )
    errors.extend(position_errors)

    cash_accounts: list[dict[str, Any]] = []
    if include_cash:
        cash_result = await get_cash_balance_impl(account=account, is_mock=is_mock)
        cash_accounts = list(cash_result.get("accounts", []))
        errors.extend(cash_result.get("errors", []))

    usd_krw = await _maybe_await(get_usd_krw_rate())
    etf_rows = await _load_etf_rows(errors)
    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=cash_accounts,
        usd_krw=float(usd_krw),
        etf_rows=etf_rows,
        include_cash=include_cash,
        include_positions=include_positions,
        target_weights=target_weights,
        drift_threshold_pct=drift_threshold_pct,
    )
    result["filters"] = {
        "account": resolved_account,
        "market": resolved_market,
        "include_cash": include_cash,
        "include_positions": include_positions,
        "target_weights": target_weights,
        "drift_threshold_pct": drift_threshold_pct,
    }
    result["errors"] = errors
    return result


def register_portfolio_allocation_tool(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_portfolio_allocation",
        description=(
            "Toss, Upbit, 수동 보유 및 PAPER 계정의 자산 배분을 읽기 전용으로 "
            "집계합니다. USD 자산/현금을 KRW로 환산하고 KR 상장 ETF의 기초 "
            "노출을 분류합니다. target_weights는 과대/과소 비중 표시만 제어하며 "
            "주문을 실행하지 않습니다. KIS account_mode/account 필터는 운영 "
            "경로가 아니므로 명시적으로 거부합니다."
        ),
    )
    async def get_portfolio_allocation(
        account: str | None = None,
        market: str | None = None,
        include_cash: bool = True,
        include_positions: bool = False,
        target_weights: dict[str, float] | None = None,
        drift_threshold_pct: float = 5.0,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if (
            routing.is_kis_live
            or routing.is_kis_mock
            or str(account or "").strip().lower().startswith("kis")
        ):
            return apply_account_routing_metadata(
                {
                    "success": False,
                    "error": "provider kis is not operational",
                },
                routing,
            )
        if routing.is_db_simulated and account is None:
            account = "paper"
        return apply_account_routing_metadata(
            await get_portfolio_allocation_impl(
                account=account,
                market=market,
                include_cash=include_cash,
                include_positions=include_positions,
                target_weights=target_weights,
                drift_threshold_pct=drift_threshold_pct,
                is_mock=routing.is_kis_mock,
            ),
            routing,
        )


__all__ = [
    "ALLOCATION_TOOL_NAMES",
    "get_portfolio_allocation_impl",
    "register_portfolio_allocation_tool",
]
