"""Progressive password cooldowns and one-time email recovery codes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote, urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.token_repository import revoke_all_refresh_tokens
from app.core.config import settings
from app.models.trading import PasswordResetToken, User

logger = logging.getLogger(__name__)

INITIAL_FAILURES_BEFORE_COOLDOWN = 5
COOLDOWN_DURATIONS = (
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=5),
    timedelta(hours=24),
)
GENERIC_RECOVERY_MESSAGE = "등록된 계정이라면 비밀번호 복구 안내를 이메일로 보냈습니다."


@dataclass(frozen=True, slots=True)
class CooldownTransition:
    level: int
    until: datetime

    @property
    def duration(self) -> timedelta:
        return COOLDOWN_DURATIONS[self.level - 1]


def utc_now() -> datetime:
    return datetime.now(UTC)


def cooldown_retry_seconds(user: User, *, now: datetime) -> int | None:
    until = user.login_cooldown_until
    if until is None or until <= now:
        return None
    return max(1, math.ceil((until - now).total_seconds()))


def record_password_failure(
    user: User,
    *,
    now: datetime,
) -> CooldownTransition | None:
    """Advance the account-local cooldown ladder after a bad password.

    The first level requires five consecutive failures. Once a cooldown has
    elapsed, the next bad password advances one step: 10m, 1h, 5h, then 24h.
    At the last level each later failure starts another 24h cooldown. A valid
    login or password reset calls :func:`reset_password_throttle`.
    """

    level = max(0, min(int(user.login_cooldown_level or 0), len(COOLDOWN_DURATIONS)))
    if level == 0:
        user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts < INITIAL_FAILURES_BEFORE_COOLDOWN:
            return None
        next_level = 1
    else:
        next_level = min(level + 1, len(COOLDOWN_DURATIONS))

    user.failed_login_attempts = 0
    user.login_cooldown_level = next_level
    user.login_cooldown_until = now + COOLDOWN_DURATIONS[next_level - 1]
    return CooldownTransition(level=next_level, until=user.login_cooldown_until)


def reset_password_throttle(user: User) -> None:
    user.failed_login_attempts = 0
    user.login_cooldown_level = 0
    user.login_cooldown_until = None


def hash_reset_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def issue_password_reset_code(
    db: AsyncSession,
    user: User,
    *,
    now: datetime,
) -> str:
    """Invalidate older codes and create one fresh single-use reset code."""

    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    code = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_reset_code(code),
            expires_at=now
            + timedelta(minutes=settings.AUTH_PASSWORD_RESET_TTL_MINUTES),
        )
    )
    await db.flush()
    return code


async def consume_password_reset_code(
    db: AsyncSession,
    *,
    code: str,
    password_hash: str,
    now: datetime,
) -> User | None:
    normalized = code.strip()
    if not normalized or len(normalized) > 256:
        return None

    token = await db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hash_reset_code(normalized))
        .with_for_update()
    )
    if token is None or token.used_at is not None or token.expires_at <= now:
        return None

    user = await db.scalar(
        select(User).where(User.id == token.user_id).with_for_update()
    )
    if user is None or not user.is_active or not user.hashed_password:
        return None

    token.used_at = now
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.id != token.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    user.hashed_password = password_hash
    user.web_session_version = int(user.web_session_version or 0) + 1
    reset_password_throttle(user)
    await revoke_all_refresh_tokens(db, user.id)
    await db.flush()
    return user


def recovery_email_configured() -> bool:
    base_url = _password_reset_base_url()
    username = settings.AUTH_SMTP_USERNAME.strip()
    password = settings.AUTH_SMTP_PASSWORD
    password_value = password.get_secret_value() if password is not None else ""
    credentials_coherent = bool(username) == bool(password_value)
    return bool(
        settings.AUTH_SMTP_HOST.strip()
        and settings.AUTH_SMTP_FROM_EMAIL.strip()
        and base_url
        and credentials_coherent
    )


async def send_password_reset_email(recipient: str, code: str) -> bool:
    base_url = _password_reset_base_url()
    if not recovery_email_configured() or base_url is None:
        logger.warning("Password recovery email is not configured")
        return False

    encoded_code = quote(code, safe="")
    reset_url = f"{base_url}/web-auth/reset-password#code={encoded_code}"
    message = EmailMessage()
    message["Subject"] = "[KAsset] 관리자 비밀번호 복구"
    message["From"] = settings.AUTH_SMTP_FROM_EMAIL.strip()
    message["To"] = recipient
    message.set_content(
        "KAsset 관리자 비밀번호 복구 요청이 접수되었습니다.\n\n"
        f"아래 링크를 열어 {settings.AUTH_PASSWORD_RESET_TTL_MINUTES}분 안에 "
        "새 비밀번호를 설정하세요.\n"
        f"{reset_url}\n\n"
        "링크를 열 수 없으면 복구 코드 입력란에 아래 코드를 붙여넣으세요.\n"
        f"{code}\n\n"
        "본인이 요청하지 않았다면 이 메일을 무시하세요."
    )
    return await _send_message(message, event="password_reset")


async def send_cooldown_email(
    recipient: str,
    transition: CooldownTransition,
) -> bool:
    base_url = _password_reset_base_url()
    if not recovery_email_configured() or base_url is None:
        return False

    minutes = int(transition.duration.total_seconds() // 60)
    duration_label = f"{minutes}분" if minutes < 60 else f"{minutes // 60}시간"
    message = EmailMessage()
    message["Subject"] = "[KAsset] 관리자 로그인 지연 알림"
    message["From"] = settings.AUTH_SMTP_FROM_EMAIL.strip()
    message["To"] = recipient
    message.set_content(
        "KAsset 관리자 계정에서 연속된 비밀번호 오류가 감지되었습니다.\n\n"
        f"보호 단계: {transition.level}/5\n"
        f"로그인 지연: {duration_label}\n"
        f"해제 시각(UTC): {transition.until.isoformat()}\n\n"
        "본인의 시도가 아니거나 비밀번호를 잊었다면 아래 페이지에서 복구하세요.\n"
        f"{base_url}/web-auth/forgot-password"
    )
    return await _send_message(message, event="login_cooldown")


def _password_reset_base_url() -> str | None:
    value = settings.AUTH_PASSWORD_RESET_BASE_URL.strip().rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "https":
        return value
    if settings.ENVIRONMENT != "production" and parsed.scheme == "http":
        return value
    return None


async def _send_message(message: EmailMessage, *, event: str) -> bool:
    try:
        await asyncio.to_thread(_send_message_sync, message)
    except Exception as exc:
        logger.error(
            "Auth email delivery failed: event=%s error_type=%s",
            event,
            type(exc).__name__,
        )
        return False
    logger.info("Auth email delivered: event=%s", event)
    return True


def _smtp_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if settings.AUTH_SMTP_ALLOW_LEGACY_TLS:
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    return context


def _send_message_sync(message: EmailMessage) -> None:
    host = settings.AUTH_SMTP_HOST.strip()
    port = settings.AUTH_SMTP_PORT
    security = settings.AUTH_SMTP_SECURITY
    context = _smtp_ssl_context()

    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            host,
            port,
            timeout=settings.AUTH_SMTP_TIMEOUT_SECONDS,
            context=context,
        )
    else:
        smtp = smtplib.SMTP(
            host,
            port,
            timeout=settings.AUTH_SMTP_TIMEOUT_SECONDS,
        )

    with smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        username = settings.AUTH_SMTP_USERNAME.strip()
        password = settings.AUTH_SMTP_PASSWORD
        if username and password is not None:
            smtp.login(username, password.get_secret_value())
        smtp.send_message(message)
