"""Tests for the dormant KIS mock order ledger and read model.

Covers the persisted model, ledger helpers, lifecycle mapping, shadow
reservations, and direct parser/client envelopes. KIS order execution routes
are intentionally not exercised because KIS is no longer an operational
provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _offline_kis_mock_baseline(monkeypatch):
    """ROB-1296: resolve the pre-trade holdings baseline without calling KIS VTS.

    ``_fetch_kis_mock_baseline_qty`` builds its own ``KISClient(is_mock=True)``,
    which issues an OAuth token against ``openapivts.koreainvestment.com`` before
    any of the seams these tests patch come into play. It is explicitly
    best-effort — it swallows every error and returns ``None`` so the reconciler
    can flag ``baseline_missing`` later — which is exactly the value these tests
    already observe, just reached via a live network round trip. Returning
    ``None`` states that unchanged outcome deterministically; it asserts nothing
    about a holding, so no fabricated position is introduced.
    """

    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        kis_mock_ledger,
        "_fetch_kis_mock_baseline_qty",
        AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Task 1: model shape
# ---------------------------------------------------------------------------


def test_model_columns_and_constraints():
    from app.models.review import KISMockOrderLedger

    cols = {c.name for c in KISMockOrderLedger.__table__.columns}
    assert {
        "id",
        "trade_date",
        "symbol",
        "instrument_type",
        "side",
        "order_type",
        "quantity",
        "price",
        "amount",
        "fee",
        "currency",
        "order_no",
        "order_time",
        "krx_fwdg_ord_orgno",
        "account_mode",
        "broker",
        "status",
        "response_code",
        "response_message",
        "raw_response",
        "reason",
        "thesis",
        "strategy",
        "notes",
        "created_at",
        # ROB-102 additive columns
        "lifecycle_state",
        "holdings_baseline_qty",
        "reconcile_attempts",
        "reconciled_at",
        "last_reconcile_detail",
    } <= cols
    assert KISMockOrderLedger.__table__.schema == "review"
    # Naming convention: ck_%(table_name)s_%(constraint_name)s
    constraint_names = {c.name for c in KISMockOrderLedger.__table__.constraints}
    assert "uq_kis_mock_ledger_order_no" in constraint_names
    assert any(
        "kis_mock_ledger_account_mode_kis_mock" in (n or "") for n in constraint_names
    )
    assert any("kis_mock_ledger_broker_kis" in (n or "") for n in constraint_names)
    assert any("kis_mock_ledger_status_allowed" in (n or "") for n in constraint_names)
    assert any(
        "kis_mock_ledger_lifecycle_state_allowed" in (n or "") for n in constraint_names
    )


# ---------------------------------------------------------------------------
# Task 3: helper insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_helper_inserts_row(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    captured: dict = {}

    class FakeResult:
        inserted_primary_key = (123,)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, stmt):
            captured["stmt"] = stmt
            return FakeResult()

        async def commit(self):
            pass

    def fake_factory():
        return lambda: FakeDB()

    monkeypatch.setattr(kis_mock_ledger, "_order_session_factory", fake_factory)

    ledger_id = await kis_mock_ledger._save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=10,
        price=70000,
        amount=700000,
        currency="KRW",
        order_no="0001234567",
        order_time="091500",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message="정상처리",
        raw_response={"rt_cd": "0", "output": {"ODNO": "0001234567"}},
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
    )
    assert ledger_id == 123


# ---------------------------------------------------------------------------
# ROB-102: lifecycle mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_to_lifecycle_state_mapping():
    """ROB-102: existing 3-value `status` maps to ROB-100 lifecycle states."""
    from app.mcp_server.tooling.kis_mock_ledger import _status_to_lifecycle_state

    assert _status_to_lifecycle_state("accepted") == "accepted"
    assert _status_to_lifecycle_state("rejected") == "failed"
    assert _status_to_lifecycle_state("unknown") == "anomaly"
    assert _status_to_lifecycle_state(None) == "anomaly"
    assert _status_to_lifecycle_state("garbage") == "anomaly"


@pytest.mark.asyncio
async def test_save_helper_persists_lifecycle_state(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    captured: dict = {}

    class FakeResult:
        inserted_primary_key = (321,)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, stmt):
            captured["stmt"] = stmt
            return FakeResult()

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: FakeDB()
    )

    new_id = await kis_mock_ledger._save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=10,
        price=1000,
        amount=10000,
        currency="KRW",
        order_no="MOCK-1",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={"rt_cd": "0"},
        reason=None,
        thesis=None,
        strategy=None,
        notes=None,
        lifecycle_state="accepted",
    )
    assert new_id == 321
    # Verify that lifecycle_state is in the insert values
    params = captured["stmt"].compile().params
    assert params["lifecycle_state"] == "accepted"


# ---------------------------------------------------------------------------
# ROB-255: KIS mock DB shadow pending helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_pending_orders_are_formatted_from_lifecycle_rows(monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from app.mcp_server.tooling import kis_mock_ledger

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class FakeSvc:
        def __init__(self, db):
            self.db = db

        async def list_open_orders(self, **kwargs):
            assert kwargs["symbol"] == "005930"
            assert kwargs["instrument_type"] == "equity_kr"
            return [
                SimpleNamespace(
                    id=123,
                    trade_date=datetime(2026, 5, 14, 9, 1, tzinfo=UTC),
                    symbol="005930",
                    instrument_type="equity_kr",
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("2"),
                    price=Decimal("70000"),
                    amount=Decimal("140000"),
                    currency="KRW",
                    order_no="MOCK-255",
                    lifecycle_state="accepted",
                )
            ]

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: FakeDB()
    )
    monkeypatch.setattr(kis_mock_ledger, "KISMockLifecycleService", FakeSvc)

    rows = await kis_mock_ledger._list_kis_mock_shadow_pending_orders(
        normalized_symbol="005930", market_type="equity_kr"
    )

    assert rows == [
        {
            "order_id": "MOCK-255",
            "ledger_id": 123,
            "symbol": "005930",
            "market": "kr",
            "instrument_type": "equity_kr",
            "side": "buy",
            "order_type": "limit",
            "status": "pending",
            "lifecycle_state": "accepted",
            "ordered_qty": 2.0,
            "remaining_qty": 2.0,
            "filled_qty": 0.0,
            "ordered_price": 70000.0,
            "amount": 140000.0,
            "currency": "KRW",
            "ordered_at": "2026-05-14T09:01:00+00:00",
            "created_at": "2026-05-14T09:01:00+00:00",
            "source": "kis_mock_ledger_shadow",
            "confidence": "db_shadow_pending",
            "warning": kis_mock_ledger.KIS_MOCK_SHADOW_PENDING_WARNING,
        }
    ]


@pytest.mark.asyncio
async def test_shadow_exposure_unknown_on_db_error(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    async def boom(**kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(kis_mock_ledger, "_list_kis_mock_shadow_pending_orders", boom)

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        normalized_symbol="005930", market_type="equity_kr"
    )

    assert result["confidence"] == "unknown"
    assert result["buy_reserved_amount"] == 0.0
    assert result["sell_reserved_quantity"] == 0.0
    assert "db unavailable" in result["error"]


# ---------------------------------------------------------------------------
# ROB-890: conservative KR DAY expiry for shadow reservation
# ---------------------------------------------------------------------------

_KST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=9))


def _make_shadow_row(  # noqa: PLR0913 — test factory
    *,
    side: str = "buy",
    instrument_type: str = "equity_kr",
    ordered_at: str = "2026-07-15T09:30:00+09:00",
    amount: float = 140_000.0,
    remaining_qty: float = 2.0,
    lifecycle_state: str = "accepted",
    order_no: str = "MOCK-001",
    ledger_id: int = 1,
) -> dict:
    """Build a shadow pending order dict matching _shadow_row_to_order output."""
    return {
        "order_id": order_no,
        "ledger_id": ledger_id,
        "symbol": "005930",
        "market": "kr" if instrument_type == "equity_kr" else "us",
        "instrument_type": instrument_type,
        "side": side,
        "order_type": "limit",
        "status": "pending",
        "lifecycle_state": lifecycle_state,
        "ordered_qty": 2.0,
        "remaining_qty": remaining_qty,
        "filled_qty": 0.0,
        "ordered_price": 70_000.0,
        "amount": amount,
        "currency": "KRW" if instrument_type == "equity_kr" else "USD",
        "ordered_at": ordered_at,
        "created_at": ordered_at,
        "source": "kis_mock_ledger_shadow",
        "confidence": "db_shadow_pending",
        "warning": None,
    }


@pytest.mark.asyncio
async def test_previous_day_sell_reservation_does_not_lock_sellable(monkeypatch):
    """ROB-890: stale KR DAY sell from a prior session frees sellable qty."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="sell",
            ordered_at="2026-07-14T09:30:00+09:00",
            remaining_qty=2.0,
            order_no="STALE-SELL",
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        normalized_symbol="005930",
        market_type="equity_kr",
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    assert result["buy_reserved_amount"] == 0.0
    assert result["sell_reserved_quantity"] == 0.0
    assert result["expired_reservation_count"] == 1
    # Expired row still visible in orders list
    assert len(result["orders"]) == 1


@pytest.mark.asyncio
async def test_previous_day_buy_reservation_does_not_lock_cash(monkeypatch):
    """ROB-890: stale KR DAY buy from a prior session frees reserved cash."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="buy",
            ordered_at="2026-07-14T09:30:00+09:00",
            amount=140_000.0,
            order_no="STALE-BUY",
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        normalized_symbol="005930",
        market_type="equity_kr",
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    assert result["buy_reserved_amount"] == 0.0
    assert result["sell_reserved_quantity"] == 0.0
    assert result["expired_reservation_count"] == 1


@pytest.mark.asyncio
async def test_same_day_valid_session_order_still_locks(monkeypatch):
    """ROB-890: same-session KR DAY order keeps reserving cash/qty."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="buy",
            ordered_at="2026-07-15T09:30:00+09:00",
            amount=140_000.0,
        ),
        _make_shadow_row(
            side="sell",
            ordered_at="2026-07-15T10:00:00+09:00",
            remaining_qty=1.0,
            order_no="MOCK-SELL",
            ledger_id=2,
        ),
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        now=__import__("datetime").datetime(2026, 7, 15, 10, 30, tzinfo=_KST),
    )

    assert result["buy_reserved_amount"] == 140_000.0
    assert result["sell_reserved_quantity"] == 1.0
    assert result["expired_reservation_count"] == 0


@pytest.mark.asyncio
async def test_partial_fill_remaining_quantity_reserved(monkeypatch):
    """ROB-890: partial fill rows reserve only remaining qty, not full order."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="sell",
            lifecycle_state="fill",
            ordered_at="2026-07-15T09:30:00+09:00",
            remaining_qty=0.5,
            order_no="MOCK-PARTIAL",
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        now=__import__("datetime").datetime(2026, 7, 15, 10, 30, tzinfo=_KST),
    )

    # Only remaining 0.5 is reserved, not the full 2.0
    assert result["sell_reserved_quantity"] == 0.5


@pytest.mark.asyncio
async def test_expired_partial_fill_does_not_lock(monkeypatch):
    """ROB-890: expired partial fill frees the remaining qty too."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="sell",
            lifecycle_state="fill",
            ordered_at="2026-07-14T09:30:00+09:00",
            remaining_qty=0.5,
            order_no="MOCK-EXPIRED-PARTIAL",
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    assert result["sell_reserved_quantity"] == 0.0


@pytest.mark.asyncio
async def test_us_equity_rows_not_expired_by_kr_day_rules(monkeypatch):
    """ROB-890: equity_us rows are not subject to KR DAY expiry."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="buy",
            instrument_type="equity_us",
            ordered_at="2026-07-14T09:30:00+09:00",
            amount=100.0,
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        market_type="equity_us",
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    # US equity rows are NOT expired by KR rules → keep locked
    assert result["buy_reserved_amount"] == 100.0
    assert result["expired_reservation_count"] == 0


@pytest.mark.asyncio
async def test_missing_ordered_at_keeps_locked_fail_closed(monkeypatch):
    """ROB-890: rows with missing/unparseable ordered_at are kept locked."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="sell",
            ordered_at=None,  # type: ignore[arg-type]
            remaining_qty=2.0,
        )
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    # Fail-closed: unparseable timestamp → keep locked
    assert result["sell_reserved_quantity"] == 2.0
    assert result["expired_reservation_count"] == 0


@pytest.mark.asyncio
async def test_mixed_expired_and_valid_rows(monkeypatch):
    """ROB-890: mix of expired and same-session rows reserves only valid ones."""
    from app.mcp_server.tooling import kis_mock_ledger

    rows = [
        _make_shadow_row(
            side="buy",
            ordered_at="2026-07-14T09:30:00+09:00",  # expired
            amount=100_000.0,
            order_no="EXPIRED",
        ),
        _make_shadow_row(
            side="buy",
            ordered_at="2026-07-15T09:30:00+09:00",  # valid
            amount=50_000.0,
            order_no="VALID",
            ledger_id=2,
        ),
    ]
    monkeypatch.setattr(
        kis_mock_ledger,
        "_list_kis_mock_shadow_pending_orders",
        AsyncMock(return_value=rows),
    )

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        now=__import__("datetime").datetime(2026, 7, 15, 9, 30, tzinfo=_KST),
    )

    assert result["buy_reserved_amount"] == 50_000.0
    assert result["expired_reservation_count"] == 1


@pytest.mark.asyncio
async def test_list_open_orders_unaffected_by_expiry(monkeypatch):
    """ROB-890: reconciliation/list_open_orders still returns all open rows."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from app.mcp_server.tooling import kis_mock_ledger

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class FakeSvc:
        def __init__(self, db):
            self.db = db

        async def list_open_orders(self, **kwargs):
            # Return both stale and fresh rows — reconciliation sees ALL
            return [
                SimpleNamespace(
                    id=1,
                    trade_date=datetime(2026, 7, 14, 9, 30, tzinfo=UTC),
                    symbol="005930",
                    instrument_type="equity_kr",
                    side="sell",
                    order_type="limit",
                    quantity=Decimal("2"),
                    price=Decimal("70000"),
                    amount=Decimal("140000"),
                    currency="KRW",
                    order_no="STALE",
                    lifecycle_state="accepted",
                ),
                SimpleNamespace(
                    id=2,
                    trade_date=datetime(2026, 7, 15, 9, 30, tzinfo=UTC),
                    symbol="005930",
                    instrument_type="equity_kr",
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("1"),
                    price=Decimal("70000"),
                    amount=Decimal("70000"),
                    currency="KRW",
                    order_no="FRESH",
                    lifecycle_state="accepted",
                ),
            ]

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: FakeDB()
    )
    monkeypatch.setattr(kis_mock_ledger, "KISMockLifecycleService", FakeSvc)

    rows = await kis_mock_ledger._list_kis_mock_shadow_pending_orders(
        normalized_symbol="005930", market_type="equity_kr"
    )

    # Both stale and fresh rows are returned — no time filtering at this level
    assert len(rows) == 2
    assert {r["order_id"] for r in rows} == {"STALE", "FRESH"}


# ---------------------------------------------------------------------------
# ROB-730: place-time provenance spine — mint correlation_id + emit forecast
# ---------------------------------------------------------------------------


def _mock_exec_result(rt_cd: str = "0", odno: str = "0001234567") -> dict:
    return {"odno": odno, "ord_tmd": "091500", "msg": "정상처리", "rt_cd": rt_cd}


def _mock_preview() -> dict:
    return {"price": 70000, "quantity": 10, "estimated_value": 700000}


@pytest.mark.asyncio
async def test_record_rejects_missing_strategy_without_writing(monkeypatch):
    """The post-send writer cannot manufacture attribution for direct callers."""
    from app.mcp_server.tooling import kis_mock_ledger
    from app.services.kis_mock_attribution import InvalidStrategy

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value="fc-1")
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    with pytest.raises(InvalidStrategy):
        await kis_mock_ledger._record_kis_mock_order(
            normalized_symbol="005930",
            market_type="equity_kr",
            side="buy",
            order_type="limit",
            dry_run_result=_mock_preview(),
            execution_result=_mock_exec_result(),
            reason="t",
            thesis=None,
            strategy=None,
            notes=None,
            correlation_id="pre-submit-cid",
        )
    save.assert_not_awaited()
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_preserves_explicit_correlation_id(monkeypatch):
    """A direct helper call must provide the pre-submit correlation id."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value="fc-1")
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    with pytest.raises(ValueError, match="pre-submit correlation_id"):
        await kis_mock_ledger._record_kis_mock_order(
            normalized_symbol="005930",
            market_type="equity_kr",
            side="buy",
            order_type="limit",
            dry_run_result=_mock_preview(),
            execution_result=_mock_exec_result(),
            reason="t",
            thesis=None,
            strategy="posture-v1",
            notes=None,
        )
    save.assert_not_awaited()
    pub.assert_not_awaited()


def _make_domestic_orders():
    """DomesticOrderClient with a mocked parent (mirrors the retry-test harness)."""
    from unittest.mock import MagicMock

    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    parent._kis_url = lambda path: f"https://host{path}"
    tok = MagicMock()
    tok.clear_token = AsyncMock()
    parent._token_manager = tok
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    return DomesticOrderClient(parent), parent


@pytest.mark.asyncio
async def test_record_accepts_real_domestic_order_shape(monkeypatch):
    """ROB-843 Blocker 1: the accepted contract must survive the REAL domestic
    order-service return shape (which the service builds after verifying
    rt_cd==0), not a hand-fabricated rt_cd fixture. The service preserves the
    provider-verified success metadata so the mock boundary can prove it."""
    from unittest.mock import patch

    from app.mcp_server.tooling import kis_mock_ledger

    instance, parent = _make_domestic_orders()
    # Realistic raw KIS accepted envelope (rt_cd at top level, ODNO in output).
    parent._request_with_rate_limit = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "주문 전송 완료 되었습니다.",
            "output": {"ODNO": "0001234567", "ORD_TMD": "091500"},
        }
    )
    with patch(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=False),
    ):
        exec_result = await instance.order_korea_stock("005930", "buy", 1, 70000)

    # provider-verified success metadata is preserved to the boundary
    assert exec_result["rt_cd"] == "0"
    assert exec_result["odno"] == "0001234567"

    monkeypatch.setattr(
        kis_mock_ledger, "_save_kis_mock_order_ledger", AsyncMock(return_value=7)
    )
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=exec_result,
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-accepted-1",
    )
    assert result["success"] is True
    assert result["status"] == "accepted"
    assert result["order_no"] == "0001234567"


@pytest.mark.asyncio
async def test_record_accepted_returns_success_true(monkeypatch):
    """ROB-843: accepted requires provider success (rt_cd==0) AND broker order ID."""
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        kis_mock_ledger, "_save_kis_mock_order_ledger", AsyncMock(return_value=5)
    )
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=_mock_exec_result(rt_cd="0", odno="0001234567"),
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-accepted-2",
    )
    assert result["success"] is True
    assert result["status"] == "accepted"
    assert result["order_no"] == "0001234567"


@pytest.mark.asyncio
async def test_record_rejected_returns_success_false(monkeypatch):
    """ROB-843: a provider rejection (rt_cd != 0) is never a success."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    exec_result = {"odno": "", "ord_tmd": None, "msg": "거부", "rt_cd": "40"}
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=exec_result,
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-rejected-1",
    )
    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "broker_rejected"
    # native lifecycle truth + raw evidence preserved
    assert save.await_args.kwargs["status"] == "rejected"
    assert result["execution"] == exec_result
    assert result["response_message"] == "거부"


@pytest.mark.asyncio
async def test_record_idless_success_code_returns_unknown_false(monkeypatch):
    """ROB-843: rt_cd==0 but no broker order ID is unknown, not success."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value=None)
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result={"rt_cd": "0", "odno": ""},
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-unknown-1",
        target_price=80000.0,
    )
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert result["reason"] == "missing_broker_order_id"
    assert result["order_no"] is None
    pub.assert_not_awaited()  # no forecast for a non-accepted order


@pytest.mark.asyncio
async def test_record_malformed_payload_returns_false(monkeypatch):
    """ROB-843: a non-mapping broker response is malformed, never success."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result="totally not a dict",  # type: ignore[arg-type]
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-malformed-1",
    )
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert result["reason"] == "malformed_response"
    assert save.await_args.kwargs["status"] == "unknown"


@pytest.mark.asyncio
async def test_record_whitespace_order_id_is_not_accepted(monkeypatch):
    """ROB-843 Blocker 2: a blank/whitespace odno is never an accepted order."""
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        kis_mock_ledger, "_save_kis_mock_order_ledger", AsyncMock(return_value=5)
    )
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result={"rt_cd": "0", "odno": "   "},
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-unknown-2",
    )
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert result["reason"] == "missing_broker_order_id"
    assert result["order_no"] is None


@pytest.mark.asyncio
async def test_record_strips_valid_order_id(monkeypatch):
    """ROB-843 Blocker 2: a valid id with surrounding whitespace is normalized
    (stripped) and stored/returned that way."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result={"rt_cd": "0", "odno": "  0001234567  "},
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-accepted-5",
    )
    assert result["success"] is True
    assert result["order_no"] == "0001234567"
    assert result["odno"] == "0001234567"
    assert save.await_args.kwargs["order_no"] == "0001234567"


@pytest.mark.asyncio
async def test_record_provider_failure_with_order_id_still_rejected(monkeypatch):
    """ROB-843 Blocker 2: a valid order id does NOT rescue a provider failure."""
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        kis_mock_ledger, "_save_kis_mock_order_ledger", AsyncMock(return_value=5)
    )
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result={"rt_cd": "40", "odno": "0001234567", "msg": "거부"},
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-rejected-4",
    )
    assert result["success"] is False
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_record_redacts_sensitive_evidence(monkeypatch):
    """ROB-843 Blocker 3: sensitive keys are redacted (recursively, case-variant,
    nested) from stored + returned evidence; original is not mutated; non-secret
    diagnostics survive; no raw secret remains in the persisted payload."""
    import copy
    import json

    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    exec_result = {
        "rt_cd": "0",
        "odno": "0001234567",
        "msg": "정상처리",
        "AppKey": "SECRETKEY",
        "approval_key": "APKEY-XYZ",
        "headers": {"Authorization": "Bearer tok123", "Cookie": "sid=abc"},
        "echoes": [{"api-key": "k1"}, {"safe": "keep-me"}],
    }
    original = copy.deepcopy(exec_result)
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=exec_result,
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-accepted-6",
    )
    saved = save.await_args.kwargs["raw_response"]
    assert saved["AppKey"] == "[REDACTED]"
    assert saved["approval_key"] == "[REDACTED]"
    assert saved["headers"]["Authorization"] == "[REDACTED]"
    assert saved["headers"]["Cookie"] == "[REDACTED]"
    assert saved["echoes"][0]["api-key"] == "[REDACTED]"
    # non-sensitive diagnostics preserved
    assert saved["echoes"][1]["safe"] == "keep-me"
    assert saved["odno"] == "0001234567"
    assert saved["rt_cd"] == "0"
    assert saved["msg"] == "정상처리"
    # returned execution is also redacted (no secret leaves the boundary)
    assert result["execution"]["AppKey"] == "[REDACTED]"
    # original object was not mutated
    assert exec_result == original
    # no raw secret string survives in the persisted payload
    blob = json.dumps(saved, ensure_ascii=False)
    assert "SECRETKEY" not in blob
    assert "APKEY-XYZ" not in blob
    assert "Bearer tok123" not in blob
    assert "sid=abc" not in blob


async def _record_accepted(monkeypatch, **over):
    """Run _record_kis_mock_order for an accepted order with publish stubbed."""
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        kis_mock_ledger, "publish_place_time_forecast", AsyncMock(return_value=None)
    )
    kw = {
        "normalized_symbol": "005930",
        "market_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "dry_run_result": _mock_preview(),
        "execution_result": {"rt_cd": "0", "odno": "0001234567"},
        "reason": "t",
        "thesis": None,
        "strategy": "posture-v1",
        "notes": None,
        "correlation_id": "cid-x",
    }
    kw.update(over)
    return await kis_mock_ledger._record_kis_mock_order(**kw)


@pytest.mark.asyncio
async def test_record_native_conflict_requeries_existing_row(monkeypatch):
    """ROB-843 P1: an on-conflict no-op with an existing native row is durable —
    tracking stays available, success preserved, and NO control row is written."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=None)  # on-conflict no-op
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "_native_row_exists", AsyncMock(return_value=True)
    )
    result = await _record_accepted(monkeypatch)
    assert result["success"] is True
    assert result["ledger_tracking_unavailable"] is False
    # exactly one ledger write (the native insert) — no fallback/marker control row
    assert save.await_count == 1


@pytest.mark.asyncio
async def test_record_native_error_signals_tracking_unavailable(monkeypatch):
    """ROB-843 P1: a lost native write (and no conflict row) signals
    ledger_tracking_unavailable so the caller KEEPS the write-ahead reservation
    unresolved. Broker success is preserved and NO control row is written."""
    from app.mcp_server.tooling import kis_mock_ledger
    from app.services.brokers.kis.mock_scalping_exec.tracking_state import (
        LedgerWriteError,
    )

    save = AsyncMock(side_effect=LedgerWriteError("db down"))
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(
        kis_mock_ledger, "_native_row_exists", AsyncMock(return_value=False)
    )
    result = await _record_accepted(monkeypatch)
    assert result["success"] is True  # broker accepted (bookkeeping failed)
    assert result["ledger_tracking_unavailable"] is True
    # only the failed native insert was attempted — no control-row write follows
    assert save.await_count == 1


@pytest.mark.asyncio
async def test_record_rejected_order_mints_but_does_not_publish(monkeypatch):
    """ROB-730: a rejected order still gets a correlation_id (spine), but no
    place-time forecast is emitted (mirrors live: publish only when accepted)."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value=None)
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=_mock_exec_result(rt_cd="40", odno=""),
        reason="t",
        thesis=None,
        strategy="posture-v1",
        notes=None,
        correlation_id="direct-record-rejected-5",
        target_price=80000.0,
    )

    assert result["status"] == "rejected"
    assert result["correlation_id"] is not None
    assert save.await_args.kwargs["correlation_id"] == result["correlation_id"]
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_kis_mock_order_ledger_persists_report_item_uuid(db_session):
    from uuid import uuid4

    from sqlalchemy import select

    from app.mcp_server.tooling.kis_mock_ledger import _save_kis_mock_order_ledger
    from app.models.review import KISMockOrderLedger

    item_uuid = uuid4()
    order_no = f"ROB734-{uuid4().hex[:10]}"
    ledger_id = await _save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1,
        price=70000,
        amount=70000,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={"rt_cd": "0"},
        reason="ROB-734 mirror",
        thesis="counterfactual",
        strategy="mirror_counterfactual",
        notes=None,
        report_item_uuid=item_uuid,
    )
    assert ledger_id is not None

    row = (
        await db_session.execute(
            select(KISMockOrderLedger).where(KISMockOrderLedger.order_no == order_no)
        )
    ).scalar_one()
    assert row.report_item_uuid == item_uuid


@pytest.mark.asyncio
async def test_record_kis_mock_order_threads_mirror_metadata(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=123)
    pub = AsyncMock(return_value="forecast-1")
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    item_uuid = uuid4()
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result={"price": 70000, "quantity": 1, "estimated_value": 70000},
        execution_result={"rt_cd": "0", "odno": "ROB743-1"},
        reason="ROB-743",
        thesis="mirror",
        strategy="mirror_counterfactual",
        notes="source_bucket=place_original",
        correlation_id=f"mirror:{item_uuid}",
        target_price=76000,
        min_hold_days=10,
        report_item_uuid=item_uuid,
        mirror_cohort="mock_counterfactual",
        mirror_source_bucket="place_original",
    )

    assert result["ledger_id"] == 123
    assert save.await_args.kwargs["report_item_uuid"] == item_uuid
    assert save.await_args.kwargs["mirror_cohort"] == "mock_counterfactual"
    assert save.await_args.kwargs["mirror_source_bucket"] == "place_original"


@pytest.mark.asyncio
async def test_record_kis_mock_order_does_not_publish_forecast_without_ledger_id(
    monkeypatch,
):
    from uuid import uuid4

    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=None)
    pub = AsyncMock(return_value="forecast-orphan")
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    item_uuid = uuid4()
    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result={"price": 70000, "quantity": 1, "estimated_value": 70000},
        execution_result={"rt_cd": "0", "odno": "ROB743-duplicate"},
        reason="ROB-743",
        thesis="mirror",
        strategy="mirror_counterfactual",
        notes="source_bucket=place_original",
        correlation_id=f"mirror:{item_uuid}",
        target_price=76000,
        min_hold_days=10,
        report_item_uuid=item_uuid,
        mirror_cohort="mock_counterfactual",
        mirror_source_bucket="place_original",
    )

    assert result["ledger_id"] is None
    pub.assert_not_awaited()
