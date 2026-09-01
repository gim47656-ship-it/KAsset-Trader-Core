from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _use_placeholder_kis_account(monkeypatch, client) -> None:
    monkeypatch.setattr(
        type(client._settings),
        "kis_account_no",
        property(lambda self: "00000000-01"),
    )


def test_kis_mock_settings_view_uses_mock_base_url(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_base_url",
        "https://live.example.invalid",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_base_url",
        "https://mock.example.invalid",
        raising=False,
    )

    client = KISClient(is_mock=True)

    assert client._settings.kis_base_url == "https://mock.example.invalid"
    assert client._kis_url("/uapi/test") == "https://mock.example.invalid/uapi/test"


@pytest.mark.asyncio
async def test_kis_mock_fetch_token_posts_to_mock_base_url(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_base_url",
        "https://mock.example.invalid",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_app_key",
        "mock-key",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_app_secret",
        "mock-secret",
        raising=False,
    )

    response = MagicMock()
    response.json.return_value = {"access_token": "token", "expires_in": 1234}
    http_client = AsyncMock()
    http_client.post.return_value = response

    client = KISClient(is_mock=True)
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=http_client))

    token, expires_in = await client._fetch_token()

    assert token == "token"
    assert expires_in == 1234
    assert http_client.post.await_args.args[0] == (
        "https://mock.example.invalid/oauth2/token"
    )


def test_order_execution_exposes_no_kis_client_factory():
    from app.mcp_server.tooling import order_execution

    assert not hasattr(order_execution, "KISClient")
    assert not hasattr(order_execution, "_create_kis_client")


def test_orders_history_exposes_no_kis_client_factory():
    from app.mcp_server.tooling import orders_history

    assert not hasattr(orders_history, "KISClient")
    assert not hasattr(orders_history, "_create_kis_client")


def test_portfolio_cash_exposes_no_kis_client_factory():
    from app.mcp_server.tooling import portfolio_cash

    assert not hasattr(portfolio_cash, "KISClient")
    assert not hasattr(portfolio_cash, "_create_kis_client")


def test_order_validation_exposes_no_kis_client_factory():
    from app.mcp_server.tooling import order_validation

    assert not hasattr(order_validation, "KISClient")
    assert not hasattr(order_validation, "_create_kis_client")


@pytest.mark.asyncio
async def test_order_validation_kis_mock_balance_is_non_operational():
    from app.mcp_server.tooling import order_validation

    with pytest.raises(ValueError, match="provider kis is not operational"):
        await order_validation._get_balance_for_order("equity_kr", is_mock=True)


@pytest.mark.asyncio
async def test_portfolio_holdings_exposes_no_kis_collector():
    from app.mcp_server.tooling import portfolio_holdings

    assert not hasattr(portfolio_holdings, "KISClient")
    assert not hasattr(portfolio_holdings, "_collect_kis_positions")


# 운영 MCP 주문 경로는 KIS mock을 preview 포함 전 단계에서 거부한다.


def test_modify_order_kis_mock_dry_run_is_non_operational():
    import asyncio

    from app.mcp_server.tooling import orders_modify_cancel

    result = asyncio.run(
        orders_modify_cancel.modify_order_impl(
            order_id="0001",
            symbol="005930",
            market="kr",
            new_price=70100.0,
            dry_run=True,
            is_mock=True,
        )
    )
    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False


@pytest.mark.asyncio
async def test_get_order_history_pending_us_mock_is_non_operational():
    from app.mcp_server.tooling import orders_history

    result = await orders_history.get_order_history_impl(
        status="pending", market="us", is_mock=True
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["orders"] == []


# KIS mock 취소·변경은 ledger 유무와 관계없이 broker 접근 전에 거부한다.


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_kr_is_non_operational():
    from app.mcp_server.tooling import orders_modify_cancel

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol="005930", market="kr", is_mock=True
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_kr_without_symbol_is_non_operational():
    from app.mcp_server.tooling import orders_modify_cancel

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol=None, market="kr", is_mock=True
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False


@pytest.mark.asyncio
async def test_modify_order_kis_mock_kr_is_non_operational():
    from app.mcp_server.tooling import orders_modify_cancel

    result = await orders_modify_cancel.modify_order_impl(
        order_id="0001",
        symbol="005930",
        market="kr",
        new_price=70100.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["mutation_sent"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_tr_id"),
    [
        ("inquire_daily_order_domestic", "VTTC8001R"),
        ("inquire_daily_order_overseas", "VTTS3035R"),
    ],
    ids=["domestic", "overseas"],
)
async def test_inquire_daily_order_mock_uses_mock_tr(
    monkeypatch,
    method_name: str,
    expected_tr_id: str,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient(is_mock=True)
    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
    _use_placeholder_kis_account(monkeypatch, client)

    captured: dict = {}

    async def fake_request(method, url, *, headers, params, **kwargs):
        captured["tr_id"] = headers.get("tr_id")
        return {"rt_cd": "0", "output1": []}

    monkeypatch.setattr(client, "_request_with_rate_limit", fake_request)

    await getattr(client, method_name)(
        start_date="20260101", end_date="20260102", is_mock=True
    )
    assert captured["tr_id"] == expected_tr_id
