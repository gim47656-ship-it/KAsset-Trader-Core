"""KAsset Android 알림용 Firebase Cloud Messaging HTTP v1 발송 모듈.

Deliberately no Firebase Admin SDK: the whole outbound path is an OAuth 2.0
service-account JWT assertion signed with the already-present PyJWT/cryptography
stack plus two ``httpx`` calls. That keeps the dependency surface unchanged and
keeps every byte of the request under this module's control.

Secrecy contract for this module: the service-account JSON, its private key, the
minted OAuth access token, the ``Authorization`` header, FCM registration tokens
and their fingerprints, and raw provider response bodies never reach a log
record, an exception message, a task return value, or a database column. Only
whitelisted Firebase error codes and HTTP status numbers do.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

import httpx
import jwt
from jwt.algorithms import get_default_algorithms
from pydantic import SecretStr
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import KST
from app.extensions.kasset.api.daily_routine_schemas import DailyRoutineAlert
from app.extensions.kasset.daily_routine_service import daily_routine_service
from app.extensions.kasset.models import (
    AndroidPaperOrder,
    KAssetDeviceSession,
    KAssetPushDelivery,
)
from app.models.symbol_master import SymbolMaster
from app.models.trading import User, UserRole

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URI: Final = "https://oauth2.googleapis.com/token"
FCM_SEND_URL_TEMPLATE: Final = (
    "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
)
_FCM_SCOPE: Final = "https://www.googleapis.com/auth/firebase.messaging"
_JWT_BEARER_GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_TTL_SECONDS: Final = 3600
# Refresh slightly early so a token never expires mid-batch.
_ACCESS_TOKEN_SKEW_SECONDS: Final = 60

NOTIFICATION_CHANNEL_ID: Final = "kasset_price_alerts"
ORDER_EXECUTION_CHANNEL_ID: Final = "kasset_order_executions"
PUSH_PAYLOAD_VERSION: Final = "1"
PUSH_PAYLOAD_TYPE: Final = "PRICE_ALERT"
ORDER_EXECUTION_PAYLOAD_TYPE: Final = "ORDER_EXECUTION"
ORDER_EXECUTION_TITLE: Final = "자동주문 체결"
PUSH_ALERT_KINDS: Final = ("RAPID_RISE", "RAPID_FALL")

MAX_ATTEMPTS: Final = 5
_RETRY_BACKOFF_SECONDS: Final = (60, 300, 900, 3600)
_PENDING_RECLAIM_AFTER: Final = timedelta(seconds=_RETRY_BACKOFF_SECONDS[0])

# Every value that may be written to ``last_error_code`` or a log record.
# An unrecognized provider code collapses to ``UNKNOWN`` so no provider text
# can reach storage.
_KNOWN_FCM_ERROR_CODES: Final = frozenset(
    {
        "UNSPECIFIED_ERROR",
        "INVALID_ARGUMENT",
        "UNREGISTERED",
        "SENDER_ID_MISMATCH",
        "QUOTA_EXCEEDED",
        "UNAVAILABLE",
        "INTERNAL",
        "THIRD_PARTY_AUTH_ERROR",
    }
)
# Firebase has permanently disowned this token: it is safe, and required, to
# stop addressing it. Everything else keeps the token.
_TOKEN_INVALID_CODES: Final = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH"})
_TRANSIENT_CODES: Final = frozenset({"QUOTA_EXCEEDED", "UNAVAILABLE", "INTERNAL"})

UNKNOWN_ERROR_CODE: Final = "UNKNOWN"
NETWORK_ERROR_CODE: Final = "NETWORK_ERROR"
OAUTH_ERROR_CODE: Final = "OAUTH_FAILED"


class FcmConfigurationError(Exception):
    """The configured service-account material cannot be used.

    Carries only a short reason token; never the offending value.
    """


class DeliveryOutcome(StrEnum):
    SENT = "SENT"
    TOKEN_INVALID = "TOKEN_INVALID"
    TRANSIENT = "TRANSIENT"
    CONFIGURATION = "CONFIGURATION"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class ServiceAccountCredentials:
    project_id: str
    client_email: str
    private_key: str
    token_uri: str


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: DeliveryOutcome
    error_code: str | None = None
    http_status: int | None = None


def _mixed_notification_payload(
    *,
    token: str,
    title: str,
    body: str,
    data: Mapping[str, str],
    collapse_key: str,
    channel_id: str,
) -> dict[str, Any]:
    """Android 공통 mixed notification/data FCM envelope를 만든다."""

    return {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": dict(data),
            "android": {
                "priority": "high",
                "collapse_key": collapse_key,
                "notification": {"channel_id": channel_id},
            },
        }
    }


@dataclass(frozen=True, slots=True)
class PushMessage:
    # Excluded from repr so an accidental "%s" of this message in a log record
    # or traceback cannot print the registration token.
    token: str = field(repr=False)
    title: str
    body: str
    alert_id: str
    kind: str
    market: str
    symbol: str

    def collapse_key(self) -> str:
        # Collapsing on the symbol+direction (not the alert id, which churns
        # with every quote tick) means a device that was offline wakes to one
        # notification per symbol, not a backlog.
        return f"{self.kind}:{self.market}:{self.symbol}"

    def as_v1_payload(self) -> dict[str, Any]:
        return _mixed_notification_payload(
            token=self.token,
            title=self.title,
            body=self.body,
            data={
                "version": PUSH_PAYLOAD_VERSION,
                "type": PUSH_PAYLOAD_TYPE,
                "alertId": self.alert_id,
                "kind": self.kind,
                "market": self.market,
                "symbol": self.symbol,
            },
            collapse_key=self.collapse_key(),
            channel_id=NOTIFICATION_CHANNEL_ID,
        )


@dataclass(frozen=True, slots=True)
class OrderExecutionPushMessage:
    token: str = field(repr=False)
    body: str
    order_id: str
    market: str
    symbol: str
    name: str
    side: str
    quantity: str
    price: str

    def as_v1_payload(self) -> dict[str, Any]:
        return _mixed_notification_payload(
            token=self.token,
            title=ORDER_EXECUTION_TITLE,
            body=self.body,
            data={
                "version": PUSH_PAYLOAD_VERSION,
                "type": ORDER_EXECUTION_PAYLOAD_TYPE,
                "orderId": self.order_id,
                "market": self.market,
                "symbol": self.symbol,
                "name": self.name,
                "side": self.side,
                "quantity": self.quantity,
                "price": self.price,
                "origin": "AUTO_PAPER",
            },
            collapse_key=f"{ORDER_EXECUTION_PAYLOAD_TYPE}:{self.order_id}",
            channel_id=ORDER_EXECUTION_CHANNEL_ID,
        )


def load_service_account_credentials(
    raw: SecretStr | None = None,
) -> ServiceAccountCredentials | None:
    """Decode the configured base64 service-account JSON.

    Returns ``None`` when nothing is configured so the caller stays disabled and
    performs no outbound request. Raises :class:`FcmConfigurationError` with a
    bare reason token when material is present but unusable.
    """

    configured = (
        raw if raw is not None else settings.KASSET_FIREBASE_SERVICE_ACCOUNT_JSON_B64
    )
    if configured is None:
        return None
    encoded = configured.get_secret_value().strip()
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise FcmConfigurationError("service_account_not_base64") from None
    try:
        document = json.loads(decoded)
    except json.JSONDecodeError:
        raise FcmConfigurationError("service_account_not_json") from None
    if not isinstance(document, dict):
        raise FcmConfigurationError("service_account_not_object")
    if document.get("type") != "service_account":
        raise FcmConfigurationError("service_account_wrong_type")

    def _required(name: str) -> str:
        value = document.get(name)
        if not isinstance(value, str) or not value.strip():
            raise FcmConfigurationError(f"service_account_missing_{name}")
        return value

    token_uri = document.get("token_uri")
    return ServiceAccountCredentials(
        project_id=_required("project_id"),
        client_email=_required("client_email"),
        private_key=_required("private_key"),
        token_uri=(
            token_uri
            if isinstance(token_uri, str) and token_uri.strip()
            else OAUTH_TOKEN_URI
        ),
    )


def _assert_rs256_available() -> None:
    if "RS256" not in get_default_algorithms():
        # PyJWT only registers RS256 when a crypto backend is installed. Fail
        # closed rather than emitting an unsigned or malformed assertion.
        raise FcmConfigurationError("rs256_backend_missing")


class FcmClient:
    """Minimal HTTP v1 sender with a process-local OAuth access-token cache."""

    def __init__(
        self,
        credentials: ServiceAccountCredentials,
        *,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout_seconds or settings.KASSET_FCM_TIMEOUT_SECONDS
        self._transport = transport
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._http: httpx.AsyncClient | None = None

    @property
    def project_id(self) -> str:
        return self._credentials.project_id

    def _http_client(self) -> httpx.AsyncClient:
        # One connection pool for the whole batch: a cycle can address every
        # registered device, and a fresh client per message would mean a fresh
        # TLS handshake per message.
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            )
        return self._http

    async def aclose(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    def _build_assertion(self, *, issued_at: int) -> str:
        _assert_rs256_available()
        claims = {
            "iss": self._credentials.client_email,
            "scope": _FCM_SCOPE,
            "aud": self._credentials.token_uri,
            "iat": issued_at,
            "exp": issued_at + _ASSERTION_TTL_SECONDS,
        }
        try:
            return jwt.encode(claims, self._credentials.private_key, algorithm="RS256")
        except Exception:
            # The private key itself must not surface through the traceback.
            raise FcmConfigurationError("service_account_key_unusable") from None

    async def _access_token_value(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        cached = self._access_token
        if cached is not None and now < self._access_token_expires_at:
            return cached
        assertion = self._build_assertion(issued_at=int(time.time()))
        response = await client.post(
            self._credentials.token_uri,
            data={"grant_type": _JWT_BEARER_GRANT, "assertion": assertion},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise _OAuthFailure(response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise _OAuthFailure(response.status_code) from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise _OAuthFailure(response.status_code)
        expires_in = payload.get("expires_in")
        lifetime = (
            expires_in if isinstance(expires_in, int) and expires_in > 0 else 3600
        )
        self._access_token = token
        self._access_token_expires_at = now + max(
            lifetime - _ACCESS_TOKEN_SKEW_SECONDS, 1
        )
        return token

    def invalidate_access_token(self) -> None:
        self._access_token = None
        self._access_token_expires_at = 0.0

    async def send(
        self, message: PushMessage | OrderExecutionPushMessage
    ) -> SendResult:
        url = FCM_SEND_URL_TEMPLATE.format(project_id=self._credentials.project_id)
        client = self._http_client()
        try:
            access_token = await self._access_token_value(client)
            response = await client.post(
                url,
                json=message.as_v1_payload(),
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except _OAuthFailure as failure:
            self.invalidate_access_token()
            return SendResult(
                DeliveryOutcome.CONFIGURATION,
                OAUTH_ERROR_CODE,
                failure.status_code,
            )
        except FcmConfigurationError as err:
            return SendResult(DeliveryOutcome.CONFIGURATION, str(err))
        except httpx.HTTPError:
            # Covers timeouts, DNS, connection resets. The token stays valid.
            return SendResult(DeliveryOutcome.TRANSIENT, NETWORK_ERROR_CODE)
        return self._classify(response)

    def _classify(self, response: httpx.Response) -> SendResult:
        status = response.status_code
        if status == 200:
            return SendResult(DeliveryOutcome.SENT, None, status)
        error_code = _extract_error_code(response)
        # An explicit FcmError code outranks the HTTP status. SENDER_ID_MISMATCH
        # really does arrive as 403, and reading that as "our credential is
        # broken" would keep a token Firebase will never accept again.
        if error_code in _TOKEN_INVALID_CODES:
            return SendResult(DeliveryOutcome.TOKEN_INVALID, error_code, status)
        if status in (401, 403):
            # Our own credential is rejected; no user token is at fault.
            self.invalidate_access_token()
            return SendResult(DeliveryOutcome.CONFIGURATION, error_code, status)
        if status == 429 or status >= 500 or error_code in _TRANSIENT_CODES:
            return SendResult(DeliveryOutcome.TRANSIENT, error_code, status)
        # Remaining 4xx (notably INVALID_ARGUMENT, which is as often a payload
        # bug as a token one) fails this delivery and keeps the token.
        return SendResult(DeliveryOutcome.TERMINAL, error_code, status)


class _OAuthFailure(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("oauth_failed")
        self.status_code = status_code


def _extract_error_code(response: httpx.Response) -> str:
    """Read the Firebase ``FcmError`` code, collapsing anything unknown.

    The raw body is never returned, stored, or logged.
    """

    try:
        payload = response.json()
    except ValueError:
        return UNKNOWN_ERROR_CODE
    if not isinstance(payload, dict):
        return UNKNOWN_ERROR_CODE
    error = payload.get("error")
    if not isinstance(error, dict):
        return UNKNOWN_ERROR_CODE
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if not str(detail.get("@type", "")).endswith("FcmError"):
                continue
            code = detail.get("errorCode")
            if isinstance(code, str) and code in _KNOWN_FCM_ERROR_CODES:
                return code
            return UNKNOWN_ERROR_CODE
    status = error.get("status")
    if isinstance(status, str) and status in _KNOWN_FCM_ERROR_CODES:
        return status
    return UNKNOWN_ERROR_CODE


def dedupe_key(*, routine_date: date, kind: str, market: str, symbol: str) -> str:
    """One push per device, per symbol, per direction, per KST day."""

    material = "|".join((routine_date.isoformat(), kind, market, symbol))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def order_execution_dedupe_key(*, order_id: str) -> str:
    """PAPER 주문별로 기기당 하나인 전달 슬롯 키를 만든다."""

    material = "|".join((ORDER_EXECUTION_PAYLOAD_TYPE, order_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Target:
    session_id: str
    owner_user_id: int
    token: str
    token_hash: str


@dataclass(frozen=True, slots=True)
class _OrderExecutionRetryBatch:
    owner_user_id: int
    order_id: str
    targets: tuple[_Target, ...]


async def _push_targets(
    db: AsyncSession,
    *,
    now: datetime,
    owner_user_id: int | None = None,
) -> list[_Target]:
    """기존 조건을 지키며, 필요하면 단일 소유자의 기기로 제한한다."""

    statement = (
        select(
            KAssetDeviceSession.id,
            KAssetDeviceSession.owner_user_id,
            KAssetDeviceSession.fcm_token,
            KAssetDeviceSession.fcm_token_hash,
        )
        .join(User, User.id == KAssetDeviceSession.owner_user_id)
        .where(
            KAssetDeviceSession.revoked_at.is_(None),
            KAssetDeviceSession.expires_at > now,
            KAssetDeviceSession.fcm_token.is_not(None),
            KAssetDeviceSession.fcm_token_hash.is_not(None),
            User.is_active.is_(True),
            User.role.in_((UserRole.trader, UserRole.admin)),
        )
    )
    if owner_user_id is not None:
        statement = statement.where(KAssetDeviceSession.owner_user_id == owner_user_id)
    rows = (await db.execute(statement.order_by(KAssetDeviceSession.id))).all()
    return [
        _Target(
            session_id=str(session_id),
            owner_user_id=int(target_owner_user_id),
            token=str(token),
            token_hash=str(token_hash),
        )
        for session_id, target_owner_user_id, token, token_hash in rows
    ]


def _price_alerts(alerts: Sequence[DailyRoutineAlert]) -> list[DailyRoutineAlert]:
    """Only the ±5% price routines that are still beyond the threshold.

    News alerts are out of scope for push. Recovered alerts stay in the app's
    day list but must not trigger a push for a move that is already over.
    """

    return [
        alert
        for alert in alerts
        if alert.kind in PUSH_ALERT_KINDS
        and alert.market is not None
        and alert.symbol is not None
        and not alert.recovered
    ]


# The title already carries name, symbol, and rate. The body stays fixed so a
# lock screen never adds holdings or account detail the title did not show.
NOTIFICATION_BODY: Final = (
    "관심종목 일간 등락 알림입니다. 앱에서 근거와 시각을 확인하세요."
)


async def _claim_delivery(
    db: AsyncSession,
    *,
    target: _Target,
    routine_date: date,
    key: str,
    alert_id: str,
    kind: str,
    market: str,
    symbol: str,
    now: datetime,
) -> KAssetPushDelivery | None:
    """전달 슬롯을 예약하고, 재시도 가능한 기존 행만 반환한다."""

    statement = (
        pg_insert(KAssetPushDelivery)
        .values(
            device_session_id=target.session_id,
            routine_date=routine_date,
            dedupe_key=key,
            alert_id=alert_id,
            kind=kind,
            market=market,
            symbol=symbol,
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                KAssetPushDelivery.device_session_id,
                KAssetPushDelivery.dedupe_key,
            ]
        )
        .returning(KAssetPushDelivery.id)
    )
    inserted = (await db.execute(statement)).scalar_one_or_none()
    await db.commit()
    if inserted is not None:
        return await db.get(KAssetPushDelivery, inserted)

    existing = await db.scalar(
        select(KAssetPushDelivery)
        .where(
            KAssetPushDelivery.device_session_id == target.session_id,
            KAssetPushDelivery.dedupe_key == key,
        )
        .with_for_update(skip_locked=True)
    )
    if existing is None:
        return None
    retry_due = (
        existing.status == "retry"
        and existing.attempt_count < MAX_ATTEMPTS
        and (existing.next_attempt_at is None or existing.next_attempt_at <= now)
    )
    stale_pending = (
        existing.status == "pending"
        and existing.attempt_count == 0
        and existing.created_at <= now - _PENDING_RECLAIM_AFTER
    )
    if not retry_due and not stale_pending:
        return None
    existing.alert_id = alert_id
    return existing


async def _record_result(
    db: AsyncSession,
    *,
    delivery: KAssetPushDelivery,
    result: SendResult,
    now: datetime,
) -> None:
    delivery.attempt_count += 1
    delivery.last_error_code = result.error_code
    if result.outcome is DeliveryOutcome.SENT:
        delivery.status = "sent"
        delivery.delivered_at = now
        delivery.next_attempt_at = None
        delivery.last_error_code = None
    elif result.outcome is DeliveryOutcome.TRANSIENT and (
        delivery.attempt_count < MAX_ATTEMPTS
    ):
        delivery.status = "retry"
        backoff = _RETRY_BACKOFF_SECONDS[
            min(delivery.attempt_count - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        ]
        delivery.next_attempt_at = now + timedelta(seconds=backoff)
    elif result.outcome is DeliveryOutcome.CONFIGURATION:
        # An operator problem, not this device's. Leave it retryable without
        # burning the attempt budget against the user.
        delivery.attempt_count -= 1
        delivery.status = "retry"
        delivery.next_attempt_at = now + timedelta(seconds=_RETRY_BACKOFF_SECONDS[0])
    else:
        delivery.status = "failed"
        delivery.next_attempt_at = None
    await db.commit()


async def _discard_invalid_token(
    db: AsyncSession, *, target: _Target, now: datetime
) -> bool:
    """Clear the token only if the session still holds the one we just sent.

    Android may have rotated and re-registered while the request was in flight;
    deleting the fresh token would silently stop that device's alerts.
    """

    record = await db.scalar(
        select(KAssetDeviceSession)
        .where(KAssetDeviceSession.id == target.session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if record is None or record.fcm_token_hash != target.token_hash:
        await db.commit()
        return False
    record.fcm_token = None
    record.fcm_token_hash = None
    record.fcm_token_updated_at = now
    await db.commit()
    return True


def _resolve_sender(
    client: FcmClient | None,
) -> tuple[FcmClient | None, dict[str, object] | None]:
    if not settings.KASSET_FCM_ENABLED:
        return None, {"enabled": False, "reason": "disabled"}
    if client is not None:
        return client, None
    try:
        credentials = load_service_account_credentials()
    except FcmConfigurationError as err:
        logger.error("kasset FCM push credentials unusable: %s", err)
        return None, {"enabled": False, "reason": str(err)}
    if credentials is None:
        return None, {"enabled": False, "reason": "credentials_missing"}
    try:
        _assert_rs256_available()
    except FcmConfigurationError as err:
        logger.error("kasset FCM push cannot sign assertions: %s", err)
        return None, {"enabled": False, "reason": str(err)}
    return FcmClient(credentials), None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _grouped_decimal_text(value: Decimal) -> str:
    text = format(value, ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _order_execution_body(
    *,
    name: str,
    market: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
) -> str:
    side_label = "매수" if side == "BUY" else "매도"
    price_text = (
        f"${price:,.2f}" if market == "US" else f"{_grouped_decimal_text(price)}원"
    )
    return f"{name} {_decimal_text(quantity)}주 {side_label} · {price_text}"


async def _order_execution_name(
    db: AsyncSession,
    *,
    order: AndroidPaperOrder,
) -> str:
    symbol = str(order.symbol).strip()
    stored_name = (order.name or "").strip()
    if stored_name and stored_name != symbol:
        return stored_name
    master_name = await db.scalar(
        select(SymbolMaster.name).where(
            SymbolMaster.market == order.market,
            SymbolMaster.symbol == order.symbol,
        )
    )
    resolved_name = (master_name or "").strip()
    return resolved_name if resolved_name and resolved_name != symbol else symbol


async def dispatch_price_alert_pushes(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    client: FcmClient | None = None,
) -> dict[str, object]:
    """Send today's outstanding ±5% alerts to every eligible device once.

    Performs no outbound request when the feature is off or unconfigured.
    """
    instant = now or datetime.now(UTC)

    sender, disabled = _resolve_sender(client)
    if disabled is not None:
        return disabled
    assert sender is not None

    try:
        return await _dispatch(db, sender=sender, instant=instant)
    finally:
        # Only close the pool this call created; an injected sender belongs to
        # its owner.
        if client is None:
            await sender.aclose()


async def _dispatch(
    db: AsyncSession, *, sender: FcmClient, instant: datetime
) -> dict[str, object]:
    targets = await _push_targets(db, now=instant)
    if not targets:
        return {"enabled": True, "targets": 0, "sent": 0, "failed": 0, "retry": 0}

    routine_date = instant.astimezone(KST).date()
    alerts_by_owner: dict[int, list[DailyRoutineAlert]] = {}
    counts = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0, "tokensCleared": 0}

    for target in targets:
        owner_alerts = alerts_by_owner.get(target.owner_user_id)
        if owner_alerts is None:
            owner_alerts = _price_alerts(
                await daily_routine_service.get_alerts(
                    db, target.owner_user_id, now=instant
                )
            )
            alerts_by_owner[target.owner_user_id] = owner_alerts
        if not owner_alerts:
            continue

        for alert in owner_alerts:
            assert alert.market is not None and alert.symbol is not None
            delivery = await _claim_delivery(
                db,
                target=target,
                routine_date=routine_date,
                key=dedupe_key(
                    routine_date=routine_date,
                    kind=alert.kind,
                    market=alert.market,
                    symbol=alert.symbol,
                ),
                alert_id=alert.id,
                kind=alert.kind,
                market=alert.market,
                symbol=alert.symbol,
                now=instant,
            )
            if delivery is None:
                counts["skipped"] += 1
                continue
            result = await sender.send(
                PushMessage(
                    token=target.token,
                    title=alert.headline,
                    body=NOTIFICATION_BODY,
                    alert_id=alert.id,
                    kind=alert.kind,
                    market=alert.market,
                    symbol=alert.symbol,
                )
            )
            await _record_result(db, delivery=delivery, result=result, now=instant)
            if result.outcome is DeliveryOutcome.SENT:
                counts["sent"] += 1
                continue
            logger.warning(
                "kasset FCM push not delivered: deliveryId=%s outcome=%s "
                "errorCode=%s httpStatus=%s",
                delivery.id,
                result.outcome.value,
                result.error_code,
                result.http_status,
            )
            if result.outcome is DeliveryOutcome.CONFIGURATION:
                # Our own credential, not this device. Every remaining send
                # this cycle would fail identically, so stop instead of
                # hammering Firebase once per registered device.
                counts["retry"] += 1
                return {
                    "enabled": True,
                    "targets": len(targets),
                    "aborted": "configuration",
                    **counts,
                }
            if result.outcome is DeliveryOutcome.TOKEN_INVALID:
                if await _discard_invalid_token(db, target=target, now=instant):
                    counts["tokensCleared"] += 1
                counts["failed"] += 1
                break
            if delivery.status == "retry":
                counts["retry"] += 1
            else:
                counts["failed"] += 1

    return {"enabled": True, "targets": len(targets), **counts}


async def dispatch_order_execution_pushes(
    db: AsyncSession,
    *,
    owner_user_id: int,
    order_id: str,
    now: datetime | None = None,
    client: FcmClient | None = None,
) -> dict[str, object]:
    """자동 PAPER 체결 한 건을 해당 소유자의 기기에만 발송한다."""

    instant = now or datetime.now(UTC)
    sender, disabled = _resolve_sender(client)
    if disabled is not None:
        return disabled
    assert sender is not None

    try:
        return await _dispatch_order_execution(
            db,
            sender=sender,
            owner_user_id=owner_user_id,
            order_id=order_id,
            instant=instant,
        )
    finally:
        if client is None:
            await sender.aclose()


async def _dispatch_order_execution(
    db: AsyncSession,
    *,
    sender: FcmClient,
    owner_user_id: int,
    order_id: str,
    instant: datetime,
    targets: Sequence[_Target] | None = None,
) -> dict[str, object]:
    if targets is None:
        targets = await _push_targets(
            db,
            now=instant,
            owner_user_id=owner_user_id,
        )
    counts = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0, "tokensCleared": 0}
    if not targets:
        return {"enabled": True, "targets": 0, **counts}

    order = await db.scalar(
        select(AndroidPaperOrder).where(
            AndroidPaperOrder.owner_user_id == owner_user_id,
            AndroidPaperOrder.id == order_id,
        )
    )
    if order is None:
        return {
            "enabled": True,
            "targets": len(targets),
            "reason": "order_not_found",
            **counts,
        }
    if order.average_fill_price is None or Decimal(order.filled_quantity) <= 0:
        return {
            "enabled": True,
            "targets": len(targets),
            "reason": "order_not_filled",
            **counts,
        }

    market = str(order.market).upper()
    symbol = str(order.symbol)
    side = str(order.side).upper()
    quantity = Decimal(order.filled_quantity)
    price = Decimal(order.average_fill_price)
    name = await _order_execution_name(db, order=order)
    routine_date = instant.astimezone(KST).date()
    key = order_execution_dedupe_key(order_id=order.id)
    body = _order_execution_body(
        name=name,
        market=market,
        side=side,
        quantity=quantity,
        price=price,
    )

    for target in targets:
        delivery = await _claim_delivery(
            db,
            target=target,
            routine_date=routine_date,
            key=key,
            alert_id=order.id,
            kind=ORDER_EXECUTION_PAYLOAD_TYPE,
            market=market,
            symbol=symbol,
            now=instant,
        )
        if delivery is None:
            counts["skipped"] += 1
            continue
        result = await sender.send(
            OrderExecutionPushMessage(
                token=target.token,
                body=body,
                order_id=order.id,
                market=market,
                symbol=symbol,
                name=name,
                side=side,
                quantity=_decimal_text(quantity),
                price=_decimal_text(price),
            )
        )
        await _record_result(db, delivery=delivery, result=result, now=instant)
        if result.outcome is DeliveryOutcome.SENT:
            counts["sent"] += 1
            continue
        logger.warning(
            "kasset order execution FCM push not delivered: deliveryId=%s "
            "outcome=%s errorCode=%s httpStatus=%s",
            delivery.id,
            result.outcome.value,
            result.error_code,
            result.http_status,
        )
        if result.outcome is DeliveryOutcome.CONFIGURATION:
            counts["retry"] += 1
            return {
                "enabled": True,
                "targets": len(targets),
                "aborted": "configuration",
                **counts,
            }
        if result.outcome is DeliveryOutcome.TOKEN_INVALID:
            if await _discard_invalid_token(db, target=target, now=instant):
                counts["tokensCleared"] += 1
            counts["failed"] += 1
        elif delivery.status == "retry":
            counts["retry"] += 1
        else:
            counts["failed"] += 1

    return {"enabled": True, "targets": len(targets), **counts}


async def _order_execution_retry_batches(
    db: AsyncSession,
    *,
    instant: datetime,
) -> list[_OrderExecutionRetryBatch]:
    stale_pending_before = instant - _PENDING_RECLAIM_AFTER
    rows = (
        await db.execute(
            select(
                KAssetDeviceSession.owner_user_id,
                KAssetPushDelivery.alert_id,
                KAssetDeviceSession.id,
                KAssetDeviceSession.fcm_token,
                KAssetDeviceSession.fcm_token_hash,
            )
            .join(
                KAssetDeviceSession,
                KAssetDeviceSession.id == KAssetPushDelivery.device_session_id,
            )
            .join(User, User.id == KAssetDeviceSession.owner_user_id)
            .where(
                KAssetPushDelivery.kind == ORDER_EXECUTION_PAYLOAD_TYPE,
                or_(
                    and_(
                        KAssetPushDelivery.status == "retry",
                        KAssetPushDelivery.attempt_count < MAX_ATTEMPTS,
                        or_(
                            KAssetPushDelivery.next_attempt_at.is_(None),
                            KAssetPushDelivery.next_attempt_at <= instant,
                        ),
                    ),
                    and_(
                        KAssetPushDelivery.status == "pending",
                        KAssetPushDelivery.attempt_count == 0,
                        KAssetPushDelivery.created_at <= stale_pending_before,
                    ),
                ),
                KAssetDeviceSession.revoked_at.is_(None),
                KAssetDeviceSession.expires_at > instant,
                KAssetDeviceSession.fcm_token.is_not(None),
                KAssetDeviceSession.fcm_token_hash.is_not(None),
                User.is_active.is_(True),
                User.role.in_((UserRole.trader, UserRole.admin)),
            )
            .order_by(
                KAssetDeviceSession.owner_user_id,
                KAssetPushDelivery.alert_id,
                KAssetPushDelivery.id,
            )
        )
    ).all()
    grouped: dict[tuple[int, str], list[_Target]] = {}
    for owner_user_id, order_id, session_id, token, token_hash in rows:
        grouped.setdefault((int(owner_user_id), str(order_id)), []).append(
            _Target(
                session_id=str(session_id),
                owner_user_id=int(owner_user_id),
                token=str(token),
                token_hash=str(token_hash),
            )
        )
    return [
        _OrderExecutionRetryBatch(
            owner_user_id=owner_user_id,
            order_id=order_id,
            targets=tuple(targets),
        )
        for (owner_user_id, order_id), targets in grouped.items()
    ]


async def _dispatch_order_execution_retries(
    db: AsyncSession,
    *,
    sender: FcmClient,
    instant: datetime,
) -> dict[str, object]:
    batches = await _order_execution_retry_batches(db, instant=instant)
    counts = {
        "orders": len(batches),
        "targets": sum(len(batch.targets) for batch in batches),
        "sent": 0,
        "failed": 0,
        "retry": 0,
        "skipped": 0,
        "tokensCleared": 0,
    }
    for batch in batches:
        result = await _dispatch_order_execution(
            db,
            sender=sender,
            owner_user_id=batch.owner_user_id,
            order_id=batch.order_id,
            instant=instant,
            targets=batch.targets,
        )
        for count_field in ("sent", "failed", "retry", "skipped", "tokensCleared"):
            value = result.get(count_field)
            if isinstance(value, int):
                counts[count_field] += value
        if result.get("aborted") == "configuration":
            return {"enabled": True, "aborted": "configuration", **counts}
    return {"enabled": True, **counts}


async def dispatch_scheduled_pushes(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    client: FcmClient | None = None,
) -> dict[str, object]:
    """가격 알림과 기한이 된 체결 알림 재시도를 한 스위프에서 처리한다."""

    instant = now or datetime.now(UTC)
    sender, disabled = _resolve_sender(client)
    if disabled is not None:
        return disabled
    assert sender is not None

    try:
        price_alerts = await _dispatch(db, sender=sender, instant=instant)
        if price_alerts.get("aborted") == "configuration":
            order_executions: dict[str, object] = {
                "enabled": True,
                "orders": 0,
                "targets": 0,
                "sent": 0,
                "failed": 0,
                "retry": 0,
                "skipped": "configuration",
                "tokensCleared": 0,
            }
        else:
            order_executions = await _dispatch_order_execution_retries(
                db,
                sender=sender,
                instant=instant,
            )
        return {**price_alerts, "orderExecutions": order_executions}
    finally:
        if client is None:
            await sender.aclose()


__all__ = [
    "DeliveryOutcome",
    "FcmClient",
    "FcmConfigurationError",
    "NOTIFICATION_BODY",
    "NOTIFICATION_CHANNEL_ID",
    "ORDER_EXECUTION_CHANNEL_ID",
    "OrderExecutionPushMessage",
    "PushMessage",
    "SendResult",
    "ServiceAccountCredentials",
    "dedupe_key",
    "dispatch_order_execution_pushes",
    "dispatch_price_alert_pushes",
    "dispatch_scheduled_pushes",
    "load_service_account_credentials",
    "order_execution_dedupe_key",
]
