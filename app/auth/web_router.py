"""Web authentication router with HTML pages and session management."""

import hashlib
import json
import logging
from typing import Annotated
from urllib.parse import urlparse

import redis.asyncio as redis
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import ValidationError
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password_recovery import (
    GENERIC_RECOVERY_MESSAGE,
    consume_password_reset_code,
    cooldown_retry_seconds,
    issue_password_reset_code,
    record_password_failure,
    recovery_email_configured,
    reset_password_throttle,
    send_cooldown_email,
    send_password_reset_email,
    utc_now,
)
from app.auth.role_hierarchy import has_min_role
from app.auth.schemas import PasswordResetConfirm, UserCreate
from app.auth.security import get_password_hash, verify_password
from app.core.config import settings
from app.core.db import get_db
from app.core.session_blacklist import get_session_blacklist
from app.core.templates import templates
from app.extensions.kasset.api.auth import MobileAuthService
from app.extensions.kasset.api.errors import MobileApiError
from app.models.trading import User, UserRole

router = APIRouter(prefix="/web-auth", tags=["web-authentication"])
logger = logging.getLogger(__name__)

# Rate limiter for brute-force protection
limiter = Limiter(key_func=get_remote_address)

# Session serializer for secure cookie-based sessions
session_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="session-cookie")

# Session cookie settings
SESSION_COOKIE_NAME = "session"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days
USER_CACHE_TTL = 300  # 5 minutes
MAX_SESSIONS_PER_USER = 5
SESSION_HASH_KEY_PREFIX = "user_session"
USER_CACHE_KEY_PREFIX = "user_cache"
_INVALID_CREDENTIALS = "사용자명 또는 비밀번호가 올바르지 않습니다."
_DUMMY_PASSWORD_HASH = get_password_hash("kasset-dummy-password-not-an-account-1!")


def _session_hash_key(user_id: int) -> str:
    return f"{SESSION_HASH_KEY_PREFIX}:{user_id}"


def _user_cache_key(user_id: int) -> str:
    return f"{USER_CACHE_KEY_PREFIX}:{user_id}"


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session_token(user_id: int, session_version: int = 0) -> str:
    """Create a secure session token for the user and credential generation."""
    token = session_serializer.dumps(
        {"user_id": user_id, "session_version": session_version}
    )
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


def _verify_session_claims(
    token: str, max_age: int = SESSION_TTL
) -> tuple[int, int] | None:
    try:
        data = session_serializer.loads(token, max_age=max_age)
        user_id = data.get("user_id")
        session_version = data.get("session_version", 0)
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
            or not isinstance(session_version, int)
            or isinstance(session_version, bool)
            or session_version < 0
        ):
            return None
        return user_id, session_version
    except (BadSignature, SignatureExpired):
        return None


def verify_session_token(token: str, max_age: int = SESSION_TTL) -> int | None:
    """Verify session token and return user_id if valid."""
    claims = _verify_session_claims(token, max_age=max_age)
    return claims[0] if claims else None


async def invalidate_user_cache(user_id: int) -> None:
    """Remove cached session data for the given user."""
    import redis.asyncio as redis

    redis_client = None
    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        await redis_client.delete(
            _session_hash_key(user_id),
            _user_cache_key(user_id),
        )
    finally:
        if redis_client:
            await redis_client.aclose()


def _security_log_extra(request: Request, **kwargs) -> dict:
    """Structured metadata for auth security logs."""
    return {
        "client_ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        **kwargs,
    }


def _sanitize_next(next_value: str | None) -> str | None:
    """Allow only internal paths for `next` to prevent open redirects."""
    if not next_value:
        return None
    try:
        parsed = urlparse(next_value)
        # Disallow absolute URLs (scheme/netloc)
        if parsed.scheme or parsed.netloc:
            return None
        if not parsed.path.startswith("/"):
            return None
        # Optionally keep query string
        if parsed.query:
            return f"{parsed.path}?{parsed.query}"
        return parsed.path
    except Exception:
        return None


async def get_current_user_from_session(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User | None:
    """Get current user from session cookie with Redis caching."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None

    claims = _verify_session_claims(session_token)
    if claims is None:
        return None
    user_id, session_version = claims

    # Password reset increments this DB-backed generation. Check it on every
    # request so old browser cookies stay revoked even if Redis invalidation
    # was unavailable or a cached user record survived.
    version_result = await db.execute(
        select(User.web_session_version).where(
            User.id == user_id,
            User.is_active.is_(True),
        )
    )
    current_session_version = version_result.scalar_one_or_none()
    if (
        current_session_version is None
        or int(current_session_version) != session_version
    ):
        return None

    # Check if user is blacklisted (session invalidated)
    blacklist = get_session_blacklist()
    if await blacklist.is_blacklisted(user_id):
        return None

    session_hash = _hash_session_token(session_token)

    # Try to validate session and get user from cache

    redis_client = None
    session_hash_key = _session_hash_key(user_id)
    user_cache_key = _user_cache_key(user_id)
    session_hash_verified = False
    redis_error = False

    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        is_member = await redis_client.sismember(session_hash_key, session_hash)
        if not is_member:
            return None

        session_hash_verified = True

        cached_user = await redis_client.get(user_cache_key)

        if cached_user:
            user_data = json.loads(cached_user)
            user = User(
                id=user_data["id"],
                username=user_data["username"],
                email=user_data["email"],
                role=UserRole[user_data["role"]],
                is_active=user_data["is_active"],
                hashed_password=user_data.get("hashed_password"),
                web_session_version=user_data.get("web_session_version", 0),
            )
            if user.is_active:
                return user
            return None
    except Exception:
        redis_error = True
        logger.warning(
            "Session cache lookup failed for user_id=%s", user_id, exc_info=True
        )
    finally:
        if redis_client:
            await redis_client.aclose()

    if not session_hash_verified and not redis_error:
        return None

    # Cache miss - query database
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        redis_client = None
        # Store in cache for 5 minutes
        try:
            redis_client = redis.from_url(
                settings.get_redis_url(),
                decode_responses=True,
            )
            user_data = {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role.name,
                "is_active": user.is_active,
                "hashed_password": user.hashed_password,
                "web_session_version": int(user.web_session_version or 0),
            }
            await redis_client.set(
                user_cache_key, json.dumps(user_data), ex=USER_CACHE_TTL
            )
            await redis_client.sadd(session_hash_key, session_hash)
            await redis_client.expire(session_hash_key, SESSION_TTL)
        except Exception:
            logger.warning(
                "Failed to refresh session cache for user_id=%s",
                user_id,
                exc_info=True,
            )
        finally:
            if redis_client:
                await redis_client.aclose()

        return user
    return None


async def require_login(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User | Response:
    """Dependency to require login for routes."""
    user = await get_current_user_from_session(request, db)
    if not user:
        # Redirect to login page with next parameter
        # Use relative path to avoid open redirect issues
        next_url = (
            f"{request.url.path}?{request.url.query}"
            if request.url.query
            else request.url.path
        )
        return RedirectResponse(
            url=f"/web-auth/login?next={next_url}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return user


async def require_role(
    min_role: UserRole,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | Response:
    """Dependency to require specific role for routes."""
    user = await get_current_user_from_session(request, db)
    if not user:
        return RedirectResponse(
            url="/web-auth/login", status_code=status.HTTP_303_SEE_OTHER
        )

    if not has_min_role(user.role, min_role):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "error": "권한이 부족합니다.",
                "message": f"이 페이지에 접근하려면 {min_role.value} 이상의 권한이 필요합니다.",
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return user


def _login_page_response(
    request: Request,
    *,
    next_value: str | None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Render the login page with both the credential and Google entry points."""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": next_value,
            "error": error,
            # Empty keeps the Google button out of the page entirely, matching
            # the fail-closed behaviour of POST /web-auth/google.
            "google_client_id": settings.WEB_GOOGLE_OAUTH_CLIENT_ID.strip(),
            "web_registration_enabled": settings.WEB_REGISTRATION_ENABLED,
        },
        status_code=status_code,
    )


async def _issue_web_session_response(
    request: Request,
    user: User,
    *,
    next_value: str | None,
    event_scope: str,
) -> RedirectResponse:
    """Mint the browser session shared by every web login path.

    Both the credential form and the Google button end here, so
    ``get_current_user_from_session`` -- and therefore ``require_admin`` --
    sees exactly one kind of session regardless of how the operator signed in.
    """
    # Google-only accounts may lack a username. Use the stable internal user ID
    # for correlation instead of putting an email or Google subject in the log.
    log_identifier = (
        user.username if user.username is not None else f"user-id:{user.id}"
    )
    username_hash = hashlib.sha256(log_identifier.encode()).hexdigest()[:16]
    session_token = create_session_token(user.id, int(user.web_session_version or 0))
    session_hash = _hash_session_token(session_token)

    redis_client = None
    redis_error = False
    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        session_hash_key = _session_hash_key(user.id)
        user_data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.name,
            "is_active": user.is_active,
            "hashed_password": user.hashed_password,
            "web_session_version": int(user.web_session_version or 0),
        }
        session_count = await redis_client.scard(session_hash_key)
        if session_count >= MAX_SESSIONS_PER_USER:
            await redis_client.spop(session_hash_key)
        await redis_client.sadd(session_hash_key, session_hash)
        await redis_client.expire(session_hash_key, SESSION_TTL)
        await redis_client.set(
            _user_cache_key(user.id), json.dumps(user_data), ex=USER_CACHE_TTL
        )
    except Exception:
        redis_error = True
        logger.warning(
            "Web login failed to persist session cache",
            exc_info=True,
            extra=_security_log_extra(
                request, username_hash=username_hash, event=f"{event_scope}_error"
            ),
        )
    finally:
        if redis_client:
            await redis_client.aclose()

    # Redirect to next page or home
    redirect_url = _sanitize_next(next_value) or "/"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
    )

    if redis_error:
        logger.info(
            "Web login succeeded without cache persistence",
            extra=_security_log_extra(
                request,
                username_hash=username_hash,
                event=f"{event_scope}_cache_bypass",
            ),
        )

    logger.info(
        "Web login succeeded",
        extra=_security_log_extra(
            request, username_hash=username_hash, event=f"{event_scope}_success"
        ),
    )

    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str | None = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Display login page."""
    # Check if already logged in
    if db:
        user = await get_current_user_from_session(request, db)
        if user:
            redirect_url = _sanitize_next(next) or "/"
            return RedirectResponse(
                url=redirect_url, status_code=status.HTTP_303_SEE_OTHER
            )

    return _login_page_response(request, next_value=next)


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    background_tasks: BackgroundTasks,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: str | None = Form(None),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Handle password login with IP limiting and account-local cooldowns."""
    normalized_username = username.strip()
    username_hash = hashlib.sha256(normalized_username.encode()).hexdigest()[:16]
    result = await db.execute(
        select(User).where(User.username == normalized_username).with_for_update()
    )
    user = result.scalar_one_or_none()
    now = utc_now()

    if user is None or not user.hashed_password:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        logger.warning(
            "Web login failed",
            extra=_security_log_extra(
                request,
                username_hash=username_hash,
                event="web_login_failure",
                reason="unknown_or_passwordless",
            ),
        )
        return _login_page_response(
            request,
            next_value=next,
            error=_INVALID_CREDENTIALS,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    retry_after = cooldown_retry_seconds(user, now=now)
    if retry_after is not None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        logger.warning(
            "Web login delayed by account cooldown",
            extra=_security_log_extra(
                request,
                username_hash=username_hash,
                event="web_login_cooldown",
                retry_after_seconds=retry_after,
            ),
        )
        return _login_page_response(
            request,
            next_value=next,
            error=_INVALID_CREDENTIALS,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        password_matches = verify_password(password, user.hashed_password)
    except (TypeError, ValueError):
        password_matches = False

    if not password_matches or not user.is_active:
        transition = record_password_failure(user, now=now) if user.is_active else None
        if transition is not None or user.failed_login_attempts:
            await db.commit()
        if transition is not None and user.email and recovery_email_configured():
            background_tasks.add_task(send_cooldown_email, user.email, transition)
        logger.warning(
            "Web login failed",
            extra=_security_log_extra(
                request,
                username_hash=username_hash,
                event="web_login_failure",
                reason="invalid_credentials",
                cooldown_level=transition.level if transition else None,
            ),
        )
        return _login_page_response(
            request,
            next_value=next,
            error=_INVALID_CREDENTIALS,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if (
        user.failed_login_attempts
        or user.login_cooldown_level
        or user.login_cooldown_until is not None
    ):
        reset_password_throttle(user)
        await db.commit()

    return await _issue_web_session_response(
        request, user, next_value=next, event_scope="web_login"
    )


@router.post("/google")
@limiter.limit("5/minute")
async def google_login(
    request: Request,
    credential: Annotated[str, Form()],
    next: str | None = Form(None),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Turn a Google Identity Services ID token into the normal web session.

    The login page posts here same-origin, so the shared
    ``TemplateFormCSRFMiddleware`` hidden-field check applies unchanged; no
    Google-specific ``g_csrf_token`` double-submit scheme is introduced.

    Unlike ``POST /api/v1/auth/google`` (the Android surface) this route never
    provisions accounts: an ID token for an unknown ``google_sub`` is refused.
    The browser surface is reachable by anyone who clears the network
    allowlist, so creating accounts stays an explicit admin action.

    Operator setup lives on ``settings.WEB_GOOGLE_OAUTH_CLIENT_ID``
    (app/core/config.py): a Google Cloud "Web application" OAuth client whose
    "Authorized JavaScript origins" contains the admin origin. No redirect URI
    and no client secret are involved.
    """
    client_id = settings.WEB_GOOGLE_OAUTH_CLIENT_ID.strip()
    if not client_id:
        logger.warning(
            "Web Google login rejected: WEB_GOOGLE_OAUTH_CLIENT_ID is unset",
            extra=_security_log_extra(request, event="web_google_login_unavailable"),
        )
        return _login_page_response(
            request,
            next_value=next,
            error="Google 로그인이 설정되지 않았습니다.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    def _rejected(
        reason: str,
        message: str = "등록되지 않은 Google 계정이거나 인증에 실패했습니다.",
    ) -> Response:
        logger.warning(
            "Web Google login failed",
            extra=_security_log_extra(
                request, event="web_google_login_failure", reason=reason
            ),
        )
        return _login_page_response(
            request,
            next_value=next,
            error=message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # Reuse the Android surface's verification verbatim
        # (app/extensions/kasset/api/auth.py:470-487): Google JWKS signature,
        # RS256 only, aud == our client id, iss in Google's issuers, and
        # aud/exp/iss/sub required.
        payload = await MobileAuthService._decode_google_id_token(credential, client_id)
    except MobileApiError:
        return _rejected("invalid_id_token")

    if payload.get("email_verified") is not True:
        return _rejected("email_not_verified")

    google_sub = payload.get("sub")
    if not isinstance(google_sub, str) or not google_sub:
        return _rejected("missing_sub")

    result = await db.execute(
        select(User).where(User.google_sub == google_sub).with_for_update()
    )
    user = result.scalar_one_or_none()
    if user is None:
        return _rejected("unknown_google_sub")

    if not user.is_active:
        return _rejected("inactive_user")

    return await _issue_web_session_response(
        request, user, next_value=next, event_scope="web_google_login"
    )


def _forgot_password_response(
    request: Request,
    *,
    email: str | None = None,
    error: str | None = None,
    success: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"email": email, "error": error, "success": success},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _reset_password_response(
    request: Request,
    *,
    code: str | None = None,
    error: str | None = None,
    success: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={"code": code, "error": error, "success": success},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request) -> Response:
    return _forgot_password_response(request)


@router.post("/forgot-password")
@limiter.limit("3/hour")
async def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> Response:
    """Issue a single-use code without revealing whether the account exists."""
    normalized_email = email.strip().lower()
    email_hash = hashlib.sha256(normalized_email.encode()).hexdigest()[:16]
    result = await db.execute(
        select(User)
        .where(
            func.lower(User.email) == normalized_email,
            User.is_active.is_(True),
            User.hashed_password.is_not(None),
        )
        .limit(2)
    )
    matches = list(result.scalars().all())
    user = matches[0] if len(matches) == 1 else None
    delivery_scheduled = False

    if user is not None and user.email and recovery_email_configured():
        code = await issue_password_reset_code(db, user, now=utc_now())
        recipient = user.email
        await db.commit()
        background_tasks.add_task(send_password_reset_email, recipient, code)
        delivery_scheduled = True
    else:
        await db.rollback()

    logger.info(
        "Password recovery requested",
        extra=_security_log_extra(
            request,
            email_hash=email_hash,
            event="password_recovery_requested",
            delivery_scheduled=delivery_scheduled,
        ),
    )
    return _forgot_password_response(
        request,
        success=GENERIC_RECOVERY_MESSAGE,
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request) -> Response:
    return _reset_password_response(request)


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    code: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> Response:
    if password != password_confirm:
        return _reset_password_response(
            request,
            code=code,
            error="비밀번호가 일치하지 않습니다.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        PasswordResetConfirm(password=password)
    except ValidationError as exc:
        error_message = "새 비밀번호가 보안 기준을 충족하지 않습니다."
        if exc.errors():
            error_message = str(exc.errors()[0].get("msg", error_message))
            if error_message.startswith("Value error, "):
                error_message = error_message[13:]
        return _reset_password_response(
            request,
            code=code,
            error=error_message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = await consume_password_reset_code(
        db,
        code=code,
        password_hash=get_password_hash(password),
        now=utc_now(),
    )
    if user is None:
        await db.rollback()
        return _reset_password_response(
            request,
            error="복구 코드가 올바르지 않거나 만료되었습니다.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_id = user.id
    await db.commit()
    try:
        await invalidate_user_cache(user_id)
    except Exception:
        logger.warning(
            "Password reset could not invalidate cached sessions for user_id=%s",
            user_id,
            exc_info=True,
        )

    logger.info(
        "Password reset completed",
        extra=_security_log_extra(
            request,
            user_id=user_id,
            event="password_reset_success",
        ),
    )
    response = _reset_password_response(
        request,
        success="비밀번호를 변경했습니다. 새 비밀번호로 로그인하세요.",
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Display registration page when explicit self-registration is enabled."""
    if not settings.WEB_REGISTRATION_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={},
    )


@router.post("/register")
async def register(
    request: Request,
    email: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Handle registration form submission when explicitly enabled."""
    if not settings.WEB_REGISTRATION_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Validate password confirmation first
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호가 일치하지 않습니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Use UserCreate schema for validation
    try:
        user_data = UserCreate(email=email, username=username, password=password)
    except ValidationError as e:
        # Extract the first error message
        error_msg = "입력값이 올바르지 않습니다."
        if e.errors():
            error_msg = e.errors()[0].get("msg", error_msg)
            # Remove "Value error, " prefix if present
            if error_msg.startswith("Value error, "):
                error_msg = error_msg[13:]

        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": error_msg,
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Check if username already exists
    result = await db.execute(select(User).where(User.username == user_data.username))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "이미 사용 중인 사용자명입니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "이미 사용 중인 이메일입니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Create new user
    hashed_password = get_password_hash(password)
    db_user = User(
        email=email,
        username=username,
        hashed_password=hashed_password,
        role=UserRole.viewer,  # Default role
        is_active=True,
    )

    try:
        db.add(db_user)
        await db.commit()
        await db.refresh(db_user)
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "계정을 생성할 수 없습니다. 다시 시도해주세요.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Show success message and redirect to login
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "success": "회원가입이 완료되었습니다! 로그인해주세요.",
        },
        status_code=status.HTTP_201_CREATED,
        headers={"Refresh": "2; url=/web-auth/login"},
    )


@router.get("/logout")
async def logout(request: Request):
    """Handle logout."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)

    if session_token:
        user_id = verify_session_token(session_token)
        if user_id:
            import redis.asyncio as redis

            redis_client = None
            try:
                redis_client = redis.from_url(
                    settings.get_redis_url(),
                    decode_responses=True,
                )
                session_hash = _hash_session_token(session_token)
                await redis_client.srem(_session_hash_key(user_id), session_hash)
            except Exception:
                logger.warning(
                    "Failed to invalidate session cache during logout for user_id=%s",
                    user_id,
                    exc_info=True,
                )
            finally:
                if redis_client:
                    await redis_client.aclose()

    response = RedirectResponse(
        url="/web-auth/login", status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
