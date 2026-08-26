import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.broker_registry import broker_registry
from app.extensions.kasset.api.credential_vault import credential_vault
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.nh_adapter import nh_adapter

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
    return object()


async def _db_override() -> AsyncIterator[object]:
    yield object()


def _client() -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)
    app.dependency_overrides[get_mobile_session] = _session_override
    app.dependency_overrides[get_db] = _db_override
    return TestClient(app)


def test_android_compatibility_surface_exposes_required_routes() -> None:
    with _client() as client:
        document = client.app.openapi()
        paths: dict[str, Any] = document["paths"]

    required = {
        "/health": {"get"},
        "/api/v1/auth/pair": {"post"},
        "/api/v1/auth/refresh": {"post"},
        "/api/v1/auth/revoke": {"post"},
        "/api/v1/system/status": {"get"},
        "/api/v1/brokers": {"get"},
        "/api/v1/brokers/{provider}/credential": {"post", "delete"},
        "/api/v1/brokers/{provider}/verify": {"post"},
        "/api/v1/account/balance": {"get"},
        "/api/v1/positions": {"get"},
        "/api/v1/market/quote": {"get"},
        "/api/v1/market/symbols": {"get"},
        "/api/v1/orders": {"get", "post"},
        "/api/v1/orders/preview": {"post"},
        "/api/v1/orders/{order_id}/cancel": {"post"},
        "/api/v1/orders/{order_id}/amend": {"post"},
        "/api/v1/fills": {"get"},
        "/api/v1/risk/policy": {"get", "put"},
        "/api/v1/ai/status": {"get"},
    }
    for path, methods in required.items():
        assert path in paths
        assert methods <= set(paths[path])


def test_health_contract_is_unauthenticated() -> None:
    with _client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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

    brokers = asyncio.run(broker_registry.list_brokers(object()))  # type: ignore[arg-type]

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


def test_nh_quote_rejects_invalid_input_before_credential_access() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/market/quote?broker=NH&market=NYSE&symbol=AAPL"
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "NH PLUG 조회는 KRX 6자리 종목코드만 지원합니다.",
        }
    }



def test_nh_quote_normalizes_official_current_price_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_quote = AsyncMock(
        return_value={
            "Output_0": {
                "iem_cd": "005930",
                "iem_nm": "삼성전자",
                "stck_prpr": 71500,
                "prdy_vrss_sign": "2",
                "prdy_vrss": 500,
                "prdy_ctrt": 0.70,
            }
        }
    )
    context = SimpleNamespace(client=SimpleNamespace(fetch_quote=fetch_quote))
    monkeypatch.setattr(
        nh_adapter,
        "prepare_read",
        AsyncMock(return_value=context),
    )

    quote = asyncio.run(
        nh_adapter.quote(object(), market="krx", symbol="005930")  # type: ignore[arg-type]
    )

    assert quote.model_dump(by_alias=True) == {
        "broker": "NH",
        "market": "KRX",
        "symbol": "005930",
        "name": "삼성전자",
        "currency": "KRW",
        "price": "71500",
        "previousClose": "71000",
        "changeAmount": "500",
        "changeRate": "0.7",
        "asOf": quote.as_of,
        "source": "NH_PLUG_MOCK",
    }
    assert quote.as_of.endswith("Z")
    fetch_quote.assert_awaited_once_with(market="KRX", symbol="005930")
