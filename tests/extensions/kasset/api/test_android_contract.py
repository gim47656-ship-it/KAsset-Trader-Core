import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session, mobile_auth
from app.extensions.kasset.api.broker_registry import broker_registry
from app.extensions.kasset.api.credential_vault import credential_vault
from app.extensions.kasset.api.installation import install_android_compat_api
from app.middleware.auth import AuthMiddleware

ORDER = {
    "clientOrderId": "client-order-1",
    "broker": "NH",
    "accountId": None,
    "market": "KRX",
    "symbol": "005930",
    "side": "BUY",
    "orderType": "MARKET",
    "quantity": "1",
    "limitPrice": None,
}


async def _session_override() -> object:
    return SimpleNamespace(user=SimpleNamespace(id=101, role="trader", is_active=True))


class _EmptyResult:
    """Minimal async-session result: every query sees an empty store."""

    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[object]:
        return []

    def first(self) -> None:
        return None


class _EmptyStoreDb:
    async def execute(self, *args: object, **kwargs: object) -> _EmptyResult:
        return _EmptyResult()

    async def scalar(self, *args: object, **kwargs: object) -> None:
        return None

    async def scalars(self, *args: object, **kwargs: object) -> _EmptyResult:
        return _EmptyResult()


async def _db_override() -> AsyncIterator[object]:
    yield _EmptyStoreDb()


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


def test_android_compatibility_surface_exposes_required_routes() -> None:
    with _client() as client:
        document = client.app.openapi()
        paths: dict[str, Any] = document["paths"]

    required = {
        "/health": {"get"},
        "/api/v1/auth/register": {"post"},
        "/api/v1/auth/login": {"post"},
        "/api/v1/auth/google": {"post"},
        "/api/v1/auth/me": {"get", "patch"},
        "/api/v1/auth/refresh": {"post"},
        "/api/v1/auth/revoke": {"post"},
        "/api/v1/system/status": {"get"},
        "/api/v1/brokers": {"get"},
        "/api/v1/brokers/{provider}/credential": {"post", "delete"},
        "/api/v1/brokers/{provider}/verify": {"post"},
        "/api/v1/account/balance": {"get"},
        "/api/v1/positions": {"get"},
        "/api/v1/positions/closed-trades": {"get"},
        "/api/v1/market/quote": {"get"},
        "/api/v1/market/quotes": {"get"},
        "/api/v1/market/overview": {"get"},
        "/api/v1/market/news": {"get"},
        "/api/v1/market/indices/{symbol}": {"get"},
        "/api/v1/market/orderbook": {"get"},
        "/api/v1/market/candles": {"get"},
        "/api/v1/market/summary": {"get"},
        "/api/v1/market/investor-flow": {"get"},
        "/api/v1/market/fx": {"get"},
        "/api/v1/market/symbols": {"get"},
        "/api/v1/instruments/search": {"get"},
        "/api/v1/orders": {"get", "post"},
        "/api/v1/orders/preview": {"post"},
        "/api/v1/orders/{order_id}/cancel": {"post"},
        "/api/v1/orders/{order_id}/amend": {"post"},
        "/api/v1/fills": {"get"},
        "/api/v1/risk/policy": {"get", "put"},
        "/api/v1/ai/daily-routine": {"get", "put"},
        "/api/v1/ai/status": {"get"},
        "/api/v1/ai/briefing": {"get"},
        "/api/v1/ai/trading/state": {"get", "put"},
        "/api/v1/watchlist": {"get", "post"},
        "/api/v1/watchlist/{symbol}": {"delete"},
        "/api/v1/push/token": {"put", "delete"},
    }
    assert "/api/v1/auth/pair" not in paths
    for path, methods in required.items():
        assert path in paths
        assert methods <= set(paths[path])


def test_health_contract_is_unauthenticated() -> None:
    with _client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ai_briefing_returns_authenticated_unavailable_empty_contract() -> None:
    with _client() as client:
        response = client.get("/api/v1/ai/briefing?market=kr&symbol=005930&limit=10")

    assert response.status_code == 200
    body = response.json()
    # `asOf` is the server-side evaluation time even for an empty payload.
    assert body.pop("asOf").endswith("Z")
    assert body == {
        "status": "empty",
        "news": {
            "status": "empty",
            "refreshedAt": None,
            "items": [],
        },
        "routineAlerts": [],
        "research": {
            "status": "empty",
            "refreshedAt": None,
            "items": [],
        },
        "briefing": {
            "status": "unavailable",
            "id": None,
            "title": None,
            "summary": None,
            "provider": None,
            "market": None,
            "asOf": None,
            "validUntil": None,
            "dataStatus": "unknown",
            "unavailableReason": "저장된 AI 브리핑 제공자가 아직 연결되지 않았습니다.",
        },
    }


def test_ai_briefing_mobile_auth_survives_upstream_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticate = AsyncMock(return_value=SimpleNamespace(user=SimpleNamespace(id=101)))
    monkeypatch.setattr(mobile_auth, "authenticate", authenticate)

    with _full_middleware_client() as client:
        authorized = client.get(
            "/api/v1/ai/briefing?market=kr&limit=3",
            headers={"Authorization": "Bearer valid-mobile-token"},
        )
        anonymous = client.get("/api/v1/ai/briefing?market=kr&limit=3")

    assert authorized.status_code == 200
    assert authorized.json()["briefing"]["status"] == "unavailable"
    assert anonymous.status_code == 401
    assert anonymous.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "인증 토큰이 필요합니다.",
        }
    }
    authenticate.assert_awaited_once()
    assert authenticate.await_args.args[1] == "valid-mobile-token"


def test_paper_order_preview_exposes_price_and_quantity_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.extensions.kasset.api.paper_orders import paper_orders
    from app.extensions.kasset.api.paper_schemas import RiskAssessment

    preview = AsyncMock(
        return_value=RiskAssessment(
            decision="APPROVED",
            reasons=[],
            estimatedAmount="60123",
            estimatedFee="9.02",
            referencePrice="60000",
            currency="KRW",
            maxQuantity="16",
            tickSize="100",
            normalizedLimitPrice="60100",
            priceAdjusted=True,
        )
    )
    monkeypatch.setattr(paper_orders, "preview", preview)
    request = {
        **ORDER,
        "broker": "PAPER",
        "orderType": "LIMIT",
        "limitPrice": "60123",
    }

    with _client() as client:
        response = client.post("/api/v1/orders/preview", json=request)

    assert response.status_code == 200
    assert response.json() == {
        "decision": "APPROVED",
        "reasons": [],
        "estimatedAmount": "60123",
        "estimatedFee": "9.02",
        "referencePrice": "60000",
        "currency": "KRW",
        "maxQuantity": "16",
        "tickSize": "100",
        "normalizedLimitPrice": "60100",
        "priceAdjusted": True,
    }


def test_nh_order_preview_submit_cancel_and_amend_are_read_only() -> None:
    with _client() as client:
        responses = [
            client.post("/api/v1/orders/preview", json=ORDER),
            client.post("/api/v1/orders", json=ORDER),
            client.post("/api/v1/orders/order-1/cancel?broker=NH"),
            client.post(
                "/api/v1/orders/order-1/amend?broker=NH",
                json={"quantity": "2", "limitPrice": None},
            ),
        ]

    expected = {
        "error": {
            "code": "BROKER_READ_ONLY",
            "message": "NH PLUG는 현재 모의 Read-Only 단계입니다.",
        }
    }
    assert all(response.status_code == 409 for response in responses)
    assert all(response.json() == expected for response in responses)


def test_broker_catalog_builds_nh_entry_with_required_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credential_vault,
        "record",
        AsyncMock(return_value=None),
    )

    brokers = asyncio.run(
        broker_registry.list_brokers(object(), 101)  # type: ignore[arg-type]
    )

    nh = next(broker for broker in brokers if broker.provider == "NH")
    assert nh.display_name == "NH투자증권 PLUG"
    assert nh.mode == "MOCK_READ_ONLY"
    assert nh.capabilities.read_only is True


def test_nh_history_reads_return_empty_contracts() -> None:
    with _client() as client:
        orders = client.get("/api/v1/orders?broker=NH")
        fills = client.get("/api/v1/fills?broker=NH")

    assert orders.status_code == 200
    assert orders.json() == {"orders": []}
    assert fills.status_code == 200
    assert fills.json() == {"fills": []}


def test_paper_kr_quote_falls_back_to_stored_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장된 KR 캔들 종가를 PAPER 시세로 내려준다."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.extensions.kasset.api import krx_quotes
    from app.extensions.kasset.api import paper as paper_module

    adapter = paper_module.PaperAccountAdapter()
    instrument_names = AsyncMock(return_value={"005930": "삼성전자"})
    monkeypatch.setattr(krx_quotes, "_instrument_names", instrument_names)
    monkeypatch.setattr(
        adapter,
        "_quote_from_candles",
        AsyncMock(
            return_value={
                "price": Decimal("71500"),
                "previous_close": Decimal("71000"),
                "price_as_of": datetime(2026, 8, 28, 6, 30, tzinfo=UTC),
                "source": "CANDLES",
            }
        ),
    )

    db = object()
    quote = asyncio.run(adapter.quote(db, market="KRX", symbol="005930"))

    assert quote.price == "71500"
    assert quote.previous_close == "71000"
    assert quote.change_amount == "500"
    assert quote.source == "PAPER_CANDLES"
    assert quote.name == "삼성전자"
    instrument_names.assert_awaited_once_with(db, "KRX", ["005930"])


def test_paper_us_quote_uses_active_symbol_toss_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    import pandas as pd

    from app.extensions.kasset.api import paper as paper_module

    lookup = AsyncMock(return_value="NASD")
    prices = AsyncMock(
        return_value={
            "AAPL": SimpleNamespace(
                price=Decimal("201.5"),
                as_of=datetime(2026, 8, 28, 14, 30, tzinfo=UTC),
            )
        }
    )
    daily = AsyncMock(
        return_value=pd.DataFrame(
            {
                "datetime": [
                    datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                    datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
                ],
                "close": [Decimal("199"), Decimal("201")],
            }
        )
    )
    monkeypatch.setattr(paper_module, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(paper_module.toss_market_data, "prices", prices)
    monkeypatch.setattr(paper_module, "fetch_daily_toss_frame", daily)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=AssertionError("DB fallback must not run"))
    )

    raw = asyncio.run(
        paper_module.PaperAccountAdapter()._quote_us_toss_or_candles(
            db,
            "AAPL",  # type: ignore[arg-type]
        )
    )

    assert raw["price"] == Decimal("201.5")
    assert raw["previous_close"] == Decimal("199")
    assert raw["source"] == "TOSS"
    lookup.assert_awaited_once_with("AAPL", db=db)


def test_paper_us_quote_falls_back_to_stored_snapshot_when_toss_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.extensions.kasset.api import paper as paper_module

    class _Rows:
        def all(self):
            return [
                (datetime(2026, 8, 28, 20, 0, tzinfo=UTC), Decimal("201")),
                (datetime(2026, 8, 27, 20, 0, tzinfo=UTC), Decimal("199")),
            ]

    monkeypatch.setattr(
        paper_module, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    monkeypatch.setattr(
        paper_module.toss_market_data,
        "prices",
        AsyncMock(side_effect=RuntimeError("toss prices unavailable")),
    )
    monkeypatch.setattr(
        paper_module,
        "fetch_daily_toss_frame",
        AsyncMock(side_effect=RuntimeError("toss candles unavailable")),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Rows()))

    raw = asyncio.run(
        paper_module.PaperAccountAdapter()._quote_us_toss_or_candles(
            db,
            "AAPL",  # type: ignore[arg-type]
        )
    )

    assert raw == {
        "price": Decimal("201"),
        "previous_close": Decimal("199"),
        "price_as_of": datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        "source": "CANDLES",
    }


def test_paper_us_quote_rejects_inactive_before_toss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.extensions.kasset.api import paper as paper_module
    from app.services.us_symbol_universe_service import USSymbolInactiveError

    lookup = AsyncMock(side_effect=USSymbolInactiveError("AAPL"))
    prices = AsyncMock()
    daily = AsyncMock()
    monkeypatch.setattr(paper_module, "get_us_exchange_by_symbol", lookup)
    monkeypatch.setattr(paper_module.toss_market_data, "prices", prices)
    monkeypatch.setattr(paper_module, "fetch_daily_toss_frame", daily)

    with pytest.raises(USSymbolInactiveError):
        asyncio.run(
            paper_module.PaperAccountAdapter()._quote_us_toss_or_candles(
                object(),
                "AAPL",  # type: ignore[arg-type]
            )
        )

    prices.assert_not_awaited()
    daily.assert_not_awaited()
