from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
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
    assert download_calls[0]["ignore_tz"] is True
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
    # 예비 티커를 선언하지 않은 심볼은 fallback_yf_ticker=None으로 내려간다.
    history.assert_awaited_once_with("^GSPC", 5, "day", fallback_yf_ticker=None)
    batch.assert_not_awaited()
    assert result["history"] == [{"date": "2026-08-28", "close": 6500.0}]


@pytest.mark.asyncio
async def test_single_index_completed_summary_keeps_existing_range_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_end = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    completed = AsyncMock(
        return_value=[
            {
                "symbol": "SPX",
                "current": 6500.0,
                "quote_asof": completed_end.isoformat(),
                "data_state": "market_closed",
            }
        ]
    )
    live = AsyncMock(side_effect=AssertionError("live current must not be used"))
    history_rows = [
        {"date": "2026-08-27", "close": 6480.0},
        {"date": "2026-08-28", "close": 6500.0},
    ]
    history = AsyncMock(return_value=history_rows)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", completed)
    monkeypatch.setattr(handler, "_fetch_index_us_current", live)
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)

    result = await handler.handle_get_market_index(
        symbol="SPX",
        period="day",
        count=5,
        completed_as_of_by_market={"US": completed_end},
    )

    completed.assert_awaited_once_with(
        ["SPX"],
        completed_as_of=completed_end,
        completed_symbols=("SPX",),
    )
    live.assert_not_awaited()
    history.assert_awaited_once_with(
        "^GSPC",
        5,
        "day",
        completed_as_of=completed_end,
        fallback_yf_ticker=None,
    )
    assert result["indices"][0]["current"] == 6500.0
    assert result["history"] == history_rows


@pytest.mark.asyncio
async def test_completed_us_history_recovers_friday_close_and_excludes_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_end = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "Open": [7670.0, 7700.0, 7735.17, 7800.0],
            "High": [7700.0, 7740.0, 7771.48, 7900.0],
            "Low": [7650.0, 7680.0, 7700.91, 7750.0],
            "Close": [7675.70, 7730.99, float("nan"), 7850.0],
            "Volume": [2500.0, 2600.0, 2589484000.0, 100.0],
        },
        index=pd.to_datetime(["2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"]),
    )

    class Ticker:
        def history(self, **_kwargs: Any) -> pd.DataFrame:
            return frame.iloc[:3]

        def get_history_metadata(self, *, repair: bool) -> dict[str, Any]:
            assert repair is False
            return {
                "regularMarketPrice": 7711.76,
                "previousClose": 7730.99,
                "currentTradingPeriod": {
                    "regular": {"end": completed_end},
                },
            }

    @contextmanager
    def traced_session() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(sources.yf, "download", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        sources.yf,
        "Ticker",
        lambda _ticker, session: Ticker(),
    )
    monkeypatch.setattr(sources, "yfinance_tracing_session", traced_session)

    history = await sources._fetch_index_us_history(
        "^GSPC",
        5,
        "day",
        completed_as_of=completed_end,
    )

    assert [row["date"] for row in history] == [
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]
    assert history[-1]["close"] == 7711.76
    assert history[-2]["close"] == 7730.99


@pytest.mark.asyncio
async def test_single_index_missing_completed_cutoff_degrades_summary_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = AsyncMock(side_effect=AssertionError("live current must not be used"))
    completed = AsyncMock(
        side_effect=AssertionError("no cutoff must not fetch current")
    )
    history_rows = [{"date": "2026-08-28", "close": 6500.0}]
    history = AsyncMock(return_value=history_rows)
    monkeypatch.setattr(handler, "_fetch_index_us_current", live)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", completed)
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)

    result = await handler.handle_get_market_index(
        symbol="SPX",
        period="day",
        count=5,
        completed_as_of_by_market={},
    )

    live.assert_not_awaited()
    completed.assert_not_awaited()
    assert result["indices"][0]["unavailable"] is True
    history.assert_not_awaited()
    assert result["history"] == []


@pytest.mark.asyncio
async def test_single_kr_index_uses_completed_close_not_live_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_end = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)
    completed_row = {
        "symbol": "KOSPI",
        "current": 6788.88,
        "quote_asof": completed_end.isoformat(),
        "data_state": "market_closed",
    }
    completed = AsyncMock(return_value=completed_row)
    live = AsyncMock(side_effect=AssertionError("live current must not be used"))
    history_rows = [{"date": "2026-08-28", "close": 6788.88}]
    history = AsyncMock(return_value=history_rows)
    monkeypatch.setattr(handler, "_fetch_index_kr_completed", completed)
    monkeypatch.setattr(handler, "_fetch_index_kr_current", live)
    monkeypatch.setattr(handler, "_fetch_index_kr_history", history)

    result = await handler.handle_get_market_index(
        symbol="KOSPI",
        period="day",
        count=5,
        completed_as_of_by_market={"KRX": completed_end},
    )

    completed.assert_awaited_once_with(
        "KOSPI",
        "코스피",
        completed_as_of=completed_end,
    )
    live.assert_not_awaited()
    history.assert_awaited_once_with(
        "KOSPI",
        5,
        "day",
        completed_as_of=completed_end,
    )
    assert result["indices"] == [completed_row]
    assert result["history"] == history_rows


@pytest.mark.asyncio
async def test_intraday_period_uses_live_current_and_ten_minute_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분봉은 진행 중 세션을 본다: 완료봉 cutoff를 넘겨도 실시간 현재가를 쓴다."""
    completed_end = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    live = AsyncMock(return_value={"symbol": "SPX", "current": 6500.0})
    completed = AsyncMock(side_effect=AssertionError("완료봉 배치를 타면 안 된다"))
    history_rows = [{"date": "2026-08-28T13:30:00Z", "close": 6500.0}]
    history = AsyncMock(return_value=history_rows)
    monkeypatch.setattr(handler, "_fetch_index_us_current", live)
    monkeypatch.setattr(handler, "_fetch_indices_us_current_batch", completed)
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)

    result = await handler.handle_get_market_index(
        symbol="SPX",
        period=sources.INDEX_INTRADAY_PERIOD,
        count=144,
        completed_as_of_by_market={"US": completed_end},
    )

    live.assert_awaited_once_with("^GSPC", "S&P 500", "SPX")
    completed.assert_not_awaited()
    history.assert_awaited_once_with(
        "^GSPC",
        144,
        "10m",
        fallback_yf_ticker=None,
    )
    assert result["history"] == history_rows


@pytest.mark.asyncio
async def test_naver_indices_reject_the_intraday_period() -> None:
    """네이버 지수 API에는 분봉이 없다. 일봉으로 조용히 대체하지 않는다."""
    for symbol in ("KOSPI", "KOSDAQ"):
        with pytest.raises(ValueError, match="intraday"):
            await handler.handle_get_market_index(
                symbol=symbol,
                period=sources.INDEX_INTRADAY_PERIOD,
                count=144,
            )


@pytest.mark.asyncio
async def test_dxy_history_declares_the_futures_fallback_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yahoo가 현물(DX-Y.NYB)을 비우면 선물(DX=F)로 한 번 더 시도해야 한다."""
    history = AsyncMock(return_value=[])
    monkeypatch.setattr(handler, "_fetch_index_us_current", AsyncMock(return_value={}))
    monkeypatch.setattr(handler, "_fetch_index_us_history", history)

    await handler.handle_get_market_index(symbol="DXY", period="day", count=5)

    history.assert_awaited_once_with(
        "DX-Y.NYB",
        5,
        "day",
        fallback_yf_ticker="DX=F",
    )


@pytest.mark.asyncio
async def test_us_history_falls_back_to_the_secondary_ticker_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def daily(
        yf_ticker: str,
        count: int,
        period: str,
        *,
        completed_as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        del count, period, completed_as_of
        requested.append(yf_ticker)
        if yf_ticker == "DX-Y.NYB":
            return []
        return [{"date": "2026-08-28", "close": 98.42}]

    monkeypatch.setattr(sources, "_fetch_index_us_daily_history", daily)

    history = await sources._fetch_index_us_history(
        "DX-Y.NYB",
        5,
        "day",
        fallback_yf_ticker="DX=F",
    )

    assert requested == ["DX-Y.NYB", "DX=F"]
    assert history == [{"date": "2026-08-28", "close": 98.42}]


@pytest.mark.asyncio
async def test_us_intraday_history_keeps_only_the_latest_session_in_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분봉은 UTC 절대시각을 보존하고, 서로 다른 날의 봉을 이어 붙이지 않는다."""
    index = pd.to_datetime(
        [
            "2026-08-27 15:50:00-04:00",
            "2026-08-28 09:30:00-04:00",
            "2026-08-28 09:40:00-04:00",
        ]
    )
    frame = pd.DataFrame(
        {
            "Open": [6470.0, 6480.0, 6490.0],
            "High": [6475.0, 6492.0, 6495.0],
            "Low": [6465.0, 6478.0, 6488.0],
            "Close": [6472.0, 6490.0, 6494.0],
            "Volume": [900.0, 100.0, 120.0],
        },
        index=index,
    )
    captured: dict[str, Any] = {}

    def download(*_args: Any, **kwargs: Any) -> pd.DataFrame:
        captured.update(kwargs)
        return frame

    @contextmanager
    def traced_session() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(sources.yf, "download", download)
    monkeypatch.setattr(sources, "yfinance_tracing_session", traced_session)

    history = await sources._fetch_index_us_history(
        "^GSPC",
        144,
        sources.INDEX_INTRADAY_PERIOD,
    )

    assert captured["interval"] == "10m"
    # 분봉은 timezone을 버릴 수 없다: naive 인덱스로는 UTC 변환이 불가능하다.
    assert captured["ignore_tz"] is False
    assert [row["date"] for row in history] == [
        "2026-08-28T13:30:00Z",
        "2026-08-28T13:40:00Z",
    ]
    assert [row["close"] for row in history] == [6490.0, 6494.0]


@pytest.mark.asyncio
async def test_upbit_symbols_route_to_the_upbit_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = AsyncMock(return_value={"symbol": "ETH", "current": 5842000})
    history_rows = [{"date": "2026-08-28T13:30:00Z", "close": 5842000.0}]
    history = AsyncMock(return_value=history_rows)
    monkeypatch.setattr(handler, "_fetch_index_upbit_current", current)
    monkeypatch.setattr(handler, "_fetch_index_upbit_history", history)

    result = await handler.handle_get_market_index(
        symbol="ETH",
        period=sources.INDEX_INTRADAY_PERIOD,
        count=144,
        # 암호화폐는 24시간 시장이라 KRX/US 완료봉 cutoff와 무관하다.
        completed_as_of_by_market={"US": datetime(2026, 8, 28, 20, 0, tzinfo=UTC)},
    )

    current.assert_awaited_once_with("KRW-ETH", "이더리움", "ETH")
    history.assert_awaited_once_with("KRW-ETH", 144, "10m")
    assert result["history"] == history_rows


@pytest.mark.asyncio
async def test_upbit_history_adapter_maps_minute_and_daily_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분봉은 KST naive를 UTC로 옮기고, 일봉은 다른 지수와 같은 거래일 라벨을 쓴다."""
    minute_frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-08-28 22:30:00", "2026-08-28 22:40:00"]),
            "open": [109_000_000.0, 109_500_000.0],
            "high": [109_600_000.0, 109_900_000.0],
            "low": [108_900_000.0, 109_400_000.0],
            "close": [109_500_000.0, 109_807_000.0],
            "volume": [1.25, 0.75],
        }
    )
    daily_frame = pd.DataFrame(
        {
            "date": [date(2026, 8, 27), date(2026, 8, 28)],
            "open": [110_000_000.0, 111_000_000.0],
            "high": [112_000_000.0, 112_500_000.0],
            "low": [109_000_000.0, 109_100_000.0],
            "close": [111_005_000.0, 109_807_000.0],
            "volume": [520.5, 480.25],
        }
    )
    minute_calls: dict[str, Any] = {}
    daily_calls: dict[str, Any] = {}

    async def minute_candles(market: str, *, unit: int, count: int) -> pd.DataFrame:
        minute_calls.update({"market": market, "unit": unit, "count": count})
        return minute_frame

    async def ohlcv(market: str, *, days: int, period: str) -> pd.DataFrame:
        daily_calls.update({"market": market, "days": days, "period": period})
        return daily_frame

    monkeypatch.setattr(sources, "upbit_fetch_minute_candles", minute_candles)
    monkeypatch.setattr(sources, "upbit_fetch_ohlcv", ohlcv)

    intraday = await sources._fetch_index_upbit_history(
        "KRW-BTC",
        144,
        sources.INDEX_INTRADAY_PERIOD,
    )
    daily = await sources._fetch_index_upbit_history("KRW-BTC", 20, "day")

    assert minute_calls == {"market": "KRW-BTC", "unit": 10, "count": 144}
    assert daily_calls == {"market": "KRW-BTC", "days": 20, "period": "day"}
    assert [row["date"] for row in intraday] == [
        "2026-08-28T13:30:00Z",
        "2026-08-28T13:40:00Z",
    ]
    # 코인 거래량은 소수점이 있으므로 정수로 자르지 않는다.
    assert [row["volume"] for row in intraday] == [1.25, 0.75]
    assert [row["date"] for row in daily] == ["2026-08-27", "2026-08-28"]


@pytest.mark.asyncio
async def test_upbit_current_adapter_converts_rate_to_percentage_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def tickers(market_codes: list[str]) -> list[dict[str, Any]]:
        assert market_codes == ["KRW-BTC"]
        return [
            {"market": "KRW-ETH", "trade_price": 1},
            {
                "market": "KRW-BTC",
                "trade_price": 109_807_000,
                "prev_closing_price": 111_005_000,
                "signed_change_price": -1_198_000,
                "signed_change_rate": -0.0108,
            },
        ]

    monkeypatch.setattr(sources, "upbit_fetch_multiple_tickers", tickers)

    row = await sources._fetch_index_upbit_current("KRW-BTC", "비트코인", "BTC")

    assert row["symbol"] == "BTC"
    assert row["name"] == "비트코인"
    assert row["current"] == 109_807_000
    # 업비트는 비율(0.01 = 1%)을 주므로 지수 행 관례인 퍼센트포인트로 옮긴다.
    assert float(row["change_pct"]) == pytest.approx(-1.08)
    assert row["source"] == "upbit"


@pytest.mark.asyncio
async def test_upbit_current_adapter_degrades_when_the_market_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def tickers(_market_codes: list[str]) -> list[dict[str, Any]]:
        return [{"market": "KRW-ETH", "trade_price": 1}]

    monkeypatch.setattr(sources, "upbit_fetch_multiple_tickers", tickers)

    row = await sources._fetch_index_upbit_current("KRW-BTC", "비트코인", "BTC")

    assert row == {
        "symbol": "BTC",
        "name": "비트코인",
        "source": "upbit",
        "unavailable": True,
    }


@pytest.mark.asyncio
async def test_upbit_symbols_are_rejected_by_the_naver_yfinance_batch() -> None:
    """배치는 naver/yfinance 전용이다. Upbit 심볼은 조용히 통과시키지 않는다."""
    with pytest.raises(ValueError, match="BTC"):
        await handler.handle_get_market_index_current_batch(["SPX", "BTC"])
