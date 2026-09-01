"""Best-effort, fail-open enrichment for fill notifications.

Toss/Upbit read-only 보유 조회로 체결 시점 평단·포지션·실현손익 근사치를
얻는다. 어떤 provider 예외도 알림을 막지 않는다.
"""

from __future__ import annotations

import logging

from app.services.fill_notification import FillEnrichment, FillOrder
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot

logger = logging.getLogger(__name__)


async def fetch_fill_enrichment(order: FillOrder) -> FillEnrichment | None:
    try:
        if order.market_type in ("kr", "us"):
            return await _fetch_toss(order)
        if order.market_type == "crypto":
            return await _fetch_upbit(order)
    except Exception:
        logger.warning(
            "fill enrichment failed (fail-open): symbol=%s market=%s",
            order.symbol,
            order.market_type,
            exc_info=True,
        )
    return None


def _build(order: FillOrder, *, qty: float, avg: float) -> FillEnrichment | None:
    if qty <= 0 or avg <= 0:
        return None
    enr = FillEnrichment(position_qty=qty, position_avg_price=avg, is_approximate=True)
    if order.side == "ask":  # 매도 → 실현손익 근사치
        enr.realized_pnl_amount = (order.filled_price - avg) * order.filled_qty
        enr.realized_pnl_rate = (order.filled_price / avg - 1) * 100
    return enr


async def _fetch_toss(order: FillOrder) -> FillEnrichment | None:
    snapshot = await fetch_toss_portfolio_snapshot(
        need_sellable=False,
        need_cash=False,
    )
    instrument_type = f"equity_{order.market_type}"
    symbol = order.symbol.strip().upper()
    position = next(
        (
            row
            for row in snapshot.positions
            if row.instrument_type == instrument_type
            and row.symbol.strip().upper() == symbol
        ),
        None,
    )
    if position is None:
        return None
    return _build(
        order,
        qty=float(position.quantity),
        avg=float(position.avg_buy_price),
    )


async def _fetch_upbit(order: FillOrder) -> FillEnrichment | None:
    from app.services.brokers.upbit.client import (
        fetch_my_coins,
        parse_upbit_account_row,
    )

    currency = order.symbol.split("-")[-1] if "-" in order.symbol else order.symbol
    accounts = await fetch_my_coins()
    for row in accounts:
        if str(row.get("currency", "")).upper() == currency.upper():
            parsed = parse_upbit_account_row(row)
            return _build(
                order,
                qty=float(parsed["total_quantity"]),
                avg=float(parsed["avg_buy_price"]),
            )
    return None
