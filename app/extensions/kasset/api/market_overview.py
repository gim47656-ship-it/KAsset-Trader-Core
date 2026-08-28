"""Cached Android market overview composed from existing read-only sources."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Literal

from app.extensions.kasset.api.schemas import (
    MarketOverviewError,
    MarketOverviewErrorCode,
    MarketOverviewItem,
    MarketOverviewItemStatus,
    MarketOverviewResponse,
    MarketOverviewSession,
    MarketSessionState,
)
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_MARKET_CLOSED,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    DATA_STATE_STALE,
    US_SESSION_AFTERHOURS,
    US_SESSION_CLOSED,
    US_SESSION_PREMARKET,
    US_SESSION_REGULAR,
    kr_market_data_state,
    us_market_session,
)
from app.services.exchange_rate_service import (
    OpenErApiUsdSnapshot,
    get_open_er_api_usd_snapshot,
)

OVERVIEW_CACHE_TTL_SECONDS = 60.0
SOURCE_TIMEOUT_SECONDS = 6.0

_IndexMarket = Literal["KRX", "US"]
_IndexCurrency = Literal["KRW", "USD"]
_INDEX_DEFINITIONS: tuple[
    tuple[str, str, _IndexMarket, _IndexCurrency], ...
] = (
    ("KOSPI", "KOSPI", "KRX", "KRW"),
    ("KOSDAQ", "KOSDAQ", "KRX", "KRW"),
    ("SPX", "S&P 500", "US", "USD"),
    ("NASDAQ", "NASDAQ", "US", "USD"),
)
_FX_DEFINITIONS = (
    ("USDKRW", "USD/KRW"),
    ("JPYKRW", "JPY/KRW"),
    ("EURKRW", "EUR/KRW"),
)
_KR_SESSION_STATES: dict[str, MarketSessionState] = {
    DATA_STATE_FRESH: "OPEN",
    DATA_STATE_PREMARKET_UNAVAILABLE: "PREOPEN",
    DATA_STATE_MARKET_CLOSED: "CLOSED",
}
_US_SESSION_STATES: dict[str, MarketSessionState] = {
    US_SESSION_REGULAR: "OPEN",
    US_SESSION_PREMARKET: "PREOPEN",
    US_SESSION_AFTERHOURS: "AFTER_HOURS",
    US_SESSION_CLOSED: "CLOSED",
}

_cache: dict[str, object] = {}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _get_cached(now: float) -> MarketOverviewResponse | None:
    expires_at = _cache.get("expires_at")
    response = _cache.get("response")
    if (
        isinstance(expires_at, int | float)
        and expires_at > now
        and isinstance(response, MarketOverviewResponse)
    ):
        return response
    return None


def _set_cached(response: MarketOverviewResponse, now: float) -> None:
    _cache["response"] = response
    _cache["expires_at"] = now + OVERVIEW_CACHE_TTL_SECONDS


def _decimal_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return format(parsed, "f")


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_as_of(row: dict[str, Any]) -> str | None:
    value = row.get("quote_asof")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _latest_as_of(items: list[MarketOverviewItem]) -> str | None:
    parsed_values: list[tuple[datetime, str]] = []
    for item in items:
        if item.as_of is None:
            continue
        try:
            parsed = datetime.fromisoformat(item.as_of.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        parsed_values.append((parsed.astimezone(UTC), item.as_of))
    if not parsed_values:
        return None
    return max(parsed_values, key=lambda pair: pair[0])[1]


def _session_snapshot() -> tuple[
    list[MarketOverviewSession], dict[str, MarketSessionState]
]:
    kr_state = _KR_SESSION_STATES[kr_market_data_state()]
    us_state = _US_SESSION_STATES[us_market_session()]
    states: dict[str, MarketSessionState] = {"KRX": kr_state, "US": us_state}
    return (
        [
            MarketOverviewSession(market="KRX", state=kr_state),
            MarketOverviewSession(market="US", state=us_state),
        ],
        states,
    )


def _unavailable_item(
    *,
    symbol: str,
    name: str,
    market: Literal["KRX", "US", "FX"],
    currency: Literal["KRW", "USD"],
    session_state: MarketSessionState | None,
) -> MarketOverviewItem:
    return MarketOverviewItem(
        symbol=symbol,
        name=name,
        market=market,
        currency=currency,
        price=None,
        change_amount=None,
        change_rate=None,
        as_of=None,
        status="unavailable",
        session_state=session_state,
    )


def _index_status(
    row: dict[str, Any], *, session_state: MarketSessionState
) -> MarketOverviewItemStatus:
    data_state = row.get("data_state")
    if data_state == DATA_STATE_FRESH:
        return "available"
    if data_state in {
        DATA_STATE_STALE,
        DATA_STATE_PREMARKET_UNAVAILABLE,
        DATA_STATE_MARKET_CLOSED,
    }:
        return "stale"
    if row.get("source") == "yfinance_history_fallback":
        return "stale"
    return "available" if session_state == "OPEN" else "stale"


def _index_items(
    result: object,
    *,
    sessions: dict[str, MarketSessionState],
    group_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketOverviewItem], list[MarketOverviewError]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    if isinstance(result, dict):
        rows = result.get("indices")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                if isinstance(symbol, str) and symbol not in rows_by_symbol:
                    rows_by_symbol[symbol] = row

    items: list[MarketOverviewItem] = []
    errors: list[MarketOverviewError] = []
    for symbol, name, market, currency in _INDEX_DEFINITIONS:
        session_state = sessions[market]
        row = rows_by_symbol.get(symbol)
        price = _decimal_text(row.get("current")) if row is not None else None
        if (
            row is None
            or row.get("error") is not None
            or row.get("unavailable") is True
            or price is None
        ):
            items.append(
                _unavailable_item(
                    symbol=symbol,
                    name=name,
                    market=market,
                    currency=currency,
                    session_state=session_state,
                )
            )
            errors.append(
                MarketOverviewError(
                    scope="indices",
                    symbol=symbol,
                    code=group_error_code or "UNAVAILABLE",
                )
            )
            continue

        items.append(
            MarketOverviewItem(
                symbol=symbol,
                name=name,
                market=market,
                currency=currency,
                price=price,
                change_amount=_decimal_text(row.get("change")),
                # The source already expresses change_pct in percentage points.
                change_rate=_decimal_text(row.get("change_pct")),
                as_of=_source_as_of(row),
                status=_index_status(row, session_state=session_state),
                session_state=session_state,
            )
        )
    return items, errors


def _fx_items(
    snapshot: OpenErApiUsdSnapshot | None,
    *,
    group_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketOverviewItem], list[MarketOverviewError]]:
    as_of = _datetime_text(snapshot.as_of) if snapshot is not None else None
    try:
        values = (
            (snapshot.usd_krw, snapshot.jpy_krw, snapshot.eur_krw)
            if snapshot is not None
            else (None, None, None)
        )
    except DecimalException:
        values = (None, None, None)

    items: list[MarketOverviewItem] = []
    errors: list[MarketOverviewError] = []
    for (symbol, name), raw_price in zip(_FX_DEFINITIONS, values, strict=True):
        price = _decimal_text(raw_price)
        if price is None:
            items.append(
                _unavailable_item(
                    symbol=symbol,
                    name=name,
                    market="FX",
                    currency="KRW",
                    session_state=None,
                )
            )
            errors.append(
                MarketOverviewError(
                    scope="fx",
                    symbol=symbol,
                    code=group_error_code or "UNAVAILABLE",
                )
            )
            continue

        items.append(
            MarketOverviewItem(
                symbol=symbol,
                name=name,
                market="FX",
                currency="KRW",
                price=price,
                change_amount=None,
                change_rate=None,
                as_of=as_of,
                status="available",
                session_state=None,
            )
        )
    return items, errors


async def _bounded[T](source: Awaitable[T]) -> T:
    return await asyncio.wait_for(source, timeout=SOURCE_TIMEOUT_SECONDS)


async def _build_market_overview() -> MarketOverviewResponse:
    sessions, session_states = _session_snapshot()
    index_result, fx_result = await asyncio.gather(
        _bounded(handle_get_market_index(symbol=None)),
        _bounded(get_open_er_api_usd_snapshot()),
        return_exceptions=True,
    )

    index_error_code: MarketOverviewErrorCode | None = None
    if isinstance(index_result, BaseException):
        index_error_code = (
            "TIMEOUT" if isinstance(index_result, TimeoutError) else "UNAVAILABLE"
        )
        index_payload: object = None
    else:
        index_payload = index_result
    indices, index_errors = _index_items(
        index_payload,
        sessions=session_states,
        group_error_code=index_error_code,
    )

    fx_error_code: MarketOverviewErrorCode | None = None
    if isinstance(fx_result, BaseException):
        fx_error_code = (
            "TIMEOUT" if isinstance(fx_result, TimeoutError) else "UNAVAILABLE"
        )
        snapshot = None
    elif isinstance(fx_result, OpenErApiUsdSnapshot):
        snapshot = fx_result
    else:
        snapshot = None
    fx, fx_errors = _fx_items(snapshot, group_error_code=fx_error_code)

    all_items = [*indices, *fx]
    unavailable_count = sum(item.status == "unavailable" for item in all_items)
    if unavailable_count == len(all_items):
        status = "unavailable"
    elif any(item.status != "available" for item in all_items):
        status = "partial"
    else:
        status = "fresh"

    return MarketOverviewResponse(
        as_of=_latest_as_of(all_items),
        status=status,
        indices=indices,
        fx=fx,
        sessions=sessions,
        errors=[*index_errors, *fx_errors],
    )


async def get_market_overview() -> MarketOverviewResponse:
    """Return one 60-second single-flight market overview snapshot."""

    now = time.monotonic()
    cached = _get_cached(now)
    if cached is not None:
        return cached

    async with _get_lock():
        now = time.monotonic()
        cached = _get_cached(now)
        if cached is not None:
            return cached

        response = await _build_market_overview()
        _set_cached(response, time.monotonic())
        return response
