from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_uses_toss_provider_timestamp(monkeypatch) -> None:
    point = SimpleNamespace(
        price=1225000.0,
        as_of=dt.datetime(2026, 6, 1, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=9))),
    )
    prices = AsyncMock(return_value={"012450": point})
    monkeypatch.setattr(market_data_quotes.toss_market_data, "prices", prices)

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price"] == 1225000.0
    assert quote["source"] == "toss"
    assert quote["instrument_type"] == "equity_kr"
    assert quote["price_as_of"] == "2026-06-01T09:30:00+09:00"
    prices.assert_awaited_once_with(["012450"])


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_missing_or_failed_toss_returns_none(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        market_data_quotes.toss_market_data,
        "prices",
        AsyncMock(return_value={}),
    )
    assert await market_data_quotes._fetch_kr_live_quote("012450") is None

    monkeypatch.setattr(
        market_data_quotes.toss_market_data,
        "prices",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )
    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


@pytest.mark.asyncio
async def test_equity_kr_quote_is_toss_only(monkeypatch) -> None:
    point = SimpleNamespace(
        price=70000.0,
        as_of=dt.datetime(2026, 6, 1, 1, 0, tzinfo=dt.UTC),
    )
    monkeypatch.setattr(
        market_data_quotes.toss_market_data,
        "prices",
        AsyncMock(return_value={"005930": point}),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "fetch_daily_toss_frame",
        AsyncMock(
            return_value=pd.DataFrame(
                {
                    "datetime": [pd.Timestamp("2026-05-29T06:30:00Z")],
                    "open": [69000.0],
                    "high": [70500.0],
                    "low": [68500.0],
                    "close": [70000.0],
                    "volume": [1000.0],
                    "value": [70000000.0],
                }
            )
        ),
    )

    quote = await market_data_quotes._fetch_quote_equity_kr("005930")

    assert quote["source"] == "toss"
    assert quote["price_source"] == "toss_price"
    assert quote["price"] == 70000.0


def test_nxt_quote_overlay_surface_is_physically_removed() -> None:
    assert not hasattr(market_data_quotes, "_apply_nxt_quote_overlay")
    assert not hasattr(market_data_quotes, "_fetch_nxt_quote_overlay")
    assert not hasattr(market_data_quotes, "_nxt_quote_session")
