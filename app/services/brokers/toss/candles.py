from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.services.brokers.toss.dto import TossCandle, TossCandlesPage

_FRAME_COLUMNS = [
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
]


class TossCandleClient(Protocol):
    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage: ...


def empty_toss_candles_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_FRAME_COLUMNS)


def toss_candles_page_to_frame(page: TossCandlesPage) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for candle in page.candles:
        timestamp = pd.Timestamp(candle.timestamp)
        close = float(candle.close_price)
        volume = float(candle.volume)
        records.append(
            {
                "datetime": timestamp,
                "date": timestamp.date(),
                "time": timestamp.time(),
                "open": float(candle.open_price),
                "high": float(candle.high_price),
                "low": float(candle.low_price),
                "close": close,
                "volume": volume,
                "value": close * volume,
            }
        )
    if not records:
        return empty_toss_candles_frame()
    return (
        pd.DataFrame(records)
        .sort_values("datetime")
        .reset_index(drop=True)
        .loc[:, _FRAME_COLUMNS]
    )


async def fetch_toss_candles(
    *,
    client: TossCandleClient,
    symbol: str,
    interval: str,
    count: int,
    before: str | None = None,
    adjusted: bool | None = None,
    max_pages: int = 20,
) -> list[TossCandle]:
    """기존 cursor 계약으로 정확한 Decimal 캔들 DTO를 페이지 병합한다."""
    remaining = max(int(count), 1)
    cursor = before
    candles_by_timestamp: dict[str, TossCandle] = {}
    for _ in range(max_pages):
        page_count = min(remaining, 200)
        page = await client.candles(
            symbol,
            interval=interval,
            count=page_count,
            before=cursor,
            adjusted=adjusted,
        )
        for candle in page.candles:
            candles_by_timestamp.setdefault(candle.timestamp, candle)
        remaining -= len(page.candles)
        if remaining <= 0 or not page.next_before:
            break
        cursor = page.next_before
    ordered = sorted(
        candles_by_timestamp.values(),
        key=lambda candle: pd.Timestamp(candle.timestamp),
    )
    return ordered[-count:]


async def fetch_toss_candles_frame(
    *,
    client: TossCandleClient,
    symbol: str,
    interval: str,
    count: int,
    before: str | None = None,
    adjusted: bool | None = None,
    max_pages: int = 20,
) -> pd.DataFrame:
    candles = await fetch_toss_candles(
        client=client,
        symbol=symbol,
        interval=interval,
        count=count,
        before=before,
        adjusted=adjusted,
        max_pages=max_pages,
    )
    return toss_candles_page_to_frame(
        TossCandlesPage(candles=candles, next_before=None)
    )
