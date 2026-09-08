"""ROB-518 — crypto live loss-sell 안전 경계.

주식은 Toss 전용 주문 구현에서 검증한다. 이 모듈은 공통 순수 guard와
레거시 crypto 실행 검증 경로가 평균단가 이하의 실자금 매도를 허용하지
않는지 검증한다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    evaluate_market_sell_loss_guard,
)


# ---------------------------------------------------------------------------
# Pure guard: evaluate_market_sell_loss_guard
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_market_guard_blocks_current_below_floor() -> None:
    err = evaluate_market_sell_loss_guard(
        current_price=68500.0,
        avg_price=90400.0,
        allow_loss_sell=False,
    )
    assert err is not None and "market sell blocked" in err.lower()


@pytest.mark.unit
def test_market_guard_allows_current_at_or_above_floor() -> None:
    assert (
        evaluate_market_sell_loss_guard(
            current_price=1010.0, avg_price=1000.0, allow_loss_sell=False
        )
        is None
    )
    assert (
        evaluate_market_sell_loss_guard(
            current_price=1200.0, avg_price=1000.0, allow_loss_sell=False
        )
        is None
    )


@pytest.mark.unit
def test_market_guard_mock_bypass_allows_loss() -> None:
    err = evaluate_market_sell_loss_guard(
        current_price=68500.0,
        avg_price=90400.0,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_market_guard_unknown_basis_fails_open() -> None:
    # avg_price <= 0 means the cost basis is unknown (e.g. manual holdings rows
    # without it); the limit guard has always been fail-open there — keep parity.
    assert (
        evaluate_market_sell_loss_guard(
            current_price=100.0, avg_price=0.0, allow_loss_sell=False
        )
        is None
    )


@pytest.mark.unit
def test_market_guard_default_is_blocking() -> None:
    # Omitting allow_loss_sell must keep the floor (no accidental relaxation).
    err = evaluate_market_sell_loss_guard(current_price=900.0, avg_price=1000.0)
    assert err is not None


# ---------------------------------------------------------------------------
# Upbit sell preview
def _patch_holdings(monkeypatch, avg_price: float, quantity: float = 10) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": avg_price, "quantity": quantity}),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_market_loss_blocked(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=90400.0)
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type="crypto",
        is_mock=False,
    )
    assert "error" in result and "market sell blocked" in result["error"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_market_profit_allowed(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=60000.0)
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type="crypto",
        is_mock=False,
    )
    assert "error" not in result
    assert result["price"] == 68500.0
    assert result["realized_pnl"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_mock_crypto_market_loss_still_blocked(monkeypatch) -> None:
    # Crypto routes to real Upbit funds; is_mock never relaxes it.
    _patch_holdings(monkeypatch, avg_price=40000000.0, quantity=0.1)
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="market",
        quantity=0.1,
        price=None,
        current_price=31000000.0,
        market_type="crypto",
        is_mock=True,
    )
    assert "error" in result and "market sell blocked" in result["error"].lower()


# ---------------------------------------------------------------------------
# _validate_sell_side: execution path mirrors the preview matrix
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_live_market_loss_blocked(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=90400.0)
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="crypto",
        quantity=10,
        order_type="market",
        price=None,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=False,
        dry_run=False,
    )
    assert err is not None
    assert "market sell blocked" in errors[0].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_live_limit_guard_unchanged(monkeypatch) -> None:
    # Regression: the existing live limit guard message is byte-for-byte intact.
    _patch_holdings(monkeypatch, avg_price=90400.0)
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="crypto",
        quantity=10,
        order_type="limit",
        price=68000.0,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=False,
        dry_run=False,
    )
    assert err is not None and "below minimum" in errors[0]
