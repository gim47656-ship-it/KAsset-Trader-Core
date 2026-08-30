"""Yahoo Finance fallback fetcher for US daily candles.

Used by the daily candle sync when KIS overseas daily returns empty
for a specific symbol (illiquid names, ETF gaps), and optionally as an
adj_close enrichment source. This module knows about Yahoo; it does
NOT know about the database or about KIS.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

import app.services.brokers.yahoo.client as yahoo_service
from app.services.invest_screener_snapshots.freshness import (
    last_completed_us_session_close,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class YahooFallbackRow:
    time_utc: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float
    value: float


_US_EASTERN = ZoneInfo("America/New_York")


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metadata_utc(value: object) -> datetime | None:
    try:
        timestamp = pd.Timestamp(value, unit="s", tz="UTC")
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def _recover_latest_completed_row(
    frame: pd.DataFrame,
    metadata: object,
    *,
    symbol: str,
    now: datetime,
) -> pd.DataFrame:
    """Recover only the exact completed-session terminal row from Yahoo metadata."""

    if frame.empty or not isinstance(metadata, Mapping):
        return frame
    completed_close = last_completed_us_session_close(now)
    if completed_close is None:
        return frame
    completed_date = completed_close.astimezone(_US_EASTERN).date()
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    target_positions = [
        position
        for position, session_date in enumerate(dates)
        if session_date == completed_date
    ]
    if not target_positions:
        return frame
    target_position = target_positions[-1]
    prior_positions = [
        position
        for position, session_date in enumerate(dates)
        if session_date is not None
        and session_date < completed_date
        and _finite(frame.iloc[position].get("close")) is not None
    ]
    if not prior_positions:
        return frame

    target = frame.iloc[target_position]
    prior = frame.iloc[prior_positions[-1]]
    previous_close = _finite(prior.get("close"))
    previous_adjusted_close = _finite(prior.get("adj_close"))
    current = _finite(metadata.get("regularMarketPrice"))
    metadata_previous = _finite(metadata.get("previousClose"))
    metadata_low = _finite(metadata.get("regularMarketDayLow"))
    metadata_high = _finite(metadata.get("regularMarketDayHigh"))
    current_period = metadata.get("currentTradingPeriod")
    regular_period = (
        current_period.get("regular") if isinstance(current_period, Mapping) else None
    )
    regular_end = (
        _metadata_utc(regular_period.get("end"))
        if isinstance(regular_period, Mapping)
        else None
    )
    open_value = _finite(target.get("open"))
    low = _finite(target.get("low"))
    high = _finite(target.get("high"))
    raw_close = _finite(target.get("close"))
    previous_matches = (
        metadata_previous is not None
        and previous_close is not None
        and (
            math.isclose(metadata_previous, previous_close, abs_tol=0.01)
            or (
                previous_adjusted_close is not None
                and math.isclose(
                    metadata_previous,
                    previous_adjusted_close,
                    abs_tol=0.01,
                )
            )
        )
    )
    if (
        current is None
        or not previous_matches
        or regular_end != completed_close.astimezone(UTC)
        or open_value is None
        or low is None
        or high is None
        or metadata_low is None
        or metadata_high is None
        or not math.isclose(metadata_low, low, abs_tol=0.01)
        or not math.isclose(metadata_high, high, abs_tol=0.01)
        or not metadata_low <= current <= metadata_high
        or (
            raw_close is not None and not math.isclose(raw_close, current, abs_tol=0.01)
        )
    ):
        return frame

    normalized_high = max(open_value, high, current)
    normalized_low = min(open_value, low, current)
    if normalized_high != high or normalized_low != low:
        logger.info(
            "Yahoo completed candle OHLC bounds normalized symbol=%s session=%s "
            "open=%s original_high=%s original_low=%s close=%s",
            symbol,
            completed_date,
            open_value,
            high,
            low,
            current,
        )
    recovered = frame.copy()
    recovered.at[recovered.index[target_position], "high"] = normalized_high
    recovered.at[recovered.index[target_position], "low"] = normalized_low
    recovered.at[recovered.index[target_position], "close"] = current
    recovered.at[recovered.index[target_position], "adj_close"] = current
    return recovered


async def fetch_us_daily_yahoo_fallback(
    *, symbol: str, n: int, now: datetime | None = None
) -> list[YahooFallbackRow]:
    moment = now or datetime.now(UTC)
    frame = await yahoo_service.fetch_ohlcv(
        ticker=symbol,
        days=n + 1,
        period="day",
        use_cache=False,
    )
    if frame.empty or "close" not in frame.columns:
        return []
    frame = frame.tail(n)
    if "adj_close" not in frame.columns:
        frame = frame.assign(adj_close=float("nan"))
    completed_close = last_completed_us_session_close(moment)
    completed_date = (
        completed_close.astimezone(_US_EASTERN).date()
        if completed_close is not None
        else None
    )
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    terminal = frame.loc[dates == completed_date]
    if not terminal.empty and (
        _finite(terminal.iloc[-1].get("close")) is None
        or _finite(terminal.iloc[-1].get("adj_close")) is None
    ):
        metadata = await yahoo_service.fetch_history_metadata(symbol)
        frame = _recover_latest_completed_row(
            frame,
            metadata,
            symbol=symbol,
            now=moment,
        )

    out: list[YahooFallbackRow] = []
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        close = _finite(record.get("close"))
        if raw_date is None or close is None:
            continue
        ts = pd.Timestamp(raw_date)
        if ts.tzinfo is None:
            ts = ts.tz_localize(UTC)
        else:
            ts = ts.tz_convert(UTC)
        volume = _finite(record.get("volume")) or 0.0
        open_value = _finite(record.get("open"))
        high_value = _finite(record.get("high"))
        low_value = _finite(record.get("low"))
        open_value = close if open_value is None else open_value
        high_value = close if high_value is None else high_value
        low_value = close if low_value is None else low_value
        out.append(
            YahooFallbackRow(
                time_utc=ts.to_pydatetime(),
                symbol=symbol,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close,
                adj_close=_finite(record.get("adj_close")),
                volume=volume,
                value=close * volume,
            )
        )
    return out
