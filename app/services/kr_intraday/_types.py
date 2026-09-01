from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


_KR_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_kr_symbol_universe.py"

logger = logging.getLogger(__name__)


def _kr_universe_sync_hint() -> str:
    return f"Sync required: {_KR_UNIVERSE_SYNC_COMMAND}"


SessionType = Literal["PRE_MARKET", "REGULAR", "AFTER_MARKET"]
VenueType = Literal["KRX", "NTX"]


@dataclass(frozen=True, slots=True)
class _UniverseRow:
    symbol: str
    nxt_eligible: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class _UniverseError:
    """Represents an error during universe lookup without raising an exception."""

    reason: str


@dataclass(frozen=True, slots=True)
class _MinuteRow:
    minute_time: datetime.datetime
    venue: VenueType
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass(frozen=True, slots=True)
class _IntradayPeriodConfig:
    period: str
    bucket_minutes: int
    history_table: str


_INTRADAY_PERIOD_CONFIGS: dict[str, _IntradayPeriodConfig] = {
    "1m": _IntradayPeriodConfig(
        period="1m",
        bucket_minutes=1,
        history_table="public.kr_candles_1m",
    ),
    "5m": _IntradayPeriodConfig(
        period="5m",
        bucket_minutes=5,
        history_table="public.kr_candles_5m",
    ),
    "15m": _IntradayPeriodConfig(
        period="15m",
        bucket_minutes=15,
        history_table="public.kr_candles_15m",
    ),
    "30m": _IntradayPeriodConfig(
        period="30m",
        bucket_minutes=30,
        history_table="public.kr_candles_30m",
    ),
    "1h": _IntradayPeriodConfig(
        period="1h",
        bucket_minutes=60,
        history_table="public.kr_candles_1h",
    ),
}

_INTRADAY_FRAME_COLUMNS = [
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "session",
    "venues",
]
