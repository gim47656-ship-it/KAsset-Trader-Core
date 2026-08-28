import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.orderbook_store import (
    NHOrderbookSnapshotStore,
    _snapshot_from_push,
    _subscription_message,
    get_orderbook_store,
)


async def _session_override() -> object:
    return SimpleNamespace(user=SimpleNamespace(id=101, role="trader", is_active=True))


class _FakeOrderbookStore:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: MobileApiError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def get_snapshot(self, *, market: str, symbol: str) -> dict[str, Any]:
        self.calls.append((market, symbol))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _client(store: _FakeOrderbookStore) -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)
    app.dependency_overrides[get_mobile_session] = _session_override
    app.dependency_overrides[get_orderbook_store] = lambda: store
    return TestClient(app)


def test_orderbook_returns_ready_snapshot_from_injected_store() -> None:
    store = _FakeOrderbookStore(
        {
            "symbol": "005930",
            "market": "KRX",
            "ready": True,
            "asOf": "2026-08-28T05:30:00+00:00",
            "source": "NH_PLUG_WS",
            "asks": [
                {"price": "260500", "volume": "1234"},
                {"price": "261000", "volume": "2345"},
            ],
            "bids": [
                {"price": "260000", "volume": "5678"},
                {"price": "259500", "volume": "6789"},
            ],
            "totalAskVolume": "43210",
            "totalBidVolume": "98765",
        }
    )

    with _client(store) as client:
        response = client.get(
            "/api/v1/market/orderbook?market=krx&symbol=005930"
        )

    assert response.status_code == 200
    assert response.json() == store.response
    assert store.calls == [("KRX", "005930")]


def test_orderbook_returns_not_ready_without_snapshot() -> None:
    store = _FakeOrderbookStore(
        {
            "symbol": "005930",
            "market": "KRX",
            "ready": False,
            "asOf": None,
            "source": "NH_PLUG_WS",
            "asks": [],
            "bids": [],
            "totalAskVolume": "0",
            "totalBidVolume": "0",
        }
    )

    with _client(store) as client:
        response = client.get(
            "/api/v1/market/orderbook?market=KRX&symbol=005930"
        )

    assert response.status_code == 200
    assert response.json() == store.response


@pytest.mark.parametrize(
    ("market", "symbol"),
    [
        ("NYSE", "005930"),
        ("KRX", "5930"),
        ("KRX", "A05930"),
    ],
)
def test_orderbook_rejects_non_krx_six_digit_keys(
    market: str,
    symbol: str,
) -> None:
    store = _FakeOrderbookStore()

    with _client(store) as client:
        response = client.get(
            "/api/v1/market/orderbook",
            params={"market": market, "symbol": symbol},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "NH PLUG 실시간 호가는 KRX 6자리 종목코드만 지원합니다.",
        }
    }
    assert store.calls == []


def test_orderbook_returns_409_when_data_channel_is_unavailable() -> None:
    store = _FakeOrderbookStore(
        error=MobileApiError(
            409,
            "BROKER_NOT_CONNECTED",
            "NH PLUG 실시간 호가 인증을 확인하지 못했습니다.",
        )
    )

    with _client(store) as client:
        response = client.get(
            "/api/v1/market/orderbook?market=KRX&symbol=005930"
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "BROKER_NOT_CONNECTED",
            "message": "NH PLUG 실시간 호가 인증을 확인하지 못했습니다.",
        }
    }


def test_nh_ob_push_maps_official_49_field_ladder_to_rest_contract() -> None:
    body: dict[str, str] = {
        "code": "005930",
        "hotime": "14:30:00",
        "offer": "101",
        "bid": "100",
        "offerrem": "11",
        "bidrem": "21",
        "P_offer": "102",
        "P_bid": "99",
        "P_offerrem": "12",
        "P_bidrem": "22",
        "S_offer": "103",
        "S_bid": "98",
        "S_offerrem": "13",
        "S_bidrem": "23",
        "T_offerrem": "155",
        "T_bidrem": "255",
        "volume": "0",
        "mid_prc": "100.5",
        "mid_offerrem": "0",
        "mid_bidrem": "0",
        "kospigb": "1",
    }
    for level in range(4, 11):
        body[f"S{level}_offer"] = str(100 + level)
        body[f"S{level}_bid"] = str(101 - level)
        body[f"S{level}_offerrem"] = str(10 + level)
        body[f"S{level}_bidrem"] = str(20 + level)

    snapshot = _snapshot_from_push(
        {
            "header": {"tr_cd": "ob", "tr_key": "005930"},
            "body": body,
        },
        received_at=datetime(2026, 8, 28, 5, 30, tzinfo=UTC),
    )

    assert snapshot is not None
    assert snapshot["asks"] == [
        {"price": str(price), "volume": str(volume)}
        for price, volume in zip(range(101, 111), range(11, 21), strict=True)
    ]
    assert snapshot["bids"] == [
        {"price": str(price), "volume": str(volume)}
        for price, volume in zip(range(100, 90, -1), range(21, 31), strict=True)
    ]
    assert snapshot["totalAskVolume"] == "155"
    assert snapshot["totalBidVolume"] == "255"
    assert snapshot["asOf"] == "2026-08-28T05:30:00+00:00"


def test_subscription_message_uses_access_token_and_official_ob_shape() -> None:
    payload = json.loads(
        _subscription_message(
            token="access-token",
            symbol="005930",
            subscribe=True,
        )
    )

    assert payload == {
        "header": {"token": "access-token", "tr_type": "1"},
        "body": {"tr_cd": "ob", "tr_key": "005930"},
    }


def test_inactive_symbol_is_unsubscribed_and_its_snapshot_is_removed() -> None:
    class _FakeConnection:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

    async def scenario() -> None:
        store = NHOrderbookSnapshotStore()
        connection = _FakeConnection()
        store._connection = connection  # type: ignore[assignment]
        store._connection_token = "access-token"
        store._subscribed.add("005930")
        store._last_requested["005930"] = time.monotonic() - 61
        store._snapshots["005930"] = {"symbol": "005930"}

        await store._expire_subscriptions(  # type: ignore[arg-type]
            connection,
            "access-token",
        )

        assert store._last_requested == {}
        assert store._snapshots == {}
        assert store._subscribed == set()
        assert [json.loads(message) for message in connection.sent] == [
            {
                "header": {"token": "access-token", "tr_type": "2"},
                "body": {"tr_cd": "ob", "tr_key": "005930"},
            }
        ]

    asyncio.run(scenario())
