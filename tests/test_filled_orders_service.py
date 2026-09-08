"""Tests for filled-orders aggregation service."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import now_kst


def _toss_order(
    *,
    order_id: str,
    symbol: str,
    filled_quantity: str = "4",
    average_price: str = "77.37",
):
    from app.services.brokers.toss.dto import TossOrder

    return TossOrder(
        order_id=order_id,
        symbol=symbol,
        side="SELL",
        order_type="LIMIT",
        time_in_force="DAY",
        status="FILLED",
        price=Decimal(average_price),
        quantity=Decimal(filled_quantity),
        order_amount=None,
        currency="KRW" if symbol.isdigit() else "USD",
        ordered_at=(now_kst() - timedelta(hours=1)).isoformat(),
        canceled_at=None,
        execution={
            "filledQuantity": Decimal(filled_quantity),
            "averageFilledPrice": Decimal(average_price),
            "filledAmount": Decimal(filled_quantity) * Decimal(average_price),
            "commission": Decimal("0.5"),
            "tax": Decimal("0.2"),
        },
    )


@pytest.mark.unit
class TestTossFilledOrdersFetch:
    @pytest.mark.asyncio
    async def test_reads_closed_orders_and_filters_requested_market(self, monkeypatch):
        from app.services import filled_orders_service as svc
        from app.services.brokers.toss.dto import TossOrdersPage

        client = MagicMock()
        client.list_orders = AsyncMock(
            return_value=TossOrdersPage(
                orders=[
                    _toss_order(order_id="KR-1", symbol="005930"),
                    _toss_order(order_id="US-1", symbol="UBER"),
                ],
                next_cursor=None,
                has_next=False,
            )
        )
        client.aclose = AsyncMock()
        monkeypatch.setattr(svc, "_default_toss_read_client", lambda: client)

        orders, errors = await svc._fetch_toss_filled(days=7, markets={"us"})

        assert errors == []
        assert [order["order_id"] for order in orders] == ["US-1"]
        assert orders[0]["account"] == "toss"
        assert orders[0]["instrument_type"] == "equity_us"
        assert orders[0]["quantity"] == pytest.approx(4.0)
        assert orders[0]["price"] == pytest.approx(77.37)
        assert orders[0]["fee"] == pytest.approx(0.7)
        assert client.list_orders.await_args.kwargs["status"] == "CLOSED"
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_paginates_closed_orders_without_duplicate_provider_queries(
        self, monkeypatch
    ):
        from app.services import filled_orders_service as svc
        from app.services.brokers.toss.dto import TossOrdersPage

        client = MagicMock()
        client.list_orders = AsyncMock(
            side_effect=[
                TossOrdersPage(
                    orders=[_toss_order(order_id="US-1", symbol="AAPL")],
                    next_cursor="next",
                    has_next=True,
                ),
                TossOrdersPage(
                    orders=[_toss_order(order_id="US-2", symbol="MSFT")],
                    next_cursor=None,
                    has_next=False,
                ),
            ]
        )
        client.aclose = AsyncMock()
        monkeypatch.setattr(svc, "_default_toss_read_client", lambda: client)

        orders, errors = await svc._fetch_toss_filled(days=7, markets={"us"})

        assert errors == []
        assert [order["order_id"] for order in orders] == ["US-1", "US-2"]
        assert client.list_orders.await_count == 2
        assert client.list_orders.await_args_list[1].kwargs["cursor"] == "next"

    @pytest.mark.asyncio
    async def test_provider_failure_returns_each_requested_market_error(
        self, monkeypatch
    ):
        from app.services import filled_orders_service as svc

        client = MagicMock()
        client.list_orders = AsyncMock(side_effect=RuntimeError("history unavailable"))
        client.aclose = AsyncMock()
        monkeypatch.setattr(svc, "_default_toss_read_client", lambda: client)

        orders, errors = await svc._fetch_toss_filled(
            days=7,
            markets={"kr", "us"},
        )

        assert orders == []
        assert errors == [
            {"market": "kr", "error": "history unavailable"},
            {"market": "us", "error": "history unavailable"},
        ]
        client.aclose.assert_awaited_once()
