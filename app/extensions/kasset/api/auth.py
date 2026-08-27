"""Pairing facade backed by the Core JWT and hashed refresh-token store."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import create_access_token, create_refresh_token
from app.auth.token_repository import hash_refresh_token, save_refresh_token
from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.errors import MobileApiError, unauthorized
from app.extensions.kasset.api.schemas import SessionTokens
from app.models.trading import RefreshToken, User, UserRole

_MOBILE_CLIENT = "kasset-android"


def _iso_z(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MobileSession:
    user: User
    refresh_token_record: RefreshToken
    device_id: str


class MobileAuthService:
    async def pair(
        self,
        db: AsyncSession,
        *,
        pairing_code: str,
        device_id: str,
        device_name: str,
    ) -> SessionTokens:
        configured = settings.PAIRING_SECRET
        if configured is None or not configured.get_secret_value():
            raise MobileApiError(
                503,
                "CONFIGURATION_ERROR",
                "서버의 페어링 설정이 준비되지 않았습니다.",
            )
        expected = hashlib.sha256(configured.get_secret_value().encode()).digest()
        supplied = hashlib.sha256(pairing_code.encode()).digest()
        if not hmac.compare_digest(expected, supplied):
            raise MobileApiError(
                401, "INVALID_PAIRING_CODE", "페어링 코드가 올바르지 않습니다."
            )

        user = await self._get_or_create_mobile_user(db)
        tokens = await self._issue(
            db, user, device_id=device_id, device_name=device_name
        )
        await db.commit()
        return tokens

    async def refresh(self, db: AsyncSession, refresh_token: str) -> SessionTokens:
        payload = self._decode(refresh_token, expected_type="refresh")
        username = self._claim(payload, "sub")
        device_id = self._claim(payload, "deviceId")
        device_name = self._claim(payload, "deviceName")

        user = await self._active_user(db, username)
        token_hash = hash_refresh_token(refresh_token)
        result = await db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user.id,
                RefreshToken.token_hash == token_hash,
            )
            .with_for_update()
        )
        token_record = result.scalar_one_or_none()
        if (
            token_record is None
            or token_record.revoked
            or _as_utc(token_record.expires_at) <= datetime.now(UTC)
        ):
            raise unauthorized()

        token_record.revoked = True
        tokens = await self._issue(
            db, user, device_id=device_id, device_name=device_name
        )
        await db.commit()
        return tokens

    async def authenticate(self, db: AsyncSession, access_token: str) -> MobileSession:
        payload = self._decode(access_token, expected_type="access")
        username = self._claim(payload, "sub")
        device_id = self._claim(payload, "deviceId")
        session_id = self._claim(payload, "sid")
        user = await self._active_user(db, username)

        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.token_hash == session_id,
                RefreshToken.revoked == False,  # noqa: E712
            )
        )
        token_record = result.scalar_one_or_none()
        if token_record is None or _as_utc(token_record.expires_at) <= datetime.now(
            UTC
        ):
            raise unauthorized()
        return MobileSession(user, token_record, device_id)

    async def revoke(self, db: AsyncSession, session: MobileSession) -> None:
        session.refresh_token_record.revoked = True
        await db.commit()

    async def _get_or_create_mobile_user(self, db: AsyncSession) -> User:
        username = settings.KASSET_MOBILE_USERNAME.strip()
        if not username:
            raise MobileApiError(
                503,
                "CONFIGURATION_ERROR",
                "서버의 모바일 사용자 설정이 올바르지 않습니다.",
            )
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                username=username,
                email=None,
                hashed_password=None,
                role=UserRole.trader,
                is_active=True,
            )
            db.add(user)
            await db.flush()
        if not user.is_active:
            raise unauthorized("비활성화된 모바일 사용자입니다.")
        return user

    async def _active_user(self, db: AsyncSession, username: str) -> User:
        mobile_username = settings.KASSET_MOBILE_USERNAME.strip()
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active or user.username != mobile_username:
            raise unauthorized()
        return user

    async def _issue(
        self,
        db: AsyncSession,
        user: User,
        *,
        device_id: str,
        device_name: str,
    ) -> SessionTokens:
        now = datetime.now(UTC)
        access_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        base_claims = {
            "sub": user.username,
            "client": _MOBILE_CLIENT,
            "deviceId": device_id,
            "deviceName": device_name,
            "jti": secrets.token_urlsafe(18),
        }
        refresh_token = create_refresh_token(base_claims, expires_delta=refresh_delta)
        session_id = hash_refresh_token(refresh_token)
        access_token = create_access_token(
            {**base_claims, "sid": session_id}, expires_delta=access_delta
        )
        await save_refresh_token(db, user.id, refresh_token)
        return SessionTokens(
            accessToken=access_token,
            refreshToken=refresh_token,
            accessTokenExpiresAt=_iso_z(now + access_delta),
            refreshTokenExpiresAt=_iso_z(now + refresh_delta),
            serverVersion=settings.KASSET_SERVER_VERSION,
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
