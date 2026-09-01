from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze, market_data_quotes

KST = ZoneInfo("Asia/Seoul")


def _ohlcv(*, candle_date=None) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": candle_date or datetime.now(KST).date(),
                "open": 100.0,
                "high": 110.0,
                "low": 95.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )


def _without_tradability(monkeypatch) -> None:
    monkeypatch.setattr(
        analysis_analyze,
        "get_kr_nxt_tradability",
        AsyncMock(return_value={}),
    )


@pytest.mark.asyncio
async def test_toss_live_quote_is_preferred_to_daily_close(monkeypatch) -> None:
    now = datetime.now(KST)
    _without_tradability(monkeypatch)
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_kr_live_quote",
        AsyncMock(
            return_value={
                "symbol": "012450",
                "instrument_type": "equity_kr",
                "price": 1_225_000.0,
                "source": "toss",
                "price_source": "toss_price",
                "price_as_of": now.isoformat(),
            }
        ),
    )

    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())

    assert quote is not None
    assert quote["price"] == 1_225_000.0
    assert quote["source"] == "toss"
    assert quote["price_source"] == "toss_price"
    assert quote["is_stale_price"] is False


@pytest.mark.asyncio
async def test_stale_toss_timestamp_remains_stale(monkeypatch) -> None:
    _without_tradability(monkeypatch)
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_kr_live_quote",
        AsyncMock(
            return_value={
                "symbol": "012450",
                "instrument_type": "equity_kr",
                "price": 1_200_000.0,
                "source": "toss",
                "price_as_of": (datetime.now(KST) - timedelta(days=10)).isoformat(),
            }
        ),
    )

    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())

    assert quote is not None
    assert quote["is_stale_price"] is True
    assert quote["price_usable"] is False


@pytest.mark.asyncio
async def test_daily_toss_close_is_used_when_live_quote_is_unavailable(
    monkeypatch,
) -> None:
    _without_tradability(monkeypatch)
    monkeypatch.setattr(
        analysis_analyze, "_fetch_kr_live_quote", AsyncMock(return_value=None)
    )

    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())

    assert quote is not None
    assert quote["price"] == 105.0
    assert quote["source"] == "toss"
    assert quote["price_as_of"] is not None


@pytest.mark.asyncio
async def test_missing_provider_timestamp_is_not_synthesized(monkeypatch) -> None:
    _without_tradability(monkeypatch)
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_kr_live_quote",
        AsyncMock(
            return_value={
                "symbol": "012450",
                "instrument_type": "equity_kr",
                "price": 123.0,
                "source": "toss",
                "price_as_of": None,
            }
        ),
    )

    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.asyncio
async def test_live_toss_helper_uses_provider_timestamp(monkeypatch) -> None:
    as_of = datetime.now(KST)
    price = SimpleNamespace(price=191300.0, as_of=as_of)
    prices = AsyncMock(return_value={"005930": price})
    monkeypatch.setattr(market_data_quotes.toss_market_data, "prices", prices)

    quote = await market_data_quotes._fetch_kr_live_quote("005930")

    prices.assert_awaited_once_with(["005930"])
    assert quote is not None
    assert quote["price"] == 191300.0
    assert quote["source"] == "toss"
    assert quote["price_as_of"] == as_of.isoformat()


@pytest.mark.asyncio
async def test_live_toss_helper_returns_none_on_provider_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data_quotes.toss_market_data,
        "prices",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )

    assert await market_data_quotes._fetch_kr_live_quote("005930") is None
