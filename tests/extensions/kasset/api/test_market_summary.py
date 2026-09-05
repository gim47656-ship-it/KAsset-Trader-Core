from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.extensions.kasset.api.market_summary as market_summary_service
import app.extensions.kasset.api.router as router_module
from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.schemas import DailyCandle, DailyCandlesResponse
from app.services.exchange_rate_service import UsdKrwExchangeRateQuote


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


def _intraday() -> DailyCandlesResponse:
    return DailyCandlesResponse(
        interval="1m",
        candles=[
            DailyCandle(
                time="2026-09-04T00:00:00Z",
                open="100",
                high="110",
                low="95",
                close="105",
                volume="10",
            ),
            DailyCandle(
                time="2026-09-04T00:01:00Z",
                open="105",
                high="112",
                low="101",
                close="111",
                volume="20",
            ),
        ],
    )


def _install_summary_repositories(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daily_rows: list[object],
    valuation_rows: list[object],
    flow_rows: list[object],
) -> None:
    class DailyRepository:
        def __init__(self, *, session: object) -> None:
            del session

        async def fetch_recent(self, **kwargs: object) -> list[object]:
            assert kwargs["count"] == 3
            return daily_rows

    class ValuationRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def latest_for_symbols(self, **kwargs: object) -> list[object]:
            return valuation_rows

    class FlowRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def latest_by_symbols(self, **kwargs: object) -> list[object]:
            return flow_rows

    monkeypatch.setattr(
        market_summary_service, "DailyCandlesRepository", DailyRepository
    )
    monkeypatch.setattr(
        market_summary_service,
        "MarketValuationSnapshotsRepository",
        ValuationRepository,
    )
    monkeypatch.setattr(
        market_summary_service,
        "InvestorFlowSnapshotsRepository",
        FlowRepository,
    )


def test_market_summary_projects_intraday_and_latest_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_summary_repositories(
        monkeypatch,
        daily_rows=[
            SimpleNamespace(
                time_utc=datetime(2026, 9, 3, 6, 30, tzinfo=UTC),
                close=Decimal("98"),
                volume=Decimal("20"),
                value=Decimal("1960"),
                source="toss_regular",
            ),
            SimpleNamespace(
                time_utc=datetime(2026, 9, 4, 6, 30, tzinfo=UTC),
                close=Decimal("111"),
                volume=Decimal("30"),
                value=Decimal("3300"),
                source="toss_regular",
            ),
        ],
        valuation_rows=[
            SimpleNamespace(
                high_52w=Decimal("150"),
                low_52w=Decimal("70"),
                market_cap=Decimal("123000000"),
                per=Decimal("9.7"),
                pbr=Decimal("1.2"),
                roe=Decimal("8.4"),
                dividend_yield=Decimal("2.1"),
            )
        ],
        flow_rows=[SimpleNamespace(foreign_holding_rate=Decimal("47.73"))],
    )
    monkeypatch.setattr(
        router_module, "market_candles", AsyncMock(return_value=_intraday())
    )

    with _client() as client:
        response = client.get("/api/v1/market/summary?market=KRX&symbol=005930")

    assert response.status_code == 200
    assert response.json() == {
        "market": "KRX",
        "symbol": "005930",
        "asOf": "2026-09-04T00:01:00Z",
        "source": "TOSS_1M",
        "open": "100",
        "high": "112",
        "low": "95",
        "prevClose": "98",
        "volume": "30",
        "tradeValue": "3300",
        "volumeChangeRate": "50.00",
        "high52w": "150",
        "low52w": "70",
        "marketCap": "123000000",
        "per": "9.7",
        "pbr": "1.2",
        "roe": "8.4",
        "dividendYield": "2.1",
        "foreignHoldingRate": "47.73",
    }


def test_market_summary_keeps_unavailable_us_values_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_summary_repositories(
        monkeypatch,
        daily_rows=[],
        valuation_rows=[],
        flow_rows=[],
    )
    monkeypatch.setattr(
        router_module,
        "market_candles",
        AsyncMock(return_value=DailyCandlesResponse(interval="1m", candles=[])),
    )

    with _client() as client:
        response = client.get("/api/v1/market/summary?market=US&symbol=AAPL")

    assert response.status_code == 200
    assert response.json() == {
        "market": "US",
        "symbol": "AAPL",
        "asOf": None,
        "source": None,
        "open": None,
        "high": None,
        "low": None,
        "prevClose": None,
        "volume": None,
        "tradeValue": None,
        "volumeChangeRate": None,
        "high52w": None,
        "low52w": None,
        "marketCap": None,
        "per": None,
        "pbr": None,
        "roe": None,
        "dividendYield": None,
        "foreignHoldingRate": None,
    }


def test_investor_flow_returns_latest_kr_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FlowRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def latest_by_symbols(self, **kwargs: object) -> list[object]:
            return [
                SimpleNamespace(
                    snapshot_date=date(2026, 9, 4),
                    individual_net=1234,
                    foreign_net=-567,
                    institution_net=890,
                )
            ]

    monkeypatch.setattr(
        market_summary_service,
        "InvestorFlowSnapshotsRepository",
        FlowRepository,
    )

    with _client() as client:
        response = client.get("/api/v1/market/investor-flow?market=KRX&symbol=005930")

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "005930",
        "asOf": "2026-09-04",
        "individualNet": "1234",
        "foreignNet": "-567",
        "institutionNet": "890",
        "unit": "SHARES",
    }


def test_investor_flow_returns_nulls_for_us_without_db_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedFlowRepository:
        def __init__(self, _session: object) -> None:
            raise AssertionError("US must not query KR investor-flow snapshots")

    monkeypatch.setattr(
        market_summary_service,
        "InvestorFlowSnapshotsRepository",
        UnexpectedFlowRepository,
    )

    with _client() as client:
        response = client.get("/api/v1/market/investor-flow?market=US&symbol=AAPL")

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "AAPL",
        "asOf": None,
        "individualNet": None,
        "foreignNet": None,
        "institutionNet": None,
        "unit": "SHARES",
    }


def test_fx_projects_exchange_rate_service_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = UsdKrwExchangeRateQuote(
        rate=1399.2,
        mid_rate=1400.25,
        source="toss",
        valid_from=datetime(2000, 1, 1, 1, 0, tzinfo=UTC),
        valid_until=datetime(2000, 1, 1, 1, 5, tzinfo=UTC),
        rate_decimal=Decimal("1399.20"),
        mid_rate_decimal=Decimal("1400.25"),
    )
    monkeypatch.setattr(
        market_summary_service,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=quote),
    )

    with _client() as client:
        response = client.get("/api/v1/market/fx?pair=USD-KRW")

    assert response.status_code == 200
    assert response.json() == {
        "pair": "USD-KRW",
        "rate": "1400.25",
        "source": "toss",
        "asOf": "2000-01-01T01:00:00Z",
        "validUntil": "2000-01-01T01:05:00Z",
        "stale": True,
    }


def test_fx_stale_is_false_while_quote_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = UsdKrwExchangeRateQuote(
        rate=1400.0,
        mid_rate=1400.0,
        source="open_er_api",
        valid_from=datetime(2026, 9, 5, 1, 0, tzinfo=UTC),
        valid_until=datetime(2026, 9, 5, 1, 5, tzinfo=UTC),
        rate_decimal=Decimal("1400"),
        mid_rate_decimal=Decimal("1400"),
    )
    monkeypatch.setattr(
        market_summary_service,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=quote),
    )

    result = asyncio.run(
        market_summary_service.get_fx_rate(
            pair="USD-KRW",
            now=datetime(2026, 9, 5, 1, 4, tzinfo=UTC),
        )
    )

    assert result.stale is False
