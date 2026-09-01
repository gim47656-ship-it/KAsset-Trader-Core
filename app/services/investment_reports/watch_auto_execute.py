"""Owner-scoped PAPER execution for triggered investment watches.

The historical KIS mock path is intentionally closed.  A watch can execute only
when its ``max_action`` explicitly selects the database-simulated PAPER account
and identifies its owner.  The Android PAPER facade remains the sole owner of
account lookup, kill-switch checks, risk approval, idempotency, and order/fill
ledger writes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import OrderRequest
from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_MODE = "db_simulated"
PAPER_BROKER = "PAPER"
_ACCEPTED_PAPER_ORDER_STATUSES = frozenset(
    {"PENDING", "OPEN", "PARTIALLY_FILLED", "FILLED"}
)


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _owner_user_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class _PlaceOutcome:
    executed: bool
    reason: str | None = None
    detail: str | None = None
    replayed: bool = False


def _normalize_place_result(result: Any) -> _PlaceOutcome:
    """Accept only a durable, non-preview PAPER order envelope."""

    if not isinstance(result, dict):
        return _PlaceOutcome(False, "malformed_result", str(result)[:200])

    detail = (
        result.get("detail") or result.get("response_message") or result.get("message")
    )
    normalized_detail = str(detail) if detail else None

    if result.get("success") is not True:
        reason = (
            result.get("reason")
            or result.get("error_code")
            or result.get("status")
            or result.get("error")
            or "order_failed"
        )
        return _PlaceOutcome(False, str(reason), normalized_detail)

    if result.get("dry_run") is not False:
        reason = (
            "dry_run_result"
            if result.get("dry_run") is True
            else "invalid_dry_run_flag"
        )
        return _PlaceOutcome(False, reason, normalized_detail)

    if result.get("source") != "paper":
        return _PlaceOutcome(False, "invalid_execution_source", normalized_detail)
    if result.get("account_mode") != PAPER_ACCOUNT_MODE:
        return _PlaceOutcome(False, "invalid_account_mode", normalized_detail)
    if result.get("broker") != PAPER_BROKER:
        return _PlaceOutcome(False, "invalid_broker", normalized_detail)

    order_no = result.get("order_no")
    if not isinstance(order_no, str) or not order_no.strip():
        return _PlaceOutcome(False, "missing_broker_order_id", normalized_detail)

    tracking_unavailable = result.get("ledger_tracking_unavailable")
    if tracking_unavailable is not False:
        reason = (
            "ledger_tracking_unavailable"
            if tracking_unavailable is True
            else "invalid_ledger_tracking_flag"
        )
        return _PlaceOutcome(False, reason, normalized_detail)

    ledger_id = result.get("ledger_id")
    if ledger_id is None:
        return _PlaceOutcome(False, "missing_ledger_id", normalized_detail)
    if isinstance(ledger_id, bool) or not (
        (isinstance(ledger_id, int) and ledger_id > 0)
        or (isinstance(ledger_id, str) and bool(ledger_id.strip()))
    ):
        return _PlaceOutcome(False, "invalid_ledger_id", normalized_detail)

    order_status = str(result.get("order_status") or "").strip().upper()
    if order_status not in _ACCEPTED_PAPER_ORDER_STATUSES:
        return _PlaceOutcome(False, "order_not_accepted", normalized_detail)

    replayed = result.get("idempotent_replay", False)
    if not isinstance(replayed, bool):
        return _PlaceOutcome(False, "invalid_idempotent_replay", normalized_detail)

    return _PlaceOutcome(True, replayed=replayed)


async def _default_place_order_fn(
    *,
    db: Any,
    owner_user_id: int,
    request: OrderRequest,
) -> dict[str, Any]:
    """Submit only through the owner-scoped Android PAPER facade."""

    from app.extensions.kasset.api.paper_orders import paper_orders

    envelope, replayed = await paper_orders.submit(db, owner_user_id, request)
    order = envelope.order
    return {
        "success": True,
        "source": "paper",
        "account_mode": PAPER_ACCOUNT_MODE,
        "broker": order.broker,
        "dry_run": False,
        "order_no": order.broker_order_id,
        "ledger_id": order.id,
        "ledger_tracking_unavailable": False,
        "order_status": order.status,
        "idempotent_replay": bool(replayed or envelope.idempotent_replay),
    }


async def maybe_auto_execute(
    db: Any,
    *,
    alert: Any,
    correlation_id: str,
    kst_date: str,
    place_order_fn: Callable[..., Any] = _default_place_order_fn,
) -> dict[str, Any]:
    """Evaluate watch gates and submit one explicit owner-scoped PAPER order."""

    if alert.action_mode != "auto_execute_mock":
        return {"executed": False, "skipped": "not_auto_execute_mock"}

    max_action: dict[str, Any] = alert.max_action or {}
    account_mode = str(max_action.get("account_mode") or "").strip().lower()

    # No historical KIS intent is translated into a PAPER order.  Live accounts
    # keep their stronger diagnostic, while every removed/unwired account fails
    # closed before owner lookup, preview, or mutation.
    if account_mode != PAPER_ACCOUNT_MODE:
        try:
            assert_auto_execute_account_allowed("auto_execute_mock", account_mode)
        except AutoExecuteLiveBlocked:
            logger.warning(
                "auto_execute_mock blocked for live account on alert %s",
                alert.alert_uuid,
            )
            return {"executed": False, "blocked_by": "live_account"}
        except AutoExecuteUnsupported:
            logger.warning(
                "auto_execute_mock unsupported account on alert %s",
                alert.alert_uuid,
            )
            return {"executed": False, "blocked_by": "unsupported_account"}
        return {"executed": False, "blocked_by": "unsupported_account"}

    reasons: list[str] = []
    if not settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED:
        reasons.append("auto_execute_globally_disabled")

    owner_user_id = _owner_user_id(max_action.get("owner_user_id"))
    if owner_user_id is None:
        reasons.append("missing_owner_user_id")

    if alert.market not in {"kr", "us"}:
        reasons.append("unsupported_market")

    side = max_action.get("side")
    quantity = _to_decimal(max_action.get("quantity"))
    limit_price = _to_decimal(max_action.get("limit_price"))
    if side not in ("buy", "sell"):
        reasons.append("missing_or_invalid_side")
    if quantity is None or quantity <= 0:
        reasons.append("missing_quantity")
    if limit_price is None or limit_price <= 0:
        reasons.append("missing_limit_price")

    if reasons:
        return {"executed": False, "blocking_reasons": reasons}

    assert owner_user_id is not None
    assert side in ("buy", "sell")
    assert quantity is not None
    assert limit_price is not None

    try:
        request = OrderRequest(
            client_order_id=f"watch:{correlation_id}",
            broker=PAPER_BROKER,
            account_id=max_action.get("account_id"),
            market=alert.market,
            symbol=alert.symbol,
            side=side,
            order_type="LIMIT",
            quantity=quantity,
            limit_price=limit_price,
        )
    except Exception as exc:  # Pydantic supplies the precise validation detail.
        return {
            "executed": False,
            "reason": "invalid_order_request",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "correlation_id": correlation_id,
        }

    try:
        place_result: Any = await place_order_fn(
            db=db,
            owner_user_id=owner_user_id,
            request=request,
        )
    except MobileApiError as exc:
        place_result = {
            "success": False,
            "reason": exc.code.lower(),
            "detail": exc.message,
        }
    except Exception as exc:  # noqa: BLE001 - ambiguity must fail closed.
        place_result = {
            "success": False,
            "reason": "order_exception",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    outcome = _normalize_place_result(place_result)
    if not outcome.executed:
        logger.warning(
            "watch PAPER order failed alert=%s reason=%s",
            alert.alert_uuid,
            outcome.reason,
        )
        return {
            "executed": False,
            "reason": outcome.reason,
            "detail": outcome.detail,
            "correlation_id": correlation_id,
        }

    if outcome.replayed:
        return {
            "executed": False,
            "skipped": "duplicate",
            "correlation_id": correlation_id,
        }

    return {"executed": True, "correlation_id": correlation_id}
