from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services.daily_candles.repository import DailyCandleRow, MarketKey


def _row(symbol: str, partition: str, moment: datetime, *, source: str = "toss"):
    return DailyCandleRow(
        time_utc=moment,
        symbol=symbol,
        partition=partition,
        open=99.0,
        high=102.0,
        low=98.0,
        close=101.0,
        adj_close=None,
        volume=10.0,
        value=1010.0,
        source=source,
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
            "open": [99.0, 100.0],
            "high": [102.0, 103.0],
            "low": [98.0, 99.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 11.0],
            "value": [1010.0, 1122.0],
        }
    )


class _SessionFactory:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_kr_warm_cache_skips_toss() -> None:
    from app.mcp_server.tooling.market_data_indicators import (
        _fetch_ohlcv_for_indicators,
    )

    session = MagicMock()
    rows = [
        _row("005930", "KRX", datetime.now(UTC) - timedelta(days=index))
        for index in range(2)
    ]
    toss = AsyncMock()
    with (
        patch("app.core.db.AsyncSessionLocal", return_value=_SessionFactory(session)),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=rows),
        ),
        patch(
            "app.mcp_server.tooling.market_data_indicators._cache_is_fresh_equity",
            return_value=True,
        ),
        patch(
            "app.mcp_server.tooling.market_data_indicators.fetch_daily_toss_frame",
            new=toss,
        ),
    ):
        result = await _fetch_ohlcv_for_indicators("005930", "equity_kr", count=2)

    assert len(result) == 2
    toss.assert_not_awaited()


@pytest.mark.asyncio
async def test_kr_cache_miss_fetches_toss_and_upserts_toss_source() -> None:
    from app.mcp_server.tooling.market_data_indicators import (
        _fetch_ohlcv_for_indicators,
    )

    session = MagicMock()
    session.commit = AsyncMock()
    toss = AsyncMock(return_value=_frame())
    upsert = AsyncMock(return_value=2)
    with (
        patch("app.core.db.AsyncSessionLocal", return_value=_SessionFactory(session)),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=upsert,
        ),
        patch(
            "app.mcp_server.tooling.market_data_indicators.fetch_daily_toss_frame",
            new=toss,
        ),
    ):
        result = await _fetch_ohlcv_for_indicators("005930", "equity_kr", count=2)

    toss.assert_awaited_once_with(symbol="005930", count=2)
    assert {row.source for row in upsert.await_args.kwargs["rows"]} == {"toss"}
    assert upsert.await_args.kwargs["market"] is MarketKey.KR
    assert len(result) == 2


@pytest.mark.asyncio
async def test_us_active_gate_precedes_toss_cache_miss() -> None:
    from app.mcp_server.tooling.market_data_indicators import (
        _fetch_ohlcv_for_indicators,
    )

    session = MagicMock()
    session.commit = AsyncMock()
    lookup = AsyncMock(return_value="NASD")
    toss = AsyncMock(return_value=_frame())
    with (
        patch("app.core.db.AsyncSessionLocal", return_value=_SessionFactory(session)),
        patch(
            "app.mcp_server.tooling.market_data_indicators.get_us_exchange_by_symbol",
            new=lookup,
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=AsyncMock(return_value=2),
        ),
        patch(
            "app.mcp_server.tooling.market_data_indicators.fetch_daily_toss_frame",
            new=toss,
        ),
    ):
        await _fetch_ohlcv_for_indicators("AAPL", "equity_us", count=2)

    lookup.assert_awaited_once_with("AAPL", db=session)
    toss.assert_awaited_once_with(symbol="AAPL", count=2)
