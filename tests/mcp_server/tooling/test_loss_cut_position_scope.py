"""레거시 KIS 포지션 검증이 Toss 손절 경로로 우회되지 않는지 검증한다."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "market_type"),
    [("005930", "equity_kr"), ("AAPL", "equity_us")],
)
async def test_legacy_equity_position_scope_is_provider_unsupported(
    symbol: str,
    market_type: str,
) -> None:
    with pytest.raises(ValueError, match="^provider kis is not operational$"):
        await order_validation._get_holdings_for_order(
            symbol,
            market_type,
            is_mock=False,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_equity_preview_is_not_rerouted_to_toss() -> None:
    result = await order_validation._preview_order(
        symbol="005930",
        side="sell",
        order_type="limit",
        quantity=1.0,
        price=99.0,
        current_price=100.0,
        market_type="equity_kr",
    )

    assert result == {
        "success": False,
        "error": "provider kis is not operational",
        "provider_unsupported": True,
    }
