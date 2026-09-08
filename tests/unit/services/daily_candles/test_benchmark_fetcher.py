from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.services.daily_candles.benchmark_fetcher import fetch_kr_benchmark_daily


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = iter(payloads)
        self.calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(next(self._payloads))


def _row(day: date, close: float) -> dict[str, object]:
    return {
        "localTradedAt": day.isoformat(),
        "openPrice": close - 1,
        "highPrice": close + 2,
        "lowPrice": close - 2,
        "closePrice": close,
        "accumulatedTradingVolume": "1,000",
        "accumulatedTradingValue": "2,000,000",
    }


@pytest.mark.asyncio
async def test_kosdaq_uses_its_naver_index_endpoint() -> None:
    client = _Client([[_row(date(2024, 5, 3), 870.0)]])

    frame = await fetch_kr_benchmark_daily(
        symbol="kosdaq",
        n=1,
        client=client,
    )

    assert frame["close"].tolist() == [870.0]
    assert client.calls[0]["url"].endswith("/KOSDAQ/price")


@pytest.mark.asyncio
async def test_naver_history_paginates_and_deduplicates_by_trading_date() -> None:
    first_day = date(2024, 1, 1)
    rows = [
        _row(first_day + timedelta(days=index), 2500.0 + index) for index in range(101)
    ]
    client = _Client([rows[:60], [rows[59], *rows[60:]]])

    frame = await fetch_kr_benchmark_daily(symbol="KOSPI", n=100, client=client)

    assert len(frame) == 101
    assert frame["date"].tolist() == [
        first_day + timedelta(days=index) for index in range(101)
    ]
    assert [call["params"]["page"] for call in client.calls] == [1, 2]
    assert all(call["params"]["timeframe"] == "day" for call in client.calls)
    assert all(call["params"]["pageSize"] == 60 for call in client.calls)


@pytest.mark.asyncio
async def test_naver_history_accepts_index_rows_without_volume() -> None:
    row = _row(date(2024, 1, 1), 2500.0)
    row.pop("accumulatedTradingVolume")
    row.pop("accumulatedTradingValue")
    client = _Client([[row]])

    frame = await fetch_kr_benchmark_daily(symbol="KOSPI", n=1, client=client)

    assert frame.iloc[0]["volume"] == 0.0
    assert frame.iloc[0]["value"] == 0.0


@pytest.mark.asyncio
async def test_naver_history_rejects_conflicting_duplicate_date() -> None:
    first_day = date(2024, 1, 1)
    rows = [
        _row(first_day + timedelta(days=index), 2500.0 + index) for index in range(100)
    ]
    conflicting = _row(first_day + timedelta(days=59), 9999.0)
    client = _Client(
        [
            rows[:60],
            [conflicting, *rows[60:], _row(first_day + timedelta(days=100), 2600.0)],
        ]
    )

    with pytest.raises(ValueError, match="중복 거래일 값이 충돌"):
        await fetch_kr_benchmark_daily(symbol="KOSPI", n=100, client=client)


@pytest.mark.asyncio
async def test_naver_history_rejects_malformed_successful_row() -> None:
    malformed = _row(date(2024, 1, 1), 2500.0)
    malformed["highPrice"] = 2400.0
    client = _Client([[malformed]])

    with pytest.raises(ValueError, match="OHLC 범위를 위반"):
        await fetch_kr_benchmark_daily(symbol="KOSPI", n=1, client=client)
