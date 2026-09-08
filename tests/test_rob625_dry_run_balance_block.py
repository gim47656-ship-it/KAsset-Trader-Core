"""주문 잔액 검증의 crypto 잔액 부족 hard-error 계약."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation


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
