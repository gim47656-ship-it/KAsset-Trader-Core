"""MCP order tool tests for get_order_history and modify_order."""

from unittest.mock import AsyncMock

import pytest

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import (
    orders_history,
    orders_toss_variants,
)
from app.services.brokers.kis.overseas_orders import _normalize_kis_exchange_code
from tests._mcp_tooling_support import build_tools


@pytest.fixture(autouse=True)
def _default_empty_toss_order_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_history(**_kwargs):
        return {
            "success": True,
            "orders": [],
            "next_cursor": None,
            "has_next": False,
        }

    monkeypatch.setattr(
        orders_toss_variants,
        "toss_get_order_history",
        _empty_history,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["filled", "cancelled"])
async def test_get_order_history_requires_symbol_for_closed_status(status):
    # filled/cancelled history is broker-symbol-keyed, so it still requires a symbol.
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required when status="):
        await tools["get_order_history"](status=status, order_id="some-id")


@pytest.mark.asyncio
async def test_get_order_history_all_without_symbol_returns_open_orders_with_warning(
    monkeypatch,
):
    """status='all' without a symbol no longer hard-errors (ROB-466): it returns
    open orders across markets and warns that closed history needs a symbol."""
    tools = build_tools()

    async def fetch_open_orders(market):
        return [
            {
                "uuid": "uuid-open",
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000",
                "volume": "0.1",
                "remaining_volume": "0.1",
                "market": "KRW-BTC",
                "created_at": "2025-01-01T00:00:00",
                "state": "wait",
            }
        ]

    monkeypatch.setattr(upbit_service, "fetch_open_orders", fetch_open_orders)

    result = await tools["get_order_history"](status="all")

    # Open orders are returned (no hard error)...
    assert [o["order_id"] for o in result["orders"]] == ["uuid-open"]
    # ...and the response warns that filled/cancelled history was omitted.
    assert any("filled/cancelled" in w for w in result["warnings"])


def test_validate_history_inputs_allows_all_status_without_symbol():
    # status='all' without a symbol resolves to all markets and does not raise (ROB-466).
    (_symbol, _oid, _mh, _side, _days, _lim, market_types, normalized_symbol) = (
        orders_history._validate_history_inputs(
            symbol=None,
            status="all",
            order_id=None,
            market=None,
            side=None,
            days=None,
            limit=50,
        )
    )
    assert normalized_symbol is None
    assert set(market_types) == {"crypto", "equity_kr", "equity_us"}


@pytest.mark.parametrize("status", ["filled", "cancelled"])
def test_validate_history_inputs_still_requires_symbol_for_closed(status):
    with pytest.raises(ValueError, match="symbol is required when status="):
        orders_history._validate_history_inputs(
            symbol=None,
            status=status,
            order_id=None,
            market=None,
            side=None,
            days=None,
            limit=50,
        )


@pytest.mark.asyncio
async def test_get_order_history_filters(monkeypatch):
    tools = build_tools()

    # Mock data with mixed sides and order_ids
    orders_data = [
        # KRW-BTC orders
        {
            "uuid": "bid-1",
            "market": "KRW-BTC",
            "side": "bid",
            "state": "done",
            "created_at": "2025-01-02",
            "ord_type": "limit",
            "price": "100",
            "volume": "1",
            "remaining_volume": "0",
            "executed_volume": "1",
        },
        {
            "uuid": "ask-1",
            "market": "KRW-BTC",
            "side": "ask",
            "state": "done",
            "created_at": "2025-01-01",
            "ord_type": "limit",
            "price": "110",
            "volume": "1",
            "remaining_volume": "0",
            "executed_volume": "1",
        },
    ]

    monkeypatch.setattr(
        upbit_service, "fetch_closed_orders", AsyncMock(return_value=orders_data)
    )
    monkeypatch.setattr(upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # Test 1: Filter by side="buy"
    res_buy = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", side="buy"
    )
    assert len(res_buy["orders"]) == 1
    assert res_buy["orders"][0]["order_id"] == "bid-1"
    assert res_buy["orders"][0]["side"] == "buy"

    # Test 2: Filter by side="sell"
    res_sell = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", side="sell"
    )
    assert len(res_sell["orders"]) == 1
    assert res_sell["orders"][0]["order_id"] == "ask-1"
    assert res_sell["orders"][0]["side"] == "sell"

    # Test 3: Filter by order_id
    res_id = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", order_id="bid-1"
    )
    assert len(res_id["orders"]) == 1
    assert res_id["orders"][0]["order_id"] == "bid-1"

    # Test 4: Filter by order_id without symbol (should attempt heuristic or all)
    # This requires fetch logic to run without symbol.
    # For crypto, our impl calls fetch_closed_orders ONLY if normalized_symbol is present for history.
    # So if we pass status="filled", order_id="bid-1" with NO symbol:
    # Logic: "if status in ... and normalized_symbol: ..." -> won't fetch history from Upbit if no symbol.
    # Wait, looking at my impl:
    # "if status in ("all", "filled", "cancelled") and normalized_symbol:"
    # So retrieving specific order history by ID without symbol is NOT supported for Upbit closed orders in this impl.
    # It IS supported for Pending (fetch_open_orders(market=None)).

    # Let's test pending by ID without symbol
    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        AsyncMock(
            return_value=[
                {
                    "uuid": "pending-1",
                    "market": "KRW-ETH",
                    "side": "bid",
                    "state": "wait",
                    "created_at": "2025-01-03",
                    "ord_type": "limit",
                    "price": "200",
                    "volume": "1",
                    "remaining_volume": "1",
                    "executed_volume": "0",
                }
            ]
        ),
    )

    res_pending_id = await tools["get_order_history"](
        status="pending", order_id="pending-1"
    )
    assert len(res_pending_id["orders"]) == 1
    assert res_pending_id["orders"][0]["order_id"] == "pending-1"


@pytest.mark.asyncio
async def test_get_order_history_pending_without_symbol(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return [
                {
                    "uuid": "uuid-1",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "50000000",
                    "volume": "0.1",
                    "remaining_volume": "0.1",
                    "market": "KRW-BTC",
                    "created_at": "2025-01-01T00:00:00",
                    "state": "wait",
                }
            ]

    monkeypatch.setattr(
        upbit_service, "fetch_open_orders", MockUpbitService().fetch_open_orders
    )

    result = await tools["get_order_history"](status="pending")

    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "uuid-1"
    # source check removed as per plan normalisation requirements


@pytest.mark.asyncio
async def test_get_order_history_pending_crypto(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return [
                {
                    "uuid": "uuid-1",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "50000000.0",
                    "volume": "0.001",
                    "remaining_volume": "0.001",
                    "executed_volume": "0.0",
                    "market": "KRW-BTC",
                    "created_at": "2024-01-01T00:00:00Z",
                    "state": "wait",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_order_history"](status="pending")

    assert result["total_available"] == 1
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["order_id"] == "uuid-1"
    assert order["symbol"] == "KRW-BTC"
    assert order["side"] == "buy"
    assert order["status"] == "pending"
    assert order["remaining_qty"] == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_get_order_history_pending_with_symbol_filter(monkeypatch):
    tools = build_tools()

    mock_fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "uuid-1",
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000.0",
                "volume": "0.001",
                "remaining_volume": "0.001",
                "executed_volume": "0.0",
                "market": "KRW-BTC",
                "created_at": "2024-01-01",
                "state": "wait",
            }
        ]
    )

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        mock_fetch_open_orders,
    )

    result = await tools["get_order_history"](symbol="KRW-BTC", status="pending")

    mock_fetch_open_orders.assert_awaited_once_with(market="KRW-BTC")
    assert len(result["orders"]) == 1
    assert result["orders"][0]["symbol"] == "KRW-BTC"


@pytest.mark.asyncio
async def test_get_order_history_pending_empty_result(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return []

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_order_history"](status="pending")

    assert result["total_available"] == 0
    assert len(result["orders"]) == 0


@pytest.mark.asyncio
async def test_get_order_history_pending_partial_failure(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            raise RuntimeError("Upbit API error")

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_order_history"](status="pending")

    assert len(result["errors"]) == 1
    assert result["errors"][0]["market"] == "crypto"
    assert result["orders"] == []


@pytest.mark.asyncio
async def test_get_order_history_crypto_uses_closed_orders(monkeypatch):
    tools = build_tools()

    mock_closed_orders = AsyncMock(
        return_value=[
            {
                "uuid": "order-1",
                "market": "KRW-BTC",
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000",
                "remaining_volume": "0",
                "executed_volume": "0.001",
                "state": "done",
                "avg_price": "49900000",
                "created_at": "2025-02-10T09:30:00",
                "done_at": "2025-02-10T09:31:00",
            }
        ]
    )
    monkeypatch.setattr(upbit_service, "fetch_closed_orders", mock_closed_orders)
    # Mock open orders to return empty (since status="all" by default calls both)
    monkeypatch.setattr(upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # explicitly status='filled'
    result = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", days=7, limit=20
    )

    assert result["market"] == "crypto"
    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "order-1"
    assert result["orders"][0]["side"] == "buy"
    assert result["summary"]["filled"] == 1
    mock_closed_orders.assert_awaited_once_with(market="KRW-BTC", limit=20)


@pytest.mark.asyncio
async def test_get_order_history_limit_logic(monkeypatch):
    tools = build_tools()

    # Mock valid response
    monkeypatch.setattr(
        upbit_service, "fetch_closed_orders", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # Limit = 0 => Should pass limit=100 (or max) to service or similar logic
    # Our impl: limit=0 or -1 means unlimited (in our logic we used if limit > 0 else 100 for closed_orders)
    # Check if no error
    await tools["get_order_history"](symbol="KRW-BTC", limit=0)
    await tools["get_order_history"](symbol="KRW-BTC", limit=-1)

    with pytest.raises(ValueError, match="limit must be >= -1"):
        await tools["get_order_history"](symbol="KRW-BTC", limit=-2)


@pytest.mark.asyncio
async def test_get_order_history_truncated_response(monkeypatch):
    tools = build_tools()

    orders = []
    for i in range(10):
        orders.append(
            {
                "uuid": f"id-{i}",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "created_at": f"2025-01-0{i + 1}",
                "volume": "1",
                "remaining_volume": "0",
                "executed_volume": "1",
                "price": "100",
            }
        )

    monkeypatch.setattr(
        upbit_service, "fetch_closed_orders", AsyncMock(return_value=orders)
    )
    monkeypatch.setattr(upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # limit=5, should get 5 orders and truncated=True
    result = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", limit=5
    )

    assert len(result["orders"]) == 5
    assert result["truncated"] is True
    assert result["total_available"] == 10


@pytest.mark.asyncio
async def test_modify_order_dry_run_contract(monkeypatch):
    tools = build_tools()

    result = await tools["modify_order"](
        order_id="od-1",
        symbol="KRW-BTC",
        market="crypto",
        new_price=56000000,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["status"] == "simulated"
    assert result["market"] == "crypto"
    assert result["method"] == "dry_run"
    assert result["changes"]["price"]["to"] == 56000000


@pytest.mark.asyncio
async def test_modify_order_crypto_success(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "od-1",
                "state": "wait",
                "ord_type": "limit",
                "side": "bid",  # buy — ROB-518 sell-reprice floor not in play
                "price": "50000000",
                "remaining_volume": "0.001",
            }
        ),
    )
    monkeypatch.setattr(
        upbit_service,
        "cancel_and_reorder",
        AsyncMock(
            return_value={
                "cancel_result": {"uuid": "od-1"},
                "new_order": {"uuid": "od-2"},
            }
        ),
    )

    result = await tools["modify_order"](
        order_id="od-1",
        symbol="KRW-BTC",
        market="crypto",
        new_price=49000000,
        new_quantity=0.002,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    assert result["method"] == "cancel_reorder"
    assert result["new_order_id"] == "od-2"


def test_normalize_kis_exchange_code_aliases():
    """Test that exchange aliases are properly normalized to KIS codes."""
    assert _normalize_kis_exchange_code("NASDAQ") == "NASD"
    assert _normalize_kis_exchange_code("NASDAQ_GS") == "NASD"
    assert _normalize_kis_exchange_code("NYQ") == "NYSE"
    assert _normalize_kis_exchange_code("NYSEMKT") == "AMEX"
    assert _normalize_kis_exchange_code("nysemkt") == "AMEX"
    assert _normalize_kis_exchange_code("NASD") == "NASD"
    assert _normalize_kis_exchange_code("NYSE") == "NYSE"
    assert _normalize_kis_exchange_code("AMEX") == "AMEX"

    # Invalid code should raise
    with pytest.raises(ValueError, match="Unsupported KIS exchange_code"):
        _normalize_kis_exchange_code("INVALID")


@pytest.mark.asyncio
async def test_cancel_order_upbit_uuid(monkeypatch):
    tools = build_tools()
    test_uuid = "550e8400-e29b-41d4-a716-446655440000"

    class MockUpbitService:
        async def cancel_orders(self, order_uuids):
            return [{"uuid": test_uuid, "created_at": "2024-01-01T00:00:00Z"}]

    monkeypatch.setattr(
        upbit_service, "cancel_orders", MockUpbitService().cancel_orders
    )

    result = await tools["cancel_order"](order_id=test_uuid)

    assert result["success"] is True
    assert result["order_id"] == test_uuid


@pytest.mark.asyncio
async def test_cancel_order_uuid_auto_detect_market(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def cancel_orders(self, order_uuids):
            return [
                {
                    "uuid": "550e8400-e29b-41d4-a716-446655440123",
                    "created_at": "2024-01-01",
                }
            ]

    monkeypatch.setattr(
        upbit_service, "cancel_orders", MockUpbitService().cancel_orders
    )

    uuid = "550e8400-e29b-41d4-a716-446655440123"
    result = await tools["cancel_order"](order_id=uuid)

    assert result["success"] is True
    assert result["order_id"] == uuid
