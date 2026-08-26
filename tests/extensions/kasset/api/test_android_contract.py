from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api

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
