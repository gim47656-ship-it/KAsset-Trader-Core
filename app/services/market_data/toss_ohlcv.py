from __future__ import annotations

import datetime as dt

import pandas as pd

from app.services.brokers.kis._base_market_data import _aggregate_minute_candles_frame
from app.services.brokers.toss.candles import (
    fetch_toss_candles_frame,
    fetch_toss_market_indicator_candles_frame,
)
from app.services.brokers.toss.client import TossReadClient
from app.services.market_data.constants import KR_BENCHMARK_INDEX_SYMBOLS

_INTRADAY_BUCKET_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def _before_from_end_date(end_date: dt.datetime | None) -> str | None:
    if end_date is None:
        return None
    return end_date.isoformat()


async def _fetch_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    bucket = _INTRADAY_BUCKET_MINUTES[period]
    # N개 구간 집계에 필요한 1분봉을 200개 단위로 페이지 조회한다.
    request_count = count if bucket == 1 else max(count * bucket, bucket)
    client = TossReadClient.from_settings()
    try:
        one_minute = await fetch_toss_candles_frame(
            client=client,
            symbol=symbol,
            interval="1m",
            count=request_count,
            before=_before_from_end_date(end_date),
            max_pages=max(1, (request_count + 199) // 200),
        )
    finally:
        await client.aclose()
    if bucket == 1:
        return one_minute.tail(count).reset_index(drop=True)
    aggregated = _aggregate_minute_candles_frame(
        one_minute,
        bucket,
        include_partial=(bucket == 60),
    )
    return aggregated.tail(count).reset_index(drop=True)


async def fetch_kr_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    return await _fetch_intraday_toss_frame(
        symbol=symbol,
        period=period,
        count=count,
        end_date=end_date,
    )


async def fetch_us_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    """미국 주식 장중봉을 공통 Toss 페이지 조회·분봉 집계 경로로 가져온다."""

    return await _fetch_intraday_toss_frame(
        symbol=symbol,
        period=period,
        count=count,
        end_date=end_date,
    )


async def fetch_kr_index_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    """Fetch a real KOSPI/KOSDAQ series from Toss's market-indicator route."""

    resolved_symbol = str(symbol or "").strip().upper()
    if resolved_symbol not in KR_BENCHMARK_INDEX_SYMBOLS:
        raise ValueError(f"unsupported KR benchmark index symbol: {symbol!r}")
    bucket = _INTRADAY_BUCKET_MINUTES[period]
    request_count = count if bucket == 1 else max(count * bucket, bucket)
    client = TossReadClient.from_settings()
    try:
        one_minute = await fetch_toss_market_indicator_candles_frame(
            client=client,
            symbol=resolved_symbol,
            interval="1m",
            count=request_count,
            before=_before_from_end_date(end_date),
            max_pages=max(1, (request_count + 199) // 200),
        )
    finally:
        await client.aclose()
    if bucket == 1:
        return one_minute.tail(count).reset_index(drop=True)
    aggregated = _aggregate_minute_candles_frame(
        one_minute,
        bucket,
        include_partial=(bucket == 60),
    )
    return aggregated.tail(count).reset_index(drop=True)


async def fetch_daily_toss_frame(
    *,
    symbol: str,
    count: int,
    end_date: dt.datetime | None = None,
) -> pd.DataFrame:
    client = TossReadClient.from_settings()
    try:
        return await fetch_toss_candles_frame(
            client=client,
            symbol=symbol,
            interval="1d",
            count=count,
            before=_before_from_end_date(end_date),
            adjusted=True,
            max_pages=max(1, (count + 199) // 200),
        )
    finally:
        await client.aclose()
