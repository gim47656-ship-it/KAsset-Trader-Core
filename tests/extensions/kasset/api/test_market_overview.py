from __future__ import annotations

import asyncio
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
        ]
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
        mod, "handle_get_market_index", AsyncMock(return_value=_index_payload())
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
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
    ]
    assert [item["symbol"] for item in body["fx"]] == [
        "USDKRW",
        "JPYKRW",
        "EURKRW",
    ]
    assert body["indices"][0]["changeRate"] == "0.70"
    assert body["indices"][0]["changeAmount"] == "18.90"
    assert "change_rate" not in body["indices"][0]
    assert body["indices"][2]["asOf"] is None
    assert [item["price"] for item in body["fx"]] == [
        "1500.00",
        "10.00",
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
    assert response.fx[1].price == "10.00"
    assert response.fx[2].price == "2000"


@pytest.mark.asyncio
async def test_overview_maps_closed_sessions_to_stale_without_age_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _index_payload()
    for row in payload["indices"]:
        if row["symbol"] in {"KOSPI", "KOSDAQ"}:
            row["data_state"] = DATA_STATE_MARKET_CLOSED
    monkeypatch.setattr(
        mod, "kr_market_data_state", lambda: DATA_STATE_MARKET_CLOSED
    )
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_AFTERHOURS)
    monkeypatch.setattr(
        mod, "handle_get_market_index", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
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


@pytest.mark.asyncio
async def test_overview_retains_failed_item_with_null_numbers_and_partial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _index_payload()
    payload["indices"][1] = {"symbol": "KOSDAQ", "error": "provider unavailable"}
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(
        mod, "handle_get_market_index", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
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
    async def fail_indices(*, symbol: str | None) -> dict[str, Any]:
        raise RuntimeError(f"indices failed: {symbol}")

    async def fail_fx() -> OpenErApiUsdSnapshot:
        raise RuntimeError("fx failed")

    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index", fail_indices)
    monkeypatch.setattr(mod, "get_open_er_api_usd_snapshot", fail_fx)

    response = await mod._build_market_overview()

    assert response.status == "unavailable"
    assert response.as_of is None
    assert [item.symbol for item in response.indices] == [
        "KOSPI",
        "KOSDAQ",
        "SPX",
        "NASDAQ",
    ]
    assert [item.symbol for item in response.fx] == ["USDKRW", "JPYKRW", "EURKRW"]
    assert all(
        item.status == "unavailable" for item in [*response.indices, *response.fx]
    )
    assert all(item.price is None for item in [*response.indices, *response.fx])
    assert len(response.errors) == 7


@pytest.mark.asyncio
async def test_overview_marks_a_bounded_source_group_timeout_without_losing_other_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_indices(*, symbol: str | None) -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError(symbol)

    monkeypatch.setattr(mod, "SOURCE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index", slow_indices)
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )

    response = await mod._build_market_overview()

    assert response.status == "partial"
    assert all(item.status == "unavailable" for item in response.indices)
    assert all(item.status == "available" for item in response.fx)
    assert [error.code for error in response.errors] == ["TIMEOUT"] * 4


@pytest.mark.asyncio
async def test_overview_cache_is_sixty_seconds_and_refresh_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monotonic_now = 1000.0

    async def indices(*, symbol: str | None) -> dict[str, Any]:
        nonlocal calls
        assert symbol is None
        calls += 1
        await asyncio.sleep(0)
        return _index_payload()

    monkeypatch.setattr(mod, "kr_market_data_state", lambda: DATA_STATE_FRESH)
    monkeypatch.setattr(mod, "us_market_session", lambda: US_SESSION_REGULAR)
    monkeypatch.setattr(mod, "handle_get_market_index", indices)
    monkeypatch.setattr(
        mod, "get_open_er_api_usd_snapshot", AsyncMock(return_value=_fx_snapshot())
    )
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)

    first_batch = await asyncio.gather(
        *(mod.get_market_overview() for _ in range(5))
    )
    assert calls == 1
    assert all(item is first_batch[0] for item in first_batch)

    monotonic_now = 1059.9
    assert await mod.get_market_overview() is first_batch[0]
    assert calls == 1

    monotonic_now = 1060.1
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
