"""`/market/overview`의 `indicators[]` 조립 규칙.

지표는 공급자가 셋(US yfinance 배치 / Upbit / 토스 시장지표)으로 갈려 있고,
하나가 죽어도 나머지가 살아야 한다. 값을 만들어내지 않는 것(등락 위조 금지,
% 값의 가격 취급 금지)이 이 파일이 지키는 계약이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.extensions.kasset.api import market_overview as mod
from app.extensions.kasset.api.toss_market_data import (
    TossIndicatorPoint,
    TossSharedMarketData,
)
from app.services.brokers.toss.dto import TossMarketIndicatorPrice

_SESSIONS = {"KRX": "OPEN", "US": "OPEN"}


def _us_batch_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"indices": rows}


def _yf_row(symbol: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "current": "10.0",
        "previous_close": "9.0",
        "change": "1.0",
        "change_pct": "11.11",
        "source": "yfinance",
    }
    row.update(overrides)
    return row


def _btc_ticker(**overrides: Any) -> dict[str, Any]:
    ticker: dict[str, Any] = {
        "market": "KRW-BTC",
        "trade_price": 109807000,
        "prev_closing_price": 111005000,
        "signed_change_price": -1198000,
        "signed_change_rate": -0.0108,
        "trade_timestamp": int(
            datetime(2026, 8, 28, 6, 10, tzinfo=UTC).timestamp() * 1000
        ),
    }
    ticker.update(overrides)
    return ticker


def _toss_points(*, as_of: datetime | None) -> dict[str, TossIndicatorPoint]:
    return {
        "KR_BOND_10Y": TossIndicatorPoint(
            symbol="KR_BOND_10Y", last_price=Decimal("4.245"), as_of=as_of
        )
    }


def _by_key(items: list[Any]) -> dict[str, Any]:
    return {item.key: item for item in items}


def test_units_follow_the_catalogue_and_percent_values_are_not_priced() -> None:
    rows = [
        _yf_row("US10Y", current="4.68", previous_close="4.71"),
        _yf_row("VIX", current="14.48"),
        _yf_row("GOLD", current="4655.6"),
    ]
    items, _errors = mod._indicator_items(
        _us_batch_payload(rows),
        None,
        _toss_points(as_of=datetime(2026, 8, 28, 6, 30, tzinfo=UTC)),
        sessions=_SESSIONS,
    )
    by_key = _by_key(items)

    # 단위는 카탈로그 지식으로 붙는다. 공급자 응답에는 단위가 없다.
    assert by_key["VIX"].unit == "POINT"
    assert by_key["US10Y"].unit == "PERCENT"
    assert by_key["KR_BOND_10Y"].unit == "PERCENT"
    assert by_key["GOLD"].unit == "USD"
    assert by_key["BTC"].unit == "KRW"
    # % 지표는 통화 환산하지 않고 소수 2자리로 제한한다.
    assert by_key["US10Y"].value == "4.68"
    assert by_key["KR_BOND_10Y"].value == "4.25"
    assert {item.group for item in items if item.key.startswith("KR_BOND")} == {"RATE"}


def test_missing_previous_close_leaves_change_null_instead_of_zero() -> None:
    rows = [
        _yf_row(
            "WTI",
            current="82.69",
            previous_close=None,
            change=None,
            change_pct=None,
        )
    ]
    items, _errors = mod._indicator_items(
        _us_batch_payload(rows), None, None, sessions=_SESSIONS
    )
    wti = _by_key(items)["WTI"]

    assert wti.value == "82.69"
    assert wti.previous_close is None
    assert wti.change_amount is None
    assert wti.change_rate is None
    assert wti.status == "available"


def test_upbit_rate_becomes_percentage_points_and_needs_a_previous_close() -> None:
    items, _errors = mod._indicator_items(None, _btc_ticker(), None, sessions=_SESSIONS)
    btc = _by_key(items)["BTC"]

    assert btc.value == "109807000"
    assert btc.previous_close == "111005000"
    assert btc.change_amount == "-1198000"
    # 업비트는 비율(-0.0108)로 주므로 퍼센트포인트(-1.08)로 옮긴다.
    assert Decimal(btc.change_rate) == Decimal("-1.08")

    without_previous, _errors = mod._indicator_items(
        None,
        _btc_ticker(prev_closing_price=None),
        None,
        sessions=_SESSIONS,
    )
    degraded = _by_key(without_previous)["BTC"]
    assert degraded.value == "109807000"
    assert degraded.previous_close is None
    assert degraded.change_amount is None
    assert degraded.change_rate is None


def test_toss_indicator_without_timestamp_is_stale_not_available() -> None:
    fresh, _errors = mod._indicator_items(
        None,
        None,
        _toss_points(as_of=datetime(2026, 8, 28, 6, 30, tzinfo=UTC)),
        sessions=_SESSIONS,
    )
    bond = _by_key(fresh)["KR_BOND_10Y"]
    assert bond.status == "available"
    assert bond.as_of == "2026-08-28T06:30:00Z"

    undated, _errors = mod._indicator_items(
        None, None, _toss_points(as_of=None), sessions=_SESSIONS
    )
    bond = _by_key(undated)["KR_BOND_10Y"]
    # 기준 시각을 증명할 수 없으면 값은 싣되 available이라고 말하지 않는다.
    assert bond.value == "4.25"
    assert bond.as_of is None
    assert bond.status == "stale"


def test_one_provider_failure_only_downgrades_its_own_indicators() -> None:
    rows = [_yf_row(symbol) for symbol in ("VIX", "US10Y", "WTI", "BRENT", "GOLD")]

    # 토스만 죽은 경우: 국채 6종만 unavailable이고 나머지는 값을 유지한다.
    items, errors = mod._indicator_items(
        _us_batch_payload(rows),
        _btc_ticker(),
        None,
        sessions=_SESSIONS,
        toss_error_code="TIMEOUT",
    )
    by_key = _by_key(items)
    unavailable = {item.key for item in items if item.status == "unavailable"}
    assert unavailable == {
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_10Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
    }
    assert by_key["VIX"].value == "10"
    assert by_key["BTC"].value == "109807000"
    assert [error.scope for error in errors] == ["indicators"] * 6
    assert {error.code for error in errors} == {"TIMEOUT"}

    # Upbit만 죽은 경우: BTC만 떨어진다.
    items, errors = mod._indicator_items(
        _us_batch_payload(rows),
        None,
        _toss_points(as_of=datetime(2026, 8, 28, 6, 30, tzinfo=UTC)),
        sessions=_SESSIONS,
        upbit_error_code="UNAVAILABLE",
    )
    by_key = _by_key(items)
    assert by_key["BTC"].status == "unavailable"
    assert by_key["BTC"].value is None
    assert by_key["KR_BOND_10Y"].value == "4.25"
    assert by_key["GOLD"].value == "10"
    assert [error.symbol for error in errors] == [
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
        "BTC",
    ]


def test_one_us_batch_row_error_does_not_touch_the_other_indicators() -> None:
    rows = [
        {"symbol": "VIX", "error": "provider unavailable"},
        _yf_row("US10Y", current="4.68"),
        _yf_row("WTI"),
        _yf_row("BRENT"),
        _yf_row("GOLD"),
    ]
    items, errors = mod._indicator_items(
        _us_batch_payload(rows), _btc_ticker(), None, sessions=_SESSIONS
    )
    by_key = _by_key(items)

    assert by_key["VIX"].status == "unavailable"
    assert by_key["VIX"].value is None
    assert by_key["US10Y"].value == "4.68"
    assert "VIX" in {error.symbol for error in errors}
    assert by_key["BTC"].status == "available"


@pytest.mark.asyncio
async def test_toss_channel_batches_one_call_and_keeps_null_timestamp_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _Client:
        async def market_indicator_prices(
            self, symbols: list[str]
        ) -> list[TossMarketIndicatorPrice]:
            calls.append(list(symbols))
            return [
                TossMarketIndicatorPrice(
                    symbol="KR_BOND_10Y",
                    timestamp=None,
                    last_price=Decimal("4.245"),
                ),
                TossMarketIndicatorPrice(
                    symbol="KR_BOND_30Y",
                    timestamp="2026-08-28T15:30:00.000+09:00",
                    last_price=Decimal("4.514"),
                ),
            ]

    channel = TossSharedMarketData(client_factory=_Client)
    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)

    points = await channel.market_indicators(["KR_BOND_10Y", "KR_BOND_30Y"])

    assert calls == [["KR_BOND_10Y", "KR_BOND_30Y"]]
    assert points["KR_BOND_10Y"].last_price == Decimal("4.245")
    # timestamp가 null이어도 값은 살리고 as_of만 비운다.
    assert points["KR_BOND_10Y"].as_of is None
    assert points["KR_BOND_30Y"].as_of == datetime(2026, 8, 28, 6, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_toss_channel_is_silent_when_disabled_or_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Failing:
        async def market_indicator_prices(
            self, symbols: list[str]
        ) -> list[TossMarketIndicatorPrice]:
            raise RuntimeError("toss unavailable")

    monkeypatch.setattr(mod.settings, "toss_api_enabled", False)
    disabled = TossSharedMarketData(client_factory=_Failing)
    assert await disabled.market_indicators(["KR_BOND_10Y"]) == {}

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    failing = TossSharedMarketData(client_factory=_Failing)
    assert await failing.market_indicators(["KR_BOND_10Y"]) == {}
