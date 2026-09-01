"""과거 KIS mock ledger read 모델과 비운영 주문 mutation 경계를 검증한다."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.mcp_server.tooling.kis_mock_ledger as kml
import app.mcp_server.tooling.orders_modify_cancel as omc
from app.models.review import KISMockOrderLedger


async def _seed(db_session: AsyncSession, **overrides) -> KISMockOrderLedger:
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        symbol=overrides.get("symbol", "005930"),
        instrument_type="equity_kr",
        side=overrides.get("side", "buy"),
        order_type="limit",
        quantity=Decimal(overrides.get("quantity", "10")),
        price=Decimal(overrides.get("price", "70000")),
        amount=Decimal("700000"),
        currency="KRW",
        order_no=overrides.get("order_no", f"MOCK-{uuid4()}"),
        krx_fwdg_ord_orgno=overrides.get("orgno", "00950"),
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_returns_fields(
    db_session: AsyncSession,
):
    row = await _seed(db_session, orgno="00950", side="buy")
    resolved = await kml.resolve_mock_order_for_cancel(row.order_no)
    assert resolved is not None
    assert resolved["ledger_id"] == row.id
    assert resolved["symbol"] == "005930"
    assert resolved["krx_fwdg_ord_orgno"] == "00950"
    assert resolved["side"] == "buy"


@pytest.mark.asyncio
async def test_resolve_mock_order_for_cancel_missing_returns_none(
    db_session: AsyncSession,
):
    assert await kml.resolve_mock_order_for_cancel("NOPE") is None


@pytest.mark.asyncio
async def test_mark_cancelled_sets_state_and_flag(db_session: AsyncSession):
    row = await _seed(db_session)
    await kml.mark_kis_mock_order_cancelled(
        ledger_id=row.id, broker_confirmed=False, detail={"reason": "x"}
    )
    await db_session.refresh(row)
    assert row.lifecycle_state == "cancelled"
    assert row.last_reconcile_detail["broker_cancel_confirmed"] is False


@pytest.mark.asyncio
async def test_mock_cancel_is_non_operational_and_leaves_ledger_unchanged(
    db_session: AsyncSession,
):
    row = await _seed(db_session)

    result = await omc._cancel_kis_domestic(row.order_no, None, is_mock=True)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["provider_unsupported"] is True
    assert result["mutation_sent"] is False
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"


@pytest.mark.asyncio
async def test_mock_modify_is_non_operational_and_leaves_ledger_unchanged(
    db_session: AsyncSession,
):
    row = await _seed(db_session, price="70000", quantity="10")

    result = await omc._modify_kis_domestic(
        row.order_no,
        "005930",
        "equity_kr",
        new_price=71000.0,
        new_quantity=8.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["provider_unsupported"] is True
    assert result["mutation_sent"] is False
    await db_session.refresh(row)
    assert row.lifecycle_state == "accepted"
    assert row.price == Decimal("70000")
    assert row.quantity == Decimal("10")
