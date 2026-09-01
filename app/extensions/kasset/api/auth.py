"""Public account and device-session authentication for KAsset clients."""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated
from uuid import uuid4

import jwt
from fastapi import Depends, Header
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.nickname import (
    ensure_user_nickname,
    generate_random_nickname,
    normalize_nickname,
)
from app.auth.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from app.auth.token_repository import hash_refresh_token
from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.errors import MobileApiError, unauthorized
from app.extensions.kasset.api.push_tokens import detach_fcm_token
from app.extensions.kasset.api.schemas import (
    CurrentUserResponse,
    GoogleLoginRequest,
    LoginRequest,
    NicknameUpdateRequest,
    RegisterRequest,
    SessionTokens,
)
from app.extensions.kasset.models import KAssetDeviceSession
from app.models.trading import User, UserRole

_MOBILE_CLIENT = "kasset-android"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_GOOGLE_JWKS_CLIENT = jwt.PyJWKClient(_GOOGLE_JWKS_URL)


def _iso_z(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MobileSession:
    user: User
    device_session: KAssetDeviceSession
    device_id: str


class MobileAuthService:
    async def register(
        self,
        db: AsyncSession,
        request: RegisterRequest,
    ) -> SessionTokens:
        username = request.username.strip()
        email = str(request.email).strip().lower()
        self._validate_username(username)
        self._validate_password(request.password)
        await self._assert_identity_available(db, username=username, email=email)

        user = User(
            username=username,
            email=email,
            nickname=generate_random_nickname(),
            hashed_password=get_password_hash(request.password),
            role=UserRole.trader,
            is_active=True,
        )
        try:
            db.add(user)
            await db.flush()
            tokens = await self._issue(
                db,
                user,
                device_id=request.device_id,
                device_name=request.device_name,
            )
            await db.commit()
            return tokens
        except IntegrityError as err:
            await db.rollback()
            await self._raise_identity_conflict(
                db,
                username=username,
                email=email,
                cause=err,
            )
            raise AssertionError("identity conflict classification must raise") from err

    async def login(
        self,
        db: AsyncSession,
        request: LoginRequest,
    ) -> SessionTokens:
        identifier = request.username.strip()
        user = await self._login_user(db, identifier)
        password_matches = False
        if user is not None and user.hashed_password:
            try:
                password_matches = verify_password(
                    request.password, user.hashed_password
                )
            except Exception:
                password_matches = False
        else:
            get_password_hash("KAsset-invalid-login-timing-guard-1!")

        if user is None or not password_matches or not user.is_active:
            raise MobileApiError(
                401,
                "INVALID_CREDENTIALS",
                "사용자 이름 또는 비밀번호가 올바르지 않습니다.",
            )

        try:
            tokens = await self._issue(
                db,
                user,
                device_id=request.device_id,
                device_name=request.device_name,
            )
            await db.commit()
            return tokens
        except IntegrityError as err:
            await db.rollback()
            raise unauthorized() from err

    async def google_login(
        self,
        db: AsyncSession,
        request: GoogleLoginRequest,
    ) -> SessionTokens:
        client_id = settings.KASSET_GOOGLE_OAUTH_CLIENT_ID.strip()
        if not client_id:
            raise MobileApiError(
                503,
                "GOOGLE_LOGIN_UNAVAILABLE",
                "Google 로그인을 사용할 수 없습니다.",
            )

        payload = await self._decode_google_id_token(request.id_token, client_id)
        if payload.get("email_verified") is not True:
            raise self._invalid_google_token()
        google_sub = self._claim(payload, "sub")

        user = await db.scalar(
            select(User).where(User.google_sub == google_sub).with_for_update()
        )
        if user is None:
            email = await self._available_google_email(db, payload.get("email"))
            user = User(
                username=await self._available_google_username(db, google_sub),
                email=email,
                nickname=generate_random_nickname(),
                google_sub=google_sub,
                hashed_password=None,
                role=UserRole.trader,
                is_active=True,
            )
            try:
                db.add(user)
                await db.flush()
            except IntegrityError as err:
                await db.rollback()
                user = await db.scalar(
                    select(User).where(User.google_sub == google_sub).with_for_update()
                )
                if user is None:
                    raise MobileApiError(
                        409,
                        "GOOGLE_ACCOUNT_CONFLICT",
                        "Google 계정을 만들 수 없습니다.",
                    ) from err

        if not user.is_active:
            raise unauthorized("사용할 수 없는 계정입니다.")

        try:
            tokens = await self._issue(
                db,
                user,
                device_id=request.device_id,
                device_name=request.device_name,
            )
            await db.commit()
            return tokens
        except IntegrityError as err:
            await db.rollback()
            raise unauthorized() from err

    async def refresh(self, db: AsyncSession, refresh_token: str) -> SessionTokens:
        payload = self._decode(refresh_token, expected_type="refresh")
        user_id = self._integer_claim(payload, "uid")
        username = self._claim(payload, "sub")
        device_id = self._claim(payload, "deviceId")
        device_name = self._claim(payload, "deviceName")
        session_id = self._claim(payload, "sessionId")
        user = await self._active_user(
            db,
            user_id=user_id,
            username=username,
            for_update=True,
        )

        result = await db.execute(
            select(KAssetDeviceSession)
            .where(
                KAssetDeviceSession.id == session_id,
                KAssetDeviceSession.owner_user_id == user.id,
                KAssetDeviceSession.device_id == device_id,
            )
            .with_for_update()
        )
        session_record = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if (
            session_record is None
            or session_record.revoked_at is not None
            or session_record.device_name != device_name
            or session_record.refresh_token_hash != hash_refresh_token(refresh_token)
            or _as_utc(session_record.expires_at) <= now
        ):
            raise unauthorized()

        tokens = await self._issue(
            db,
            user,
            device_id=device_id,
            device_name=device_name,
            session_record=session_record,
        )
        await db.commit()
        return tokens

    async def authenticate(self, db: AsyncSession, access_token: str) -> MobileSession:
        payload = self._decode(access_token, expected_type="access")
        user_id = self._integer_claim(payload, "uid")
        username = self._claim(payload, "sub")
        device_id = self._claim(payload, "deviceId")
        session_id = self._claim(payload, "sessionId")
        user = await self._active_user(db, user_id=user_id, username=username)

        # Deliberately does not compare the token's "sid" claim against
        # ``refresh_token_hash``: that hash rotates on every refresh, so one
        # refresh would invalidate every unexpired access token already in
        # flight on the device. Revocation stays immediate (``revoked_at``),
        # the session still expires (``expires_at``), and the access token's
        # own short ``exp`` bounds how long a leaked token survives.
        result = await db.execute(
            select(KAssetDeviceSession).where(
                KAssetDeviceSession.id == session_id,
                KAssetDeviceSession.owner_user_id == user.id,
                KAssetDeviceSession.device_id == device_id,
                KAssetDeviceSession.revoked_at.is_(None),
            )
        )
        session_record = result.scalar_one_or_none()
        if session_record is None or _as_utc(session_record.expires_at) <= datetime.now(
            UTC
        ):
            raise unauthorized()
        return MobileSession(user, session_record, device_id)

    async def revoke(self, db: AsyncSession, session: MobileSession) -> None:
        result = await db.execute(
            select(KAssetDeviceSession)
            .where(
                KAssetDeviceSession.id == session.device_session.id,
                KAssetDeviceSession.owner_user_id == session.user.id,
                KAssetDeviceSession.device_id == session.device_id,
            )
            .with_for_update()
        )
        session_record = result.scalar_one_or_none()
        if session_record is None:
            raise unauthorized()
        now = datetime.now(UTC)
        session_record.revoked_at = now
        # Logout must stop push in the same transaction that kills the session;
        # otherwise a revoked device keeps receiving another person's alerts if
        # the phone changes hands.
        detach_fcm_token(session_record, now=now)
        await db.commit()

    async def current_user(
        self,
        db: AsyncSession,
        session: MobileSession,
    ) -> CurrentUserResponse:
        user = session.user
        if not user.username or not user.email:
            raise unauthorized()
        nickname = await ensure_user_nickname(db, user)
        return CurrentUserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            nickname=nickname,
            role=user.role,
        )

    async def update_nickname(
        self,
        db: AsyncSession,
        session: MobileSession,
        request: NicknameUpdateRequest,
    ) -> CurrentUserResponse:
        try:
            nickname = normalize_nickname(request.nickname)
        except ValueError as err:
            raise MobileApiError(
                422,
                "VALIDATION_ERROR",
                "닉네임은 공백을 제외하고 1자에서 16자 사이여야 합니다.",
            ) from err
        session.user.nickname = nickname
        await db.commit()
        await db.refresh(session.user)
        return await self.current_user(db, session)

    async def _issue(
        self,
        db: AsyncSession,
        user: User,
        *,
        device_id: str,
        device_name: str,
        session_record: KAssetDeviceSession | None = None,
    ) -> SessionTokens:
        normalized_device_id = device_id.strip()
        normalized_device_name = device_name.strip()
        if not normalized_device_id or not normalized_device_name:
            raise MobileApiError(
                422, "VALIDATION_ERROR", "기기 정보가 올바르지 않습니다."
            )
        if session_record is None:
            result = await db.execute(
                select(KAssetDeviceSession)
                .where(
                    KAssetDeviceSession.owner_user_id == user.id,
                    KAssetDeviceSession.device_id == normalized_device_id,
                )
                .with_for_update()
            )
            session_record = result.scalar_one_or_none()

        session_id = session_record.id if session_record is not None else str(uuid4())
        now = datetime.now(UTC)
        access_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        base_claims = {
            "sub": user.username,
            "uid": str(user.id),
            "client": _MOBILE_CLIENT,
            "deviceId": normalized_device_id,
            "deviceName": normalized_device_name,
            "jti": secrets.token_urlsafe(18),
            "sessionId": session_id,
        }
        refresh_token = create_refresh_token(base_claims, expires_delta=refresh_delta)
        session_token_hash = hash_refresh_token(refresh_token)
        access_token = create_access_token(
            {**base_claims, "sid": session_token_hash}, expires_delta=access_delta
        )

        if session_record is None:
            session_record = KAssetDeviceSession(
                id=session_id,
                owner_user_id=user.id,
                device_id=normalized_device_id,
                device_name=normalized_device_name,
                refresh_token_hash=session_token_hash,
                expires_at=now + refresh_delta,
                revoked_at=None,
            )
            db.add(session_record)
        else:
            if (
                session_record.owner_user_id != user.id
                or session_record.device_id != normalized_device_id
            ):
                raise unauthorized()
            session_record.device_name = normalized_device_name
            session_record.refresh_token_hash = session_token_hash
            session_record.expires_at = now + refresh_delta
            session_record.revoked_at = None
        await db.flush()
        return SessionTokens(
            accessToken=access_token,
            refreshToken=refresh_token,
            accessTokenExpiresAt=_iso_z(now + access_delta),
            refreshTokenExpiresAt=_iso_z(now + refresh_delta),
            serverVersion=settings.KASSET_SERVER_VERSION,
        )

    async def _active_user(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        username: str,
        for_update: bool = False,
    ) -> User:
        query = select(User).where(User.id == user_id, User.username == username)
        if for_update:
            query = query.with_for_update()
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise unauthorized()
        return user

    @staticmethod
    async def _login_user(db: AsyncSession, identifier: str) -> User | None:
        if not identifier:
            return None
        username_result = await db.execute(
            select(User)
            .where(func.lower(User.username) == identifier.lower())
            .limit(2)
            .with_for_update()
        )
        username_matches = list(username_result.scalars().all())
        if len(username_matches) == 1:
            return username_matches[0]
        if username_matches:
            return None
        email_result = await db.execute(
            select(User)
            .where(func.lower(User.email) == identifier.lower())
            .limit(2)
            .with_for_update()
        )
        email_matches = list(email_result.scalars().all())
        return email_matches[0] if len(email_matches) == 1 else None

    @staticmethod
    async def _available_google_email(
        db: AsyncSession, email_claim: object
    ) -> str | None:
        if not isinstance(email_claim, str):
            return None
        email = email_claim.strip().lower()
        if not email:
            return None
        email_exists = await db.scalar(
            select(User.id).where(func.lower(User.email) == email).limit(1)
        )
        return None if email_exists is not None else email

    @staticmethod
    async def _available_google_username(db: AsyncSession, google_sub: str) -> str:
        base = f"google-{google_sub}"[:32]
        candidate = base
        digest = sha256(google_sub.encode()).hexdigest()[:12]
        attempt = 0
        while (
            await db.scalar(
                select(User.id)
                .where(func.lower(User.username) == candidate.lower())
                .limit(1)
            )
            is not None
        ):
            suffix = f"-{digest}" if attempt == 0 else f"-{digest}-{attempt}"
            candidate = f"{base[: 50 - len(suffix)]}{suffix}"
            attempt += 1
        return candidate

    @staticmethod
    async def _decode_google_id_token(
        id_token: str, client_id: str
    ) -> dict[str, object]:
        try:
            signing_key = await asyncio.to_thread(
                _GOOGLE_JWKS_CLIENT.get_signing_key_from_jwt, id_token
            )
            return jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=_GOOGLE_ISSUERS,
                options={"require": ["aud", "exp", "iss", "sub"]},
            )
        except jwt.PyJWTError as err:
            raise MobileAuthService._invalid_google_token() from err

    @staticmethod
    def _invalid_google_token() -> MobileApiError:
        return MobileApiError(
            401,
            "INVALID_GOOGLE_TOKEN",
            "Google 인증 토큰이 올바르지 않습니다.",
        )

    @staticmethod
    async def _assert_identity_available(
        db: AsyncSession,
        *,
        username: str,
        email: str,
    ) -> None:
        username_exists = await db.scalar(
            select(User.id)
            .where(func.lower(User.username) == username.lower())
            .limit(1)
        )
        if username_exists is not None:
            raise MobileApiError(
                409, "USERNAME_TAKEN", "이미 사용 중인 사용자 이름입니다."
            )
        email_exists = await db.scalar(
            select(User.id).where(func.lower(User.email) == email.lower()).limit(1)
        )
        if email_exists is not None:
            raise MobileApiError(409, "EMAIL_TAKEN", "이미 사용 중인 이메일입니다.")

    @staticmethod
    async def _raise_identity_conflict(
        db: AsyncSession,
        *,
        username: str,
        email: str,
        cause: IntegrityError,
    ) -> None:
        try:
            await MobileAuthService._assert_identity_available(
                db,
                username=username,
                email=email,
            )
        except MobileApiError as err:
            raise err from cause
        raise MobileApiError(
            409, "IDENTITY_CONFLICT", "계정을 만들 수 없습니다."
        ) from cause

    @staticmethod
    def _validate_username(username: str) -> None:
        if len(username) < 3 or len(username) > 50:
            raise MobileApiError(
                422, "VALIDATION_ERROR", "사용자 이름은 3자 이상 50자 이하여야 합니다."
            )

    @staticmethod
    def _validate_password(password: str) -> None:
        if (
            len(password) < 8
            or not any(character.isupper() for character in password)
            or not any(character.isdigit() for character in password)
            or not any(character in string.punctuation for character in password)
        ):
            raise MobileApiError(
                422,
                "WEAK_PASSWORD",
                "비밀번호는 8자 이상이며 대문자, 숫자, 특수문자를 포함해야 합니다.",
            )

    @staticmethod
    def _decode(token: str, *, expected_type: str) -> dict:
        try:
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except jwt.PyJWTError as err:
            raise unauthorized() from err
        if (
            payload.get("type") != expected_type
            or payload.get("client") != _MOBILE_CLIENT
        ):
            raise unauthorized()
        return payload

    @staticmethod
    def _claim(payload: dict, name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise unauthorized()
        return value

    @staticmethod
    def _integer_claim(payload: dict, name: str) -> int:
        value = MobileAuthService._claim(payload, name)
        if not value.isascii() or not value.isdecimal():
            raise unauthorized()
        return int(value)


mobile_auth = MobileAuthService()


async def get_mobile_session(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> MobileSession:
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized("인증 토큰이 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise unauthorized("인증 토큰이 필요합니다.")
    return await mobile_auth.authenticate(db, token)
