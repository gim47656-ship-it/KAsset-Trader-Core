from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import us_intraday_candles_read_service as service
from app.services.us_symbol_universe_service import USSymbolInactiveError

_ET = ZoneInfo("America/New_York")


def _toss_frame(timestamp: str = "2024-06-28T14:00:00Z") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": [pd.Timestamp(timestamp)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10.0],
            "value": [1005.0],
        }
    )


@pytest.mark.asyncio
async def test_toss_offset_timestamp_is_normalized_to_et_naive_boundary(
    monkeypatch,
) -> None:
    toss = AsyncMock(return_value=_toss_frame())
    monkeypatch.setattr(service, "fetch_us_intraday_toss_frame", toss)
    bucket = service._et_naive_to_utc(dt.datetime(2024, 6, 28, 10, 0))

    internal = await service._fetch_minutes_from_toss(
        symbol="AAPL",
        end_time_et=dt.datetime(2024, 6, 28, 10, 0),
        required_buckets={bucket},
        required_window_bucket_count=1,
        period="1m",
    )
    output = service._to_output_frame(internal)

    assert output["datetime"].tolist() == [pd.Timestamp("2024-06-28 10:00:00")]
    assert output["datetime"].dt.tz is None
    assert output["session"].tolist() == ["REGULAR"]
    assert internal["datetime"].iloc[0].tzinfo is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_timestamp", "et_naive", "expected_session"),
    [
        ("2024-06-28T09:00:00Z", dt.datetime(2024, 6, 28, 5, 0), "PRE_MARKET"),
        ("2024-06-28T22:00:00Z", dt.datetime(2024, 6, 28, 18, 0), "POST_MARKET"),
    ],
)
async def test_toss_extended_session_timestamp_classification(
    monkeypatch,
    provider_timestamp: str,
    et_naive: dt.datetime,
    expected_session: str,
) -> None:
    monkeypatch.setattr(
        service,
        "fetch_us_intraday_toss_frame",
        AsyncMock(return_value=_toss_frame(provider_timestamp)),
    )
    bucket = service._et_naive_to_utc(et_naive)

    internal = await service._fetch_minutes_from_toss(
        symbol="AAPL",
        end_time_et=et_naive,
        required_buckets={bucket},
        required_window_bucket_count=1,
        period="1m",
    )
    output = service._to_output_frame(internal)

    assert output["datetime"].tolist() == [pd.Timestamp(et_naive)]
    assert output["session"].tolist() == [expected_session]


@pytest.mark.asyncio
async def test_inactive_symbol_rejects_before_db_or_toss(monkeypatch) -> None:
    universe = AsyncMock(side_effect=USSymbolInactiveError("AAPL"))
    db_fetch = AsyncMock()
    toss = AsyncMock()
    monkeypatch.setattr(service, "get_us_exchange_by_symbol", universe)
    monkeypatch.setattr(service, "_fetch_candles_1m_from_db", db_fetch)
    monkeypatch.setattr(service, "fetch_us_intraday_toss_frame", toss)

    with pytest.raises(USSymbolInactiveError):
        await service.read_us_intraday_candles(
            symbol="AAPL",
            period="1m",
            count=1,
            end_date=dt.datetime(2024, 6, 28, 10, 0, tzinfo=_ET),
        )

    db_fetch.assert_not_awaited()
    toss.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_normal_toss_response_returns_empty_frame(monkeypatch) -> None:
    monkeypatch.setattr(
        service, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        service,
        "_fetch_candles_1m_from_db",
        AsyncMock(return_value=service._empty_internal_frame()),
    )
    monkeypatch.setattr(
        service, "fetch_us_intraday_toss_frame", AsyncMock(return_value=pd.DataFrame())
    )

    result = await service.read_us_intraday_candles(
        symbol="AAPL",
        period="1m",
        count=1,
        end_date=dt.datetime(2024, 6, 28, 10, 0, tzinfo=_ET),
    )

    assert result.empty
    assert list(result.columns) == service._OUTPUT_FRAME_COLUMNS


@pytest.mark.asyncio
async def test_toss_provider_failure_propagates(monkeypatch) -> None:
    monkeypatch.setattr(
        service, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        service,
        "_fetch_candles_1m_from_db",
        AsyncMock(return_value=service._empty_internal_frame()),
    )
    monkeypatch.setattr(
        service,
        "fetch_us_intraday_toss_frame",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )

    with pytest.raises(RuntimeError, match="toss unavailable"):
        await service.read_us_intraday_candles(
            symbol="AAPL",
            period="1m",
            count=1,
            end_date=dt.datetime(2024, 6, 28, 10, 0, tzinfo=_ET),
        )
