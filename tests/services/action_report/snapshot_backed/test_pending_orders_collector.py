"""Toss/Upbit pending-orders collector 계약 테스트."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.brokers.toss.dto import TossOrder
from app.services.investment_snapshots.collectors import CollectorRequest


def _request(market: str, account_scope: str) -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
    )


def _toss_order(
    *, order_id: str, symbol: str, side: str = "buy", filled: str = "2"
) -> TossOrder:
    return TossOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        time_in_force="DAY",
        status="OPEN",
        price=Decimal("70000"),
        quantity=Decimal("10"),
        order_amount=Decimal("700000"),
        currency="KRW" if symbol.isdigit() else "USD",
        ordered_at="2026-05-19T12:00:00+09:00",
        canceled_at=None,
        execution={"filledQuantity": filled},
    )


@pytest.mark.asyncio
async def test_pending_orders_collector_kr_uses_only_toss_kr_orders():
    fetcher = AsyncMock(
        return_value=[
            _toss_order(order_id="K1", symbol="005930"),
            _toss_order(order_id="U1", symbol="AAPL"),
        ]
    )
    collector = PendingOrdersSnapshotCollector(
        toss_orders_fetcher=fetcher,
        upbit_client=None,
    )

    results = await collector.collect(_request("kr", "toss_live"))

    payload = results[0].payload_json
    assert payload["count"] == 1
    assert payload["pending_orders"][0]["target_ref"]["broker"] == "toss"
    assert payload["pending_orders"][0]["target_ref"]["id"] == "K1"
    assert payload["pending_orders"][0]["remaining_quantity"] == "8"
    assert payload["pending_orders"][0]["market"] == "kr"
    assert results[0].source_kind == "toss_remote_debug"
    fetcher.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pending_orders_collector_us_uses_only_toss_us_orders():
    fetcher = AsyncMock(
        return_value=[
            _toss_order(order_id="K1", symbol="005930"),
            _toss_order(order_id="U1", symbol="AAPL", side="sell", filled="0"),
        ]
    )
    collector = PendingOrdersSnapshotCollector(
        toss_orders_fetcher=fetcher,
        upbit_client=None,
    )

    results = await collector.collect(_request("us", "toss_live"))

    order = results[0].payload_json["pending_orders"][0]
    assert order["target_ref"]["id"] == "U1"
    assert order["side"] == "sell"
    assert order["market"] == "us"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["kis_live", "kis_mock"])
async def test_pending_orders_collector_rejects_non_operational_kis_scopes(scope: str):
    fetcher = AsyncMock(return_value=[])
    collector = PendingOrdersSnapshotCollector(
        toss_orders_fetcher=fetcher,
        upbit_client=None,
    )

    results = await collector.collect(_request("kr", scope))

    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"] == "provider kis is not operational"
    fetcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_orders_collector_sanitizes_toss_failure():
    fetcher = AsyncMock(side_effect=RuntimeError("secret-token"))
    collector = PendingOrdersSnapshotCollector(
        toss_orders_fetcher=fetcher,
        upbit_client=None,
    )

    results = await collector.collect(_request("kr", "toss_live"))

    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"] == "toss_fetch_failed:RuntimeError"
    assert "secret-token" not in str(results[0].errors_json)


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_flags_stale():
    fake_upbit = AsyncMock()
    placed = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=48)
    fake_upbit.fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "U1",
                "market": "KRW-BTC",
                "side": "bid",
                "price": "100000000",
                "volume": "0.01",
                "remaining_volume": "0.01",
                "created_at": placed.isoformat(),
            }
        ]
    )
    collector = PendingOrdersSnapshotCollector(upbit_client=fake_upbit)

    results = await collector.collect(_request("crypto", "upbit_live"))

    order = results[0].payload_json["pending_orders"][0]
    assert order["stale"] is True
    assert order["side"] == "buy"
    assert order["target_ref"]["broker"] == "upbit"


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_not_stale_when_recent():
    fake_upbit = AsyncMock()
    placed = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)
    fake_upbit.fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "U2",
                "market": "KRW-ETH",
                "side": "ask",
                "price": "5000000",
                "volume": "0.1",
                "remaining_volume": "0.1",
                "created_at": placed.isoformat(),
            }
        ]
    )
    collector = PendingOrdersSnapshotCollector(upbit_client=fake_upbit)

    results = await collector.collect(_request("crypto", "upbit_live"))

    order = results[0].payload_json["pending_orders"][0]
    assert order["stale"] is False
    assert order["side"] == "sell"


@pytest.mark.asyncio
async def test_pending_orders_collector_crypto_requires_upbit_client():
    collector = PendingOrdersSnapshotCollector(upbit_client=None)

    results = await collector.collect(_request("crypto", "upbit_live"))

    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"] == "upbit_client_unavailable"
