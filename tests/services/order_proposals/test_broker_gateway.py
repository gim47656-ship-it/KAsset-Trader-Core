from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.services.order_proposals.broker_gateway import (
    SUPPORTED_TARGET_ACTIONS,
    cancel_target_order,
    fetch_target_order,
)
from app.services.order_proposals.errors import OrderProposalError

NOW = datetime(2026, 7, 11, 8, 23, tzinfo=UTC)


def _toss_order(**overrides):
    values = {
        "order_id": "broker-1",
        "client_order_id": None,
        "status": "FILLED",
        "symbol": "005930",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": Decimal("1.00000000"),
        "price": Decimal("100.00"),
        "execution": {},
        "ordered_at": NOW.isoformat(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _toss_rung(**overrides):
    values = {
        "rung_index": 0,
        "idempotency_key": "tosprop-legacy-1",
        "broker_order_id": None,
        "side": "buy",
        "quantity": Decimal("1"),
        "limit_price": Decimal("100.0000"),
        "created_at": NOW,
        "updated_at": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit
def test_supported_target_actions_are_toss_only():
    assert SUPPORTED_TARGET_ACTIONS == frozenset(
        {
            ("toss_live", "equity_kr"),
            ("toss_live", "equity_us"),
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_mode", "market"),
    [
        ("kis_mock", "equity_kr"),
        ("kis_live", "crypto"),
        ("upbit", "crypto"),
        ("upbit", "equity_us"),
        ("toss_live", "crypto"),
    ],
)
async def test_fetch_rejects_unsupported_target_tuple(account_mode, market):
    with pytest.raises(OrderProposalError, match="lookup unsupported"):
        await fetch_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market=market,
            account_mode=account_mode,
            now=NOW,
            toss_client=SimpleNamespace(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_unsupported_target_tuple():
    with pytest.raises(OrderProposalError, match="cancel unsupported"):
        await cancel_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market="crypto",
            account_mode="kis_live",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_unsupported_toss_target_tuple():
    with pytest.raises(OrderProposalError, match="cancel unsupported"):
        await cancel_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market="crypto",
            account_mode="toss_live",
            toss_cancel_fn=lambda **_kwargs: pytest.fail(
                "toss cancel must not be called"
            ),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_toss_target_order_returns_open_snapshot_for_pending():
    class FakeTossClient:
        async def get_order(self, order_id):
            assert order_id == "broker-1"
            return _toss_order(status="PENDING", quantity=Decimal("2"))

    snapshot = await fetch_target_order(
        order_id="broker-1",
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert snapshot.status == "open"
    assert snapshot.remaining_quantity == "2"
    assert snapshot.broker_order_id == "broker-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_toss_target_order_computes_remaining_from_partial_fill():
    class FakeTossClient:
        async def get_order(self, order_id):
            return _toss_order(
                status="PARTIAL_FILLED",
                quantity=Decimal("5"),
                execution={"filledQuantity": Decimal("2")},
            )

    snapshot = await fetch_target_order(
        order_id="broker-1",
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert snapshot.status == "open"
    assert snapshot.remaining_quantity == "3"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("broker_status", ["CANCELED", "REPLACED"])
async def test_fetch_toss_target_order_maps_canceled_and_replaced_to_cancelled(
    broker_status,
):
    class FakeTossClient:
        async def get_order(self, order_id):
            return _toss_order(status=broker_status, quantity=Decimal("1"))

    snapshot = await fetch_target_order(
        order_id="broker-1",
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert snapshot.status == "cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_toss_target_order_rejects_symbol_mismatch():
    class FakeTossClient:
        async def get_order(self, order_id):
            return _toss_order(symbol="000660")

    with pytest.raises(OrderProposalError, match="symbol mismatch"):
        await fetch_target_order(
            order_id="broker-1",
            symbol="005930",
            market="equity_kr",
            account_mode="toss_live",
            now=NOW,
            toss_client=FakeTossClient(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_toss_target_order_maps_404_to_not_found():
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    class FakeTossClient:
        async def get_order(self, order_id):
            raise TossApiResponseError(
                TossErrorEnvelope(
                    request_id="req-1", code="order-not-found", message="", data=None
                ),
                status_code=404,
            )

    with pytest.raises(OrderProposalError, match="not found uniquely"):
        await fetch_target_order(
            order_id="broker-1",
            symbol="005930",
            market="equity_kr",
            account_mode="toss_live",
            now=NOW,
            toss_client=FakeTossClient(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_toss_target_order_wraps_other_broker_errors():
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    class FakeTossClient:
        async def get_order(self, order_id):
            raise TossApiResponseError(
                TossErrorEnvelope(
                    request_id="req-1", code="internal-error", message="boom", data=None
                ),
                status_code=500,
            )

    with pytest.raises(OrderProposalError, match="lookup failed"):
        await fetch_target_order(
            order_id="broker-1",
            symbol="005930",
            market="equity_kr",
            account_mode="toss_live",
            now=NOW,
            toss_client=FakeTossClient(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_toss_target_order_routes_live_confirm_and_returns_broker_result():
    captured = {}
    broker_result = {"success": True, "original_order_id": "broker-1"}

    async def fake_toss_cancel(**kwargs):
        captured.update(kwargs)
        return broker_result

    result = await cancel_target_order(
        order_id="broker-1",
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        toss_cancel_fn=fake_toss_cancel,
    )

    assert result == broker_result
    assert captured == {
        "order_id": "broker-1",
        "dry_run": False,
        "confirm": True,
        "account_mode": "toss_live",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_toss_target_order_returns_broker_rejection():
    async def fake_toss_cancel(**_kwargs):
        return {"success": False, "error": "already filled"}

    assert await cancel_target_order(
        order_id="broker-1",
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        toss_cancel_fn=fake_toss_cancel,
    ) == {"success": False, "error": "already filled"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_proves_absence_across_open_and_closed():
    from app.services.order_proposals import broker_gateway

    calls = []

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(orders=[], has_next=False, next_cursor=None)

    rung = _toss_rung(created_at=NOW - timedelta(days=2))
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"
    assert "OPEN" in evidence[0].lookup_scope
    assert "CLOSED" in evidence[0].lookup_scope
    assert [call["status"] for call in calls] == ["OPEN", "CLOSED"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_fails_closed_when_open_is_paginated():
    from app.services.order_proposals import broker_gateway

    calls = []

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            calls.append(kwargs)
            if kwargs["status"] == "OPEN":
                return SimpleNamespace(
                    orders=[], has_next=True, next_cursor="unexpected-open-cursor"
                )
            return SimpleNamespace(orders=[], has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung()],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert [call["status"] for call in calls] == ["OPEN", "CLOSED"]
    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "OPEN order scan unexpectedly paginated"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("broker_state", ["OPEN", "FILLED"])
async def test_operator_void_toss_scan_exposes_found_broker_state(broker_state):
    from app.services.order_proposals import broker_gateway

    found = _toss_order(
        status=broker_state,
        quantity=Decimal("1.00000000"),
        price=Decimal("100.00"),
    )

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = (
                [found]
                if kwargs["status"] == ("OPEN" if broker_state == "OPEN" else "CLOSED")
                else []
            )
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "found"
    assert evidence[0].broker_order_id == "broker-1"
    assert evidence[0].broker_state == broker_state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_fails_closed_on_timeout():
    from app.services.order_proposals import broker_gateway

    class FakeTossClient:
        async def list_orders(self, **_kwargs):
            raise httpx.ReadTimeout("")

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "ReadTimeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_proves_composite_absence_without_client_id():
    from app.services.order_proposals import broker_gateway

    unrelated_order = _toss_order(
        order_id="unrelated-order",
        symbol="000660",
        status="OPEN",
    )

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [unrelated_order] if kwargs["status"] == "OPEN" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"
    assert "combination_matches=0" in evidence[0].lookup_scope


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_overrides", "expected_reason"),
    [
        ({"quantity": "not-a-decimal"}, "invalid decimal order evidence"),
        ({"price": Decimal("Infinity")}, "non-finite decimal order evidence"),
        ({"ordered_at": "not-a-datetime"}, "invalid ordered_at order evidence"),
        (
            {"ordered_at": NOW.replace(tzinfo=None).isoformat()},
            "ordered_at must be a timezone-aware datetime",
        ),
    ],
)
async def test_operator_void_toss_scan_fails_closed_on_malformed_potential_candidate(
    candidate_overrides, expected_reason
):
    from app.services.order_proposals import broker_gateway

    candidate = _toss_order(**candidate_overrides)

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [candidate] if kwargs["status"] == "OPEN" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung()],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == expected_reason


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["start", "end"])
async def test_operator_void_toss_scan_includes_composite_window_boundaries(boundary):
    from app.services.order_proposals import broker_gateway

    rung = _toss_rung()
    valid_until = NOW + timedelta(hours=4)
    window_start = rung.created_at - timedelta(hours=24)
    window_end = max(valid_until, rung.updated_at) + timedelta(hours=24)
    ordered_at = window_start if boundary == "start" else window_end
    found = _toss_order(ordered_at=ordered_at.isoformat())

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [found] if kwargs["status"] == "CLOSED" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=window_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "found"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_start", "after_end"])
async def test_operator_void_toss_scan_excludes_orders_outside_composite_window(
    boundary,
):
    from app.services.order_proposals import broker_gateway

    rung = _toss_rung()
    valid_until = NOW + timedelta(hours=4)
    window_start = rung.created_at - timedelta(hours=24)
    window_end = max(valid_until, rung.updated_at) + timedelta(hours=24)
    ordered_at = (
        window_start - timedelta(microseconds=1)
        if boundary == "before_start"
        else window_end + timedelta(microseconds=1)
    )
    outside_order = _toss_order(ordered_at=ordered_at.isoformat())

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [outside_order] if kwargs["status"] == "CLOSED" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=window_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_uses_kst_dates_and_attempt_anchor():
    from app.core.timezone import KST
    from app.services.order_proposals import broker_gateway

    created_at = datetime(2026, 7, 10, 16, 30, tzinfo=UTC)
    updated_at = datetime(2026, 7, 11, 16, 30, tzinfo=UTC)
    valid_until = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
    expected_start = created_at - timedelta(hours=24)
    expected_end = max(valid_until, updated_at) + timedelta(hours=24)
    calls = []

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(orders=[], has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung(created_at=created_at, updated_at=updated_at)],
        now=expected_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    expected_dates = {
        "from_date": expected_start.astimezone(KST).date().isoformat(),
        "to_date": expected_end.astimezone(KST).date().isoformat(),
    }
    assert evidence[0].outcome == "absent"
    assert [call["status"] for call in calls] == ["OPEN", "CLOSED"]
    assert [{key: call[key] for key in ("from_date", "to_date")} for call in calls] == [
        expected_dates,
        expected_dates,
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_fails_closed_at_closed_page_cap():
    from app.services.order_proposals import broker_gateway

    closed_pages = 0

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            nonlocal closed_pages
            if kwargs["status"] == "OPEN":
                return SimpleNamespace(orders=[], has_next=False, next_cursor=None)
            closed_pages += 1
            return SimpleNamespace(
                orders=[],
                has_next=True,
                next_cursor=f"cursor-{closed_pages}",
            )

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung()],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert closed_pages == broker_gateway._TOSS_CLOSED_PAGE_CAP
    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "CLOSED order scan page cap reached"
    assert all(item.outcome != "absent" for item in evidence.values())


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "cursor-1"])
async def test_operator_void_toss_scan_fails_closed_on_invalid_pagination(cursor):
    from app.services.order_proposals import broker_gateway

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            if kwargs["status"] == "OPEN":
                return SimpleNamespace(orders=[], has_next=False, next_cursor=None)
            return SimpleNamespace(orders=[], has_next=True, next_cursor=cursor)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "unknown"
    assert "cursor" in (evidence[0].reason or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_kis_lookup_is_explicitly_unsupported():
    from app.services.order_proposals import broker_gateway

    rung = _toss_rung(rung_index=0)
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
    )

    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "operator void lookup unsupported"
