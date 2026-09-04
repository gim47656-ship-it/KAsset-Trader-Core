"""Delivery-ledger contract for the scheduled price-alert push cycle.

The real ``DailyRoutineService`` drives these runs — only the quote source and
the HTTP transport are stubbed — so the ±5% decision under test is the same one
``GET /api/v1/ai/daily-routine`` returns.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from app.extensions.kasset.api.daily_routine_schemas import DailyRoutineAlert
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.push_tokens import hash_fcm_token, register_push_token
from app.extensions.kasset.api.schemas import RegisterRequest
from app.extensions.kasset.daily_routine_service import (
    DailyRoutineService,
    price_alert_market_date,
)
from app.extensions.kasset.fcm_push_service import (
    FcmClient,
    ServiceAccountCredentials,
    dedupe_key,
    dispatch_order_execution_pushes,
    dispatch_price_alert_pushes,
    dispatch_scheduled_pushes,
    order_execution_dedupe_key,
)
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    AndroidPaperOrder,
    KAssetDailyRoutineSetting,
    KAssetDeviceSession,
    KAssetPushDelivery,
)
from app.models.paper_trading import PaperAccount
from app.models.symbol_master import SymbolMaster
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
            type=(InstrumentType.equity_us if index == 0 else InstrumentType.equity_kr),
            base_currency="USD" if index == 0 else "KRW",
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
        assert market in {"KRX", "US"}
        by_symbol = dict(zip(symbols, zip(_RATES, _PRICES, strict=True), strict=True))
        result = []
        for symbol in requested:
            rate, price = by_symbol[symbol]
            result.append(
                Quote(
                    broker="PAPER",
                    market=market,
                    symbol=symbol,
                    name=None,
                    currency="USD" if market == "US" else "KRW",
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


@asynccontextmanager
async def _filled_order(
    db_session: AsyncSession,
    *,
    owner_user_id: int,
) -> AsyncIterator[dict[str, object]]:
    symbol = f"P{uuid4().hex[:10].upper()}"
    order_id = str(uuid4())
    account = PaperAccount(
        name=f"KAsset retry push {uuid4().hex}",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    account_id = account.id
    db_session.add_all(
        [
            AndroidPaperAccount(
                owner_user_id=owner_user_id,
                paper_account_id=account_id,
            ),
            AndroidPaperOrder(
                id=order_id,
                owner_user_id=owner_user_id,
                client_order_id=f"ai-rec:{uuid4().hex}",
                paper_account_id=account_id,
                broker_order_id=f"PAPER-{uuid4().hex}",
                market="KRX",
                symbol=symbol,
                name=None,
                currency="KRW",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal("3"),
                status="FILLED",
                filled_quantity=Decimal("3"),
                average_fill_price=Decimal("135100"),
            ),
            SymbolMaster(
                market="KRX",
                symbol=symbol,
                name="메리츠금융지주",
                security_type="COMMON_STOCK",
                is_active=True,
            ),
        ]
    )
    await db_session.commit()
    try:
        yield {
            "id": order_id,
            "symbol": symbol,
        }
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(AndroidPaperOrder).where(AndroidPaperOrder.id == order_id)
        )
        await db_session.execute(
            delete(AndroidPaperAccount).where(
                AndroidPaperAccount.owner_user_id == owner_user_id,
                AndroidPaperAccount.paper_account_id == account_id,
            )
        )
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.id == account_id)
        )
        await db_session.execute(
            delete(SymbolMaster).where(
                SymbolMaster.market == "KRX",
                SymbolMaster.symbol == symbol,
            )
        )
        await db_session.commit()


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
    assert {row.market for row in rows} == {"KRX", "US"}

    # Owner B watches nothing, so nothing was addressed to that device.
    assert await _deliveries(db_session, str(push_world["sessionIdB"])) == []


@pytest.mark.asyncio
async def test_order_execution_is_owner_scoped_contract_exact_and_deduplicated(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    owner_a = int(push_world["ownerA"])
    session_a = str(push_world["sessionIdA"])
    symbol = f"P{uuid4().hex[:10].upper()}"
    order_id = str(uuid4())
    account = PaperAccount(
        name=f"KAsset order push {uuid4().hex}",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        cash_usd=Decimal("0"),
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    account_id = account.id
    db_session.add_all(
        [
            AndroidPaperAccount(
                owner_user_id=owner_a,
                paper_account_id=account_id,
            ),
            AndroidPaperOrder(
                id=order_id,
                owner_user_id=owner_a,
                client_order_id=f"ai-rec:{uuid4().hex}",
                paper_account_id=account_id,
                broker_order_id=f"PAPER-{uuid4().hex}",
                market="KRX",
                symbol=symbol,
                name=None,
                currency="KRW",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal("3"),
                status="FILLED",
                filled_quantity=Decimal("3"),
                average_fill_price=Decimal("135100"),
            ),
            SymbolMaster(
                market="KRX",
                symbol=symbol,
                name="메리츠금융지주",
                security_type="COMMON_STOCK",
                is_active=True,
            ),
        ]
    )
    await db_session.commit()

    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        first = await dispatch_order_execution_pushes(
            db_session,
            owner_user_id=owner_a,
            order_id=order_id,
            now=_MOMENT,
            client=client,
        )
        second = await dispatch_order_execution_pushes(
            db_session,
            owner_user_id=owner_a,
            order_id=order_id,
            now=_MOMENT + timedelta(minutes=1),
            client=client,
        )
        foreign_owner = await dispatch_order_execution_pushes(
            db_session,
            owner_user_id=int(push_world["ownerB"]),
            order_id=order_id,
            now=_MOMENT + timedelta(minutes=2),
            client=client,
        )

        assert first["sent"] == 1
        assert second["sent"] == 0
        assert second["skipped"] == 1
        assert foreign_owner["reason"] == "order_not_found"
        assert foreign_owner["sent"] == 0
        assert len(firebase.messages) == 1
        message = firebase.messages[0]
        assert message["token"] == TOKEN_A
        assert message["notification"] == {
            "title": "자동주문 체결",
            "body": "메리츠금융지주 3주 매수 · 135,100원",
        }
        assert message["data"] == {
            "version": "1",
            "type": "ORDER_EXECUTION",
            "orderId": order_id,
            "market": "KRX",
            "symbol": symbol,
            "name": "메리츠금융지주",
            "side": "BUY",
            "quantity": "3",
            "price": "135100",
            "origin": "AUTO_PAPER",
        }
        assert all(isinstance(value, str) for value in message["data"].values())
        assert message["android"]["notification"]["channel_id"] == (
            push.ORDER_EXECUTION_CHANNEL_ID
        )
        assert message["android"]["collapse_key"] == f"ORDER_EXECUTION:{order_id}"

        rows = await _deliveries(db_session, session_a)
        assert len(rows) == 1
        assert rows[0].alert_id == order_id
        assert rows[0].kind == "ORDER_EXECUTION"
        assert rows[0].dedupe_key == order_execution_dedupe_key(order_id=order_id)
        assert rows[0].status == "sent"
        assert await _deliveries(db_session, str(push_world["sessionIdB"])) == []
    finally:
        await client.aclose()
        await db_session.rollback()
        await db_session.execute(
            delete(AndroidPaperOrder).where(AndroidPaperOrder.id == order_id)
        )
        await db_session.execute(
            delete(AndroidPaperAccount).where(
                AndroidPaperAccount.owner_user_id == owner_a,
                AndroidPaperAccount.paper_account_id == account_id,
            )
        )
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.id == account_id)
        )
        await db_session.execute(
            delete(SymbolMaster).where(
                SymbolMaster.market == "KRX",
                SymbolMaster.symbol == symbol,
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_scheduled_sweep_retries_order_execution_once_and_keeps_price_alerts(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    owner_a = int(push_world["ownerA"])
    session_a = str(push_world["sessionIdA"])
    async with _filled_order(db_session, owner_user_id=owner_a) as order:
        order_id = str(order["id"])
        firebase = _Firebase()
        firebase.responses = [_error(503, "UNAVAILABLE")]
        client = firebase.client(credentials)
        try:
            initial = await dispatch_order_execution_pushes(
                db_session,
                owner_user_id=owner_a,
                order_id=order_id,
                now=_MOMENT,
                client=client,
            )
            due = await dispatch_scheduled_pushes(
                db_session,
                now=_MOMENT + timedelta(minutes=10),
                client=client,
            )
            after_success = await dispatch_scheduled_pushes(
                db_session,
                now=_MOMENT + timedelta(minutes=20),
                client=client,
            )
        finally:
            await client.aclose()

        order_messages = [
            message
            for message in firebase.messages
            if message["data"]["type"] == "ORDER_EXECUTION"
        ]
        assert initial["retry"] == 1
        assert due["sent"] == 2
        assert due["orderExecutions"]["sent"] == 1
        assert after_success["orderExecutions"]["sent"] == 0
        assert len(order_messages) == 2
        assert order_messages[0]["notification"] == order_messages[1]["notification"]
        assert order_messages[0]["data"] == order_messages[1]["data"]

        key = order_execution_dedupe_key(order_id=order_id)
        rows = [
            row
            for row in await _deliveries(db_session, session_a)
            if row.dedupe_key == key
        ]
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].attempt_count == 2


@pytest.mark.asyncio
async def test_scheduled_sweep_recovers_stale_pending_order_execution(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    owner_a = int(push_world["ownerA"])
    session_a = str(push_world["sessionIdA"])
    async with _filled_order(db_session, owner_user_id=owner_a) as order:
        order_id = str(order["id"])
        symbol = str(order["symbol"])
        key = order_execution_dedupe_key(order_id=order_id)
        db_session.add(
            KAssetPushDelivery(
                device_session_id=session_a,
                routine_date=_MOMENT.astimezone(KST).date(),
                dedupe_key=key,
                alert_id=order_id,
                kind="ORDER_EXECUTION",
                market="KRX",
                symbol=symbol,
                status="pending",
                attempt_count=0,
                next_attempt_at=_MOMENT - timedelta(minutes=2),
                created_at=_MOMENT - timedelta(minutes=2),
            )
        )
        await db_session.commit()

        firebase = _Firebase()
        client = firebase.client(credentials)
        try:
            result = await dispatch_scheduled_pushes(
                db_session,
                now=_MOMENT,
                client=client,
            )
        finally:
            await client.aclose()

        order_messages = [
            message
            for message in firebase.messages
            if message["data"]["type"] == "ORDER_EXECUTION"
        ]
        assert result["sent"] == 2
        assert result["orderExecutions"]["sent"] == 1
        assert len(order_messages) == 1
        delivery = await db_session.scalar(
            select(KAssetPushDelivery).where(
                KAssetPushDelivery.device_session_id == session_a,
                KAssetPushDelivery.dedupe_key == key,
            )
        )
        assert delivery is not None
        assert delivery.status == "sent"
        assert delivery.attempt_count == 1


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
async def test_kst_midnight_keeps_us_dedupe_slot_and_rolls_krx_slot(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
) -> None:
    before_midnight = datetime(2026, 9, 1, 14, 59, tzinfo=UTC)
    after_midnight = before_midnight + timedelta(minutes=2)
    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        first = await dispatch_price_alert_pushes(
            db_session, now=before_midnight, client=client
        )
        second = await dispatch_price_alert_pushes(
            db_session, now=after_midnight, client=client
        )
    finally:
        await client.aclose()

    assert first["sent"] == 2
    assert second["sent"] == 1
    assert second["skipped"] == 1
    assert len(firebase.messages) == 3

    rows = await _deliveries(db_session, str(push_world["sessionIdA"]))
    assert len(rows) == 3
    us_rows = [row for row in rows if row.market == "US"]
    krx_rows = [row for row in rows if row.market == "KRX"]
    assert len(us_rows) == 1
    assert len(krx_rows) == 2
    assert us_rows[0].routine_date.isoformat() == "2026-09-01"
    assert {row.routine_date.isoformat() for row in krx_rows} == {
        "2026-09-01",
        "2026-09-02",
    }


@pytest.mark.asyncio
async def test_crypto_delivery_uses_current_kst_date_despite_stale_alert_time(
    db_session: AsyncSession,
    push_world: dict[str, object],
    credentials: ServiceAccountCredentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_a = int(push_world["ownerA"])
    current = datetime(2026, 9, 1, 15, 1, tzinfo=UTC)
    stale_candle_time = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
    alert = DailyRoutineAlert(
        id="price:2026-09-02:RAPID_RISE:CRYPTO:KRW-BTC",
        kind="RAPID_RISE",
        headline="비트코인(KRW-BTC) +6.00% 급등",
        symbol="KRW-BTC",
        market="CRYPTO",
        source="UPBIT",
        occurred_at=stale_candle_time,
        detected_rate_pct="6.00",
        current_rate_pct="6.00",
        recovered=False,
        last_seen_at=stale_candle_time,
    )

    class _CryptoAlerts:
        async def get_alerts(
            self,
            _db: AsyncSession,
            owner_user_id: int,
            *,
            now: datetime,
        ) -> list[DailyRoutineAlert]:
            assert now == current
            return [alert] if owner_user_id == owner_a else []

    monkeypatch.setattr(push, "daily_routine_service", _CryptoAlerts())
    firebase = _Firebase()
    client = firebase.client(credentials)
    try:
        result = await dispatch_price_alert_pushes(
            db_session, now=current, client=client
        )
    finally:
        await client.aclose()

    assert result["sent"] == 1
    rows = await _deliveries(db_session, str(push_world["sessionIdA"]))
    assert len(rows) == 1
    assert rows[0].routine_date.isoformat() == "2026-09-02"
    assert rows[0].alert_id.startswith("price:2026-09-02:")


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
        routine_date=price_alert_market_date(attempted.market, _MOMENT),
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
