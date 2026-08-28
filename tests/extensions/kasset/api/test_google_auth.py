from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.installation import install_android_compat_api
from app.middleware.auth import AuthMiddleware
from app.models.trading import User, UserRole

_GOOGLE_CLIENT_ID = (
    "87055660911-049dp7t9frnr0dali9k60t71ulm8tjdm.apps.googleusercontent.com"
)


@pytest.fixture(scope="module")
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def google_verifier(
    monkeypatch: pytest.MonkeyPatch,
    rsa_key_pair,
) -> None:
    _, public_key = rsa_key_pair

    def signing_key_from_token(_client, _token: str) -> SimpleNamespace:
        return SimpleNamespace(key=public_key)

    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        signing_key_from_token,
    )
    monkeypatch.setattr(
        settings,
        "KASSET_GOOGLE_OAUTH_CLIENT_ID",
        _GOOGLE_CLIENT_ID,
    )


@pytest.fixture
def google_sub() -> str:
    return f"kasset-google-test-{uuid4().hex}"


@pytest_asyncio.fixture(autouse=True)
async def cleanup_google_user(
    db_session: AsyncSession,
    google_sub: str,
) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.google_sub == google_sub))
    await db_session.commit()


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
        client=(f"google-auth-test-{uuid4().hex}", 443),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://kasset.test",
    ) as test_client:
        yield test_client


def id_token(
    private_key,
    google_sub: str,
    **overrides: object,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": _GOOGLE_CLIENT_ID,
        "sub": google_sub,
        "email": f"{google_sub}@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "kasset-test-key"},
    )


def login_payload(token: str, *, device_id: str = "google-phone") -> dict[str, str]:
    return {
        "idToken": token,
        "deviceId": device_id,
        "deviceName": "Google test phone",
    }


@pytest.mark.asyncio
async def test_google_login_registers_user_issues_tokens_and_supports_auth_me(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(id_token(private_key, google_sub)),
    )

    assert response.status_code == 200
    tokens = response.json()
    assert set(tokens) == {
        "accessToken",
        "refreshToken",
        "accessTokenExpiresAt",
        "refreshTokenExpiresAt",
        "serverVersion",
    }

    user = await db_session.scalar(select(User).where(User.google_sub == google_sub))
    assert user is not None
    assert user.username == f"google-{google_sub}"[:32]
    assert user.email == f"{google_sub}@example.com"
    assert user.hashed_password is None
    assert user.role == UserRole.trader
    assert user.is_active is True
    assert user.nickname is not None
    assert user.nickname[-2:].isdigit()
    assert len(user.nickname) <= 16

    user.nickname = None
    await db_session.commit()
    await db_session.refresh(user)

    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    assert me.status_code == 200
    me_body = me.json()
    assert me_body == {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "nickname": user.nickname,
        "role": "trader",
    }
    assert me_body["nickname"] is not None
    assert await db_session.scalar(
        select(User.nickname).where(User.id == user.id)
    ) == me_body["nickname"]

    updated = await client.patch(
        "/api/v1/auth/me",
        json={"nickname": "  나무늘보  "},
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    assert updated.status_code == 200
    assert updated.json()["nickname"] == "나무늘보"
    assert await db_session.scalar(
        select(User.nickname).where(User.id == user.id)
    ) == "나무늘보"

    empty = await client.patch(
        "/api/v1/auth/me",
        json={"nickname": "   "},
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    too_long = await client.patch(
        "/api/v1/auth/me",
        json={"nickname": "가" * 17},
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    assert empty.status_code == 422
    assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_google_login_reuses_existing_user_for_same_sub(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    token = id_token(private_key, google_sub)

    first = await client.post(
        "/api/v1/auth/google",
        json=login_payload(token, device_id="first-device"),
    )
    assert first.status_code == 200
    first_user = await db_session.scalar(
        select(User).where(User.google_sub == google_sub)
    )
    assert first_user is not None
    first_user_id = first_user.id

    second = await client.post(
        "/api/v1/auth/google",
        json=login_payload(token, device_id="second-device"),
    )
    assert second.status_code == 200
    assert (
        await db_session.scalar(
            select(func.count(User.id)).where(User.google_sub == google_sub)
        )
        == 1
    )
    reused_user = await db_session.scalar(
        select(User).where(User.google_sub == google_sub)
    )
    assert reused_user is not None
    assert reused_user.id == first_user_id


@pytest.mark.asyncio
async def test_google_login_rejects_mismatched_audience(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(
            id_token(private_key, google_sub, aud="different-client-id")
        ),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_GOOGLE_TOKEN"


@pytest.mark.asyncio
async def test_google_login_rejects_mismatched_issuer(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(
            id_token(private_key, google_sub, iss="https://example.com")
        ),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_GOOGLE_TOKEN"


@pytest.mark.asyncio
async def test_google_login_rejects_expired_token(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(
            id_token(
                private_key,
                google_sub,
                exp=datetime.now(UTC) - timedelta(seconds=1),
            )
        ),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_GOOGLE_TOKEN"


@pytest.mark.asyncio
async def test_google_login_rejects_unverified_email(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(id_token(private_key, google_sub, email_verified=False)),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_GOOGLE_TOKEN"


@pytest.mark.asyncio
async def test_google_login_fails_closed_when_client_id_is_unset(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, _ = rsa_key_pair
    monkeypatch.setattr(settings, "KASSET_GOOGLE_OAUTH_CLIENT_ID", "")

    response = await client.post(
        "/api/v1/auth/google",
        json=login_payload(id_token(private_key, google_sub)),
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "GOOGLE_LOGIN_UNAVAILABLE",
            "message": "Google 로그인을 사용할 수 없습니다.",
        }
    }


@pytest.mark.asyncio
async def test_google_login_token_passes_existing_device_bound_protected_route(
    client: httpx.AsyncClient,
    rsa_key_pair,
    google_sub: str,
) -> None:
    private_key, _ = rsa_key_pair
    login = await client.post(
        "/api/v1/auth/google",
        json=login_payload(
            id_token(private_key, google_sub),
            device_id="protected-route-device",
        ),
    )
    assert login.status_code == 200

    brokers = await client.get(
        "/api/v1/brokers",
        headers={
            "Authorization": f"Bearer {login.json()['accessToken']}",
        },
    )
    assert brokers.status_code == 200
    assert [item["provider"] for item in brokers.json()["brokers"]] == [
        "PAPER",
        "NH",
        "KIS",
        "TOSS",
        "KB",
    ]
