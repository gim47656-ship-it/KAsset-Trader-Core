"""Authentication dependencies for FastAPI."""

from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import TokenData
from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.paths import is_kasset_token_allowed_path
from app.extensions.kasset.models import KAssetDeviceSession
from app.models.trading import User

# OAuth2 scheme for token authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
) -> User:
    """
    Get the current authenticated user from JWT token.

    Args:
        token: JWT access token from Authorization header
        db: Database session

    Returns:
        User object from database

    Raises:
        HTTPException: If token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        username = payload.get("sub")
        token_type = payload.get("type")

        if not isinstance(username, str) or not username or token_type != "access":
            raise credentials_exception

        token_data = TokenData(username=username)
    except jwt.ExpiredSignatureError as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from err
    except jwt.InvalidTokenError as err:
        raise credentials_exception from err

    # Query user from database
    result = await db.execute(select(User).where(User.username == token_data.username))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    if payload.get("client") == "kasset-android":
        # KAsset mobile tokens carry trader role for the mobile product only.
        # They are valid solely on the Android compatibility surface and the
        # recommendation review API; every other generic Core endpoint
        # (including trader-gated web APIs) must reject them.
        if not is_kasset_token_allowed_path(request.url.path):
            raise credentials_exception
        uid = payload.get("uid")
        device_id = payload.get("deviceId")
        session_id = payload.get("sessionId")
        if (
            not isinstance(uid, str)
            or not uid.isascii()
            or not uid.isdecimal()
            or int(uid) != user.id
            or not isinstance(device_id, str)
            or not device_id
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise credentials_exception
        # The device session's refresh_token_hash rotates on every refresh, so
        # requiring the access token's `sid` to still match it would kill every
        # unexpired access token the moment one request refreshes. Session
        # identity, revocation and expiry below are the real gate; the access
        # token's own signature and short expiry cover the rest.
        session_result = await db.execute(
            select(KAssetDeviceSession).where(
                KAssetDeviceSession.id == session_id,
                KAssetDeviceSession.owner_user_id == user.id,
                KAssetDeviceSession.device_id == device_id,
                KAssetDeviceSession.revoked_at.is_(None),
            )
        )
        device_session = session_result.scalar_one_or_none()
        if device_session is None:
            raise credentials_exception
        expires_at = device_session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at.astimezone(UTC) <= datetime.now(UTC):
            raise credentials_exception

    return user


def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """
    Get the current active user.

    Args:
        current_user: User from get_current_user dependency

    Returns:
        User object if active

    Raises:
        HTTPException: If user is inactive
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )
    return current_user
