"""Owner/device isolation contract for FCM registration token endpoints.

Exercised through the installed routes with real Bearer authentication so the
device-session binding — not a dependency override — decides which row a token
lands on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.api.auth import MobileAuthService
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.push_tokens import hash_fcm_token
from app.extensions.kasset.api.schemas import LoginRequest, RegisterRequest
from app.extensions.kasset.models import KAssetDeviceSession
from app.models.trading import User

_PASSWORD = "Push-Owner-secret-1!"
TOKEN_A = "fcm-token-owner-a-" + "x" * 40
TOKEN_B = "fcm-token-owner-b-" + "y" * 40


@pytest_asyncio.fixture
async def push_sessions(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    """Two owners; owner A also has a second device."""

    suffix = uuid4().hex[:12]
    auth = MobileAuthService()
    usernames = [f"push-a-{suffix}", f"push-b-{suffix}"]
    tokens_a_phone = await auth.register(
        db_session,
        RegisterRequest(
            username=usernames[0],
            email=f"{usernames[0]}@example.com",
            password=_PASSWORD,
            deviceId="shared-hardware-id",
            deviceName="A phone",
        ),
    )
    tokens_b_phone = await auth.register(
        db_session,
        RegisterRequest(
            username=usernames[1],
            email=f"{usernames[1]}@example.com",
            password=_PASSWORD,
            deviceId="shared-hardware-id",
            deviceName="B phone",
        ),
    )
    tokens_a_tablet = await auth.login(
        db_session,
        LoginRequest(
            username=usernames[0],
            password=_PASSWORD,
            deviceId="a-tablet-id",
            deviceName="A tablet",
        ),
    )
    session_a = await auth.authenticate(db_session, tokens_a_phone.access_token)
    session_b = await auth.authenticate(db_session, tokens_b_phone.access_token)
    session_a_tablet = await auth.authenticate(db_session, tokens_a_tablet.access_token)
    payload = {
        "auth": auth,
        "accessA": tokens_a_phone.access_token,
        "accessB": tokens_b_phone.access_token,
        "accessATablet": tokens_a_tablet.access_token,
        "sessionIdA": session_a.device_session.id,
        "sessionIdB": session_b.device_session.id,
        "sessionIdATablet": session_a_tablet.device_session.id,
        "sessionA": session_a,
        "usernames": usernames,
    }
    try:
        yield payload
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.username.in_(usernames)))
        await db_session.commit()


def _client(db_session: AsyncSession) -> httpx.AsyncClient:
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = db_override
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://kasset.test",
    )


async def _stored(
    db_session: AsyncSession, session_id: str
) -> tuple[str | None, str | None]:
    row = (
        await db_session.execute(
            select(
                KAssetDeviceSession.fcm_token,
                KAssetDeviceSession.fcm_token_hash,
            ).where(KAssetDeviceSession.id == session_id)
        )
    ).one_or_none()
    return (None, None) if row is None else (row[0], row[1])


@pytest.mark.asyncio
async def test_put_binds_token_to_the_calling_session_only(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    async with _client(db_session) as client:
        response = await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )

    assert response.status_code == 204
    # 204 carries no body at all: neither the token nor its fingerprint leaks.
    assert response.content == b""
    assert TOKEN_A not in response.text
    assert hash_fcm_token(TOKEN_A) not in response.text

    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (
        TOKEN_A,
        hash_fcm_token(TOKEN_A),
    )
    # The other owner sharing the same hardware id, and this owner's other
    # device, stay untouched.
    assert await _stored(db_session, str(push_sessions["sessionIdB"])) == (None, None)
    assert await _stored(db_session, str(push_sessions["sessionIdATablet"])) == (
        None,
        None,
    )


@pytest.mark.asyncio
async def test_registering_a_token_takes_it_from_its_previous_holder(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    """A reinstalled app can hand the same token to a different account."""

    async with _client(db_session) as client:
        first = await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )
        second = await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": f"Bearer {push_sessions['accessB']}"},
        )

    assert (first.status_code, second.status_code) == (204, 204)
    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (None, None)
    assert await _stored(db_session, str(push_sessions["sessionIdB"])) == (
        TOKEN_A,
        hash_fcm_token(TOKEN_A),
    )


@pytest.mark.asyncio
async def test_reregistering_the_same_token_on_the_same_session_is_stable(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    async with _client(db_session) as client:
        for _ in range(2):
            response = await client.put(
                "/api/v1/push/token",
                json={"token": TOKEN_A},
                headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
            )
            assert response.status_code == 204

    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (
        TOKEN_A,
        hash_fcm_token(TOKEN_A),
    )


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_scoped_to_the_calling_device(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    async with _client(db_session) as client:
        await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )
        await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_B},
            headers={"Authorization": f"Bearer {push_sessions['accessATablet']}"},
        )
        first = await client.delete(
            "/api/v1/push/token",
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )
        repeated = await client.delete(
            "/api/v1/push/token",
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )

    assert (first.status_code, repeated.status_code) == (204, 204)
    assert first.content == b""
    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (None, None)
    # The owner's other device keeps receiving alerts.
    assert await _stored(db_session, str(push_sessions["sessionIdATablet"])) == (
        TOKEN_B,
        hash_fcm_token(TOKEN_B),
    )


@pytest.mark.asyncio
async def test_token_endpoints_require_authentication(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    async with _client(db_session) as client:
        anonymous_put = await client.put("/api/v1/push/token", json={"token": TOKEN_A})
        anonymous_delete = await client.delete("/api/v1/push/token")
        bad_bearer = await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert anonymous_put.status_code == 401
    assert anonymous_delete.status_code == 401
    assert bad_bearer.status_code == 401
    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (None, None)


@pytest.mark.asyncio
async def test_put_rejects_blank_oversize_and_client_asserted_fields(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    headers = {"Authorization": f"Bearer {push_sessions['accessA']}"}
    async with _client(db_session) as client:
        missing = await client.put("/api/v1/push/token", json={}, headers=headers)
        empty = await client.put(
            "/api/v1/push/token", json={"token": ""}, headers=headers
        )
        blank = await client.put(
            "/api/v1/push/token", json={"token": "   "}, headers=headers
        )
        oversize = await client.put(
            "/api/v1/push/token", json={"token": "t" * 4097}, headers=headers
        )
        # deviceId is the server's decision, taken from the access token.
        spoofed_device = await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A, "deviceId": "someone-elses-device"},
            headers=headers,
        )

    assert [
        missing.status_code,
        empty.status_code,
        blank.status_code,
        oversize.status_code,
        spoofed_device.status_code,
    ] == [422, 422, 422, 422, 422]
    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (None, None)


@pytest.mark.asyncio
async def test_revoke_retires_the_token_of_that_device_only(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    async with _client(db_session) as client:
        await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_A},
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )
        await client.put(
            "/api/v1/push/token",
            json={"token": TOKEN_B},
            headers={"Authorization": f"Bearer {push_sessions['accessATablet']}"},
        )
        revoked = await client.post(
            "/api/v1/auth/revoke",
            headers={"Authorization": f"Bearer {push_sessions['accessA']}"},
        )

    assert revoked.status_code == 204
    assert await _stored(db_session, str(push_sessions["sessionIdA"])) == (None, None)
    assert await _stored(db_session, str(push_sessions["sessionIdATablet"])) == (
        TOKEN_B,
        hash_fcm_token(TOKEN_B),
    )


@pytest.mark.asyncio
async def test_token_is_absent_from_every_authenticated_response_body(
    db_session: AsyncSession,
    push_sessions: dict[str, object],
) -> None:
    headers = {"Authorization": f"Bearer {push_sessions['accessA']}"}
    async with _client(db_session) as client:
        await client.put("/api/v1/push/token", json={"token": TOKEN_A}, headers=headers)
        me = await client.get("/api/v1/auth/me", headers=headers)

    assert me.status_code == 200
    assert TOKEN_A not in me.text
    assert hash_fcm_token(TOKEN_A) not in me.text
