"""KR 분봉의 공개 API와 DB/Toss 오케스트레이션."""

from __future__ import annotations

import datetime

import pandas as pd

from app.services.kr_intraday._repository import (
    _fetch_intraday_history_rows,
    _resolve_universe_row,
    _store_minute_candles_background,
    _UniverseError,
)
from app.services.kr_intraday._types import (
    _INTRADAY_FRAME_COLUMNS,
    _INTRADAY_PERIOD_CONFIGS,
    _KST,
)
from app.services.kr_intraday._utils import (
    _aggregate_minutes_to_buckets,
    _empty_intraday_frame,
    _ensure_kst_aware,
    _history_rows_to_frame,
    _merge_overlay_into_intraday_frame,
    _to_kst_naive_series,
)
from app.services.market_data.toss_ohlcv import fetch_kr_intraday_toss_frame

__all__ = [
    "read_kr_intraday_candles",
    "read_kr_hourly_candles_1h",
    "_aggregate_minutes_to_hourly",
    "_empty_intraday_frame",
    "_INTRADAY_FRAME_COLUMNS",
    "_INTRADAY_PERIOD_CONFIGS",
    "_store_minute_candles_background",
]


def _aggregate_minutes_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """분봉을 1시간봉으로 집계하고 KST-naive 시각을 반환한다."""
    aggregated = _aggregate_minutes_to_buckets(df, bucket_minutes=60)
    if aggregated.empty:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
    aggregated["datetime"] = _to_kst_naive_series(aggregated["datetime"])
    return aggregated[["datetime", "open", "high", "low", "close", "volume"]]


def _session_for_toss_minute(value: datetime.datetime) -> str | None:
    clock = value.time()
    if datetime.time(8, 0) <= clock < datetime.time(9, 0):
        return "PRE_MARKET"
    if datetime.time(9, 0) <= clock < datetime.time(15, 30):
        return "REGULAR"
    if datetime.time(15, 30) <= clock < datetime.time(20, 0):
        return "AFTER_MARKET"
    return None


def _prepare_toss_overlay(
    frame: pd.DataFrame,
    *,
    end_time_kst: datetime.datetime,
    nxt_eligible: bool,
) -> pd.DataFrame:
    if frame.empty:
        return _empty_intraday_frame()
    out = frame.copy()
    out["datetime"] = _to_kst_naive_series(out["datetime"])
    out["date"] = out["datetime"].dt.date
    out["time"] = out["datetime"].dt.time
    end_naive = _ensure_kst_aware(end_time_kst).replace(tzinfo=None)
    out = out.loc[out["datetime"] <= end_naive]
    if not nxt_eligible:
        clocks = out["datetime"].dt.time
        out = out.loc[
            (clocks >= datetime.time(9, 0)) & (clocks < datetime.time(15, 30))
        ]
    if out.empty:
        return _empty_intraday_frame()
    out["session"] = out["datetime"].map(_session_for_toss_minute)
    out = out.dropna(subset=["session"])
    out["venues"] = [[] for _ in range(len(out))]
    return out.loc[:, _INTRADAY_FRAME_COLUMNS].reset_index(drop=True)


def _resolve_intraday_end_bounds(
    *,
    resolved_now: datetime.datetime,
    end_date: datetime.datetime | None,
) -> tuple[datetime.date, datetime.datetime]:
    if end_date is None:
        return resolved_now.date(), resolved_now
    end_day = _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
    return end_day, datetime.datetime.combine(
        end_day,
        datetime.time(20, 0),
        tzinfo=_KST,
    )


async def read_kr_intraday_candles(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: datetime.datetime | None,
    now_kst: datetime.datetime | None = None,
) -> pd.DataFrame:
    """DB 과거 분봉을 읽고 현재 Toss 분봉을 overlay한다."""
    normalized_period = str(period or "1h").strip().lower()
    config = _INTRADAY_PERIOD_CONFIGS.get(normalized_period)
    if config is None:
        raise ValueError(f"Unsupported KR intraday period: {period}")

    capped_count = max(int(count), 1)
    resolved_now = _ensure_kst_aware(now_kst or datetime.datetime.now(_KST))
    universe = await _resolve_universe_row(symbol)
    if isinstance(universe, _UniverseError):
        return _empty_intraday_frame()

    end_day, end_time_kst = _resolve_intraday_end_bounds(
        resolved_now=resolved_now,
        end_date=end_date,
    )
    history_rows = await _fetch_intraday_history_rows(
        config=config,
        symbol=universe.symbol,
        end_time_kst=end_time_kst,
        limit=min(max(capped_count * 3, capped_count + 12), 1000),
    )
    out = _history_rows_to_frame(config=config, rows=history_rows)

    if end_day == resolved_now.date() and datetime.time(
        8, 0
    ) <= resolved_now.time() < datetime.time(20, 0):
        minute_count = min(
            max(capped_count * config.bucket_minutes, 60),
            720,
        )
        toss_frame = await fetch_kr_intraday_toss_frame(
            symbol=universe.symbol,
            period="1m",
            count=minute_count,
            end_date=end_time_kst,
        )
        overlay = _prepare_toss_overlay(
            toss_frame,
            end_time_kst=end_time_kst,
            nxt_eligible=universe.nxt_eligible,
        )
        out = _merge_overlay_into_intraday_frame(
            out=out,
            overlay_frame=overlay,
            bucket_minutes=config.bucket_minutes,
        )

    if out.empty:
        return _empty_intraday_frame()
    out = out.sort_values("datetime").reset_index(drop=True)
    out["datetime"] = _to_kst_naive_series(out["datetime"])
    return out.tail(capped_count).reset_index(drop=True)


async def read_kr_hourly_candles_1h(
    *,
    symbol: str,
    count: int,
    end_date: datetime.datetime | None,
    now_kst: datetime.datetime | None = None,
) -> pd.DataFrame:
    return await read_kr_intraday_candles(
        symbol=symbol,
        period="1h",
        count=count,
        end_date=end_date,
        now_kst=now_kst,
    )
