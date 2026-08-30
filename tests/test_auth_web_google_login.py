"""Browser-session Google Sign-In for ``/web-auth``.

The additive ``POST /web-auth/google`` route must mint the *existing* session
cookie (so ``get_current_user_from_session`` and therefore ``require_admin``
keep working unchanged), must never provision accounts from the browser, and
must stay fail-closed while ``WEB_GOOGLE_OAUTH_CLIENT_ID`` is unset.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from app.auth import web_router
from app.auth.security import get_password_hash
from app.auth.web_router import SESSION_COOKIE_NAME, get_current_user_from_session
from app.core.config import settings
from app.models.trading import User, UserRole

# Not a credential: an audience string that only this suite mints tokens for.
_WEB_CLIENT_ID = "web-admin-test.apps.googleusercontent.com"
_GOOGLE_SUB = "web-admin-test-google-sub"
_REJECTED_TEXT = "등록되지 않은 Google 계정"


@pytest.fixture(scope="module")
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """The route shares the 5/minute login limiter; several tests exceed it."""
    original = web_router.limiter.enabled
    web_router.limiter.enabled = False
    yield
    web_router.limiter.enabled = original


@pytest.fixture(autouse=True)
def _google_jwks(monkeypatch: pytest.MonkeyPatch, rsa_key_pair) -> None:
    """Serve the test signing key instead of Google's JWKS endpoint."""
    _, public_key = rsa_key_pair

    def signing_key_from_token(_client, _token):
        return SimpleNamespace(key=public_key)

    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", signing_key_from_token
    )


@pytest.fixture
def google_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEB_GOOGLE_OAUTH_CLIENT_ID", _WEB_CLIENT_ID)


@pytest.fixture
def google_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEB_GOOGLE_OAUTH_CLIENT_ID", "")


def _id_token(private_key, **overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": _WEB_CLIENT_ID,
        "sub": _GOOGLE_SUB,
        "email": "operator@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "web-google-test-key"},
    )


def _google_user(*, is_active: bool = True) -> User:
    """A Google-only account: no password, so only this route can sign it in."""
    return User(
        id=4,
        username="google-web-admin",
        email="operator@example.com",
        hashed_password=None,
        google_sub=_GOOGLE_SUB,
        role=UserRole.admin,
        is_active=is_active,
    )


def _redis_mock() -> AsyncMock:
    client = AsyncMock()
    client.scard = AsyncMock(return_value=0)
    client.spop = AsyncMock(return_value=None)
    client.sadd = AsyncMock(return_value=1)
    client.expire = AsyncMock(return_value=True)
    client.set = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    client.sismember = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    return client


def _csrf_token(client) -> str:
    """Reuse the login page's hidden CSRF field, exactly like the password form."""
    response = client.get("/web-auth/login")
    assert response.status_code == 200
    match = re.search(r'name="csrftoken" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _request_with_session(session_cookie: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/ops",
            "query_string": b"",
            "headers": [
                (b"cookie", f"{SESSION_COOKIE_NAME}={session_cookie}".encode())
            ],
            "client": ("testclient", 50000),
        }
    )


def _scalar_result(value: User | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def test_google_login_issues_existing_web_session_cookie(
    auth_test_client, auth_mock_session, rsa_key_pair, google_enabled
):
    """Success must go through the same cookie + Redis path as the password form."""
    private_key, _ = rsa_key_pair
    user = _google_user()
    result = _scalar_result(user)
    auth_mock_session.execute.return_value = result
    redis_client = _redis_mock()

    with patch("app.auth.web_router.redis.from_url", return_value=redis_client):
        response = auth_test_client.post(
            "/web-auth/google",
            data={
                "credential": _id_token(private_key),
                "csrftoken": _csrf_token(auth_test_client),
                "next": "/admin/ops",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ops"
    session_cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert session_cookie
    assert "HttpOnly" in " ".join(response.headers.get_list("set-cookie"))
    # Same Redis shape the password login writes.
    assert redis_client.sadd.called
    assert redis_client.set.called

    # The cookie must be understood by the shared session reader that
    # require_admin() sits on top of.
    resolver_db = AsyncMock()
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = 0
    resolver_db.execute.side_effect = [version_result, result]
    with (
        patch("app.auth.web_router.redis.from_url", return_value=redis_client),
        patch("app.auth.web_router.get_session_blacklist") as blacklist,
    ):
        blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)
        resolved = asyncio.run(
            get_current_user_from_session(
                _request_with_session(session_cookie), resolver_db
            )
        )

    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.username == user.username
    assert resolved.role is UserRole.admin
    # The signed cookie decoded to this user's session set, not somebody else's.
    assert redis_client.sismember.await_args.args[0] == "user_session:4"


def test_password_reset_session_generation_rejects_old_cookie() -> None:
    stale_cookie = web_router.create_session_token(4, session_version=0)
    resolver_db = AsyncMock()
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = 1
    resolver_db.execute.return_value = version_result

    resolved = asyncio.run(
        get_current_user_from_session(
            _request_with_session(stale_cookie),
            resolver_db,
        )
    )

    assert resolved is None


def test_google_login_issues_session_for_user_without_username_and_redacts_identifiers(
    auth_test_client,
    auth_mock_session,
    rsa_key_pair,
    google_enabled,
    caplog: pytest.LogCaptureFixture,
):
    private_key, _ = rsa_key_pair
    user = _google_user()
    user.username = None
    auth_mock_session.execute.return_value = _scalar_result(user)
    redis_client = _redis_mock()

    with (
        caplog.at_level("INFO", logger=web_router.__name__),
        patch("app.auth.web_router.redis.from_url", return_value=redis_client),
    ):
        response = auth_test_client.post(
            "/web-auth/google",
            data={
                "credential": _id_token(private_key),
                "csrftoken": _csrf_token(auth_test_client),
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.cookies.get(SESSION_COOKIE_NAME)
    assert redis_client.sadd.called

    success_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "web_google_login_success"
    )
    assert success_record.username_hash == hashlib.sha256(b"user-id:4").hexdigest()[:16]
    security_log = "\n".join(
        f"{record.getMessage()} {vars(record)}"
        for record in caplog.records
        if record.name == web_router.__name__
    )
    assert user.email not in security_log
    assert user.google_sub not in security_log


def test_google_login_rejects_unknown_sub_and_creates_no_user(
    auth_test_client, auth_mock_session, rsa_key_pair, google_enabled
):
    """Unlike the Android route, the browser surface never provisions accounts."""
    private_key, _ = rsa_key_pair
    auth_mock_session.execute.return_value = _scalar_result(None)
    redis_client = _redis_mock()

    with patch("app.auth.web_router.redis.from_url", return_value=redis_client):
        response = auth_test_client.post(
            "/web-auth/google",
            data={
                "credential": _id_token(private_key, sub="stranger-google-sub"),
                "csrftoken": _csrf_token(auth_test_client),
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert _REJECTED_TEXT in response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    assert not auth_mock_session.add.called
    assert not auth_mock_session.commit.called
    assert not redis_client.sadd.called


def test_google_login_rejects_unverified_email_before_touching_the_database(
    auth_test_client, auth_mock_session, rsa_key_pair, google_enabled
):
    private_key, _ = rsa_key_pair

    response = auth_test_client.post(
        "/web-auth/google",
        data={
            "credential": _id_token(private_key, email_verified=False),
            "csrftoken": _csrf_token(auth_test_client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _REJECTED_TEXT in response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    assert not auth_mock_session.execute.called


def test_google_login_rejects_inactive_account(
    auth_test_client,
    auth_mock_session,
    rsa_key_pair,
    google_enabled,
    caplog: pytest.LogCaptureFixture,
):
    """Deployment disables the leftover test accounts via is_active."""
    private_key, _ = rsa_key_pair
    auth_mock_session.execute.return_value = _scalar_result(
        _google_user(is_active=False)
    )
    redis_client = _redis_mock()

    with (
        caplog.at_level("WARNING", logger=web_router.__name__),
        patch("app.auth.web_router.redis.from_url", return_value=redis_client),
    ):
        response = auth_test_client.post(
            "/web-auth/google",
            data={
                "credential": _id_token(private_key),
                "csrftoken": _csrf_token(auth_test_client),
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert _REJECTED_TEXT in response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    assert not redis_client.sadd.called
    failure_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "web_google_login_failure"
    )
    assert failure_record.reason == "inactive_user"


@pytest.mark.parametrize(
    "flaw",
    ["malformed", "wrong_audience", "wrong_issuer", "expired"],
)
def test_google_login_rejects_invalid_id_token(
    flaw, auth_test_client, auth_mock_session, rsa_key_pair, google_enabled
):
    """A valid account exists, so a 400 can only come from token verification."""
    private_key, _ = rsa_key_pair
    auth_mock_session.execute.return_value = _scalar_result(_google_user())
    now = datetime.now(UTC)

    if flaw == "malformed":
        credential = "not-a-google-id-token"
    elif flaw == "wrong_audience":
        credential = _id_token(
            private_key, aud="someone-else.apps.googleusercontent.com"
        )
    elif flaw == "wrong_issuer":
        credential = _id_token(private_key, iss="https://accounts.evil.example")
    else:
        credential = _id_token(
            private_key,
            iat=now - timedelta(hours=2),
            exp=now - timedelta(hours=1),
        )

    response = auth_test_client.post(
        "/web-auth/google",
        data={
            "credential": credential,
            "csrftoken": _csrf_token(auth_test_client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _REJECTED_TEXT in response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    assert not auth_mock_session.execute.called


def test_google_login_requires_the_shared_csrf_token(
    auth_test_client, auth_mock_session, rsa_key_pair, google_enabled
):
    """No Google-specific CSRF scheme: the shared middleware still guards it."""
    private_key, _ = rsa_key_pair
    auth_mock_session.execute.return_value = _scalar_result(_google_user())

    response = auth_test_client.post(
        "/web-auth/google",
        data={"credential": _id_token(private_key)},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "CSRF token verification failed" in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_login_page_and_google_route_fail_closed_when_unconfigured(
    auth_test_client, auth_mock_session, rsa_key_pair, google_disabled
):
    """The app must serve normally; only the Google path is refused."""
    private_key, _ = rsa_key_pair
    auth_mock_session.execute.return_value = _scalar_result(_google_user())

    page = auth_test_client.get("/web-auth/login")
    assert page.status_code == 200
    assert "accounts.google.com/gsi/client" not in page.text
    assert 'action="/web-auth/google"' not in page.text
    assert page.text.count('<div class="divider">또는</div>') == 0

    response = auth_test_client.post(
        "/web-auth/google",
        data={
            "credential": _id_token(private_key),
            "csrftoken": _csrf_token(auth_test_client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert "Google 로그인이 설정되지 않았습니다." in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


def test_login_page_offers_google_button_when_configured(
    auth_test_client, google_enabled
):
    page = auth_test_client.get("/web-auth/login")

    assert page.status_code == 200
    assert 'action="/web-auth/google"' in page.text
    assert f'data-client_id="{_WEB_CLIENT_ID}"' in page.text
    assert "accounts.google.com/gsi/client" in page.text
    # The password form must survive next to it.
    assert 'action="/web-auth/login"' in page.text
    assert 'name="password"' in page.text
    assert page.text.count('<div class="divider">또는</div>') == 1


def test_password_login_still_issues_a_session_with_google_enabled(
    auth_test_client, auth_mock_session, google_enabled
):
    """Both mechanisms share the session issuer; neither may shadow the other."""
    user = User(
        id=5,
        username="passworduser",
        email="password@example.com",
        hashed_password=get_password_hash("password123"),
        role=UserRole.trader,
        is_active=True,
    )
    auth_mock_session.execute.return_value = _scalar_result(user)
    redis_client = _redis_mock()

    with patch("app.auth.web_router.redis.from_url", return_value=redis_client):
        response = auth_test_client.post(
            "/web-auth/login",
            data={
                "username": "passworduser",
                "password": "password123",
                "csrftoken": _csrf_token(auth_test_client),
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.cookies.get(SESSION_COOKIE_NAME)
    assert redis_client.sadd.called
