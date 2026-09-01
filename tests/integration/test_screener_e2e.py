from __future__ import annotations

from collections.abc import Generator
from time import monotonic, sleep
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import screener
from app.services.screener_service import ScreenerService


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return False
        _ = ex
        self.store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str):
        _ = ttl
        self.store[key] = value
        return True

    async def delete(self, key: str):
        self.store.pop(key, None)
        return 1


def _generated_report() -> dict[str, Any]:
    return {
        "decision": "hold",
        "confidence": 62,
        "reasons": ["range"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 99},
            "sell_target_range": {"min": 140, "max": 150},
        },
        "detailed_text": "stable trend",
    }


def _wait_for_report_status(
    client: TestClient,
    job_id: str,
    expected_status: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while monotonic() < deadline:
        response = client.get(f"/api/screener/report/{job_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest.get("status") == expected_status:
            return latest
        sleep(0.01)
    pytest.fail(
        f"report {job_id} did not reach {expected_status!r}; latest response: {latest}"
    )


@pytest.fixture
def screener_app_success() -> Generator[tuple[TestClient, AsyncMock]]:
    fake_redis = _FakeRedis()
    generator = AsyncMock(return_value=_generated_report())
    service = ScreenerService(redis_client=fake_redis, report_generator=generator)

    app = FastAPI()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: service

    with TestClient(app) as client:
        yield client, generator


@pytest.fixture
def screener_app_generation_failure() -> Generator[TestClient]:
    fake_redis = _FakeRedis()
    generator = AsyncMock(side_effect=RuntimeError("mcp down"))
    service = ScreenerService(redis_client=fake_redis, report_generator=generator)

    app = FastAPI()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: service

    with TestClient(app) as client:
        yield client


@pytest.fixture
def screener_app_screening(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, AsyncMock]]:
    fake_redis = _FakeRedis()
    service = ScreenerService(redis_client=fake_redis)
    screen_mock = AsyncMock(
        return_value={
            "results": [
                {"code": "AAPL", "volume": 500},
                {"code": "MSFT", "volume": 1000},
                {"code": "NVDA", "volume": 2500},
                {"code": "TSLA", "volume": 1500},
            ],
            "total_count": 4,
            "returned_count": 4,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", screen_mock)

    app = FastAPI()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: service

    with TestClient(app) as client:
        yield client, screen_mock


@pytest.mark.integration
def test_screener_report_lifecycle_e2e(
    screener_app_success: tuple[TestClient, AsyncMock],
) -> None:
    client, generator = screener_app_success

    create_res = client.post(
        "/api/screener/report",
        json={"market": "us", "symbol": "AAPL", "name": "Apple"},
    )
    assert create_res.status_code == 200
    create_body = create_res.json()
    assert create_body["job_id"]
    assert create_body["status"] == "queued"

    completed_body = _wait_for_report_status(
        client,
        create_body["job_id"],
        "completed",
    )
    assert completed_body["report"]["decision"] == "hold"
    generator.assert_awaited_once_with(
        market="us",
        symbol="AAPL",
        name="Apple",
    )

    unknown_res = client.get("/api/screener/report/unknown-job-id")
    assert unknown_res.status_code == 200
    assert unknown_res.json() == {
        "job_id": "unknown-job-id",
        "status": "failed",
        "error": "job_not_found",
        "not_found": True,
    }


@pytest.mark.integration
def test_screener_report_failure_contains_error(
    screener_app_generation_failure: TestClient,
) -> None:
    client = screener_app_generation_failure

    create_res = client.post(
        "/api/screener/report",
        json={"market": "us", "symbol": "AAPL", "name": "Apple"},
    )
    assert create_res.status_code == 200
    create_body = create_res.json()
    assert create_body["status"] == "queued"

    status_body = _wait_for_report_status(
        client,
        create_body["job_id"],
        "failed",
    )
    assert "mcp down" in status_body["error"]


@pytest.mark.integration
def test_screener_list_min_volume_e2e(
    screener_app_screening: tuple[TestClient, AsyncMock],
) -> None:
    client, screen_mock = screener_app_screening

    response = client.get(
        "/api/screener/list",
        params={"market": "us", "min_volume": 1000, "limit": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["code"] for item in body["results"]] == ["MSFT", "NVDA"]
    assert body["returned_count"] == 2
    assert body["total_count"] == 3
    assert body["filters_applied"]["min_volume"] == pytest.approx(1000.0)

    await_args = screen_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["limit"] == 6
