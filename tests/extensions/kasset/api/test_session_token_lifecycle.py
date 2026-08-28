"""Access-token acceptance for a device session that keeps refreshing.

A refresh rotates ``KAssetDeviceSession.refresh_token_hash``. Access tokens
must survive that rotation -- an Android client fires several requests in
parallel over one session, so invalidating in-flight tokens on every refresh
forced a full re-login. These tests pin both halves: rotation does not strand
an unexpired access token, and revoke/session-expiry/identity mismatch/forged
signature still fail closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.models import KAssetDeviceSession
from app.middleware.auth import AuthMiddleware
from app.models.trading import User

_PASSWORD = "Session-lifecycle-1!"


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_android_compat_api(app)
    app.add_middleware(AuthMiddleware)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = db_override
    transport = httpx.ASGITransport(
        app=app,
        # Per-test client host: the register/login routes are rate limited by
        # remote address, so tests must not share a bucket.
        client=(f"session-lifecycle-{uuid4().hex}", 443),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://kasset.test",
    ) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def created_usernames(db_session: AsyncSession) -> AsyncIterator[list[str]]:
    usernames: list[str] = []
    try:
        yield usernames
    finally:
        await db_session.rollback()
        if usernames:
            await db_session.execute(delete(User).where(User.username.in_(usernames)))
        await db_session.commit()


async def _register(
    client: httpx.AsyncClient,
    created_usernames: list[str],
    *,
    device_id: str,
    device_name: str = "Acceptance phone",
) -> dict[str, str]:
    username = f"session-life-{uuid4().hex[:16]}"
    created_usernames.append(username)
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": _PASSWORD,
            "deviceId": device_id,
            "deviceName": device_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claims(token: str) -> dict[str, object]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


def _access_token(claims: dict[str, object], *, secret: str) -> str:
    payload = {key: value for key, value in claims.items() if key != "exp"}
    payload["type"] = "access"
    payload["exp"] = datetime.now(UTC) + timedelta(minutes=5)
    return jwt.encode(payload, secret, algorithm=settings.ALGORITHM)


async def _session_row(
    db_session: AsyncSession, session_id: object
) -> KAssetDeviceSession:
    row = await db_session.scalar(
        select(KAssetDeviceSession).where(KAssetDeviceSession.id == session_id)
    )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_access_token_issued_before_refresh_still_authenticates(
    client: httpx.AsyncClient,
    created_usernames: list[str],
) -> None:
    tokens = await _register(client, created_usernames, device_id="rotation-device")

    rotated = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": tokens["refreshToken"]},
    )
    assert rotated.status_code == 200, rotated.text
    rotated_tokens = rotated.json()
    assert rotated_tokens["accessToken"] != tokens["accessToken"]

    # The pre-refresh access token has not expired, so it must keep working
    # alongside the freshly issued one.
    stale = await client.get("/api/v1/auth/me", headers=_bearer(tokens["accessToken"]))
    fresh = await client.get(
        "/api/v1/auth/me", headers=_bearer(rotated_tokens["accessToken"])
    )
    assert stale.status_code == 200, stale.text
    assert fresh.status_code == 200, fresh.text
    assert stale.json() == fresh.json()

    # Two rotations in a row must not retroactively kill the oldest token
    # either: only exp/revoke/session expiry end it.
    second = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": rotated_tokens["refreshToken"]},
    )
    assert second.status_code == 200, second.text
    still_valid = await client.get(
        "/api/v1/auth/me", headers=_bearer(tokens["accessToken"])
    )
    assert still_valid.status_code == 200, still_valid.text


@pytest.mark.asyncio
async def test_revoked_session_rejects_every_outstanding_access_token(
    client: httpx.AsyncClient,
    created_usernames: list[str],
) -> None:
    tokens = await _register(client, created_usernames, device_id="revoke-device")
    rotated = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": tokens["refreshToken"]},
    )
    assert rotated.status_code == 200, rotated.text
    rotated_tokens = rotated.json()

    revoked = await client.post(
        "/api/v1/auth/revoke",
        headers=_bearer(rotated_tokens["accessToken"]),
    )
    assert revoked.status_code == 204, revoked.text

    for token in (tokens["accessToken"], rotated_tokens["accessToken"]):
        response = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    replayed = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": rotated_tokens["refreshToken"]},
    )
    assert replayed.status_code == 401, replayed.text


@pytest.mark.asyncio
async def test_expired_device_session_rejects_unexpired_access_token(
    client: httpx.AsyncClient,
    created_usernames: list[str],
    db_session: AsyncSession,
) -> None:
    tokens = await _register(client, created_usernames, device_id="expiry-device")
    session_id = _claims(tokens["accessToken"])["sessionId"]

    row = await _session_row(db_session, session_id)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    response = await client.get(
        "/api/v1/auth/me", headers=_bearer(tokens["accessToken"])
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_inactive_user_rejects_unexpired_access_token(
    client: httpx.AsyncClient,
    created_usernames: list[str],
    db_session: AsyncSession,
) -> None:
    tokens = await _register(client, created_usernames, device_id="inactive-device")
    username = _claims(tokens["accessToken"])["sub"]

    user = await db_session.scalar(select(User).where(User.username == username))
    assert user is not None
    user.is_active = False
    await db_session.commit()

    response = await client.get(
        "/api/v1/auth/me", headers=_bearer(tokens["accessToken"])
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_device_owner_and_signature_mismatches_stay_unauthorized(
    client: httpx.AsyncClient,
    created_usernames: list[str],
) -> None:
    owner = await _register(client, created_usernames, device_id="owner-device")
    other = await _register(client, created_usernames, device_id="other-device")
    owner_claims = _claims(owner["accessToken"])
    other_claims = _claims(other["accessToken"])
    assert owner_claims["sessionId"] != other_claims["sessionId"]

    forged = {
        "another device id for the same session": _access_token(
            {**owner_claims, "deviceId": "not-the-enrolled-device"},
            secret=settings.SECRET_KEY,
        ),
        "another user claiming this session": _access_token(
            {
                **owner_claims,
                "sub": other_claims["sub"],
                "uid": other_claims["uid"],
            },
            secret=settings.SECRET_KEY,
        ),
        "another session id": _access_token(
            {**owner_claims, "sessionId": str(uuid4())},
            secret=settings.SECRET_KEY,
        ),
        "signed with a foreign key": _access_token(
            dict(owner_claims),
            secret=f"{settings.SECRET_KEY}-forged",
        ),
    }
    for reason, token in forged.items():
        response = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert response.status_code == 401, f"{reason}: {response.text}"
        assert response.json()["error"]["code"] == "UNAUTHORIZED", reason

    # Control: the untampered token for the same session still passes, so the
    # rejections above are caused by the mismatch and nothing else.
    control = await client.get("/api/v1/auth/me", headers=_bearer(owner["accessToken"]))
    assert control.status_code == 200, control.text


@pytest.mark.asyncio
async def test_refresh_accepts_only_the_current_token_hash(
    client: httpx.AsyncClient,
    created_usernames: list[str],
    db_session: AsyncSession,
) -> None:
    tokens = await _register(client, created_usernames, device_id="refresh-device")

    rotated = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": tokens["refreshToken"]},
    )
    assert rotated.status_code == 200, rotated.text
    rotated_tokens = rotated.json()
    assert rotated_tokens["refreshToken"] != tokens["refreshToken"]

    replayed = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": tokens["refreshToken"]},
    )
    assert replayed.status_code == 401, replayed.text
    assert replayed.json()["error"]["code"] == "UNAUTHORIZED"

    accepted = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": rotated_tokens["refreshToken"]},
    )
    assert accepted.status_code == 200, accepted.text

    session_id = _claims(tokens["accessToken"])["sessionId"]
    row = await _session_row(db_session, session_id)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    expired = await client.post(
        "/api/v1/auth/refresh",
        json={"refreshToken": accepted.json()["refreshToken"]},
    )
    assert expired.status_code == 401, expired.text
