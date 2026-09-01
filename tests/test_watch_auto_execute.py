"""Owner-scoped PAPER watch auto-execution regression tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.extensions.kasset.api.errors import MobileApiError
from app.models.investment_reports import InvestmentWatchAlert
from app.services.investment_reports import watch_auto_execute


def _alert(max_action: dict[str, Any] | None, action_mode: str = "auto_execute_mock"):
    return InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=None,
        source_item_uuid=None,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal("55000"),
        threshold_key="55000",
        intent="buy_review",
        action_mode=action_mode,
        rationale="r",
        max_action=max_action or {},
        valid_until=datetime(2026, 12, 31, tzinfo=UTC),
    )


def _good_max_action() -> dict[str, Any]:
    return {
        "side": "buy",
        "quantity": "10",
        "limit_price": "55000",
        "account_mode": "db_simulated",
        "owner_user_id": 7,
    }


def _paper_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "source": "paper",
        "account_mode": "db_simulated",
        "broker": "PAPER",
        "dry_run": False,
        "order_no": "PAPER-ORDER-1",
        "ledger_id": "paper-ledger-101",
        "ledger_tracking_unavailable": False,
        "order_status": "FILLED",
        "idempotent_replay": False,
    }
    result.update(overrides)
    return result


def _make_place_spy(result: Any = None):
    calls: list[dict[str, Any]] = []

    async def _spy(**kwargs: Any):
        calls.append(kwargs)
        selected = _paper_result() if result is None else result
        return dict(selected) if isinstance(selected, dict) else selected

    return _spy, calls


class _FakePaperFacade:
    """Small PaperOrderFacade double with durable client-order idempotency."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, int, Any]] = []
        self.ledger: dict[str, Any] = {}

    async def submit(self, db: Any, owner_user_id: int, request: Any):
        self.calls.append((db, owner_user_id, request))
        replayed = request.client_order_id in self.ledger
        if replayed:
            order = self.ledger[request.client_order_id]
        else:
            order = SimpleNamespace(
                id=f"paper-ledger-{len(self.ledger) + 1}",
                broker_order_id=f"PAPER-ORDER-{len(self.ledger) + 1}",
                broker="PAPER",
                status="FILLED",
            )
            self.ledger[request.client_order_id] = order
        return (
            SimpleNamespace(
                order=order,
                idempotent_replay=replayed,
            ),
            replayed,
        )


@pytest.mark.asyncio
async def test_global_flag_off_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", False
    )
    spy, calls = _make_place_spy()

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=f"corr-{uuid.uuid4().hex}",
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome["executed"] is False
    assert "auto_execute_globally_disabled" in outcome["blocking_reasons"]
    assert calls == []


@pytest.mark.asyncio
async def test_db_simulated_requires_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    max_action = _good_max_action()
    max_action.pop("owner_user_id")
    spy, calls = _make_place_spy()

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(max_action),
        correlation_id=f"corr-{uuid.uuid4().hex}",
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {
        "executed": False,
        "blocking_reasons": ["missing_owner_user_id"],
    }
    assert calls == []


@pytest.mark.parametrize("account_mode", ["kis_mock", "kis_live"])
@pytest.mark.asyncio
async def test_kis_intent_fails_closed_without_paper_reroute(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    max_action = {**_good_max_action(), "account_mode": account_mode}
    spy, calls = _make_place_spy()

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(max_action),
        correlation_id=f"corr-{uuid.uuid4().hex}",
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {"executed": False, "blocked_by": "unsupported_account"}
    assert calls == []


@pytest.mark.asyncio
async def test_toss_live_is_never_promoted_from_mock_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    max_action = {**_good_max_action(), "account_mode": "toss_live"}
    spy, calls = _make_place_spy()

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(max_action),
        correlation_id=f"corr-{uuid.uuid4().hex}",
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {"executed": False, "blocked_by": "live_account"}
    assert calls == []


@pytest.mark.asyncio
async def test_happy_path_delegates_to_owner_scoped_paper_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.extensions.kasset.api.paper_orders as paper_orders_module

    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    facade = _FakePaperFacade()
    monkeypatch.setattr(paper_orders_module, "paper_orders", facade)
    db = object()
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        db,
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
    )

    assert outcome == {"executed": True, "correlation_id": cid}
    assert len(facade.calls) == 1
    called_db, owner_user_id, request = facade.calls[0]
    assert called_db is db
    assert owner_user_id == 7
    assert request.broker == "PAPER"
    assert request.market == "KR"
    assert request.symbol == "005930"
    assert request.side == "BUY"
    assert request.order_type == "LIMIT"
    assert request.quantity == Decimal("10")
    assert request.limit_price == Decimal("55000")
    assert request.client_order_id == f"watch:{cid}"


@pytest.mark.asyncio
async def test_broker_order_without_ledger_row_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    detail = "PAPER order accepted but ledger insert returned no id"
    spy, calls = _make_place_spy(
        _paper_result(
            ledger_id=None,
            ledger_tracking_unavailable=True,
            message=detail,
        )
    )
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {
        "executed": False,
        "reason": "ledger_tracking_unavailable",
        "detail": detail,
        "correlation_id": cid,
    }
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dry_run_preview_never_counts_as_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    detail = "PAPER order preview"
    spy, calls = _make_place_spy(
        _paper_result(
            dry_run=True,
            order_no="",
            ledger_id=None,
            message=detail,
        )
    )
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {
        "executed": False,
        "reason": "dry_run_result",
        "detail": detail,
        "correlation_id": cid,
    }
    assert len(calls) == 1


@pytest.mark.parametrize("ledger_value", [None, "missing"])
@pytest.mark.asyncio
async def test_missing_ledger_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    ledger_value: object,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    place_result = _paper_result()
    if ledger_value == "missing":
        place_result.pop("ledger_id")
    else:
        place_result["ledger_id"] = None
    spy, _calls = _make_place_spy(place_result)
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {
        "executed": False,
        "reason": "missing_ledger_id",
        "detail": None,
        "correlation_id": cid,
    }


def test_invalid_non_null_ledger_id_is_rejected_separately() -> None:
    outcome = watch_auto_execute._normalize_place_result(_paper_result(ledger_id=0))

    assert outcome.executed is False
    assert outcome.reason == "invalid_ledger_id"


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.extensions.kasset.api.paper_orders as paper_orders_module

    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    facade = _FakePaperFacade()
    monkeypatch.setattr(paper_orders_module, "paper_orders", facade)
    db = object()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"

    first = await watch_auto_execute.maybe_auto_execute(
        db,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
    )
    second = await watch_auto_execute.maybe_auto_execute(
        db,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
    )

    assert first == {"executed": True, "correlation_id": cid}
    assert second == {
        "executed": False,
        "skipped": "duplicate",
        "correlation_id": cid,
    }
    assert len(facade.ledger) == 1


@pytest.mark.asyncio
async def test_failed_broker_result_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy(
        {
            "success": False,
            "reason": "broker_rejected",
            "detail": "거부",
        }
    )
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome == {
        "executed": False,
        "reason": "broker_rejected",
        "detail": "거부",
        "correlation_id": cid,
    }
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_kill_switch_error_stays_distinct_and_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )

    async def _kill_switch(**_kwargs: Any):
        raise MobileApiError(403, "KILL_SWITCH_ON", "거래 중지 상태입니다.")

    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=_kill_switch,
    )

    assert outcome == {
        "executed": False,
        "reason": "kill_switch_on",
        "detail": "거래 중지 상태입니다.",
        "correlation_id": cid,
    }


@pytest.mark.asyncio
async def test_place_order_exception_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )

    async def _raise(**_kwargs: Any):
        raise RuntimeError("PAPER submit blew up")

    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=_raise,
    )

    assert outcome["executed"] is False
    assert outcome["reason"] == "order_exception"
    assert "PAPER submit blew up" in (outcome["detail"] or "")


@pytest.mark.asyncio
async def test_malformed_broker_result_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )

    async def _malformed(**_kwargs: Any):
        return None

    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=_malformed,
    )

    assert outcome == {
        "executed": False,
        "reason": "malformed_result",
        "detail": "None",
        "correlation_id": cid,
    }


@pytest.mark.asyncio
async def test_unaccepted_order_status_never_counts_as_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, _calls = _make_place_spy(_paper_result(order_status="REJECTED"))
    cid = f"corr-{uuid.uuid4().hex}"

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(_good_max_action()),
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome["executed"] is False
    assert outcome["reason"] == "order_not_accepted"


@pytest.mark.asyncio
async def test_missing_limit_price_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    max_action = _good_max_action()
    max_action.pop("limit_price")
    spy, calls = _make_place_spy()

    outcome = await watch_auto_execute.maybe_auto_execute(
        object(),
        alert=_alert(max_action),
        correlation_id=f"corr-{uuid.uuid4().hex}",
        kst_date="2026-06-01",
        place_order_fn=spy,
    )

    assert outcome["executed"] is False
    assert "missing_limit_price" in outcome["blocking_reasons"]
    assert calls == []
