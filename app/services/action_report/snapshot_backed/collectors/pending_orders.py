"""Toss/Upbit 미체결 주문 read-only snapshot collector."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.action_report.common.staleness import (
    is_crypto_pending_order_stale,
)
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.brokers.toss.dto import TossOrder
from app.services.current_orders_service import (
    fetch_toss_open_orders,
    toss_order_market,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class _UpbitClientProtocol(Protocol):
    async def fetch_open_orders(
        self, market: str | None = None
    ) -> list[dict[str, Any]]: ...


class PendingOrdersSnapshotCollector:
    """KR/US Toss와 crypto Upbit 미체결 주문을 수집한다."""

    snapshot_kind: str = "pending_orders"

    def __init__(
        self,
        *,
        toss_orders_fetcher: Any | None = None,
        upbit_client: _UpbitClientProtocol | None,
    ) -> None:
        self._fetch_toss_orders = toss_orders_fetcher or fetch_toss_open_orders
        self._upbit = upbit_client

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        market = request.market
        if request.account_scope in {"kis_live", "kis_mock"}:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="provider kis is not operational",
                    as_of=now,
                )
            ]
        if market in {"kr", "us"}:
            if request.account_scope != "toss_live":
                return [
                    unavailable_result(
                        snapshot_kind=self.snapshot_kind,
                        market=market,
                        account_scope=request.account_scope,
                        origin="auto_trader_db",
                        reason=(
                            f"unsupported equity account scope: {request.account_scope}"
                        ),
                        as_of=now,
                    )
                ]
            return [await self._collect_toss(now=now, request=request)]
        if market == "crypto":
            return [await self._collect_upbit(now=now, request=request)]
        return [
            unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=market,
                account_scope=request.account_scope,
                origin="auto_trader_db",
                reason=f"unsupported_market:{market}",
                as_of=now,
            )
        ]

    async def _collect_toss(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        try:
            all_orders = await self._fetch_toss_orders()
        except Exception as exc:  # noqa: BLE001 - snapshot must fail closed
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                origin="toss_api",
                reason=f"toss_fetch_failed:{type(exc).__name__}",
                as_of=now,
            )

        orders = [
            order
            for order in (all_orders or [])
            if toss_order_market(str(order.symbol)) == request.market
        ]
        normalized = [
            _normalize_toss_order(order, market=request.market) for order in orders
        ]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=request.market,
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="toss_api",
            as_of=now,
            coverage={"count": len(normalized)},
        )

    async def _collect_upbit(
        self, *, now: dt.datetime, request: CollectorRequest
    ) -> SnapshotCollectResult:
        if self._upbit is None:
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason="upbit_client_unavailable",
                as_of=now,
            )
        try:
            raw = await self._upbit.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001 - snapshot must fail closed
            return unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market="crypto",
                account_scope=request.account_scope,
                origin="upbit_mcp",
                reason=f"upbit_fetch_failed:{type(exc).__name__}",
                as_of=now,
            )
        normalized = [_normalize_upbit_order(row, now=now) for row in raw or []]
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market="crypto",
            account_scope=request.account_scope,
            payload={"pending_orders": normalized, "count": len(normalized)},
            origin="upbit_mcp",
            as_of=now,
            coverage={"count": len(normalized)},
        )


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _stringify_decimal(value: Any) -> str | None:
    parsed = _decimal(value)
    return str(parsed) if parsed is not None else None


def _normalize_toss_order(order: TossOrder, *, market: str) -> dict[str, Any]:
    quantity = _decimal(order.quantity)
    filled = _decimal(order.execution.get("filledQuantity"))
    remaining = (
        quantity - filled
        if quantity is not None and filled is not None and 0 <= filled <= quantity
        else None
    )
    side_raw = str(order.side or "").strip().lower()
    side = "buy" if side_raw == "buy" else "sell" if side_raw == "sell" else "unknown"
    placed_at = _coerce_datetime(order.ordered_at)
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "toss",
            "id": str(order.order_id),
            "raw": {
                "status": order.status,
                "order_type": order.order_type,
                "time_in_force": order.time_in_force,
                "currency": order.currency,
            },
        },
        "symbol": order.symbol,
        "side": side,
        "price": _stringify_decimal(order.price),
        "quantity": _stringify_decimal(order.quantity),
        "remaining_quantity": (str(remaining) if remaining is not None else None),
        "placed_at": placed_at.isoformat() if placed_at is not None else None,
        "expected_expiry": None,
        "expiry_reason": None,
        "stale": False,
        "market": market,
    }


def _normalize_upbit_order(row: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    placed_at_raw = row.get("created_at") or row.get("placed_at")
    placed_at = _coerce_datetime(placed_at_raw)
    stale = bool(placed_at and is_crypto_pending_order_stale(placed_at, now=now))
    side_raw = str(row.get("side") or "").strip().lower()
    if side_raw == "bid":
        side = "buy"
    elif side_raw == "ask":
        side = "sell"
    else:
        side = side_raw or "unknown"
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "upbit",
            "id": str(row.get("uuid") or ""),
            "raw": dict(row),
        },
        "symbol": row.get("market"),
        "side": side,
        "price": _stringify_optional(row.get("price")),
        "quantity": _stringify_optional(row.get("volume")),
        "remaining_quantity": _stringify_optional(row.get("remaining_volume")),
        "placed_at": placed_at.isoformat() if placed_at is not None else None,
        "expected_expiry": None,
        "expiry_reason": None,
        "stale": stale,
        "market": "crypto",
    }


def _stringify_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None
