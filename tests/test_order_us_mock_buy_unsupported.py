"""레거시 주문 검증의 KIS 비운영 fail-closed 계약을 검증한다."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_equity_holdings_validation_rejects_nonoperational_kis(
    market_type: str,
) -> None:
    with pytest.raises(ValueError, match="^provider kis is not operational$"):
        await order_validation._get_holdings_for_order(
            "005930" if market_type == "equity_kr" else "AAPL",
            market_type,
            is_mock=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
@pytest.mark.parametrize("is_mock", [False, True])
async def test_equity_balance_validation_rejects_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    market_type: str,
    is_mock: bool,
) -> None:
    provider_call = AsyncMock(side_effect=AssertionError("provider call is forbidden"))
    monkeypatch.setattr(order_validation.upbit_service, "fetch_my_coins", provider_call)

    with pytest.raises(ValueError, match="^provider kis is not operational$"):
        await order_validation._get_balance_for_order(market_type, is_mock=is_mock)

    provider_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_equity_balance_guard_returns_provider_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    market_type: str,
) -> None:
    balance_lookup = AsyncMock(
        side_effect=AssertionError("balance lookup is forbidden")
    )
    monkeypatch.setattr(order_validation, "_get_balance_for_order", balance_lookup)

    warning, error = await order_validation._check_balance_and_warn(
        market_type=market_type,
        normalized_symbol="005930" if market_type == "equity_kr" else "AAPL",
        side="buy",
        order_amount=100.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )

    assert warning is None
    assert error == {
        "success": False,
        "error": "provider kis is not operational",
        "provider_unsupported": True,
    }
    balance_lookup.assert_not_awaited()


def test_order_validation_exposes_no_kis_client_factory() -> None:
    assert not hasattr(order_validation, "_create_kis_client")
    assert not hasattr(order_validation, "_call_kis")
