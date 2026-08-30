from __future__ import annotations

import asyncio
import re
import ssl
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import password_recovery, web_router
from app.auth.password_recovery import (
    COOLDOWN_DURATIONS,
    GENERIC_RECOVERY_MESSAGE,
    consume_password_reset_code,
    cooldown_retry_seconds,
    hash_reset_code,
    issue_password_reset_code,
    record_password_failure,
    reset_password_throttle,
)
from app.auth.security import get_password_hash, verify_password
from app.core.config import settings
from app.extensions.kasset.models import KAssetDeviceSession
from app.models.trading import PasswordResetToken, RefreshToken, User, UserRole


def _csrf_token(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrftoken" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


@pytest.fixture(autouse=True)
def _disable_auth_rate_limits():
    original = web_router.limiter.enabled
    web_router.limiter.enabled = False
    yield
    web_router.limiter.enabled = original


def test_progressive_cooldown_ladder_and_success_reset() -> None:
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    user = User(
        id=9001,
        username="cooldown-admin",
        email="cooldown@example.com",
        hashed_password="unused",
        role=UserRole.admin,
        is_active=True,
        failed_login_attempts=0,
        login_cooldown_level=0,
        login_cooldown_until=None,
    )

    for expected_attempts in range(1, 5):
        assert record_password_failure(user, now=now) is None
        assert user.failed_login_attempts == expected_attempts

    transition = record_password_failure(user, now=now)
    assert transition is not None
    assert transition.level == 1
    assert transition.until == now + timedelta(minutes=5)
    assert cooldown_retry_seconds(user, now=now) == 300

    for expected_level, duration in enumerate(COOLDOWN_DURATIONS[1:], start=2):
        now = transition.until + timedelta(seconds=1)
        transition = record_password_failure(user, now=now)
        assert transition is not None
        assert transition.level == expected_level
        assert transition.until == now + duration

    now = transition.until + timedelta(seconds=1)
    repeated_max = record_password_failure(user, now=now)
    assert repeated_max is not None
    assert repeated_max.level == 5
    assert repeated_max.until == now + timedelta(hours=24)

    reset_password_throttle(user)
    assert user.failed_login_attempts == 0
    assert user.login_cooldown_level == 0
    assert user.login_cooldown_until is None


@pytest.mark.asyncio
async def test_reset_code_is_hashed_single_use_and_revokes_all_refresh_mechanisms(
    db_session: AsyncSession,
) -> None:
    suffix = uuid4().hex
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    user = User(
        username=f"recovery-{suffix}",
        email=f"recovery-{suffix}@example.com",
        hashed_password=get_password_hash("OldPassword1!"),
        role=UserRole.admin,
        is_active=True,
        failed_login_attempts=4,
        login_cooldown_level=3,
        login_cooldown_until=now + timedelta(hours=1),
    )
    db_session.add(user)
    await db_session.flush()
    user_id = user.id
    db_session.add_all(
        [
            RefreshToken(
                user_id=user.id,
                token_hash=f"refresh-{suffix}",
                expires_at=now + timedelta(days=1),
                revoked=False,
            ),
            KAssetDeviceSession(
                id=f"reset-device-{suffix}",
                owner_user_id=user.id,
                device_id=f"device-{suffix}",
                device_name="Reset test device",
                refresh_token_hash=f"device-refresh-{suffix}",
                expires_at=now + timedelta(days=1),
                revoked_at=None,
            ),
        ]
    )
    await db_session.commit()

    try:
        code = await issue_password_reset_code(db_session, user, now=now)
        await db_session.commit()
        token = await db_session.scalar(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
        assert token is not None
        assert token.token_hash == hash_reset_code(code)
        assert code not in token.token_hash

        reset_user = await consume_password_reset_code(
            db_session,
            code=code,
            password_hash=get_password_hash("NewPassword2!"),
            now=now + timedelta(minutes=1),
        )
        assert reset_user is not None
        await db_session.commit()
        await db_session.refresh(reset_user)
        assert verify_password("NewPassword2!", reset_user.hashed_password)
        assert reset_user.failed_login_attempts == 0
        assert reset_user.login_cooldown_level == 0
        assert reset_user.login_cooldown_until is None
        assert reset_user.web_session_version == 1

        token = await db_session.get(PasswordResetToken, token.id)
        refresh = await db_session.scalar(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        )
        device_session = await db_session.scalar(
            select(KAssetDeviceSession).where(
                KAssetDeviceSession.owner_user_id == user.id
            )
        )
        assert token is not None and token.used_at is not None
        assert refresh is not None and refresh.revoked is True
        assert device_session is not None and device_session.revoked_at is not None

        assert (
            await consume_password_reset_code(
                db_session,
                code=code,
                password_hash=get_password_hash("ThirdPassword3!"),
                now=now + timedelta(minutes=2),
            )
            is None
        )
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.id == user_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_concurrent_reset_code_issuance_leaves_one_active_code(
    db_session: AsyncSession,
) -> None:
    from app.core.db import AsyncSessionLocal

    suffix = uuid4().hex
    now = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    user = User(
        username=f"recovery-race-{suffix}",
        email=f"recovery-race-{suffix}@example.com",
        hashed_password=get_password_hash("OldPassword1!"),
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    user_id = user.id

    async def issue() -> str:
        async with AsyncSessionLocal() as session:
            concurrent_user = await session.get(User, user_id)
            assert concurrent_user is not None
            code = await issue_password_reset_code(session, concurrent_user, now=now)
            await session.commit()
            return code

    try:
        codes = await asyncio.gather(issue(), issue())
        tokens = list(
            (
                await db_session.scalars(
                    select(PasswordResetToken)
                    .where(PasswordResetToken.user_id == user_id)
                    .order_by(PasswordResetToken.id)
                )
            ).all()
        )
        active_tokens = [token for token in tokens if token.used_at is None]

        assert len(tokens) == 2
        assert len(active_tokens) == 1
        assert active_tokens[0].token_hash in {hash_reset_code(code) for code in codes}
        assert all(
            token.used_at == now for token in tokens if token.used_at is not None
        )
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.id == user_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_password_email_uses_fragment_not_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AUTH_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "AUTH_SMTP_FROM_EMAIL", "security@example.test")
    monkeypatch.setattr(settings, "AUTH_SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "AUTH_SMTP_PASSWORD", None)
    monkeypatch.setattr(
        settings,
        "AUTH_PASSWORD_RESET_BASE_URL",
        "https://admin.example.test",
    )
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    captured = {}

    def capture(message) -> None:
        captured["message"] = message

    monkeypatch.setattr(password_recovery, "_send_message_sync", capture)
    code = "secret-reset-code"

    assert await password_recovery.send_password_reset_email(
        "operator@example.test", code
    )
    body = captured["message"].get_content()
    assert f"#code={code}" in body
    assert "?code=" not in body
    assert captured["message"]["To"] == "operator@example.test"


def test_legacy_smtp_tls_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AUTH_SMTP_ALLOW_LEGACY_TLS", True)

    with pytest.warns(DeprecationWarning):
        context = password_recovery._smtp_ssl_context()

    assert context.minimum_version is ssl.TLSVersion.TLSv1
    assert context.options & ssl.OP_LEGACY_SERVER_CONNECT


def test_recovery_pages_are_csrf_protected_and_fragment_safe(auth_test_client) -> None:
    forgot = auth_test_client.get("/web-auth/forgot-password")
    reset = auth_test_client.get("/web-auth/reset-password")
    login = auth_test_client.get("/web-auth/login")

    assert forgot.status_code == reset.status_code == login.status_code == 200
    assert 'name="csrftoken"' in forgot.text
    assert 'name="csrftoken"' in reset.text
    assert 'href="/web-auth/forgot-password"' in login.text
    assert "window.location.hash.slice(1)" in reset.text
    assert "history.replaceState" in reset.text
    assert forgot.headers["cache-control"] == "no-store"
    assert reset.headers["cache-control"] == "no-store"


def test_forgot_password_has_same_external_response_for_known_and_unknown_email(
    auth_test_client,
    auth_mock_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=4,
        username="admin@example.test",
        email="admin@example.test",
        hashed_password=get_password_hash("Password1!"),
        role=UserRole.admin,
        is_active=True,
    )
    known_result = MagicMock()
    known_result.scalars.return_value.all.return_value = [user]
    unknown_result = MagicMock()
    unknown_result.scalars.return_value.all.return_value = []
    auth_mock_session.execute.side_effect = [known_result, unknown_result]
    monkeypatch.setattr(web_router, "recovery_email_configured", lambda: True)
    issue = AsyncMock(return_value="one-time-code")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(web_router, "issue_password_reset_code", issue)
    monkeypatch.setattr(web_router, "send_password_reset_email", send)

    token = _csrf_token(auth_test_client, "/web-auth/forgot-password")
    known = auth_test_client.post(
        "/web-auth/forgot-password",
        data={"email": user.email, "csrftoken": token},
    )
    token = _csrf_token(auth_test_client, "/web-auth/forgot-password")
    unknown = auth_test_client.post(
        "/web-auth/forgot-password",
        data={"email": "unknown@example.test", "csrftoken": token},
    )

    assert known.status_code == unknown.status_code == 200
    assert GENERIC_RECOVERY_MESSAGE in known.text
    assert GENERIC_RECOVERY_MESSAGE in unknown.text
    assert user.email not in known.text
    issue.assert_awaited_once()
    send.assert_awaited_once_with(user.email, "one-time-code")


def test_password_login_enters_and_escalates_cooldown_then_success_resets(
    auth_test_client,
    auth_mock_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=4,
        username="admin@example.test",
        email="admin@example.test",
        hashed_password=get_password_hash("CorrectPassword1!"),
        role=UserRole.admin,
        is_active=True,
        failed_login_attempts=0,
        login_cooldown_level=0,
        login_cooldown_until=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    auth_mock_session.execute.return_value = result
    clock = [datetime(2026, 8, 30, 10, 0, tzinfo=UTC)]
    monkeypatch.setattr(web_router, "utc_now", lambda: clock[0])
    monkeypatch.setattr(web_router, "recovery_email_configured", lambda: True)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(web_router, "send_cooldown_email", send)

    def submit(password: str):
        token = _csrf_token(auth_test_client, "/web-auth/login")
        return auth_test_client.post(
            "/web-auth/login",
            data={
                "username": user.username,
                "password": password,
                "csrftoken": token,
            },
            follow_redirects=False,
        )

    for _ in range(5):
        assert submit("wrong-password").status_code == 400
    assert user.login_cooldown_level == 1
    assert user.login_cooldown_until == clock[0] + timedelta(minutes=5)
    assert send.await_count == 1

    assert submit("CorrectPassword1!").status_code == 400
    assert user.login_cooldown_level == 1

    clock[0] = user.login_cooldown_until + timedelta(seconds=1)
    assert submit("wrong-password").status_code == 400
    assert user.login_cooldown_level == 2
    assert user.login_cooldown_until == clock[0] + timedelta(minutes=10)
    assert send.await_count == 2

    clock[0] = user.login_cooldown_until + timedelta(seconds=1)
    with patch(
        "app.auth.web_router.redis.from_url", side_effect=RuntimeError("offline")
    ):
        success = submit("CorrectPassword1!")
    assert success.status_code == 303
    assert user.failed_login_attempts == 0
    assert user.login_cooldown_level == 0
    assert user.login_cooldown_until is None


def test_reset_route_revokes_cached_sessions_and_never_echoes_valid_password(
    auth_test_client,
    auth_mock_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=77,
        username="admin",
        email="admin@example.test",
        hashed_password=get_password_hash("OldPassword1!"),
        role=UserRole.admin,
        is_active=True,
    )
    consume = AsyncMock(return_value=user)
    invalidate = AsyncMock()
    monkeypatch.setattr(web_router, "consume_password_reset_code", consume)
    monkeypatch.setattr(web_router, "invalidate_user_cache", invalidate)
    token = _csrf_token(auth_test_client, "/web-auth/reset-password")
    new_password = "NewPassword2!"

    response = auth_test_client.post(
        "/web-auth/reset-password",
        data={
            "code": "valid-code",
            "password": new_password,
            "password_confirm": new_password,
            "csrftoken": token,
        },
    )

    assert response.status_code == 200
    assert "비밀번호를 변경했습니다" in response.text
    assert new_password not in response.text
    consume.assert_awaited_once()
    invalidate.assert_awaited_once_with(user.id)
    auth_mock_session.commit.assert_awaited()
