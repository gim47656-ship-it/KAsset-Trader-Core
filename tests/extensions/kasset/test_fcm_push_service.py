"""Wire and failure-classification contract for the FCM HTTP v1 sender.

Everything here runs against ``httpx.MockTransport``: the real request that
would reach Google is built, signed, and inspected, but no socket is opened.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.core.config import settings
from app.extensions.kasset import fcm_push_service as push
from app.extensions.kasset.fcm_push_service import (
    DeliveryOutcome,
    FcmClient,
    FcmConfigurationError,
    OrderExecutionPushMessage,
    PushMessage,
    ServiceAccountCredentials,
    dedupe_key,
    dispatch_order_execution_pushes,
    dispatch_price_alert_pushes,
    load_service_account_credentials,
    order_execution_dedupe_key,
)

PROJECT_ID = "kasset-trader-e17c3"
CLIENT_EMAIL = "push@kasset-trader-e17c3.iam.gserviceaccount.com"
ACCESS_TOKEN = "ya29.mock-access-token-value"
DEVICE_TOKEN = "device-registration-token-" + "z" * 40
SEND_URL = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture
def credentials(private_key_pem: str) -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        project_id=PROJECT_ID,
        client_email=CLIENT_EMAIL,
        private_key=private_key_pem,
        token_uri=push.OAUTH_TOKEN_URI,
    )


def _service_account_b64(private_key_pem: str, **overrides: object) -> SecretStr:
    document: dict[str, object] = {
        "type": "service_account",
        "project_id": PROJECT_ID,
        "client_email": CLIENT_EMAIL,
        "private_key": private_key_pem,
        "private_key_id": "abc123",
    }
    document.update(overrides)
    raw = json.dumps(document).encode("utf-8")
    return SecretStr(base64.b64encode(raw).decode("ascii"))


def _message(**overrides: str) -> PushMessage:
    fields: dict[str, str] = {
        "token": DEVICE_TOKEN,
        "title": "삼성전자(005930) +5.20% 급등",
        "body": push.NOTIFICATION_BODY,
        "alert_id": "price:abc123def456abc123def456",
        "kind": "RAPID_RISE",
        "market": "KRX",
        "symbol": "005930",
    }
    fields.update(overrides)
    return PushMessage(**fields)  # type: ignore[arg-type]


def _transport(
    send_handler: Callable[[httpx.Request], httpx.Response],
    *,
    recorded: list[httpx.Request] | None = None,
    oauth_status: int = 200,
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        if str(request.url) == push.OAUTH_TOKEN_URI:
            if oauth_status != 200:
                return httpx.Response(oauth_status, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                },
            )
        return send_handler(request)

    return httpx.MockTransport(handle)


def _accepted(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"name": "projects/x/messages/1"})


def _fcm_error(status: int, error_code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": "sanitized provider message",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                        "errorCode": error_code,
                    }
                ],
            }
        },
    )


# --------------------------------------------------------------------------
# credential loading
# --------------------------------------------------------------------------


def test_absent_credentials_are_disabled_not_an_error() -> None:
    assert load_service_account_credentials(None) is None
    assert load_service_account_credentials(SecretStr("")) is None
    assert load_service_account_credentials(SecretStr("   ")) is None


def test_credentials_decode_from_base64_service_account_json(
    private_key_pem: str,
) -> None:
    loaded = load_service_account_credentials(_service_account_b64(private_key_pem))

    assert loaded is not None
    assert loaded.project_id == PROJECT_ID
    assert loaded.client_email == CLIENT_EMAIL
    assert loaded.token_uri == push.OAUTH_TOKEN_URI


def test_malformed_credentials_raise_a_reason_without_echoing_the_material(
    private_key_pem: str,
) -> None:
    secret_marker = "SUPER-SECRET-KEY-MATERIAL"
    cases = {
        "service_account_not_base64": SecretStr(f"not-base64-{secret_marker}!!"),
        "service_account_not_json": SecretStr(
            base64.b64encode(f"plain {secret_marker}".encode()).decode()
        ),
        "service_account_wrong_type": _service_account_b64(
            private_key_pem, type="authorized_user"
        ),
        "service_account_missing_client_email": _service_account_b64(
            private_key_pem, client_email=""
        ),
        "service_account_missing_private_key": _service_account_b64(
            private_key_pem, private_key=None
        ),
    }
    for reason, material in cases.items():
        with pytest.raises(FcmConfigurationError) as raised:
            load_service_account_credentials(material)
        assert str(raised.value) == reason
        assert secret_marker not in str(raised.value)
        assert private_key_pem not in str(raised.value)


# --------------------------------------------------------------------------
# OAuth assertion + HTTP v1 request shape
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_assertion_is_a_scoped_service_account_jwt(
    credentials: ServiceAccountCredentials, private_key_pem: str
) -> None:
    requests: list[httpx.Request] = []
    client = FcmClient(
        credentials,
        transport=_transport(
            _accepted,
            recorded=requests,
        ),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.SENT
    oauth_request = requests[0]
    assert str(oauth_request.url) == push.OAUTH_TOKEN_URI
    form = dict(
        pair.split("=", 1) for pair in oauth_request.content.decode().split("&")
    )
    assert form["grant_type"] == (
        "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
    )
    public_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    ).public_key()
    claims = jwt.decode(
        httpx.QueryParams(oauth_request.content.decode())["assertion"],
        public_key,
        algorithms=["RS256"],
        audience=push.OAUTH_TOKEN_URI,
    )
    assert claims["iss"] == CLIENT_EMAIL
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert claims["exp"] - claims["iat"] == 3600


@pytest.mark.asyncio
async def test_send_posts_the_http_v1_notification_and_data_contract(
    credentials: ServiceAccountCredentials,
) -> None:
    requests: list[httpx.Request] = []
    client = FcmClient(
        credentials,
        transport=_transport(
            _accepted,
            recorded=requests,
        ),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.SENT
    send_request = requests[1]
    assert str(send_request.url) == SEND_URL
    assert send_request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    payload = json.loads(send_request.content)["message"]
    assert payload["token"] == DEVICE_TOKEN
    assert payload["notification"] == {
        "title": "삼성전자(005930) +5.20% 급등",
        "body": push.NOTIFICATION_BODY,
    }
    # Android reads only strings out of an FCM data map.
    assert payload["data"] == {
        "version": "1",
        "type": "PRICE_ALERT",
        "alertId": "price:abc123def456abc123def456",
        "kind": "RAPID_RISE",
        "market": "KRX",
        "symbol": "005930",
    }
    assert all(isinstance(value, str) for value in payload["data"].values())
    assert payload["android"]["notification"]["channel_id"] == (
        push.NOTIFICATION_CHANNEL_ID
    )
    # Collapsing on symbol+direction, never the churning alert id.
    assert payload["android"]["collapse_key"] == "RAPID_RISE:KRX:005930"
    assert "click_action" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_send_posts_the_order_execution_notification_and_data_contract(
    credentials: ServiceAccountCredentials,
) -> None:
    requests: list[httpx.Request] = []
    client = FcmClient(
        credentials,
        transport=_transport(
            _accepted,
            recorded=requests,
        ),
    )
    message = OrderExecutionPushMessage(
        token=DEVICE_TOKEN,
        body="메리츠금융지주 3주 매수 · 135,100원",
        order_id="4dd8953f-1111-2222-3333-444444444444",
        market="KRX",
        symbol="138040",
        name="메리츠금융지주",
        side="BUY",
        quantity="3",
        price="135100",
    )
    try:
        result = await client.send(message)
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.SENT
    payload = json.loads(requests[1].content)["message"]
    assert payload["notification"] == {
        "title": "자동주문 체결",
        "body": "메리츠금융지주 3주 매수 · 135,100원",
    }
    assert payload["data"] == {
        "version": "1",
        "type": "ORDER_EXECUTION",
        "orderId": "4dd8953f-1111-2222-3333-444444444444",
        "market": "KRX",
        "symbol": "138040",
        "name": "메리츠금융지주",
        "side": "BUY",
        "quantity": "3",
        "price": "135100",
        "origin": "AUTO_PAPER",
    }
    assert all(isinstance(value, str) for value in payload["data"].values())
    assert payload["android"]["notification"]["channel_id"] == (
        push.ORDER_EXECUTION_CHANNEL_ID
    )
    assert payload["android"]["collapse_key"] == (
        "ORDER_EXECUTION:4dd8953f-1111-2222-3333-444444444444"
    )
    assert "click_action" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("market", "side", "quantity", "price", "expected"),
    [
        (
            "KRX",
            "BUY",
            Decimal("3.00000000"),
            Decimal("135100.00000000"),
            "메리츠금융지주 3주 매수 · 135,100원",
        ),
        (
            "US",
            "SELL",
            Decimal("1.50000000"),
            Decimal("135.1"),
            "Apple 1.5주 매도 · $135.10",
        ),
    ],
)
def test_order_execution_body_uses_market_currency_format(
    market: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    expected: str,
) -> None:
    assert (
        push._order_execution_body(
            name="메리츠금융지주" if market == "KRX" else "Apple",
            market=market,
            side=side,
            quantity=quantity,
            price=price,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_access_token_is_minted_once_and_reused(
    credentials: ServiceAccountCredentials,
) -> None:
    requests: list[httpx.Request] = []
    client = FcmClient(
        credentials,
        transport=_transport(
            _accepted,
            recorded=requests,
        ),
    )
    try:
        for _ in range(3):
            assert (await client.send(_message())).outcome is DeliveryOutcome.SENT
    finally:
        await client.aclose()

    oauth_calls = [r for r in requests if str(r.url) == push.OAUTH_TOKEN_URI]
    assert len(oauth_calls) == 1
    assert len(requests) == 4


# --------------------------------------------------------------------------
# failure classification
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        # Firebase returns SENDER_ID_MISMATCH as 403 and UNREGISTERED as 404;
        # the explicit FcmError code, not the status, decides.
        (404, "UNREGISTERED", DeliveryOutcome.TOKEN_INVALID),
        (403, "SENDER_ID_MISMATCH", DeliveryOutcome.TOKEN_INVALID),
        (429, "QUOTA_EXCEEDED", DeliveryOutcome.TRANSIENT),
        (503, "UNAVAILABLE", DeliveryOutcome.TRANSIENT),
        (500, "INTERNAL", DeliveryOutcome.TRANSIENT),
        (400, "INVALID_ARGUMENT", DeliveryOutcome.TERMINAL),
        (401, "UNSPECIFIED_ERROR", DeliveryOutcome.CONFIGURATION),
        (403, "UNSPECIFIED_ERROR", DeliveryOutcome.CONFIGURATION),
        (401, "THIRD_PARTY_AUTH_ERROR", DeliveryOutcome.CONFIGURATION),
    ],
)
async def test_provider_failures_are_classified_for_token_retention(
    credentials: ServiceAccountCredentials,
    status: int,
    error_code: str,
    expected: DeliveryOutcome,
) -> None:
    client = FcmClient(
        credentials,
        transport=_transport(lambda _request: _fcm_error(status, error_code)),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is expected
    assert result.http_status == status
    # Whitelisted codes survive verbatim so operators can act on them.
    assert result.error_code == error_code


@pytest.mark.asyncio
async def test_unknown_provider_error_code_collapses_to_a_safe_token(
    credentials: ServiceAccountCredentials,
) -> None:
    leaked = "user@example.com quota for project 99 exhausted"
    client = FcmClient(
        credentials,
        transport=_transport(
            lambda _request: httpx.Response(
                400,
                json={
                    "error": {
                        "status": leaked,
                        "message": leaked,
                        "details": [
                            {
                                "@type": (
                                    "type.googleapis.com/"
                                    "google.firebase.fcm.v1.FcmError"
                                ),
                                "errorCode": leaked,
                            }
                        ],
                    }
                },
            )
        ),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.error_code == push.UNKNOWN_ERROR_CODE
    assert result.outcome is DeliveryOutcome.TERMINAL


@pytest.mark.asyncio
async def test_network_failure_is_transient_and_keeps_the_token(
    credentials: ServiceAccountCredentials,
) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    client = FcmClient(credentials, transport=_transport(explode))
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.TRANSIENT
    assert result.error_code == push.NETWORK_ERROR_CODE


@pytest.mark.asyncio
async def test_oauth_rejection_is_a_configuration_fault_not_a_bad_token(
    credentials: ServiceAccountCredentials,
) -> None:
    client = FcmClient(
        credentials,
        transport=_transport(
            lambda _request: httpx.Response(200, json={"name": "ok"}),
            oauth_status=401,
        ),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.CONFIGURATION
    assert result.error_code == push.OAUTH_ERROR_CODE
    assert result.http_status == 401


@pytest.mark.asyncio
async def test_no_secret_reaches_the_log_stream(
    credentials: ServiceAccountCredentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client = FcmClient(
        credentials,
        transport=_transport(lambda _request: _fcm_error(404, "UNREGISTERED")),
    )
    try:
        result = await client.send(_message())
    finally:
        await client.aclose()

    assert result.outcome is DeliveryOutcome.TOKEN_INVALID
    emitted = caplog.text
    for secret in (
        DEVICE_TOKEN,
        ACCESS_TOKEN,
        credentials.private_key,
        "sanitized provider message",
    ):
        assert secret not in emitted


# --------------------------------------------------------------------------
# dedupe key
# --------------------------------------------------------------------------


def test_dedupe_key_is_one_slot_per_day_kind_market_symbol() -> None:
    base = {
        "routine_date": date(2026, 9, 1),
        "kind": "RAPID_RISE",
        "market": "KRX",
        "symbol": "005930",
    }
    assert dedupe_key(**base) == dedupe_key(**base)
    assert dedupe_key(**{**base, "kind": "RAPID_FALL"}) != dedupe_key(**base)
    assert dedupe_key(**{**base, "market": "US"}) != dedupe_key(**base)
    assert dedupe_key(**{**base, "symbol": "000660"}) != dedupe_key(**base)
    assert dedupe_key(**{**base, "routine_date": date(2026, 9, 2)}) != dedupe_key(
        **base
    )
    assert len(dedupe_key(**base)) == 64


def test_order_execution_dedupe_key_is_unique_per_order() -> None:
    first = order_execution_dedupe_key(order_id="paper-order-1")
    assert first == order_execution_dedupe_key(order_id="paper-order-1")
    assert first != order_execution_dedupe_key(order_id="paper-order-2")
    assert len(first) == 64


# --------------------------------------------------------------------------
# fail-closed dispatch (no database access on either branch)
# --------------------------------------------------------------------------


class _ForbiddenSession:
    """Any database use here would be a bug: both branches return first."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"disabled dispatch touched the database: {name}")


@pytest.mark.asyncio
async def test_disabled_dispatch_sends_nothing_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_FCM_ENABLED", False)
    monkeypatch.setattr(
        settings, "KASSET_FIREBASE_SERVICE_ACCOUNT_JSON_B64", SecretStr("x")
    )

    session = _ForbiddenSession()
    result = await dispatch_price_alert_pushes(session)  # type: ignore[arg-type]
    order_result = await dispatch_order_execution_pushes(
        session,  # type: ignore[arg-type]
        owner_user_id=1,
        order_id="paper-order-1",
    )

    assert result == {"enabled": False, "reason": "disabled"}
    assert order_result == {"enabled": False, "reason": "disabled"}


@pytest.mark.asyncio
async def test_enabled_but_unconfigured_dispatch_makes_no_outbound_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_FCM_ENABLED", True)
    monkeypatch.setattr(settings, "KASSET_FIREBASE_SERVICE_ACCOUNT_JSON_B64", None)

    session = _ForbiddenSession()
    result = await dispatch_price_alert_pushes(session)  # type: ignore[arg-type]
    order_result = await dispatch_order_execution_pushes(
        session,  # type: ignore[arg-type]
        owner_user_id=1,
        order_id="paper-order-1",
    )

    assert result == {"enabled": False, "reason": "credentials_missing"}
    assert order_result == {"enabled": False, "reason": "credentials_missing"}


@pytest.mark.asyncio
async def test_enabled_with_broken_credentials_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "KASSET_FCM_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "KASSET_FIREBASE_SERVICE_ACCOUNT_JSON_B64",
        SecretStr("this-is-not-base64!!"),
    )

    session = _ForbiddenSession()
    result = await dispatch_price_alert_pushes(session)  # type: ignore[arg-type]

    assert result == {"enabled": False, "reason": "service_account_not_base64"}
