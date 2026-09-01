"""Generic MCP order routing cutover tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_execution, orders_registration
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "expected_symbol", "expected_market"),
    [("005930", "005930", "kr"), ("aapl", "AAPL", "us")],
)
async def test_default_equity_preview_delegates_to_toss(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    expected_symbol: str,
    expected_market: str,
):
    preview = AsyncMock(
        return_value={
            "success": True,
            "source": "toss",
            "account_mode": "toss_live",
            "approval_hash": "approval-token",
        }
    )
    legacy = AsyncMock(side_effect=AssertionError("legacy order path called"))
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_preview_order",
        preview,
    )
    monkeypatch.setattr(order_execution, "_place_order_impl", legacy)

    result = await build_tools()["place_order"](
        symbol=symbol,
        side="buy",
        quantity=2,
        price=100,
    )

    assert result["success"] is True
    assert result["account_mode"] == "toss_live"
    assert preview.await_args.kwargs["symbol"] == expected_symbol
    assert preview.await_args.kwargs["market"] == expected_market
    assert preview.await_args.kwargs["account_mode"] == "toss_live"
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_equity_submit_preserves_toss_safety_arguments(monkeypatch):
    submit = AsyncMock(
        return_value={
            "success": True,
            "source": "toss",
            "account_mode": "toss_live",
            "order_id": "toss-order",
            "client_order_id": "client-order",
            "broker_status": "accepted",
        }
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_place_order",
        submit,
    )

    result = await build_tools()["place_order"](
        symbol="AAPL",
        side="buy",
        quantity=1,
        price=200,
        dry_run=False,
        confirm=True,
        confirm_high_value_order=True,
        approval_hash="approval-token",
        rung="2",
        thesis="thesis",
        strategy="strategy",
    )

    assert result["success"] is True
    kwargs = submit.await_args.kwargs
    assert kwargs["dry_run"] is False
    assert kwargs["confirm"] is True
    assert kwargs["confirm_high_value_order"] is True
    assert kwargs["approval_hash"] == "approval-token"
    assert kwargs["rung"] == "2"
    assert kwargs["account_mode"] == "toss_live"


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_kis_equity_intent_rejects_without_rerouting(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
):
    toss = AsyncMock()
    legacy = AsyncMock()
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_preview_order",
        toss,
    )
    monkeypatch.setattr(order_execution, "_place_order_impl", legacy)

    result = await build_tools()["place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        account_mode=account_mode,
    )

    assert result == {
        "success": False,
        "error": "provider kis is not operational",
        "account_mode": account_mode,
        "symbol": "005930",
    }
    toss.assert_not_awaited()
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_crypto_keeps_upbit_legacy_path(monkeypatch):
    legacy = AsyncMock(return_value={"success": True, "source": "upbit"})
    toss = AsyncMock()
    monkeypatch.setattr(order_execution, "_place_order_impl", legacy)
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_preview_order",
        toss,
    )

    result = await build_tools()["place_order"](
        symbol="KRW-BTC",
        side="buy",
        quantity=0.01,
        price=100_000_000,
    )

    assert result["success"] is True
    assert result["account_mode"] == "upbit"
    assert legacy.await_args.kwargs["is_mock"] is False
    toss.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_toss_selector_rejects_crypto(monkeypatch):
    legacy = AsyncMock()
    monkeypatch.setattr(order_execution, "_place_order_impl", legacy)

    result = await build_tools()["place_order"](
        symbol="KRW-BTC",
        side="buy",
        quantity=0.01,
        price=100_000_000,
        account_mode="toss_live",
    )

    assert result["success"] is False
    assert "does not support crypto" in result["error"]
    legacy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (
            {
                "symbol": "005930",
                "side": "buy",
                "order_type": "market",
                "amount": 1_000_000,
            },
            "market orders are not allowed",
        ),
        (
            {
                "symbol": "005930",
                "side": "sell",
                "price": 70000,
                "exit_intent": "loss_cut",
            },
            "loss_cut_direct_path_disabled",
        ),
        (
            {
                "symbol": "005930",
                "side": "sell",
                "price": 70000,
                "defensive_trim": True,
            },
            "defensive_trim_direct_path_disabled",
        ),
    ],
)
async def test_direct_hard_risk_paths_remain_disabled(arguments, expected_error):
    result = await build_tools()["place_order"](**arguments)

    assert result["success"] is False
    assert expected_error in result["error"]


@pytest.mark.asyncio
async def test_generic_cancel_delegates_to_toss_confirm_boundary(monkeypatch):
    cancel = AsyncMock(
        return_value={"success": True, "source": "toss", "mutation_sent": False}
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_cancel_order",
        cancel,
    )

    result = await build_tools()["cancel_order"](
        order_id="toss-order",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is True
    assert result["account_mode"] == "toss_live"
    cancel.assert_awaited_once_with(
        order_id="toss-order",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )


@pytest.mark.asyncio
async def test_generic_modify_delegates_to_toss_confirm_boundary(monkeypatch):
    modify = AsyncMock(
        return_value={"success": True, "source": "toss", "mutation_sent": False}
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_modify_order",
        modify,
    )

    result = await build_tools()["modify_order"](
        order_id="toss-order",
        symbol="AAPL",
        new_price=201,
        dry_run=False,
        confirm=True,
        confirm_high_value_order=True,
    )

    assert result["success"] is True
    assert result["account_mode"] == "toss_live"
    assert modify.await_args.kwargs == {
        "order_id": "toss-order",
        "new_price": 201,
        "new_quantity": None,
        "market": "us",
        "dry_run": False,
        "confirm": True,
        "confirm_high_value_order": True,
        "account_mode": "toss_live",
    }


@pytest.mark.asyncio
async def test_legacy_direct_equity_order_is_fail_closed_before_provider(monkeypatch):
    provider = AsyncMock()
    price = AsyncMock()
    monkeypatch.setattr(order_execution, "_execute_order", provider)
    monkeypatch.setattr(order_execution, "_fetch_current_price", price)

    result = await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False
    provider.assert_not_awaited()
    price.assert_not_awaited()
