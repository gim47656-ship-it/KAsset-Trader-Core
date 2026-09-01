"""레거시 주문 잔액 검증의 Upbit 전용 및 KIS fail-closed 계약."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_execution, order_validation


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


@pytest.mark.asyncio
async def test_crypto_insufficient_balance_returns_structured_hard_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_balance_for_order",
        AsyncMock(return_value=75.0),
    )

    warning, error = await order_validation._check_balance_and_warn(
        market_type="crypto",
        normalized_symbol="KRW-BTC",
        side="buy",
        order_amount=100.0,
        dry_run=True,
        order_error_fn=_order_error,
    )

    assert warning is None
    assert error is not None
    assert error["insufficient_balance"] is True
    assert error["insufficient_balance_detail"] == {
        "balance": 75.0,
        "order_amount": 100.0,
        "currency": "KRW",
        "shortfall": 25.0,
    }


@pytest.mark.asyncio
async def test_crypto_sufficient_balance_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_balance_for_order",
        AsyncMock(return_value=100.0),
    )

    assert await order_validation._check_balance_and_warn(
        market_type="crypto",
        normalized_symbol="KRW-BTC",
        side="buy",
        order_amount=100.0,
        dry_run=False,
        order_error_fn=_order_error,
    ) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "market"),
    [("005930", "kr"), ("AAPL", "us")],
)
async def test_legacy_equity_place_order_rejects_before_validation_or_mutation(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    market: str,
) -> None:
    validation_call = AsyncMock(
        side_effect=AssertionError("validation call is forbidden")
    )
    monkeypatch.setattr(order_execution, "_fetch_current_price", validation_call)

    result = await order_execution._place_order_impl(
        symbol=symbol,
        side="buy",
        market=market,
        order_type="limit",
        quantity=1.0,
        price=100.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False
    validation_call.assert_not_awaited()
