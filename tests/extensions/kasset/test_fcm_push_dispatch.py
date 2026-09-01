"""Delivery-ledger contract for the scheduled price-alert push cycle.

The real ``DailyRoutineService`` drives these runs — only the quote source and
the HTTP transport are stubbed — so the ±5% decision under test is the same one
``GET /api/v1/ai/daily-routine`` returns.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import KST
from app.extensions.kasset import fcm_push_service as push
from app.extensions.kasset.api.auth import MobileAuthService
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.push_tokens import hash_fcm_token, register_push_token
from app.extensions.kasset.api.schemas import RegisterRequest
from app.extensions.kasset.daily_routine_service import DailyRoutineService
from app.extensions.kasset.fcm_push_service import (
    FcmClient,
    ServiceAccountCredentials,
    dedupe_key,
    dispatch_price_alert_pushes,
)
from app.extensions.kasset.models import (
    KAssetDailyRoutineSetting,
    KAssetDeviceSession,
    KAssetPushDelivery,
)
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserWatchItem,
)

_PASSWORD = "Dispatch-Owner-secret-1!"
_MOMENT = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
_QUOTE_TIME = datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
# +5.20 crosses the rise threshold, -6.00 the fall threshold, +1.00 neither.
_RATES = ("5.20", "-6.00", "1.00")
_PRICES = ("105.20", "94.00", "101.00")

TOKEN_A = "dispatch-token-a-" + "a" * 40
TOKEN_B = "dispatch-token-b-" + "b" * 40
ACCESS_TOKEN = "ya29.dispatch-access-token"


@pytest.fixture(scope="module")
def credentials() -> ServiceAccountCredentials:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return ServiceAccountCredentials(
        project_id="kasset-trader-e17c3",
        client_email="push@kasset-trader-e17c3.iam.gserviceaccount.com",
        private_key=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii"),
        token_uri=push.OAUTH_TOKEN_URI,
    )


class _Firebase:
    """Scripted Firebase double that records every message it is handed."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.responses: list[httpx.Response] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if str(request.url) == push.OAUTH_TOKEN_URI:
                return httpx.Response(
                    200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599}
                )
            self.messages.append(json.loads(request.content)["message"])
            if self.responses:
                return self.responses.pop(0)
            return httpx.Response(200, json={"name": "projects/x/messages/1"})

        return httpx.MockTransport(handle)

    def client(self, credentials: ServiceAccountCredentials) -> FcmClient:
        return FcmClient(credentials, transport=self.transport())


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": "sanitized",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                        "errorCode": code,
                    }
                ],
            }
        },
    )


@pytest_asyncio.fixture
async def push_world(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, object]]:
    suffix = uuid4().hex[:10].upper()
    auth = MobileAuthService()
    usernames = [f"dispatch-a-{suffix.lower()}", f"dispatch-b-{suffix.lower()}"]

    exchange = Exchange(
        code=f"PD{suffix}", name="Push Dispatch Exchange", tz="Asia/Seoul"
    )
    db_session.add(exchange)
    await db_session.flush()
    instruments = [
        Instrument(
            exchange_id=exchange.id,
            symbol=f"P{index}{suffix}",
            name=f"푸시 종목 {index}",
            type=InstrumentType.equity_kr,
            base_currency="KRW",
            is_active=True,
        )
        for index in range(3)
    ]
    db_session.add_all(instruments)
    await db_session.flush()
    exchange_id = exchange.id
    instrument_ids = [instrument.id for instrument in instruments]
    symbols = [instrument.symbol for instrument in instruments]
    await db_session.commit()

    tokens_a = await auth.register(
        db_session,
        RegisterRequest(
            username=usernames[0],
            email=f"{usernames[0]}@example.com",
            password=_PASSWORD,
            deviceId="dispatch-a-phone",
            deviceName="A phone",
        ),
    )
    tokens_b = await auth.register(
        db_session,
        RegisterRequest(
            username=usernames[1],
            email=f"{usernames[1]}@example.com",
            password=_PASSWORD,
            deviceId="dispatch-b-phone",
            deviceName="B phone",
        ),
    )
    session_a = await auth.authenticate(db_session, tokens_a.access_token)
    session_b = await auth.authenticate(db_session, tokens_b.access_token)
    owner_a = session_a.user.id
    owner_b = session_b.user.id
    session_id_a = session_a.device_session.id
    session_id_b = session_b.device_session.id

    # Only owner A watches the moving symbols.
    db_session.add_all(
        UserWatchItem(
            user_id=owner_a,
            instrument_id=instrument_id,
            notify_cooldown=timedelta(hours=1),
            is_active=True,
        )
        for instrument_id in instrument_ids
    )
    for owner in (owner_a, owner_b):
        db_session.add(
            KAssetDailyRoutineSetting(
                owner_user_id=owner,
                routine_date=_MOMENT.astimezone(KST).date(),
                enabled_routines=["RAPID_RISE", "RAPID_FALL"],
                updated_at=_MOMENT,
            )
        )
    await db_session.commit()

    await register_push_token(
        db_session,
        session_id=session_id_a,
        owner_user_id=owner_a,
        device_id="dispatch-a-phone",
        token=TOKEN_A,
    )
    await register_push_token(
        db_session,
        session_id=session_id_b,
        owner_user_id=owner_b,
        device_id="dispatch-b-phone",
        token=TOKEN_B,
    )

    async def quotes(
        _db: AsyncSession, market: str, requested: Sequence[str]
    ) -> list[Quote]:
        assert market == "KRX"
        by_symbol = dict(zip(symbols, zip(_RATES, _PRICES, strict=True), strict=True))
        result = []
        for symbol in requested:
            rate, price = by_symbol[symbol]
            result.append(
                Quote(
                    broker="PAPER",
                    market="KRX",
                    symbol=symbol,
                    name=None,
                    currency="KRW",
                    price=price,
                    previous_close="100",
                    change_amount="0",
                    change_rate=rate,
                    session="AFTER_MARKET",
                    regular_close="100",
                    session_change_amount=rate,
                    session_change_rate=rate,
                    as_of=_QUOTE_TIME.isoformat(),
                    source="TOSS_OPENAPI",
                )
            )
        return result

    monkeypatch.setattr(
        push, "daily_routine_service", DailyRoutineService(quote_loader=quotes)
    )
    monkeypatch.setattr(settings, "KASSET_FCM_ENABLED", True)

    try:
        yield {
            "ownerA": owner_a,
            "ownerB": owner_b,
            "sessionIdA": session_id_a,
            "sessionIdB": session_id_b,
            "symbols": symbols,
            "usernames": usernames,
        }
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.username.in_(usernames)))
        await db_session.execute(
            delete(UserWatchItem).where(UserWatchItem.user_id.in_((owner_a, owner_b)))
        )
        await db_session.execute(
            delete(Instrument).where(Instrument.id.in_(instrument_ids))
        )
        await db_session.execute(delete(Exchange).where(Exchange.id == exchange_id))
        await db_session.commit()


async def _deliveries(
    db_session: AsyncSession, session_id: str
) -> list[KAssetPushDelivery]:
    return list(
        (
            await db_session.scalars(
                select(KAssetPushDelivery)
                .where(KAssetPushDelivery.device_session_id == session_id)
                .order_by(KAssetPushDelivery.symbol, KAssetPushDelivery.kind)
            )
        ).all()
    )


async def _token_of(db_session: AsyncSession, session_id: str) -> str | None:
    return await db_session.scalar(
        select(KAssetDeviceSession.fcm_token).where(
            KAssetDeviceSession.id == session_id
        )
    )


@pytest.mark.asyncio
async def test_only_the_watching_owner_receives_the_rapid_change_alerts(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    assert result["enabled"] is True
    assert result["sent"] == 2
    # +1.00% never crosses the threshold, so exactly the rise and the fall ship.
    assert {message["data"]["kind"] for message in firebase.messages} == {
        "RAPID_RISE",
        "RAPID_FALL",
    }
    assert {message["token"] for message in firebase.messages} == {TOKEN_A}

    session_a = str(push_world["sessionIdA"])
    rows = await _deliveries(db_session, session_a)
    assert [row.status for row in rows] == ["sent", "sent"]
    assert all(row.delivered_at is not None for row in rows)
    assert all(row.last_error_code is None for row in rows)
    assert {row.market for row in rows} == {"KRX"}

    # Owner B watches nothing, so nothing was addressed to that device.
    assert await _deliveries(db_session, str(push_world["sessionIdB"])) == []


@pytest.mark.asyncio
async def test_a_second_cycle_the_same_day_sends_nothing_again(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        first = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
        # Ten minutes later the quote has moved on, so the alert id differs.
        second = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT + timedelta(minutes=10), client=client
        )
    finally:
        await client.aclose()

    assert first["sent"] == 2
    assert second["sent"] == 0
    assert second["skipped"] == 2
    assert len(firebase.messages) == 2
    assert len(await _deliveries(db_session, str(push_world["sessionIdA"]))) == 2


@pytest.mark.asyncio
async def test_the_next_kst_day_is_a_fresh_dedupe_slot(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        await dispatch_price_alert_pushes(db_session, now=_MOMENT, client=client)
        tomorrow = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT + timedelta(days=1), client=client
        )
    finally:
        await client.aclose()

    assert tomorrow["sent"] == 2
    rows = await _deliveries(db_session, str(push_world["sessionIdA"]))
    assert len(rows) == 4
    assert len({row.routine_date for row in rows}) == 2
    assert len({row.dedupe_key for row in rows}) == 4


@pytest.mark.asyncio
async def test_unregistered_clears_the_token_that_was_actually_sent(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    firebase.responses = [_error(404, "UNREGISTERED")]
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    session_a = str(push_world["sessionIdA"])
    assert result["tokensCleared"] == 1
    assert await _token_of(db_session, session_a) is None
    # The rest of that device's alerts are abandoned, not retried blindly.
    assert len(firebase.messages) == 1
    rows = await _deliveries(db_session, session_a)
    assert [row.status for row in rows if row.last_error_code] == ["failed"]
    assert {row.last_error_code for row in rows if row.last_error_code} == {
        "UNREGISTERED"
    }
    # Another owner's token is untouched.
    assert await _token_of(db_session, str(push_world["sessionIdB"])) == TOKEN_B


@pytest.mark.asyncio
async def test_a_rejected_service_account_aborts_without_burning_more_sends(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    """A broken credential is global; it must not be retried per device."""

    firebase = _Firebase()
    firebase.responses = [_error(401, "UNSPECIFIED_ERROR")]
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    session_a = str(push_world["sessionIdA"])
    assert result["aborted"] == "configuration"
    assert result["sent"] == 0
    assert len(firebase.messages) == 1
    # No user's token is blamed for our own credential problem.
    assert result["tokensCleared"] == 0
    assert await _token_of(db_session, session_a) == TOKEN_A
    rows = await _deliveries(db_session, session_a)
    assert [row.status for row in rows] == ["retry"]
    # The attempt budget is not spent on an operator fault.
    assert rows[0].attempt_count == 0
    assert rows[0].last_error_code == "UNSPECIFIED_ERROR"


@pytest.mark.asyncio
async def test_a_token_rotated_mid_flight_survives_an_unregistered_reply(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    """The device re-registered while the request was out; keep the new token."""

    rotated = "dispatch-token-a-rotated-" + "r" * 30
    session_a = str(push_world["sessionIdA"])

    class _RotatingFirebase(_Firebase):
        def transport(self) -> httpx.MockTransport:
            inner = super().transport()

            async def handle(request: httpx.Request) -> httpx.Response:
                response = await inner.handle_async_request(request)
                if str(request.url) != push.OAUTH_TOKEN_URI:
                    await db_session.execute(
                        update(KAssetDeviceSession)
                        .where(KAssetDeviceSession.id == session_a)
                        .values(
                            fcm_token=rotated,
                            fcm_token_hash=hash_fcm_token(rotated),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    await db_session.commit()
                return response

            return httpx.MockTransport(handle)

    firebase = _RotatingFirebase()
    firebase.responses = [_error(404, "UNREGISTERED")]
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    assert result["tokensCleared"] == 0
    assert await _token_of(db_session, session_a) == rotated


@pytest.mark.asyncio
async def test_transient_failures_keep_the_token_and_schedule_a_retry(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    firebase.responses = [_error(503, "UNAVAILABLE"), _error(429, "QUOTA_EXCEEDED")]
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    session_a = str(push_world["sessionIdA"])
    assert result["retry"] == 2
    assert result["sent"] == 0
    assert result["tokensCleared"] == 0
    assert await _token_of(db_session, session_a) == TOKEN_A
    rows = await _deliveries(db_session, session_a)
    assert [row.status for row in rows] == ["retry", "retry"]
    assert all(row.attempt_count == 1 for row in rows)
    assert all(
        row.next_attempt_at is not None and row.next_attempt_at > _MOMENT
        for row in rows
    )
    assert {row.last_error_code for row in rows} == {"UNAVAILABLE", "QUOTA_EXCEEDED"}


@pytest.mark.asyncio
async def test_a_due_retry_is_resent_and_a_pending_backoff_is_not(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    firebase = _Firebase()
    firebase.responses = [_error(503, "UNAVAILABLE"), _error(503, "UNAVAILABLE")]
    client = firebase.client(credentials)
    try:
        await dispatch_price_alert_pushes(db_session, now=_MOMENT, client=client)
        too_soon = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT + timedelta(seconds=30), client=client
        )
        due = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT + timedelta(minutes=5), client=client
        )
    finally:
        await client.aclose()

    assert too_soon["sent"] == 0
    assert too_soon["skipped"] == 2
    assert due["sent"] == 2
    rows = await _deliveries(db_session, str(push_world["sessionIdA"]))
    assert [row.status for row in rows] == ["sent", "sent"]
    assert all(row.attempt_count == 2 for row in rows)


@pytest.mark.asyncio
async def test_revoked_and_expired_sessions_are_never_addressed(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    session_a = str(push_world["sessionIdA"])
    await db_session.execute(
        update(KAssetDeviceSession)
        .where(KAssetDeviceSession.id == session_a)
        .values(revoked_at=_MOMENT - timedelta(minutes=1))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        revoked_run = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
        await db_session.execute(
            update(KAssetDeviceSession)
            .where(KAssetDeviceSession.id == session_a)
            .values(revoked_at=None, expires_at=_MOMENT - timedelta(days=1))
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        expired_run = await dispatch_price_alert_pushes(
            db_session, now=_MOMENT, client=client
        )
    finally:
        await client.aclose()

    assert revoked_run["targets"] == 1
    assert expired_run["targets"] == 1
    # The single remaining target is owner B, who watches nothing.
    assert firebase.messages == []
    assert await _deliveries(db_session, session_a) == []


@pytest.mark.asyncio
async def test_dedupe_row_matches_the_published_key_and_no_secret_is_logged(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    firebase = _Firebase()
    firebase.responses = [_error(404, "UNREGISTERED")]
    client = firebase.client(credentials)
    try:
        await dispatch_price_alert_pushes(db_session, now=_MOMENT, client=client)
    finally:
        await client.aclose()

    rows = await _deliveries(db_session, str(push_world["sessionIdA"]))
    attempted = next(row for row in rows if row.last_error_code)
    assert attempted.dedupe_key == dedupe_key(
        routine_date=_MOMENT.astimezone(KST).date(),
        kind=attempted.kind,
        market=attempted.market,
        symbol=attempted.symbol,
    )

    emitted = caplog.text
    for secret in (
        TOKEN_A,
        TOKEN_B,
        ACCESS_TOKEN,
        credentials.private_key,
        "sanitized",
    ):
        assert secret not in emitted
    assert hash_fcm_token(TOKEN_A) not in emitted
