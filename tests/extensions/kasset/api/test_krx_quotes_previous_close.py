"""KR 시세의 직전 KRX 정규장 종가 우선순위 회귀 테스트."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.toss_market_data import TossQuotePoint
from app.services.brokers.toss.market_calendar import TossSessionWindow
from app.services.daily_candles.repository import DailyCandleRow


def _point(symbol: str, *, price: str = "1612000") -> TossQuotePoint:
    return TossQuotePoint(
        symbol=symbol,
        price=Decimal(price),
        currency="KRW",
        as_of=datetime(2026, 9, 2, 6, tzinfo=UTC),
    )


def _stored_row(symbol: str, *, close: float = 1652000.0) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=datetime(2026, 9, 1, tzinfo=UTC),
        symbol=symbol,
        partition="KRX",
        open=close,
        high=close,
        low=close,
        close=close,
        adj_close=None,
        volume=1.0,
        value=0.0,
        source="test",
    )


def _previous_regular_window() -> TossSessionWindow:
    return TossSessionWindow(
        start=datetime(2026, 9, 1, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 6, 30, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_kr_regular_session_resolves_latest_completed_regular_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _previous_regular_window()
    resolve_state = AsyncMock(return_value="REGULAR")
    latest_window = AsyncMock(return_value=window)
    monkeypatch.setattr(krx_quotes, "resolve_market_session_state", resolve_state)
    monkeypatch.setattr(
        krx_quotes,
        "get_latest_completed_regular_window_from_toss",
        latest_window,
    )

    states, resolved_window = await krx_quotes._quote_session_context(
        object(),  # type: ignore[arg-type]
        market="KRX",
        symbols=["000660"],
    )

    assert states == {"000660": "REGULAR"}
    assert resolved_window == window
    latest_window.assert_awaited_once()
    market, moment = latest_window.await_args.args
    assert market == "kr"
    assert moment.tzinfo is not None


@pytest.mark.asyncio
async def test_resolve_quotes_kr_prefers_regular_close_over_stored_nxt_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ["000660", "005930"]
    points = {
        "000660": _point("000660"),
        "005930": _point("005930", price="72000"),
    }
    candles = {
        "000660": [_stored_row("000660")],
        "005930": [_stored_row("005930", close=71000.0)],
    }
    window = _previous_regular_window()
    regular_closes = {
        "000660": Decimal("1692000"),
        "005930": Decimal("70000"),
    }
    regular_lookup = AsyncMock(return_value=regular_closes)
    daily_lookup = AsyncMock(
        side_effect=AssertionError("토스 1d 폴백을 호출하면 안 됩니다.")
    )

    monkeypatch.setattr(
        krx_quotes,
        "_quote_session_context",
        AsyncMock(return_value=(dict.fromkeys(symbols, "REGULAR"), window)),
    )
    monkeypatch.setattr(krx_quotes, "_toss_points", AsyncMock(return_value=points))
    monkeypatch.setattr(krx_quotes, "_candle_rows", AsyncMock(return_value=candles))
    monkeypatch.setattr(krx_quotes, "_instrument_names", AsyncMock(return_value={}))
    monkeypatch.setattr(krx_quotes, "_regular_closes", regular_lookup)
    monkeypatch.setattr(krx_quotes.toss_market_data, "previous_closes", daily_lookup)

    quotes = await krx_quotes.resolve_quotes(
        object(),  # type: ignore[arg-type]
        market="KRX",
        symbols=symbols,
    )

    assert [
        (quote.symbol, quote.previous_close, quote.change_rate) for quote in quotes
    ] == [
        ("000660", "1692000", "-4.73"),
        ("005930", "70000", "2.86"),
    ]
    regular_lookup.assert_awaited_once_with(points, window=window)
    daily_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_quote_kr_prefers_regular_close_over_stored_nxt_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = "000660"
    point = _point(symbol)
    window = _previous_regular_window()

    monkeypatch.setattr(
        krx_quotes,
        "_quote_session_context",
        AsyncMock(return_value=({symbol: "REGULAR"}, window)),
    )
    monkeypatch.setattr(
        krx_quotes, "_toss_points", AsyncMock(return_value={symbol: point})
    )
    monkeypatch.setattr(
        krx_quotes,
        "_candle_rows",
        AsyncMock(return_value={symbol: [_stored_row(symbol)]}),
    )
    monkeypatch.setattr(krx_quotes, "_instrument_names", AsyncMock(return_value={}))
    monkeypatch.setattr(
        krx_quotes,
        "_regular_closes",
        AsyncMock(return_value={symbol: Decimal("1692000")}),
    )
    monkeypatch.setattr(
        krx_quotes.toss_market_data,
        "previous_closes",
        AsyncMock(side_effect=AssertionError("토스 1d 폴백을 호출하면 안 됩니다.")),
    )

    quote = await krx_quotes.resolve_quote(
        object(),  # type: ignore[arg-type]
        market="KR",
        symbol=symbol,
    )

    assert quote.previous_close == "1692000"
    assert quote.change_amount == "-80000"
    assert quote.change_rate == "-4.73"


@pytest.mark.asyncio
async def test_resolve_quote_kr_falls_back_to_stored_close_when_regular_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = "000660"
    point = _point(symbol)
    window = _previous_regular_window()
    daily_lookup = AsyncMock(
        side_effect=AssertionError("저장 일봉이 있으면 토스 1d를 호출하면 안 됩니다.")
    )

    monkeypatch.setattr(
        krx_quotes,
        "_quote_session_context",
        AsyncMock(return_value=({symbol: "REGULAR"}, window)),
    )
    monkeypatch.setattr(
        krx_quotes, "_toss_points", AsyncMock(return_value={symbol: point})
    )
    monkeypatch.setattr(
        krx_quotes,
        "_candle_rows",
        AsyncMock(return_value={symbol: [_stored_row(symbol)]}),
    )
    monkeypatch.setattr(krx_quotes, "_instrument_names", AsyncMock(return_value={}))
    monkeypatch.setattr(krx_quotes, "_regular_closes", AsyncMock(return_value={}))
    monkeypatch.setattr(krx_quotes.toss_market_data, "previous_closes", daily_lookup)

    quote = await krx_quotes.resolve_quote(
        object(),  # type: ignore[arg-type]
        market="KRX",
        symbol=symbol,
    )

    assert quote.previous_close == "1652000"
    assert quote.change_rate == "-2.42"
    daily_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_quote_us_keeps_stored_previous_close_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = "TQQQ"
    point = TossQuotePoint(
        symbol=symbol,
        price=Decimal("73.50"),
        currency="USD",
        as_of=datetime(2026, 9, 2, 15, tzinfo=UTC),
    )
    stored = _stored_row(symbol, close=70.0)
    regular_lookup = AsyncMock(return_value={symbol: Decimal("72")})

    monkeypatch.setattr(
        krx_quotes,
        "_quote_session_context",
        AsyncMock(return_value=({symbol: "REGULAR"}, None)),
    )
    monkeypatch.setattr(
        krx_quotes, "_toss_points", AsyncMock(return_value={symbol: point})
    )
    monkeypatch.setattr(
        krx_quotes, "_candle_rows", AsyncMock(return_value={symbol: [stored]})
    )
    monkeypatch.setattr(krx_quotes, "_instrument_names", AsyncMock(return_value={}))
    monkeypatch.setattr(krx_quotes, "_regular_closes", regular_lookup)

    quote = await krx_quotes.resolve_quote(
        object(),  # type: ignore[arg-type]
        market="NASDAQ",
        symbol=symbol,
    )

    assert quote.market == "US"
    assert quote.previous_close == "70"
    assert quote.change_rate == "5.00"
    regular_lookup.assert_awaited_once_with({symbol: point}, window=None)
