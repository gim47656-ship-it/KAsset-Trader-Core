from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.orders_history import get_order_history_impl
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.market_data import get_quote
from app.services.order_brief_formatting import enrich_order_fmt, enrich_summary_fmt

_MARKETS: tuple[str, ...] = ("kr", "us")
_EQUITY_QUOTE_CONCURRENCY = 5


def _to_external_market(value: str | None) -> str:
    mapping = {
        "equity_kr": "kr",
        "equity_us": "us",
        "kr": "kr",
        "us": "us",
    }
    return mapping.get(str(value or "").strip().lower(), str(value or "").strip())


def _parse_created_at(value: str, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback.replace(microsecond=0)

    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST, microsecond=0)
        except ValueError:
            continue

    # KIS sometimes returns time-only HHMMSS (e.g. "135334") — combine with fallback date
    if text.isdigit() and len(text) == 6:
        date_prefix = fallback.astimezone(KST).strftime("%Y%m%d")
        return datetime.strptime(date_prefix + text, "%Y%m%d%H%M%S").replace(
            tzinfo=KST, microsecond=0
        )

    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).replace(microsecond=0)


async def _fetch_market_batch(
    market: str,
    side: Literal["buy", "sell"] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    result = await get_order_history_impl(
        status="pending",
        market=market,
        side=side,
        limit=-1,
    )
    orders = [dict(order, _market=market) for order in result.get("orders", [])]
    errors = [
        {
            "market": _to_external_market(error.get("market")),
            "error": str(error.get("error") or "unknown error"),
        }
        for error in result.get("errors", [])
    ]
    return orders, errors


async def _fetch_equity_quotes(
    symbols: list[str],
    market: str,
) -> tuple[dict[str, float], list[dict[str, str]]]:
    unique_symbols = sorted({symbol for symbol in symbols if symbol})
    if not unique_symbols:
        return {}, []

    semaphore = asyncio.Semaphore(_EQUITY_QUOTE_CONCURRENCY)
    prices: dict[str, float] = {}
    errors: list[dict[str, str]] = []

    async def fetch_one(symbol: str) -> None:
        async with semaphore:
            try:
                quote = await get_quote(symbol, market)
            except Exception as exc:  # noqa: BLE001
                errors.append({"market": market, "error": f"{symbol}: {exc}"})
                return
            prices[symbol] = float(quote.price)

    await asyncio.gather(*(fetch_one(symbol) for symbol in unique_symbols))
    return prices, errors


def _normalize_order(
    order: dict[str, Any],
    *,
    as_of: datetime,
    usd_krw_rate: float | None,
) -> dict[str, Any]:
    market = str(order.get("_market") or "").strip().lower()
    raw_symbol = str(order.get("symbol") or "").strip()

    created_dt = _parse_created_at(str(order.get("ordered_at") or ""), as_of)
    order_price = float(order.get("ordered_price") or 0.0)
    quantity = float(order.get("ordered_qty") or 0.0)
    remaining_qty = float(order.get("remaining_qty") or 0.0)
    base_amount = order_price * remaining_qty
    amount_krw: float | None = base_amount
    if market == "us":
        amount_krw = None if usd_krw_rate is None else base_amount * usd_krw_rate

    age_hours = max(0, int((as_of - created_dt).total_seconds() // 3600))

    return {
        "order_id": str(order.get("order_id") or ""),
        "symbol": raw_symbol,
        "name": None,
        "raw_symbol": raw_symbol,
        "market": market,
        "side": str(order.get("side") or ""),
        "status": str(order.get("status") or ""),
        "order_price": order_price,
        "current_price": None,
        "gap_pct": None,
        "amount_krw": amount_krw,
        "quantity": quantity,
        "remaining_qty": remaining_qty,
        "created_at": created_dt.isoformat(),
        "age_hours": age_hours,
        "age_days": age_hours // 24,
        "currency": str(order.get("currency") or ""),
        "_created_dt": created_dt,
    }


def _apply_current_price(order: dict[str, Any], current_price: float | None) -> None:
    order["current_price"] = current_price
    order_price = float(order.get("order_price") or 0.0)
    if current_price is None or order_price <= 0:
        order["gap_pct"] = None
        return
    order["gap_pct"] = round((current_price - order_price) / order_price * 100, 2)


def _build_summary(orders: list[dict[str, Any]]) -> dict[str, float | int]:
    buy_orders = [order for order in orders if order.get("side") == "buy"]
    sell_orders = [order for order in orders if order.get("side") == "sell"]
    return {
        "total": len(orders),
        "buy_count": len(buy_orders),
        "sell_count": len(sell_orders),
        "total_buy_krw": sum(
            float(order["amount_krw"])
            for order in buy_orders
            if order.get("amount_krw") is not None
        ),
        "total_sell_krw": sum(
            float(order["amount_krw"])
            for order in sell_orders
            if order.get("amount_krw") is not None
        ),
    }


async def fetch_pending_orders(
    *,
    market: Literal["kr", "us", "all"] = "all",
    min_amount: float = 0,
    include_current_price: bool = True,
    side: Literal["buy", "sell"] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    requested_markets = list(_MARKETS if market == "all" else (market,))
    effective_as_of = as_of or now_kst().replace(microsecond=0)

    batch_results = await asyncio.gather(
        *(
            _fetch_market_batch(requested_market, side)
            for requested_market in requested_markets
        )
    )

    source_orders: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for batch_orders, batch_errors in batch_results:
        source_orders.extend(batch_orders)
        errors.extend(batch_errors)

    usd_krw_rate: float | None = None
    if any(str(order.get("_market") or "") == "us" for order in source_orders):
        try:
            usd_krw_rate = await get_usd_krw_rate()
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"market": "us", "error": f"USD/KRW rate fetch failed: {exc}"}
            )

    normalized_orders = [
        _normalize_order(order, as_of=effective_as_of, usd_krw_rate=usd_krw_rate)
        for order in source_orders
    ]

    # --- KR name enrichment ---
    kr_symbols = [
        order["symbol"]
        for order in normalized_orders
        if order["market"] == "kr" and order["symbol"]
    ]
    kr_name_map: dict[str, str] = {}
    if kr_symbols:
        try:
            kr_name_map = await get_kr_names_by_symbols(kr_symbols)
        except Exception as exc:  # noqa: BLE001
            errors.append({"market": "kr", "error": f"name lookup failed: {exc}"})

    for order in normalized_orders:
        order["name"] = (
            kr_name_map.get(order["symbol"]) if order["market"] == "kr" else None
        )

    if include_current_price:
        (
            (kr_prices, kr_errors),
            (us_prices, us_errors),
        ) = await asyncio.gather(
            _fetch_equity_quotes(
                [
                    order["raw_symbol"]
                    for order in normalized_orders
                    if order["market"] == "kr"
                ],
                "kr",
            ),
            _fetch_equity_quotes(
                [
                    order["raw_symbol"]
                    for order in normalized_orders
                    if order["market"] == "us"
                ],
                "us",
            ),
        )
        errors.extend(kr_errors)
        errors.extend(us_errors)

        for order in normalized_orders:
            current_price: float | None = None
            if order["market"] == "kr":
                current_price = kr_prices.get(order["raw_symbol"])
            elif order["market"] == "us":
                current_price = us_prices.get(order["raw_symbol"])
            _apply_current_price(order, current_price)

    filtered_orders = [
        order
        for order in normalized_orders
        if order.get("amount_krw") is None
        or float(order["amount_krw"]) >= float(min_amount)
    ]
    filtered_orders.sort(key=lambda order: order["_created_dt"])

    for order in filtered_orders:
        order.pop("_created_dt", None)

    for order in filtered_orders:
        enrich_order_fmt(order)

    summary = _build_summary(filtered_orders)
    enrich_summary_fmt(summary, as_of=effective_as_of)

    return {
        "success": bool(filtered_orders) or not errors,
        "market": market,
        "orders": filtered_orders,
        "summary": summary,
        "errors": errors,
    }


__all__ = ["fetch_pending_orders"]
