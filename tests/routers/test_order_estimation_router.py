from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.routers.order_estimation import router


def _user(uid=1):
    return SimpleNamespace(id=uid)


@pytest.fixture
def base_app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.mark.unit
def test_domestic_pending_cost_unavailable_is_explicit_503(base_app):
    from app.services.order_estimation_service import (
        PendingBuyCostUnavailableError,
    )

    with (
        patch(
            "app.routers.order_estimation.get_user_from_request",
            new=AsyncMock(return_value=_user()),
        ),
        patch(
            "app.routers.order_estimation.SymbolTradeSettingsService"
        ) as MockSettingsSvc,
        patch("app.routers.order_estimation.StockAnalysisService"),
        patch(
            "app.routers.order_estimation.fetch_pending_domestic_buy_cost",
            new=AsyncMock(
                side_effect=PendingBuyCostUnavailableError(
                    market="kr", reason="provider_unavailable"
                )
            ),
        ),
    ):
        MockSettingsSvc.return_value.get_all = AsyncMock(return_value=[])

        response = TestClient(base_app).get(
            "/api/symbol-settings/symbols/domestic/estimated-cost"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "pending_buy_cost_unavailable",
        "market": "kr",
        "reason": "provider_unavailable",
    }
