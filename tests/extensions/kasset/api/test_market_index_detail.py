from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.extensions.kasset.api import market_overview as mod
from app.extensions.kasset.api.auth import get_mobile_session, mobile_auth
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.schemas import MarketIndexRange
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_MARKET_CLOSED,
)
from app.middleware.auth import AuthMiddleware

_KR_COMPLETED_END = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)
_US_COMPLETED_END = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clear_index_detail_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    mod._index_detail_cache.clear()

    async def session_state(market: str, *, moment=None) -> str:
        del market, moment
        return "REGULAR"

    monkeypatch.setattr(mod.krx_quotes, "resolve_market_session_state", session_state)

    async def latest_completed(market: str, moment: datetime) -> object:
        del moment
        end = _US_COMPLETED_END if market == "us" else _KR_COMPLETED_END
        return SimpleNamespace(end=end)

    monkeypatch.setattr(
        mod,
        "get_latest_completed_regular_window_from_toss",
        latest_completed,
    )


async def _session_override() -> object:
    return SimpleNamespace(user=SimpleNamespace(id=101, role="trader", is_active=True))


async def _db_override() -> AsyncIterator[object]:
    yield object()


def _client(*, middleware: bool = False) -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)
    if middleware:
        app.add_middleware(AuthMiddleware)
    else:
        app.dependency_overrides[get_mobile_session] = _session_override
    app.dependency_overrides[get_db] = _db_override
    return TestClient(app)


def _index_result(symbol: str = "SPX") -> dict[str, Any]:
    return {
        "indices": [
            {
                "symbol": symbol,
                "current": "6500.50",
                "change": "20.15",
                "change_pct": "0.31",
                "source": "yfinance",
            }
        ],
        "history": [
            {
                "date": "2026-08-28",
                "open": 6480.0,
                "high": 6510.0,
                "low": 6475.0,
                "close": 6500.5,
                "volume": 1000,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("range_", "period", "count"),
    [
        ("1W", "day", 5),
        ("1M", "day", 20),
        ("3M", "day", 60),
        ("6M", "day", 126),
    ],
)
async def test_index_detail_maps_each_public_range(
    monkeypatch: pytest.MonkeyPatch,
    range_: MarketIndexRange,
    period: str,
    count: int,
) -> None:
    source = AsyncMock(return_value=_index_result())
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    response = await mod.get_market_index_detail("spx", range_)

    assert response.summary.symbol == "SPX"
    assert response.summary.range == range_
    source.assert_awaited_once_with(
        symbol="SPX",
        period=period,
        count=count,
        completed_as_of_by_market={"US": _US_COMPLETED_END},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["DJI", "RUT", "SOX"])
@pytest.mark.parametrize(
    ("range_", "period", "count"),
    [
        ("1W", "day", 5),
        ("1M", "day", 20),
        ("3M", "day", 60),
        ("6M", "day", 126),
    ],
)
async def test_new_us_indices_are_whitelisted_for_every_public_range(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    range_: MarketIndexRange,
    period: str,
    count: int,
) -> None:
    """신설 지수도 상세 화이트리스트와 range 4종을 그대로 탄다."""
    source = AsyncMock(return_value=_index_result(symbol))
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    response = await mod.get_market_index_detail(symbol.lower(), range_)

    assert response.summary.symbol == symbol
    assert response.summary.market == "US"
    assert response.summary.currency == "USD"
    assert response.summary.range == range_
    assert response.summary.price == "6500.5"
    assert response.summary.status == "available"
    assert len(response.candles) == 1
    source.assert_awaited_once_with(
        symbol=symbol,
        period=period,
        count=count,
        completed_as_of_by_market={"US": _US_COMPLETED_END},
    )


@pytest.mark.asyncio
async def test_kr_bond_keys_are_not_reachable_as_index_details() -> None:
    """토스 국채 지표는 차트 소스가 없으므로 상세 화이트리스트에 없다."""
    for key in (
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_10Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
    ):
        with pytest.raises(mod.MobileApiError) as excinfo:
            await mod.get_market_index_detail(key, "1W")
        assert excinfo.value.status_code == 404
        assert excinfo.value.code == "UNKNOWN_INDEX"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "kind", "unit", "group"),
    [
        ("VIX", "INDICATOR", "POINT", "VOLATILITY"),
        ("US10Y", "INDICATOR", "PERCENT", "RATE"),
        ("WTI", "INDICATOR", "USD", "COMMODITY"),
        ("BRENT", "INDICATOR", "USD", "COMMODITY"),
        ("GOLD", "INDICATOR", "USD", "COMMODITY"),
        ("DXY", "INDICATOR", "POINT", "FX"),
        ("BTC", "INDICATOR", "KRW", "CRYPTO"),
        ("ETH", "INDICATOR", "KRW", "CRYPTO"),
    ],
)
async def test_indicator_detail_carries_kind_unit_group_and_ranges(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    kind: str,
    unit: str,
    group: str,
) -> None:
    """지표 상세는 통화 대신 unit/group으로 값의 의미를 전달한다."""
    source = AsyncMock(return_value=_index_result(symbol))
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    response = await mod.get_market_index_detail(symbol.lower(), "1M")

    assert response.summary.symbol == symbol
    assert response.summary.kind == kind
    assert response.summary.unit == unit
    assert response.summary.group == group
    # 지표는 한 거래소 세션에 속하지 않는다: market="GLOBAL", currency/session 없음.
    assert response.summary.market == "GLOBAL"
    assert response.summary.currency is None
    assert response.summary.session_state is None
    assert response.summary.supported_ranges == ["1D", "1W", "1M", "3M", "6M"]
    # 지표는 KRX/US 완료 정규장 cutoff를 강제하지 않는다(정규장 밖에도 거래된다).
    source.assert_awaited_once_with(
        symbol=symbol,
        period="day",
        count=20,
        completed_as_of_by_market=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["KOSPI", "KOSDAQ"])
async def test_kr_indices_reject_intraday_and_omit_it_from_supported_ranges(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
) -> None:
    """네이버 지수 API에는 분봉이 없으므로 1D를 노출하지도, 대체하지도 않는다."""
    source = AsyncMock(return_value=_index_result(symbol))
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    with pytest.raises(mod.MobileApiError) as excinfo:
        await mod.get_market_index_detail(symbol, "1D")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "UNSUPPORTED_RANGE"
    source.assert_not_awaited()

    monthly = await mod.get_market_index_detail(symbol, "1M")
    assert monthly.summary.supported_ranges == ["1W", "1M", "3M", "6M"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SPX", "WTI", "BTC"])
async def test_intraday_detail_keeps_every_bar_of_the_same_day(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
) -> None:
    """분봉은 날짜로 뭉개지 않는다. 같은 날 여러 봉이 시각별로 남아야 한다."""
    result = {
        "indices": [
            {
                "symbol": symbol,
                "current": 6500.5,
                "change": 20.15,
                "change_pct": 0.31,
            }
        ],
        "history": [
            {
                "date": "2026-08-28T13:40:00Z",
                "open": 6490.0,
                "high": 6495.0,
                "low": 6488.0,
                "close": 6494.0,
                "volume": 120,
            },
            {
                "date": "2026-08-28T13:30:00+00:00",
                "open": 6480.0,
                "high": 6492.0,
                "low": 6478.0,
                "close": 6490.0,
                "volume": 100,
            },
            # timezone을 증명할 수 없는 행은 자정으로 뭉개지 않고 버린다.
            {
                "date": "2026-08-28T13:50:00",
                "open": 6494.0,
                "high": 6499.0,
                "low": 6493.0,
                "close": 6498.0,
                "volume": 130,
            },
        ],
    }
    source = AsyncMock(return_value=result)
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    response = await mod.get_market_index_detail(symbol, "1D")

    assert response.summary.range == "1D"
    assert "1D" in response.summary.supported_ranges
    assert [candle.time for candle in response.candles] == [
        "2026-08-28T13:30:00Z",
        "2026-08-28T13:40:00Z",
    ]
    # 캔들 값은 공급자가 준 정밀도를 그대로 보존한다(반올림·자릿수 축소 없음).
    assert [candle.close for candle in response.candles] == ["6490.0", "6494.0"]
    # 분봉은 완료 정규장 cutoff와 결합하지 않는다(진행 중 세션을 본다).
    source.assert_awaited_once_with(
        symbol=symbol,
        period="10m",
        count=144,
        completed_as_of_by_market=None,
    )


def test_indicator_detail_http_contract_carries_unit_group_and_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "indices": [
            {
                "symbol": "WTI",
                "current": 82.6900024,
                "previous_close": 81.9,
                "change": 0.79,
                "change_pct": 0.96,
                "quote_asof": "2026-08-28T17:40:00+00:00",
                "source": "provider-must-not-leak",
            }
        ],
        "history": [
            {
                "date": "2026-08-28T17:30:00Z",
                "open": 82.5,
                "high": 82.72,
                "low": 82.48,
                "close": 82.6,
                "volume": 1200,
            },
            {
                "date": "2026-08-28T17:40:00Z",
                "open": 82.6,
                "high": 82.75,
                "low": 82.55,
                "close": 82.69,
                "volume": 900,
            },
        ],
    }
    monkeypatch.setattr(mod, "handle_get_market_index", AsyncMock(return_value=result))

    with _client() as client:
        response = client.get("/api/v1/market/indices/WTI?range=1D")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "summary": {
            "symbol": "WTI",
            "name": "WTI",
            "market": "GLOBAL",
            "currency": None,
            "price": "82.69",
            "changeAmount": "0.79",
            "changeRate": "0.96",
            "asOf": "2026-08-28T17:40:00Z",
            "status": "available",
            "sessionState": None,
            "range": "1D",
            "kind": "INDICATOR",
            "unit": "USD",
            "group": "COMMODITY",
            "supportedRanges": ["1D", "1W", "1M", "3M", "6M"],
        },
        "candles": [
            {
                "time": "2026-08-28T17:30:00Z",
                "open": "82.5",
                "high": "82.72",
                "low": "82.48",
                "close": "82.6",
                "volume": "1200",
            },
            {
                "time": "2026-08-28T17:40:00Z",
                "open": "82.6",
                "high": "82.75",
                "low": "82.55",
                "close": "82.69",
                "volume": "900",
            },
        ],
    }
    assert "provider-must-not-leak" not in response.text
    assert "supported_ranges" not in response.text


def test_index_detail_http_contract_is_camel_case_decimal_and_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "indices": [
            {
                "symbol": "SPX",
                "current": 6500.5,
                "change": 20.15,
                "change_pct": 0.31,
                "source": "provider-must-not-leak",
                "internal": "secret",
            }
        ],
        "history": [
            {
                "date": "2026-08-27",
                "open": 6400,
                "high": 6490,
                "low": 6390,
                "close": 6480,
                "volume": 900,
            },
            {
                "date": "2026-08-26",
                "open": 6350,
                "high": 6420,
                "low": 6340,
                "close": 6400,
                "volume": 800,
            },
            {
                "date": "2026-08-27",
                "open": 6410,
                "high": 6500,
                "low": 6400,
                "close": 6490,
                "volume": 950,
            },
            {
                "date": "2026-08-28",
                "open": None,
                "high": 6510,
                "low": 6475,
                "close": 6500.5,
                "volume": 1000,
            },
        ],
    }
    monkeypatch.setattr(mod, "handle_get_market_index", AsyncMock(return_value=result))

    with _client() as client:
        response = client.get("/api/v1/market/indices/spx?range=1M")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "symbol": "SPX",
        "name": "S&P 500",
        "market": "US",
        "currency": "USD",
        "price": "6500.5",
        "changeAmount": "20.15",
        "changeRate": "0.31",
        "asOf": None,
        "status": "available",
        "sessionState": "REGULAR",
        "range": "1M",
        "kind": "INDEX",
        "unit": "POINT",
        "group": None,
        "supportedRanges": ["1D", "1W", "1M", "3M", "6M"],
    }
    assert body["candles"] == [
        {
            "time": "2026-08-26T00:00:00Z",
            "open": "6350",
            "high": "6420",
            "low": "6340",
            "close": "6400",
            "volume": "800",
        },
        {
            "time": "2026-08-27T00:00:00Z",
            "open": "6410",
            "high": "6500",
            "low": "6400",
            "close": "6490",
            "volume": "950",
        },
    ]
    assert "provider-must-not-leak" not in response.text
    assert "internal" not in response.text
    assert "change_amount" not in response.text


def test_index_detail_uses_completed_kr_close_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "indices": [
            {
                "symbol": "KOSPI",
                "current": 6788.88,
                "change": -123.49,
                "change_pct": -1.79,
                "quote_asof": _KR_COMPLETED_END.isoformat(),
                "data_state": DATA_STATE_MARKET_CLOSED,
            }
        ],
        "history": [],
    }
    source = AsyncMock(return_value=result)
    monkeypatch.setattr(mod, "handle_get_market_index", source)

    with _client() as client:
        response = client.get("/api/v1/market/indices/KOSPI?range=1W")

    assert response.status_code == 200
    assert response.json()["summary"]["asOf"] == "2026-08-28T06:30:00Z"
    source.assert_awaited_once_with(
        symbol="KOSPI",
        period="day",
        count=5,
        completed_as_of_by_market={"KRX": _KR_COMPLETED_END},
    )


def test_index_detail_keeps_kr_bars_that_carry_no_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Naver index price rows carry no traded volume, so dropping volume-less
    # bars would leave the KR chart permanently empty.
    result = {
        "indices": [
            {
                "symbol": "KOSPI",
                "current": 6788.88,
                "change": -123.49,
                "change_pct": -1.79,
                "data_state": DATA_STATE_FRESH,
            }
        ],
        "history": [
            {
                "date": "2026-08-28",
                "open": 6846.54,
                "high": 6901.78,
                "low": 6780.13,
                "close": 6788.88,
                "volume": None,
            }
        ],
    }
    monkeypatch.setattr(mod, "handle_get_market_index", AsyncMock(return_value=result))

    with _client() as client:
        response = client.get("/api/v1/market/indices/KOSPI?range=1M")

    assert response.status_code == 200
    assert response.json()["candles"] == [
        {
            "time": "2026-08-28T00:00:00Z",
            "open": "6846.54",
            "high": "6901.78",
            "low": "6780.13",
            "close": "6788.88",
            "volume": None,
        }
    ]


@pytest.mark.asyncio
async def test_index_detail_returns_sanitized_unavailable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_source(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("sensitive provider exception")

    monkeypatch.setattr(mod, "handle_get_market_index", fail_source)

    response = await mod.get_market_index_detail("NASDAQ", "1W")
    payload = response.model_dump(by_alias=True)

    assert payload["summary"] == {
        "symbol": "NASDAQ",
        "name": "NASDAQ",
        "market": "US",
        "currency": "USD",
        "price": None,
        "changeAmount": None,
        "changeRate": None,
        "asOf": None,
        "status": "unavailable",
        "sessionState": "REGULAR",
        "range": "1W",
        "kind": "INDEX",
        "unit": "POINT",
        "group": None,
        "supportedRanges": ["1D", "1W", "1M", "3M", "6M"],
    }
    assert payload["candles"] == []
    assert "sensitive provider exception" not in str(payload)


@pytest.mark.asyncio
async def test_index_detail_cache_is_keyed_single_flight_for_fifteen_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []
    monotonic_now = 1000.0

    async def source(
        *,
        symbol: str,
        period: str,
        count: int,
        completed_as_of_by_market: dict[str, datetime],
    ) -> dict[str, Any]:
        expected_market = "KRX" if symbol in {"KOSPI", "KOSDAQ"} else "US"
        expected_end = (
            _KR_COMPLETED_END if expected_market == "KRX" else _US_COMPLETED_END
        )
        assert completed_as_of_by_market == {expected_market: expected_end}
        calls.append((symbol, period, count))
        await asyncio.sleep(0)
        return _index_result(symbol)

    monkeypatch.setattr(mod, "handle_get_market_index", source)
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)

    same_key = await asyncio.gather(
        *(mod.get_market_index_detail("SPX", "1W") for _ in range(5))
    )
    assert calls == [("SPX", "day", 5)]
    assert all(response is same_key[0] for response in same_key)

    monotonic_now = 1014.9
    assert await mod.get_market_index_detail("SPX", "1W") is same_key[0]
    await mod.get_market_index_detail("SPX", "1M")
    assert calls[-1] == ("SPX", "day", 20)

    monotonic_now = 1015.1
    refreshed = await mod.get_market_index_detail("SPX", "1W")
    assert calls.count(("SPX", "day", 5)) == 2
    assert refreshed is not same_key[0]


def test_index_detail_requires_mobile_auth_and_rejects_invalid_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticate = AsyncMock(return_value=object())
    monkeypatch.setattr(mobile_auth, "authenticate", authenticate)
    monkeypatch.setattr(
        mod,
        "get_market_index_detail",
        AsyncMock(
            return_value=mod.MarketIndexDetailResponse(
                summary=mod.MarketIndexSummary(
                    symbol="SPX",
                    name="S&P 500",
                    market="US",
                    currency="USD",
                    price=None,
                    change_amount=None,
                    change_rate=None,
                    as_of=None,
                    status="unavailable",
                    session_state="CLOSED",
                    range="1W",
                    supported_ranges=["1D", "1W", "1M", "3M", "6M"],
                ),
                candles=[],
            )
        ),
    )

    with _client(middleware=True) as client:
        authorized = client.get(
            "/api/v1/market/indices/SPX?range=1W",
            headers={"Authorization": "Bearer valid-mobile-token"},
        )
        anonymous = client.get("/api/v1/market/indices/SPX?range=1W")
        invalid_range = client.get(
            "/api/v1/market/indices/SPX?range=1Y",
            headers={"Authorization": "Bearer valid-mobile-token"},
        )

    assert authorized.status_code == 200
    assert authorized.json()["summary"]["status"] == "unavailable"
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "UNAUTHORIZED"
    assert invalid_range.status_code == 422
