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


@pytest.mark.unit
class TestUpbitFilledOrdersFetch:
    @pytest.mark.asyncio
    async def test_cancel_with_partial_fill_is_accepted(self, monkeypatch):
        """Issue 1 regression: cancelled orders with executed_volume > 0 must not be dropped."""
        from app.services import filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()
        fake_order = {
            "state": "cancel",
            "market": "KRW-ETH",
            "side": "bid",
            "executed_volume": "0.5",
            "price": "3000000",
            "avg_price": "3000000",
            "paid_fee": "750",
            "uuid": "cancel-partial-uuid",
            "created_at": recent_ts,
            "trades": [
                {
                    "uuid": "trade-cancel-p",
                    "volume": "0.5",
                    "funds": "1500000",
                    "created_at": recent_ts,
                }
            ],
        }

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = AsyncMock(return_value=[fake_order])
        fake_upbit.fetch_order_detail = AsyncMock(return_value=fake_order)
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        assert len(orders) == 1
        assert orders[0]["symbol"] == "ETH"
        assert abs(orders[0]["quantity"] - 0.5) < 1e-9

    @pytest.mark.asyncio
    async def test_time_window_crawl_continues_after_cancel_only_window(
        self, monkeypatch
    ):
        from app.services import filled_orders_service as svc

        end_at = now_kst().replace(microsecond=0)
        start_at = end_at - timedelta(days=8)

        def _make_order(
            uuid_val: str,
            ts: str,
            *,
            state: str = "done",
            executed_volume: str = "0.01",
        ) -> dict:
            return {
                "state": state,
                "market": "KRW-BTC",
                "side": "bid",
                "executed_volume": executed_volume,
                "price": "100000000",
                "avg_price": "100000000",
                "paid_fee": "500",
                "uuid": uuid_val,
                "created_at": ts,
                "trades": [
                    {
                        "uuid": f"trade-{uuid_val}",
                        "volume": "0.01",
                        "funds": "1000000",
                        "created_at": ts,
                    }
                ],
            }

        older_ts = (start_at + timedelta(hours=1)).isoformat()
        calls = []

        def fake_fetch_closed(market, limit, **kwargs):
            calls.append((market, limit, kwargs["start_time"], kwargs["end_time"]))
            if len(calls) == 1:
                cancel_ts = (kwargs["end_time"] - timedelta(minutes=1)).isoformat()
                return [
                    _make_order(
                        "cancel-only",
                        cancel_ts,
                        state="cancel",
                        executed_volume="0",
                    )
                ]
            return [_make_order("older-fill", older_ts)]

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = AsyncMock(side_effect=fake_fetch_closed)
        fake_upbit.fetch_order_detail = AsyncMock(
            side_effect=lambda uuid: _make_order(uuid, older_ts)
        )
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(
            days=8, start_at=start_at, end_at=end_at
        )

        assert errors == []
        assert len(calls) == 2
        assert calls[0][2] == end_at - timedelta(days=7)
        assert calls[0][3] == end_at
        assert calls[1][2] == start_at
        assert calls[1][3] == end_at - timedelta(days=7)
        assert [order["order_id"] for order in orders] == ["older-fill"]

    @pytest.mark.asyncio
    async def test_saturated_time_window_is_recursively_split(self, monkeypatch):
        from app.services import filled_orders_service as svc

        end_at = now_kst().replace(microsecond=0)
        start_at = end_at - timedelta(hours=2)
        midpoint = start_at + timedelta(hours=1)

        def _make_order(uuid_val: str, ts: str) -> dict:
            return {
                "state": "done",
                "market": "KRW-BTC",
                "side": "bid",
                "executed_volume": "0.01",
                "price": "100000000",
                "avg_price": "100000000",
                "paid_fee": "500",
                "uuid": uuid_val,
                "created_at": ts,
                "trades": [
                    {
                        "uuid": f"trade-{uuid_val}",
                        "volume": "0.01",
                        "funds": "1000000",
                        "created_at": ts,
                    }
                ],
            }

        calls = []

        def fake_fetch_closed(market, limit, **kwargs):
            calls.append((market, limit, kwargs["start_time"], kwargs["end_time"]))
            if kwargs["start_time"] == start_at and kwargs["end_time"] == end_at:
                return [
                    _make_order("saturated-a", start_at.isoformat()),
                    _make_order("saturated-b", start_at.isoformat()),
                ]
            if kwargs["end_time"] == midpoint:
                return []
            return [
                _make_order("split-fill", (midpoint + timedelta(minutes=1)).isoformat())
            ]

        fake_upbit = MagicMock()
        monkeypatch.setattr(svc, "_UPBIT_CLOSED_ORDERS_LIMIT", 2)
        fake_upbit.fetch_closed_orders = AsyncMock(side_effect=fake_fetch_closed)
        fake_upbit.fetch_order_detail = AsyncMock(
            side_effect=lambda uuid: _make_order(uuid, end_at.isoformat())
        )
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(
            days=1, start_at=start_at, end_at=end_at
        )

        assert errors == []
        assert len(calls) == 3
        assert calls[0][2:] == (start_at, end_at)
        assert calls[1][2:] == (start_at, midpoint)
        assert calls[2][2:] == (midpoint, end_at)
        assert [order["order_id"] for order in orders] == ["split-fill"]

    @pytest.mark.asyncio
    async def test_detail_fetch_failure_falls_back_to_aggregate_fill(self, monkeypatch):
        """When order detail fetch fails, the aggregate fill (no trades) should be returned."""
        from app.services import filled_orders_service as svc

        recent_ts = (now_kst() - timedelta(hours=1)).isoformat()
        raw_order = {
            "state": "done",
            "market": "KRW-BTC",
            "side": "ask",
            "executed_volume": "0.02",
            "price": "50000000",
            "avg_price": "50000000",
            "paid_fee": "500",
            "uuid": "order-no-detail",
            "created_at": recent_ts,
            "trades": [],  # no trades in list response
        }

        fake_upbit = MagicMock()
        fake_upbit.fetch_closed_orders = AsyncMock(return_value=[raw_order])
        fake_upbit.fetch_order_detail = AsyncMock(side_effect=RuntimeError("API error"))
        monkeypatch.setattr(svc, "upbit_service", fake_upbit)

        orders, errors = await svc._fetch_upbit_filled(days=1)

        assert errors == []
        # Falls back to aggregate fill (fill_seq=0, full executed_volume)
        assert len(orders) == 1
        assert orders[0]["fill_seq"] == 0
        assert abs(orders[0]["quantity"] - 0.02) < 1e-9
