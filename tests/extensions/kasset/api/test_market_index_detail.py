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
async def test_indicator_keys_are_not_reachable_as_index_details() -> None:
    """지표 심볼(_INDEX_META에 있어도)은 지수 상세 화이트리스트가 아니다."""
    for key in ("VIX", "US10Y", "WTI", "GOLD", "KR_BOND_10Y", "BTC"):
        with pytest.raises(mod.MobileApiError) as excinfo:
            await mod.get_market_index_detail(key, "1W")
        assert excinfo.value.code == "UNKNOWN_INDEX"


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
