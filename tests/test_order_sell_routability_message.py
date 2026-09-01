"""Retired equity-provider sell intents fail closed instead of rerouting."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import _no_holdings_sell_message


def test_equity_kr_message_is_explicitly_provider_unsupported():
    msg = _no_holdings_sell_message("005930", "equity_kr", is_mock=False)
    assert msg == "provider kis is not operational"


def test_equity_us_mock_message_is_explicitly_provider_unsupported():
    msg = _no_holdings_sell_message("AAPL", "equity_us", is_mock=True)
    assert msg == "provider kis is not operational"


def test_crypto_message_remains_upbit_owned():
    msg = _no_holdings_sell_message("KRW-BTC", "crypto", is_mock=False)
    assert msg == "No holdings found for KRW-BTC on Upbit"


@pytest.mark.asyncio
async def test_preview_sell_uses_fail_closed_message_after_no_holdings(monkeypatch):
    async def no_holdings(*_a, **_k):
        return None

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", no_holdings)
    result = await order_validation._preview_sell(
        symbol="AAPL",
        order_type="limit",
        quantity=1.0,
        price=100.0,
        current_price=100.0,
        market_type="equity_us",
        is_mock=False,
    )
    assert result["error"] == "provider kis is not operational"


@pytest.mark.asyncio
async def test_validate_sell_side_fails_closed_before_retired_equity_lookup(
    monkeypatch,
):
    async def retired_holdings(*_a, **_k):
        pytest.fail("retired equity holdings lookup must not run")

    captured: dict[str, str] = {}

    def order_error(msg: str) -> dict[str, str]:
        captured["msg"] = msg
        return {"error": msg}

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", retired_holdings)
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="AAPL",
        normalized_symbol="AAPL",
        market_type="equity_us",
        quantity=1.0,
        order_type="limit",
        price=100.0,
        current_price=100.0,
        order_error_fn=order_error,
        is_mock=True,
    )
    assert (qty, avg) == (0.0, 0.0)
    assert err is not None
    assert captured["msg"] == "provider kis is not operational"


def test_no_holdings_sell_message_never_reroutes_to_toss_when_enabled(monkeypatch):
    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", True)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert msg == "provider kis is not operational"


def test_no_holdings_sell_message_stays_fail_closed_when_toss_disabled(monkeypatch):
    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", False)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert msg == "provider kis is not operational"
