"""Live read-only current open-order service for /invest (ROB-572)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.open_orders import (
    OpenOrderDataState,
    OpenOrderMarket,
    OpenOrderRow,
    OpenOrderSourceState,
    OpenOrdersQueryMarket,
    OpenOrdersResponse,
)
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossOrder
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.us_symbol_universe_service import get_us_names_by_symbols

logger = logging.getLogger(__name__)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(value: object) -> dt.datetime | None:
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


def _default_toss_client() -> Any:
    return TossReadClient.from_settings()


def toss_order_market(symbol: str) -> Literal["kr", "us"]:
    normalized = symbol.strip().upper()
    return "kr" if len(normalized) == 6 and normalized.isdigit() else "us"


def normalize_toss_order(order: TossOrder) -> OpenOrderRow:
    filled = _decimal(order.execution.get("filledQuantity"))
    remaining = order.quantity - (filled or Decimal("0"))
    side_raw = order.side.strip().lower()
    side: Literal["buy", "sell", "unknown"]
    if side_raw in {"buy", "bid", "매수"}:
        side = "buy"
    elif side_raw in {"sell", "ask", "매도"}:
        side = "sell"
    else:
        side = "unknown"
    market = toss_order_market(order.symbol)
    return OpenOrderRow(
        broker="toss",
        market=market,
        symbol=order.symbol.strip().upper() if market == "us" else order.symbol.strip(),
        symbol_name=None,
        side=side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        price=order.price,
        quantity=order.quantity,
        remaining_qty=remaining if remaining >= 0 else Decimal("0"),
        filled_qty=filled,
        status="pending",
        raw_status=order.status,
        ordered_at=_parse_datetime(order.ordered_at),
        order_no=order.order_id,
        exchange="TOSS",
        currency=order.currency,
    )


# Bound the Toss OPEN-order pagination: a single operator's open orders never
# need many pages, so cap it to convert a stuck/echoing cursor (broker
# misbehavior) into a bounded partial result instead of an infinite loop.
_TOSS_MAX_PAGES = 50


async def fetch_toss_open_orders(
    *,
    client_factory: Callable[[], Any] = _default_toss_client,
) -> list[TossOrder]:
    """Fetch every Toss OPEN order and close the request-scoped client."""

    client: Any | None = None
    try:
        client = client_factory()
        cursor: str | None = None
        orders: list[TossOrder] = []
        seen_cursors: set[str] = set()
        for _ in range(_TOSS_MAX_PAGES):
            page = await client.list_orders(status="OPEN", cursor=cursor)
            orders.extend(page.orders)
            if not page.has_next or not page.next_cursor:
                break
            if page.next_cursor in seen_cursors:
                logger.warning("Toss pagination cursor did not advance; stopping")
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            logger.warning(
                "Toss pagination hit max page cap (%d); returning partial",
                _TOSS_MAX_PAGES,
            )
        return orders
    finally:
        close = getattr(client, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001 - close must never break the request
                logger.warning("Toss client close failed", exc_info=True)


def _source(
    *,
    broker: Literal["toss", "upbit"],
    market: OpenOrderMarket,
    status: OpenOrderDataState,
    fetched_at: dt.datetime | None,
    count: int,
    message: str | None = None,
) -> OpenOrderSourceState:
    return OpenOrderSourceState(
        broker=broker,
        market=market,
        status=status,
        fetched_at=fetched_at,
        count=count,
        message=message,
    )


def _overall_state(sources: list[OpenOrderSourceState]) -> OpenOrderDataState:
    if not sources or all(source.status == "unavailable" for source in sources):
        return "unavailable"
    if any(source.status != "ok" for source in sources):
        return "degraded"
    return "ok"


def _sort_key(row: OpenOrderRow) -> tuple[int, dt.datetime]:
    if row.ordered_at is None:
        return (1, dt.datetime.min.replace(tzinfo=dt.UTC))
    return (0, row.ordered_at.astimezone(dt.UTC))


class CurrentOrdersService:
    def __init__(
        self,
        *,
        toss_client_factory: Callable[[], Any] | None = _default_toss_client,
        db: AsyncSession | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._toss_client_factory = toss_client_factory
        self._db = db
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))

    async def _attach_symbol_names(
        self, rows: list[OpenOrderRow]
    ) -> list[OpenOrderRow]:
        """Best-effort display-name enrichment for broker rows that lack names."""
        if self._db is None or not rows:
            return rows

        kr_symbols = sorted(
            {row.symbol for row in rows if row.market == "kr" and not row.symbol_name}
        )
        us_symbols = sorted(
            {row.symbol for row in rows if row.market == "us" and not row.symbol_name}
        )

        async def _safe(coro, label: str):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - display names must fail open
                logger.warning(
                    "open-order symbol-name resolution failed for %s",
                    label,
                    exc_info=True,
                )
                return {}

        kr_names = (
            await _safe(get_kr_names_by_symbols(kr_symbols, self._db), "kr")
            if kr_symbols
            else {}
        )
        us_names = (
            await _safe(get_us_names_by_symbols(us_symbols, self._db), "us")
            if us_symbols
            else {}
        )

        enriched: list[OpenOrderRow] = []
        for row in rows:
            if row.symbol_name:
                enriched.append(row)
                continue
            name: str | None = None
            if row.market == "kr":
                name = kr_names.get(row.symbol)
            elif row.market == "us":
                name = us_names.get(row.symbol)
            if name and name != row.symbol:
                enriched.append(row.model_copy(update={"symbol_name": name}))
            else:
                enriched.append(row)
        return enriched

    async def list_open_orders(
        self,
        *,
        market: OpenOrdersQueryMarket = "all",
    ) -> OpenOrdersResponse:
        def _fallback(
            broker: Literal["toss"],
            markets: tuple[OpenOrderMarket, ...],
        ) -> list[OpenOrderSourceState]:
            return [
                _source(
                    broker=broker,
                    market=m,
                    status="unavailable",
                    fetched_at=None,
                    count=0,
                    message="collector_error",
                )
                for m in markets
            ]

        specs: list[tuple[Any, list[OpenOrderSourceState]]] = []
        # crypto 미체결은 인증이 필요한 Upbit 계좌 읽기였고 해당 주문 모듈이
        # 제거되어 지원 공급자가 없다. 빈 목록으로 위장하지 않고 unavailable
        # 소스로 닫는다.
        preset_sources: list[OpenOrderSourceState] = []
        if market in ("all", "crypto"):
            preset_sources.append(
                _source(
                    broker="upbit",
                    market="crypto",
                    status="unavailable",
                    fetched_at=None,
                    count=0,
                    message="upbit_order_read_unsupported",
                )
            )
        if market in ("all", "kr", "us"):
            toss_markets: tuple[OpenOrderMarket, ...] = (
                ("kr",)
                if market == "kr"
                else ("us",)
                if market == "us"
                else ("kr", "us")
            )
            specs.append(
                (
                    self._collect_toss_equities(target_market=market),
                    _fallback("toss", toss_markets),
                )
            )

        # return_exceptions=True: collectors already fail open per broker, but if
        # one ever raises unexpectedly it must degrade only its market(s), never
        # 500 the whole endpoint (which would blank every tab).
        results = await asyncio.gather(
            *(coro for coro, _ in specs), return_exceptions=True
        )
        rows: list[OpenOrderRow] = []
        sources: list[OpenOrderSourceState] = list(preset_sources)
        for (_, fallback_sources), result in zip(specs, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "open-order collector raised unexpectedly", exc_info=result
                )
                sources.extend(fallback_sources)
                continue
            result_rows, result_sources = result
            rows.extend(result_rows)
            if isinstance(result_sources, list):
                sources.extend(result_sources)
            else:
                sources.append(result_sources)

        rows.sort(key=_sort_key, reverse=True)
        rows = await self._attach_symbol_names(rows)
        data_state = _overall_state(sources)
        warnings = [
            f"{source.broker}/{source.market}: {source.message or source.status}"
            for source in sources
            if source.status != "ok"
        ]
        empty_reason = None
        if not rows:
            if data_state == "unavailable":
                empty_reason = "all requested broker sources are unavailable"
            elif data_state == "degraded":
                empty_reason = (
                    "some broker sources are unavailable; no open orders from "
                    "available sources"
                )
            else:
                empty_reason = "no open orders for the selected market"
        return OpenOrdersResponse(
            market=market,
            count=len(rows),
            data_state=data_state,
            as_of=self._clock(),
            items=rows,
            sources=sources,
            warnings=warnings,
            empty_reason=empty_reason,
        )

    async def _collect_toss_equities(
        self,
        *,
        target_market: OpenOrdersQueryMarket,
    ) -> tuple[list[OpenOrderRow], OpenOrderSourceState | list[OpenOrderSourceState]]:
        now = self._clock()
        markets: tuple[Literal["kr", "us"], ...]
        if target_market == "kr":
            markets = ("kr",)
        elif target_market == "us":
            markets = ("us",)
        else:
            markets = ("kr", "us")

        if self._toss_client_factory is None:
            states = [
                _source(
                    broker="toss",
                    market=market,
                    status="unavailable",
                    fetched_at=None,
                    count=0,
                    message="toss_client_unavailable",
                )
                for market in markets
            ]
            return [], states

        try:
            orders = await fetch_toss_open_orders(
                client_factory=self._toss_client_factory
            )
            rows = [normalize_toss_order(order) for order in orders]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Toss open-order fetch failed", exc_info=True)
            states = [
                _source(
                    broker="toss",
                    market=market,
                    status="unavailable",
                    fetched_at=now,
                    count=0,
                    message=type(exc).__name__,
                )
                for market in markets
            ]
            return [], states

        filtered = [row for row in rows if row.market in markets]
        states = [
            _source(
                broker="toss",
                market=market,
                status="ok",
                fetched_at=now,
                count=sum(1 for row in filtered if row.market == market),
            )
            for market in markets
        ]
        return filtered, states
