"""ROB-473 — 보고서 항목에 연결된 과거 live ledger 행의 감사 읽기.

이 원장에 기록하던 주문 경로(KIS 해외·Upbit)는 제거되어 writer가 없다. 남은 계약은
``report_item_uuid``로 이미 저장된 행을 찾아 공용 LinkedOrderView 형태로 투영하는
읽기뿐이므로, 행은 ORM으로 직접 넣어 그 읽기만 검증한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.asyncio


async def test_list_live_orders_by_report_item_uuid_projects_linked_view(db_session):
    from app.core.db import AsyncSessionLocal
    from app.mcp_server.tooling import live_order_ledger as m
    from app.models.review import LiveOrderLedger

    rid = uuid.uuid4()
    order_no = f"rob473-{uuid.uuid4().hex[:10]}"
    async with AsyncSessionLocal() as db:
        row = LiveOrderLedger(
            trade_date=datetime(2026, 3, 4, 14, 30, tzinfo=UTC),
            broker="kis",
            account_scope="kis_live",
            market="us",
            symbol="AAPL",
            exchange="NASD",
            side="buy",
            order_kind="limit",
            quantity=Decimal("1"),
            price=Decimal("200"),
            amount=Decimal("200"),
            currency="USD",
            order_no=order_no,
            order_time="0930",
            status="filled",
            lifecycle_state="reconciled",
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("200.5"),
            thesis="report-driven entry",
            report_item_uuid=rid,
        )
        db.add(row)
        await db.commit()

    rows = await m.list_live_orders_by_report_item_uuid(rid)

    linked = next(r for r in rows if r["order_no"] == order_no)
    assert linked["report_item_uuid"] == str(rid)
    assert linked["broker"] == "kis"
    assert linked["account_scope"] == "kis_live"
    assert linked["market"] == "us"
    assert linked["symbol"] == "AAPL"
    assert linked["status"] == "filled"
    assert linked["thesis"] == "report-driven entry"


async def test_list_live_orders_by_report_item_uuid_ignores_other_report_items(
    db_session,
):
    from app.core.db import AsyncSessionLocal
    from app.mcp_server.tooling import live_order_ledger as m
    from app.models.review import LiveOrderLedger

    linked_rid = uuid.uuid4()
    other_rid = uuid.uuid4()
    other_order_no = f"rob473-other-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        db.add(
            LiveOrderLedger(
                trade_date=datetime(2026, 3, 4, 14, 30, tzinfo=UTC),
                broker="upbit",
                account_scope="upbit_live",
                market="crypto",
                symbol="BTC",
                market_symbol="KRW-BTC",
                side="buy",
                order_kind="limit",
                order_no=other_order_no,
                status="accepted",
                lifecycle_state="accepted",
                report_item_uuid=other_rid,
            )
        )
        await db.commit()

    assert await m.list_live_orders_by_report_item_uuid(linked_rid) == []
    assert [
        r["order_no"] for r in await m.list_live_orders_by_report_item_uuid(other_rid)
    ] == [other_order_no]
