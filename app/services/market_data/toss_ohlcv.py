from __future__ import annotations

import datetime as dt
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd
from pandas import DataFrame

from app.services.brokers.toss.candles import (
    fetch_toss_candles_frame,
    fetch_toss_market_indicator_candles_frame,
)
from app.services.brokers.toss.client import TossReadClient
from app.services.market_data.constants import KR_BENCHMARK_INDEX_SYMBOLS

_INTRADAY_BUCKET_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

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
_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_FRAME_COLUMNS)


def _bucket_compatible_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        return timestamp.tz_convert(dt.UTC)
    return timestamp


def _aggregate_minute_candles_frame(
    one_minute: pd.DataFrame,
    bucket_minutes: int,
    *,
    include_partial: bool = False,
) -> pd.DataFrame:
    """Broker adapter를 가져오지 않고 Toss 1분봉을 집계한다."""
    if one_minute.empty:
        return _empty_frame()

    grouped = one_minute.copy()
    grouped["datetime"] = grouped["datetime"].map(_bucket_compatible_timestamp)
    grouped = grouped.dropna(subset=["datetime"]).sort_values("datetime")
    grouped["time_group"] = grouped["datetime"].dt.floor(f"{bucket_minutes}min")
    aggregated = (
        grouped.groupby("time_group")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "value": "sum",
            }
        )
        .reset_index()
    )
    if not include_partial:
        group_sizes = grouped.groupby("time_group").size()
        aggregated = aggregated[
            aggregated["time_group"].map(group_sizes).ge(bucket_minutes).fillna(False)
        ]
    if aggregated.empty:
        return _empty_frame()

    aggregated = aggregated.rename(columns={"time_group": "datetime"})
    aggregated["date"] = aggregated["datetime"].dt.date
    aggregated["time"] = aggregated["datetime"].dt.time
    return cast(DataFrame, aggregated.loc[:, _FRAME_COLUMNS].copy())


def _market_naive_timestamp(value: object, timezone: ZoneInfo) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(timezone).tz_localize(None)
    return timestamp.tz_convert(timezone).tz_localize(None)


def _normalize_intraday_market_time(
    frame: pd.DataFrame,
    *,
    timezone: ZoneInfo,
    session_start: dt.time,
    session_end: dt.time,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.copy()
    normalized["datetime"] = normalized["datetime"].map(
        lambda value: _market_naive_timestamp(value, timezone)
    )
    normalized = normalized.dropna(subset=["datetime"])
    clocks = normalized["datetime"].dt.time
    normalized = normalized.loc[
        clocks.map(lambda value: session_start <= value < session_end)
    ]
    if end_date is None:
        completed_before = dt.datetime.now(timezone).replace(
            tzinfo=None,
            second=0,
            microsecond=0,
        )
        normalized = normalized.loc[normalized["datetime"] < completed_before]
    else:
        end_timestamp = _market_naive_timestamp(end_date, timezone)
        normalized = normalized.loc[normalized["datetime"] <= end_timestamp]
    if normalized.empty:
        return _empty_frame()
    normalized["date"] = normalized["datetime"].dt.date
    normalized["time"] = normalized["datetime"].dt.time
    return cast(
        DataFrame,
        normalized.sort_values("datetime")
        .reset_index(drop=True)
        .loc[:, _FRAME_COLUMNS],
    )


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
    frame = await _fetch_intraday_toss_frame(
        symbol=symbol,
        period=period,
        count=count,
        end_date=end_date,
    )
    return _normalize_intraday_market_time(
        frame,
        timezone=_KST,
        session_start=dt.time(8, 0),
        session_end=dt.time(20, 0),
        end_date=end_date,
    )


async def fetch_us_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    """완료된 미국 extended-session 봉을 ET-naive 시각으로 반환한다."""
    frame = await _fetch_intraday_toss_frame(
        symbol=symbol,
        period=period,
        count=count,
        end_date=end_date,
    )
    return _normalize_intraday_market_time(
        frame,
        timezone=_ET,
        session_start=dt.time(4, 0),
        session_end=dt.time(20, 0),
        end_date=end_date,
    )


async def fetch_kr_index_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    """Toss market-indicator 경로에서 실제 KOSPI/KOSDAQ 시계열을 가져온다."""

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
        frame = one_minute.tail(count).reset_index(drop=True)
    else:
        frame = (
            _aggregate_minute_candles_frame(
                one_minute,
                bucket,
                include_partial=(bucket == 60),
            )
            .tail(count)
            .reset_index(drop=True)
        )
    return _normalize_intraday_market_time(
        frame,
        timezone=_KST,
        session_start=dt.time(8, 0),
        session_end=dt.time(20, 0),
        end_date=end_date,
    )


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


def _resample_daily_frame(
    frame: pd.DataFrame,
    *,
    period: str,
    count: int,
) -> pd.DataFrame:
    """누락된 세션을 합성하지 않고 실제 Toss 일봉을 주봉 또는 월봉으로 묶는다."""
    if frame.empty:
        return _empty_frame()
    if period not in {"week", "month"}:
        raise ValueError(f"unsupported daily resample period: {period!r}")

    normalized = frame.copy()
    date_column = "date" if "date" in normalized.columns else "datetime"
    normalized["__date"] = pd.to_datetime(
        normalized[date_column],
        errors="coerce",
        utc=True,
    ).dt.tz_localize(None)
    normalized = normalized.dropna(subset=["__date"]).sort_values("__date")
    if normalized.empty:
        return _empty_frame()
    frequency = "W-FRI" if period == "week" else "M"
    normalized["__bucket"] = normalized["__date"].dt.to_period(frequency)
    rows: list[dict[str, object]] = []
    for _, group in normalized.groupby("__bucket", sort=True):
        last_date = pd.Timestamp(group["__date"].iloc[-1])
        rows.append(
            {
                "datetime": last_date,
                "date": last_date.date(),
                "time": last_date.time(),
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": float(group["volume"].sum()),
                "value": float(group["value"].sum()),
            }
        )
    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows, columns=_FRAME_COLUMNS).tail(count).reset_index(drop=True)


async def fetch_resampled_daily_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None = None,
) -> pd.DataFrame:
    multiplier = 7 if period == "week" else 31
    daily = await fetch_daily_toss_frame(
        symbol=symbol,
        count=max(count * multiplier, count),
        end_date=end_date,
    )
    return _resample_daily_frame(daily, period=period, count=count)


async def fetch_kr_index_daily_toss_frame(
    *,
    symbol: str,
    count: int,
    period: str = "day",
    end_date: dt.datetime | None = None,
) -> pd.DataFrame:
    resolved_symbol = str(symbol or "").strip().upper()
    if resolved_symbol not in KR_BENCHMARK_INDEX_SYMBOLS:
        raise ValueError(f"unsupported KR benchmark index symbol: {symbol!r}")
    if period not in {"day", "week", "month"}:
        raise ValueError(f"unsupported KR benchmark period: {period!r}")
    request_count = (
        count if period == "day" else count * (7 if period == "week" else 31)
    )
    client = TossReadClient.from_settings()
    try:
        frame = await fetch_toss_market_indicator_candles_frame(
            client=client,
            symbol=resolved_symbol,
            interval="1d",
            count=request_count,
            before=_before_from_end_date(end_date),
            max_pages=max(1, (request_count + 199) // 200),
        )
    finally:
        await client.aclose()
    if period == "day":
        return frame.tail(count).reset_index(drop=True)
    return _resample_daily_frame(frame, period=period, count=count)
