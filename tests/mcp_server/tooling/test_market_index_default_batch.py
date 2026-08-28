from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import fundamentals_sources_indices as sources
from app.mcp_server.tooling.fundamentals import _market_index as handler


@pytest.mark.asyncio
async def test_us_index_batch_download_computes_rows_and_isolates_missing_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    columns = pd.MultiIndex.from_tuples(
        [
            (field, ticker)
            for field in ("Open", "High", "Low", "Close", "Volume")
            for ticker in ("^GSPC", "^IXIC")
        ]
    )
    frame = pd.DataFrame(
        [
            [
                99.0,
                200.0,
                102.0,
                205.0,
                98.0,
                195.0,
                100.0,
                None,
                1000.0,
                None,
            ],
            [
                101.0,
                201.0,
                106.0,
                206.0,
                100.0,
                196.0,
                105.0,
                None,
                1200.0,
                None,
            ],
        ],
        index=pd.to_datetime(["2026-08-27", "2026-08-28"]),
        columns=columns,
    )
    download_calls: list[dict[str, Any]] = []

    def download(tickers: list[str], **kwargs: Any) -> pd.DataFrame:
        download_calls.append({"tickers": tickers, **kwargs})
        return frame

    @contextmanager
    def traced_session() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(sources.yf, "download", download)
    monkeypatch.setattr(sources, "yfinance_tracing_session", traced_session)

    rows = await sources._fetch_indices_us_current_batch(["SPX", "NASDAQ"])

    assert len(download_calls) == 1
    assert download_calls[0]["tickers"] == ["^GSPC", "^IXIC"]
    assert download_calls[0]["period"] == "5d"
    assert download_calls[0]["interval"] == "1d"
    spx, nasdaq = rows
    assert spx == {
        "symbol": "SPX",
        "name": "S&P 500",
        "current": 105.0,
        "previous_close": 100.0,
        "change": 5.0,
        "change_pct": 5.0,
        "open": 101.0,
        "high": 106.0,
        "low": 100.0,
        "volume": 1200,
        "source": "yfinance",
    }
    assert nasdaq["symbol"] == "NASDAQ"
    assert nasdaq["unavailable"] is True
    assert nasdaq["current"] is None
    assert nasdaq["previous_close"] is None


@pytest.mark.asyncio
async def test_default_indices_use_one_us_batch_and_never_call_us_fast_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def kr_current(code: str, name: str) -> dict[str, Any]:
        return {
            "symbol": code,
            "name": name,
            "current": 100.0,
            "change": 1.0,
            "change_pct": 1.0,
        }

    us_rows = [
        {
            "symbol": "SPX",
            "name": "S&P 500",
            "current": 6500.0,
            "change": 10.0,
            "change_pct": 0.15,
            "source": "yfinance",
        },
        {
            "symbol": "NASDAQ",
            "name": "NASDAQ Composite",
            "current": 21000.0,
            "change": 20.0,
            "change_pct": 0.1,
            "source": "yfinance",
        },
    ]
    batch = AsyncMock(return_value=us_rows)
    individual_us = AsyncMock(side_effect=AssertionError("fast_info must not be used"))
    monkeypatch.setattr(handler, "_fetch_index_kr_current", kr_current)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", batch)
    monkeypatch.setattr(handler, "_fetch_index_us_current", individual_us)
    monkeypatch.setattr(handler, "kr_market_data_state", lambda: "fresh")

    result = await handler.handle_get_market_index(symbol=None)

    batch.assert_awaited_once_with(["SPX", "NASDAQ", "DJI", "RUT", "SOX"])
    individual_us.assert_not_awaited()
    assert [row["symbol"] for row in result["indices"]] == [
        "KOSPI",
        "KOSDAQ",
        "SPX",
        "NASDAQ",
        "DJI",
        "RUT",
        "SOX",
    ]


@pytest.mark.asyncio
async def test_current_batch_shares_one_us_download_across_indices_and_indicators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """지수 + 지표 심볼을 한 번에 요청해도 US 다운로드는 1회다(왕복 증가 없음)."""

    async def kr_current(code: str, name: str) -> dict[str, Any]:
        return {"symbol": code, "name": name, "current": 100.0}

    requested: list[list[str]] = []

    async def batch(symbols: list[str]) -> list[dict[str, Any]]:
        requested.append(list(symbols))
        return [
            {"symbol": symbol, "current": 1.0, "source": "yfinance"}
            for symbol in symbols
        ]

    history = AsyncMock(side_effect=AssertionError("current batch fetches no history"))
    monkeypatch.setattr(handler, "_fetch_index_kr_current", kr_current)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", batch)
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)
    monkeypatch.setattr(handler, "kr_market_data_state", lambda: "fresh")

    result = await handler.handle_get_market_index_current_batch(
        ["KOSPI", "SPX", "vix", "US10Y", "GOLD"]
    )

    assert requested == [["SPX", "VIX", "US10Y", "GOLD"]]
    history.assert_not_awaited()
    assert "history" not in result
    # 요청 순서를 그대로 유지한다.
    assert [row["symbol"] for row in result["indices"]] == [
        "KOSPI",
        "SPX",
        "VIX",
        "US10Y",
        "GOLD",
    ]


@pytest.mark.asyncio
async def test_current_batch_isolates_a_failed_kr_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def kr_current(code: str, name: str) -> dict[str, Any]:
        raise RuntimeError(f"naver down: {code}")

    batch = AsyncMock(
        return_value=[{"symbol": "SPX", "current": 6500.0, "source": "yfinance"}]
    )
    monkeypatch.setattr(handler, "_fetch_index_kr_current", kr_current)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", batch)
    monkeypatch.setattr(handler, "kr_market_data_state", lambda: "fresh")

    result = await handler.handle_get_market_index_current_batch(["KOSPI", "SPX"])

    kospi, spx = result["indices"]
    assert "error" in kospi
    assert spx["current"] == 6500.0


@pytest.mark.asyncio
async def test_current_batch_rejects_symbols_it_cannot_batch() -> None:
    # coingecko 지표는 배치 대상이 아니다. 조용히 빠뜨리지 않고 거부한다.
    with pytest.raises(ValueError, match="CRYPTO"):
        await handler.handle_get_market_index_current_batch(["KOSPI", "CRYPTO"])
    with pytest.raises(ValueError, match="NOPE"):
        await handler.handle_get_market_index_current_batch(["NOPE"])


@pytest.mark.asyncio
async def test_single_us_index_keeps_individual_current_and_history_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = AsyncMock(
        return_value={
            "symbol": "SPX",
            "name": "S&P 500",
            "current": 6500.0,
            "source": "yfinance",
        }
    )
    history = AsyncMock(return_value=[{"date": "2026-08-28", "close": 6500.0}])
    batch = AsyncMock(side_effect=AssertionError("batch is default-only"))
    monkeypatch.setattr(handler, "_fetch_index_us_current", current)
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", batch)

    result = await handler.handle_get_market_index(symbol="SPX", period="day", count=5)

    current.assert_awaited_once_with("^GSPC", "S&P 500", "SPX")
    history.assert_awaited_once_with("^GSPC", 5, "day")
    batch.assert_not_awaited()
    assert result["history"] == [{"date": "2026-08-28", "close": 6500.0}]
