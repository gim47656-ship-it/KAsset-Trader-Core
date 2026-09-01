from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class Quote:
    symbol: str
    market: str
    price: float
    source: str
    previous_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    value: float | None = None


@dataclass(slots=True)
class Candle:
    symbol: str
    market: str
    source: str
    period: str
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float | None = None


@dataclass(slots=True)
class OrderbookLevel:
    price: float
    quantity: float


@dataclass(slots=True)
class OrderbookSnapshot:
    symbol: str
    instrument_type: str
    source: str
    asks: list[OrderbookLevel]
    bids: list[OrderbookLevel]
    total_ask_qty: float
    total_bid_qty: float
    bid_ask_ratio: float | None
    # 공급자가 제공한 timezone-aware 시각만 허용한다. 수신 시각으로
    # 비어 있는 provider 시각을 합성하지 않는다.
    as_of: dt.datetime | None = None
    price_as_of_source: Literal["broker"] | None = None
    venue: str | None = None
    venue_label: str | None = None
    is_empty_book: bool | None = None
    requires_final_recheck: bool | None = None
    empty_reason: str | None = None


__all__ = ["Quote", "Candle", "OrderbookLevel", "OrderbookSnapshot"]
