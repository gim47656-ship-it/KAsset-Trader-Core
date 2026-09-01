from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import kr_intraday as module
from app.services.kr_intraday import (
    _aggregate_minutes_to_hourly,
    read_kr_intraday_candles,
)

_KST = dt.timezone(dt.timedelta(hours=9))


def _minute_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2025-01-02T09:00:00+09:00", "2025-01-02T09:01:00+09:00"]
            ),
            "date": [dt.date(2025, 1, 2), dt.date(2025, 1, 2)],
            "time": [dt.time(9, 0), dt.time(9, 1)],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 20.0],
            "value": [1_000.0, 2_000.0],
        }
    )


def test_minute_aggregation_uses_completed_ohlcv_values() -> None:
    frame = _minute_frame()

    result = _aggregate_minutes_to_hourly(frame)

    assert result.to_dict("records") == [
        {
            "datetime": pd.Timestamp("2025-01-02 09:00:00"),
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 30.0,
        }
    ]


@pytest.mark.asyncio
async def test_current_session_overlays_toss_and_returns_kst_naive(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_resolve_universe_row",
        AsyncMock(return_value=SimpleNamespace(symbol="005930", nxt_eligible=False)),
    )
    monkeypatch.setattr(
        module, "_fetch_intraday_history_rows", AsyncMock(return_value=[])
    )
    toss = AsyncMock(return_value=_minute_frame())
    monkeypatch.setattr(module, "fetch_kr_intraday_toss_frame", toss)

    result = await read_kr_intraday_candles(
        symbol="005930",
        period="1m",
        count=10,
        end_date=None,
        now_kst=dt.datetime(2025, 1, 2, 9, 2, tzinfo=_KST),
    )

    assert result["datetime"].dt.tz is None
    assert result.columns.tolist() == [
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
    assert result["datetime"].tolist() == [
        pd.Timestamp("2025-01-02 09:00:00"),
        pd.Timestamp("2025-01-02 09:01:00"),
    ]
    assert result["session"].tolist() == ["REGULAR", "REGULAR"]
    assert result["venues"].tolist() == [[], []]
    toss.assert_awaited_once()


@pytest.mark.asyncio
async def test_historical_request_does_not_call_toss(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_resolve_universe_row",
        AsyncMock(return_value=SimpleNamespace(symbol="005930", nxt_eligible=True)),
    )
    monkeypatch.setattr(
        module, "_fetch_intraday_history_rows", AsyncMock(return_value=[])
    )
    toss = AsyncMock()
    monkeypatch.setattr(module, "fetch_kr_intraday_toss_frame", toss)

    result = await read_kr_intraday_candles(
        symbol="005930",
        period="1h",
        count=3,
        end_date=dt.datetime(2025, 1, 1),
        now_kst=dt.datetime(2025, 1, 2, 10, 0, tzinfo=_KST),
    )

    assert result.empty
    toss.assert_not_awaited()
