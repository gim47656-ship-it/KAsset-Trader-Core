from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.mcp_server.tooling.orders_toss_variants as toss_orders
from app.core.config import settings


@pytest.fixture
def _stub_toss_preview(monkeypatch):
    """Keep Toss approval-token coverage offline and deterministic."""
    monkeypatch.setattr(toss_orders, "validate_toss_api_config", lambda: [])

    @asynccontextmanager
    async def client_context():
        yield SimpleNamespace()

    monkeypatch.setattr(toss_orders, "_client_context", client_context)
    monkeypatch.setattr(
        toss_orders,
        "_preview_price_context",
        AsyncMock(return_value=(Decimal("70000"), "KRW", None)),
    )
    monkeypatch.setattr(
        toss_orders,
        "check_warnings_guard",
        AsyncMock(return_value=SimpleNamespace(warnings=[], error_message=None)),
    )
    monkeypatch.setattr(
        toss_orders, "_nxt_preflight_context", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        toss_orders,
        "_preview_cost_context",
        AsyncMock(
            return_value={
                "estimated_value": "700000",
                "estimated_value_currency": "KRW",
            }
        ),
    )
    monkeypatch.setattr(
        toss_orders, "evaluate_sector_concentration", AsyncMock(return_value=None)
    )


def _order_kwargs(**overrides):
    kwargs = {
        "symbol": "005930",
        "side": "buy",
        "market": "kr",
        "order_type": "limit",
        "quantity": 10,
        "price": 70000,
        "account_mode": "toss_live",
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_toss_preview_emits_approval_hash(_stub_toss_preview):
    res = await toss_orders.toss_preview_order(**_order_kwargs())

    assert res["success"] is True and res["preview"] is True
    assert res["approval_hash"].startswith("p6a1.")
    assert "approval_expires_at" in res
    assert res["payload_preview"]["clientOrderId"].startswith("tossp6-")


@pytest.mark.asyncio
async def test_required_mode_blocks_without_hash(monkeypatch, _stub_toss_preview):
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "required")

    res = await toss_orders._toss_place_order_impl(
        **_order_kwargs(dry_run=False, confirm=True)
    )

    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"
    assert res["mutation_sent"] is False


@pytest.mark.asyncio
async def test_required_mode_rejects_retired_kis_mock_path(
    monkeypatch,
    _stub_toss_preview,
):
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "required")

    res = await toss_orders._toss_place_order_impl(
        **_order_kwargs(
            account_mode="kis_mock",
            dry_run=False,
            confirm=True,
        )
    )

    assert res["success"] is False
    assert "Toss live tools only support account_mode='toss_live'" in res["error"]
    assert res["account_mode"] == "toss_live"


@pytest.mark.asyncio
async def test_required_mode_still_blocks_default_live_path(
    monkeypatch,
    _stub_toss_preview,
):
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "required")

    res = await toss_orders._toss_place_order_impl(
        **_order_kwargs(
            account_mode=None,
            dry_run=False,
            confirm=True,
        )
    )

    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"
    assert res["mutation_sent"] is False


@pytest.mark.asyncio
async def test_mismatched_hash_fails_closed_with_diff(
    monkeypatch,
    _stub_toss_preview,
):
    monkeypatch.setattr(settings, "toss_approval_hash_mode", "required")
    preview = await toss_orders.toss_preview_order(**_order_kwargs())

    res = await toss_orders._toss_place_order_impl(
        **_order_kwargs(
            quantity=11,
            dry_run=False,
            confirm=True,
            approval_hash=preview["approval_hash"],
        )
    )

    assert res["success"] is False
    assert res["error_code"] == "approval_hash_mismatch"
    assert res["mutation_sent"] is False
    assert "diff" in res
