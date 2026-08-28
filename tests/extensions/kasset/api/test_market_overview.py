from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
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
from app.extensions.kasset.api.schemas import MarketOverviewResponse
from app.extensions.kasset.api.toss_market_data import TossIndicatorPoint
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_MARKET_CLOSED,
    US_SESSION_AFTERHOURS,
    US_SESSION_REGULAR,
)
from app.middleware.auth import AuthMiddleware
from app.services.exchange_rate_service import (
    OpenErApiUsdSnapshot,
    UsdKrwExchangeRateQuote,
)


@pytest.fixture(autouse=True)
def clear_overview_cache() -> None:
    mod._cache.clear()


async def _session_override() -> object:
    return SimpleNamespace(user=SimpleNamespace(id=101, role="trader", is_active=True))


async def _db_override() -> AsyncIterator[object]:
    yield object()


def _client() -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)
    app.dependency_overrides[get_mobile_session] = _session_override
    app.dependency_overrides[get_db] = _db_override
    return TestClient(app)


def _full_middleware_client() -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = _db_override
    return TestClient(app)


def _index_payload() -> dict[str, Any]:
    # Deliberately shuffled: the overview owns its fixed public order.
    return {
        "indices": [
            {
                "symbol": "NASDAQ",
                "current": "21000.25",
                "change": "84.5",
                "change_pct": "0.40",
                "source": "yfinance",
            },
            {
                "symbol": "KOSDAQ",
                "current": "900.20",
                "change": "-2.10",
                "change_pct": "-0.23",
                "quote_asof": "2026-08-28T14:01:00+09:00",
                "data_state": DATA_STATE_FRESH,
                "source": "naver",
            },
            {
                "symbol": "SPX",
                "current": "6500.50",
                "change": "20.15",
                "change_pct": "0.31",
                "source": "yfinance",
            },
            {
                "symbol": "KOSPI",
                "current": "2700.10",
                "change": "18.90",
                "change_pct": "0.70",
                "quote_asof": "2026-08-28T14:00:00+09:00",
                "data_state": DATA_STATE_FRESH,
                "source": "naver",
            },
            {
                "symbol": "DJI",
                "current": "53681.19",
                "previous_close": "53500.00",
                "change": "181.19",
                "change_pct": "0.34",
                "source": "yfinance",
            },
            {
                "symbol": "RUT",
                "current": "3014.3787",
                "previous_close": "3000.00",
                "change": "14.38",
                "change_pct": "0.48",
                "source": "yfinance",
            },
            {
                "symbol": "SOX",
                "current": "11735.794",
                "previous_close": "11600.00",
                "change": "135.79",
                "change_pct": "1.17",
                "source": "yfinance",
            },
            *_indicator_rows(),
        ]
    }


def _indicator_rows() -> list[dict[str, Any]]:
    """US 배치가 지표 심볼까지 함께 실어 오는 행들(별도 왕복 없음)."""
    return [
        {
            "symbol": "VIX",
            "current": "14.48",
            "previous_close": "15.02",
            "change": "-0.54",
            "change_pct": "-3.6",
            "source": "yfinance",
        },
        {
            "symbol": "US10Y",
            "current": "4.68",
            "previous_close": "4.71",
            "change": "-0.03",
            "change_pct": "-0.64",
            "source": "yfinance",
        },
        {
            "symbol": "WTI",
            "current": "82.69",
            "previous_close": "81.90",
            "change": "0.79",
            "change_pct": "0.96",
            "source": "yfinance",
        },
        {
            "symbol": "BRENT",
            "current": "87.63",
            "previous_close": "86.80",
            "change": "0.83",
            "change_pct": "0.96",
            "source": "yfinance",
        },
        {
            "symbol": "GOLD",
            "current": "4655.6",
            "previous_close": "4640.10",
            "change": "15.5",
            "change_pct": "0.33",
            "source": "yfinance",
        },
    ]


def _btc_ticker() -> dict[str, Any]:
    """Upbit 공개 티커 응답 한 건(무인증 /v1/ticker 필드 이름 그대로)."""
    return {
        "market": "KRW-BTC",
        "trade_price": 109807000,
        "prev_closing_price": 111005000,
        "signed_change_price": -1198000,
        "signed_change_rate": -0.0108,
        "trade_timestamp": int(
            datetime(2026, 8, 28, 6, 10, tzinfo=UTC).timestamp() * 1000
        ),
    }


def _toss_points() -> dict[str, TossIndicatorPoint]:
    """토스 시장지표 배치 응답(한국 국채 6종). 값은 % 수익률이다."""
    as_of = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)
    yields = {
        "KR_BOND_2Y": "3.512",
        "KR_BOND_3Y": "3.604",
        "KR_BOND_5Y": "3.881",
        "KR_BOND_10Y": "4.245",
        "KR_BOND_20Y": "4.470",
        "KR_BOND_30Y": "4.514",
    }
    return {
        symbol: TossIndicatorPoint(
            symbol=symbol, last_price=Decimal(value), as_of=as_of
        )
        for symbol, value in yields.items()
    }


def _fx_snapshot() -> OpenErApiUsdSnapshot:
    return OpenErApiUsdSnapshot(
        usd_krw=Decimal("1500.00"),
        jpy_per_usd=Decimal("150"),
        eur_per_usd=Decimal("0.75"),
        as_of=datetime(2026, 8, 28, 6, 0, tzinfo=UTC),
    )


def _stub_successful_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(
        mod,
        "handle_get_market_index_current_batch",
        AsyncMock(return_value=_index_payload()),
    )
    monkeypatch.setattr(
        mod, "fetch_multiple_tickers", AsyncMock(return_value=[_btc_ticker()])
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(
        mod, "_toss_indicator_points", AsyncMock(return_value=_toss_points())
    )


def test_overview_http_contract_is_camel_case_ordered_and_uses_percentage_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_sources(monkeypatch)

    with _client() as client:
        response = client.get("/api/v1/market/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fresh"
    assert body["asOf"] == "2026-08-28T06:00:00Z"
    assert [item["symbol"] for item in body["indices"]] == [
        "KOSPI",
        "KOSDAQ",
        "SPX",
        "NASDAQ",
        "DJI",
        "RUT",
        "SOX",
    ]
    assert [item["key"] for item in body["indicators"]] == [
        "VIX",
        "US10Y",
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_10Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
        "WTI",
        "BRENT",
        "GOLD",
        "BTC",
    ]
    assert [item["unit"] for item in body["indicators"]] == [
        "POINT",
        "PERCENT",
        "PERCENT",
        "PERCENT",
        "PERCENT",
        "PERCENT",
        "PERCENT",
        "PERCENT",
        "USD",
        "USD",
        "USD",
        "KRW",
    ]
    # ^TNX와 국고채는 % 단위를 유지하되 값은 소수 2자리로 제한한다.
    assert body["indicators"][1]["value"] == "4.68"
    assert body["indicators"][5]["key"] == "KR_BOND_10Y"
    assert body["indicators"][5]["value"] == "4.25"
    assert body["indicators"][5]["previousClose"] is None
    assert body["indicators"][5]["changeAmount"] is None
    assert body["indicators"][5]["changeRate"] is None
    # indicators는 세션과 결합하지 않으므로 market/sessionState 필드가 없다.
    assert all("market" not in item for item in body["indicators"])
    assert all("sessionState" not in item for item in body["indicators"])
    assert [item["symbol"] for item in body["fx"]] == [
        "USDKRW",
        "JPYKRW",
        "EURKRW",
    ]
    assert body["indices"][0]["changeRate"] == "0.7"
    assert body["indices"][0]["changeAmount"] == "18.9"
    assert "change_rate" not in body["indices"][0]
    assert body["indices"][2]["asOf"] is None
    assert [item["price"] for item in body["fx"]] == [
        "1500",
        "10",
        "2000",
    ]
    assert all(item["changeAmount"] is None for item in body["fx"])
    assert all(item["changeRate"] is None for item in body["fx"])
    assert all(item["sessionState"] is None for item in body["fx"])
    assert body["sessions"] == [
        {"market": "KRX", "state": "OPEN"},
        {"market": "US", "state": "OPEN"},
    ]
    assert body["errors"] == []


def test_overview_quantizes_live_provider_precision_without_exponent_notation() -> None:
    index_result = {
        "indices": [
            {"symbol": "KOSPI", "current": "6788.88", "source": "naver"},
            {"symbol": "KOSDAQ", "current": "838.41", "source": "naver"},
            {
                "symbol": "SPX",
                "current": 7767.33984375,
                "change": 36.349998474121094,
                "change_pct": 0.4699999988079071,
                "source": "yfinance",
            },
            {"symbol": "NASDAQ", "current": 26667.576171875, "source": "yfinance"},
            {"symbol": "DJI", "current": 53794.37109375, "source": "yfinance"},
            {"symbol": "RUT", "current": 2997.286865234375, "source": "yfinance"},
            {"symbol": "SOX", "current": 11689.716796875, "source": "yfinance"},
        ]
    }
    indices, index_errors = mod._index_items(
        index_result,
        sessions={"KRX": "OPEN", "US": "OPEN"},
    )

    assert index_errors == []
    assert {item.symbol: item.price for item in indices} == {
        "KOSPI": "6788.88",
        "KOSDAQ": "838.41",
        "SPX": "7767.34",
        "NASDAQ": "26667.58",
        "DJI": "53794.37",
        "RUT": "2997.29",
        "SOX": "11689.72",
    }
    spx = next(item for item in indices if item.symbol == "SPX")
    assert spx.change_amount == "36.35"
    assert spx.change_rate == "0.47"
    assert (
        mod._quantized_decimal_text(
            Decimal("1E+8"),
            decimal_places=mod.INDEX_DECIMAL_PLACES,
        )
        == "100000000"
    )

    indicator_result = {
        "indices": [
            {"symbol": "VIX", "current": 14.220000267028809},
            {"symbol": "US10Y", "current": 4.696000099182129},
            {"symbol": "WTI", "current": 83.1500015258789},
            {"symbol": "GOLD", "current": 4607.7001953125},
        ]
    }
    indicators, _indicator_errors = mod._indicator_items(
        indicator_result,
        {
            "trade_price": 109975000.0,
            "prev_closing_price": 111000000.0,
            "signed_change_price": -1025000.0,
            "signed_change_rate": -0.0092699362,
        },
        {
            "KR_BOND_10Y": TossIndicatorPoint(
                symbol="KR_BOND_10Y",
                last_price=Decimal("4.245"),
                as_of=datetime(2026, 8, 28, 6, 30, tzinfo=UTC),
            )
        },
        sessions={"KRX": "OPEN", "US": "OPEN"},
    )
    indicators_by_key = {item.key: item for item in indicators}

    assert {
        key: indicators_by_key[key].value for key in ("VIX", "US10Y", "WTI", "GOLD")
    } == {
        "VIX": "14.22",
        "US10Y": "4.7",
        "WTI": "83.15",
        "GOLD": "4607.7",
    }
    assert indicators_by_key["KR_BOND_10Y"].value == "4.25"
    assert indicators_by_key["BTC"].value == "109975000"
    assert indicators_by_key["BTC"].change_rate == "-0.93"

    fx, fx_errors = mod._fx_items(
        SimpleNamespace(
            usd_krw=Decimal("1381.39"),
            jpy_krw=Decimal("8.671256687620105017577911603"),
            eur_krw=Decimal("1609.719278172501299131038629"),
            as_of=datetime(2026, 8, 28, 6, 0, tzinfo=UTC),
        ),
        None,
    )

    assert fx_errors == []
    assert {item.symbol: item.price for item in fx} == {
        "USDKRW": "1381.39",
        "JPYKRW": "8.67",
        "EURKRW": "1609.72",
    }

    wire_numbers = [
        *(item.price for item in indices),
        *(item.value for item in indicators),
        *(item.previous_close for item in indicators),
        *(item.change_amount for item in indicators),
        *(item.change_rate for item in indicators),
        *(item.price for item in fx),
    ]
    assert all(
        value is None or re.fullmatch(r"-?\d+(?:\.\d+)?", value)
        for value in wire_numbers
    )


@pytest.mark.asyncio
async def test_overview_prefers_configured_toss_for_usd_krw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_sources(monkeypatch)
    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        mod,
        "get_usd_krw_rate_details",
        AsyncMock(
            return_value=UsdKrwExchangeRateQuote(
                rate=1512.25,
                mid_rate=1512.25,
                source="toss",
                valid_from=datetime(2026, 8, 28, 6, 5, tzinfo=UTC),
            )
        ),
    )

    response = await mod._build_market_overview()

    assert response.fx[0].price == "1512.25"
    assert response.fx[0].as_of == "2026-08-28T06:05:00Z"
    assert response.fx[1].price == "10"
    assert response.fx[2].price == "2000"


@pytest.mark.asyncio
async def test_overview_maps_closed_sessions_to_stale_without_age_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _index_payload()
    for row in payload["indices"]:
        if row["symbol"] in {"KOSPI", "KOSDAQ"}:
            row["data_state"] = DATA_STATE_MARKET_CLOSED
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_MARKET_CLOSED)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_AFTERHOURS)
    monkeypatch.setattr(
        mod,
        "handle_get_market_index_current_batch",
        AsyncMock(return_value=payload),
    )
    monkeypatch.setattr(
        mod, "fetch_multiple_tickers", AsyncMock(return_value=[_btc_ticker()])
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(
        mod, "_toss_indicator_points", AsyncMock(return_value=_toss_points())
    )

    response = await mod._build_market_overview()

    assert response.status == "partial"
    assert [session.state for session in response.sessions] == [
        "CLOSED",
        "AFTER_HOURS",
    ]
    assert all(item.status == "stale" for item in response.indices)
    assert all(item.status == "available" for item in response.fx)
    assert response.errors == []
    # US 배치 지표는 미국 세션을 따라 stale로 내려간다. 24시간 시장인 BTC와,
    # 기준 시각이 있는 토스 국채는 세션과 무관하게 available로 남는다.
    assert [item.status for item in response.indicators] == [
        "stale",
        "stale",
        "available",
        "available",
        "available",
        "available",
        "available",
        "available",
        "stale",
        "stale",
        "stale",
        "available",
    ]


@pytest.mark.asyncio
async def test_overview_retains_failed_item_with_null_numbers_and_partial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _index_payload()
    payload["indices"][1] = {"symbol": "KOSDAQ", "error": "provider unavailable"}
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(
        mod,
        "handle_get_market_index_current_batch",
        AsyncMock(return_value=payload),
    )
    monkeypatch.setattr(
        mod, "fetch_multiple_tickers", AsyncMock(return_value=[_btc_ticker()])
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(
        mod, "_toss_indicator_points", AsyncMock(return_value=_toss_points())
    )

    response = await mod._build_market_overview()

    failed = response.indices[1]
    assert response.status == "partial"
    assert failed.symbol == "KOSDAQ"
    assert failed.status == "unavailable"
    assert failed.price is None
    assert failed.change_amount is None
    assert failed.change_rate is None
    assert response.errors == [
        mod.MarketOverviewError(scope="indices", symbol="KOSDAQ", code="UNAVAILABLE")
    ]


@pytest.mark.asyncio
async def test_overview_returns_all_fixed_entries_when_both_source_groups_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_indices(symbols: tuple[str, ...]) -> dict[str, Any]:
        raise RuntimeError(f"indices failed: {symbols}")

    async def fail_fx() -> OpenErApiUsdSnapshot:
        raise RuntimeError("fx failed")

    async def fail_btc(market_codes: list[str]) -> list[dict[str, Any]]:
        raise RuntimeError(f"upbit failed: {market_codes}")

    async def fail_toss() -> dict[str, object]:
        raise RuntimeError("toss indicators failed")

    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index_current_batch", fail_indices)
    monkeypatch.setattr(mod, "fetch_multiple_tickers", fail_btc)
    monkeypatch.setattr(mod, "_toss_indicator_points", fail_toss)
    monkeypatch.setattr(mod, "get_open_er_api_usd_snapshot", fail_fx)

    response = await mod._build_market_overview()

    assert response.status == "unavailable"
    assert response.as_of is None
    assert [item.symbol for item in response.indices] == [
        "KOSPI",
        "KOSDAQ",
        "SPX",
        "NASDAQ",
        "DJI",
        "RUT",
        "SOX",
    ]
    assert [item.key for item in response.indicators] == [
        "VIX",
        "US10Y",
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_10Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
        "WTI",
        "BRENT",
        "GOLD",
        "BTC",
    ]
    assert [item.symbol for item in response.fx] == ["USDKRW", "JPYKRW", "EURKRW"]
    assert all(
        item.status == "unavailable"
        for item in [*response.indices, *response.fx, *response.indicators]
    )
    assert all(item.price is None for item in [*response.indices, *response.fx])
    assert all(item.value is None for item in response.indicators)
    # 지수 7 + 지표 12 + 환율 3
    assert len(response.errors) == 22


@pytest.mark.asyncio
async def test_overview_marks_a_bounded_source_group_timeout_without_losing_other_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_indices(symbols: tuple[str, ...]) -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError(symbols)

    monkeypatch.setattr(mod, "SOURCE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index_current_batch", slow_indices)
    monkeypatch.setattr(
        mod, "fetch_multiple_tickers", AsyncMock(return_value=[_btc_ticker()])
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(
        mod, "_toss_indicator_points", AsyncMock(return_value=_toss_points())
    )

    response = await mod._build_market_overview()

    assert response.status == "partial"
    assert all(item.status == "unavailable" for item in response.indices)
    assert all(item.status == "available" for item in response.fx)
    # 배치 타임아웃은 지수 7건과 US 배치 지표 5건만 때린다. Upbit BTC는 살아 있다.
    assert [error.code for error in response.errors] == ["TIMEOUT"] * 12
    btc = next(item for item in response.indicators if item.key == "BTC")
    assert btc.status == "available"
    assert btc.value == "109807000"


@pytest.mark.asyncio
async def test_overview_cache_is_fifteen_seconds_and_refresh_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monotonic_now = 1000.0

    async def indices(symbols: tuple[str, ...]) -> dict[str, Any]:
        nonlocal calls
        # 지수와 US 배치 지표가 한 번의 호출로 함께 요청된다(왕복 증가 없음).
        assert symbols == mod._OVERVIEW_BATCH_SYMBOLS
        calls += 1
        await asyncio.sleep(0)
        return _index_payload()

    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index_current_batch", indices)
    monkeypatch.setattr(
        mod, "fetch_multiple_tickers", AsyncMock(return_value=[_btc_ticker()])
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(
        mod, "_toss_indicator_points", AsyncMock(return_value=_toss_points())
    )
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)

    first_batch = await asyncio.gather(*(mod.get_market_overview() for _ in range(5)))
    assert calls == 1
    assert all(item is first_batch[0] for item in first_batch)

    monotonic_now = 1014.9
    assert await mod.get_market_overview() is first_batch[0]
    assert calls == 1

    monotonic_now = 1015.1
    refreshed_batch = await asyncio.gather(
        *(mod.get_market_overview() for _ in range(5))
    )
    assert calls == 2
    assert all(item is refreshed_batch[0] for item in refreshed_batch)
    assert refreshed_batch[0] is not first_batch[0]


def test_overview_requires_mobile_auth_through_upstream_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticate = AsyncMock(return_value=object())
    monkeypatch.setattr(mobile_auth, "authenticate", authenticate)
    response_model = MarketOverviewResponse(
        as_of=None,
        status="unavailable",
        indices=[],
        indicators=[],
        fx=[],
        sessions=[],
        errors=[],
    )
    monkeypatch.setattr(
        mod, "get_market_overview", AsyncMock(return_value=response_model)
    )

    with _full_middleware_client() as client:
        authorized = client.get(
            "/api/v1/market/overview",
            headers={"Authorization": "Bearer valid-mobile-token"},
        )
        anonymous = client.get("/api/v1/market/overview")

    assert authorized.status_code == 200
    assert authorized.json()["status"] == "unavailable"
    assert anonymous.status_code == 401
    assert anonymous.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "인증 토큰이 필요합니다.",
        }
    }
    authenticate.assert_awaited_once()
    assert authenticate.await_args.args[1] == "valid-mobile-token"
