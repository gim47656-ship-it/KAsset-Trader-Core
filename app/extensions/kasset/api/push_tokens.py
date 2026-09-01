"""FCM registration token attribution for one authenticated device session.

The plaintext token is what Firebase needs to address a device, so it is stored
on ``kasset_device_sessions``. Nothing here ever returns, logs, or raises the
token: callers get ``None`` and the HTTP layer answers ``204``. The SHA-256
fingerprint is the only value used for lookups and cross-session comparison.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import unauthorized
from app.extensions.kasset.models import KAssetDeviceSession


def hash_fcm_token(token: str) -> str:
    """Stable fingerprint used as the only identifier for a token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def detach_fcm_token(record: KAssetDeviceSession, *, now: datetime) -> None:
    """Drop the token from a session row without committing.

    Kept separate so session revocation can retire the token inside the very
    transaction that records ``revoked_at``.
    """

    if record.fcm_token is None and record.fcm_token_hash is None:
        return
    record.fcm_token = None
    record.fcm_token_hash = None
    record.fcm_token_updated_at = now


async def _locked_session(
    db: AsyncSession,
    *,
    session_id: str,
    owner_user_id: int,
    device_id: str,
) -> KAssetDeviceSession:
    record = await db.scalar(
        select(KAssetDeviceSession)
        .where(
            KAssetDeviceSession.id == session_id,
            KAssetDeviceSession.owner_user_id == owner_user_id,
            KAssetDeviceSession.device_id == device_id,
            KAssetDeviceSession.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if record is None:
        raise unauthorized()
    return record


async def register_push_token(
    db: AsyncSession,
    *,
    session_id: str,
    owner_user_id: int,
    device_id: str,
    token: str,
    now: datetime | None = None,
) -> None:
    """Bind ``token`` to exactly this session, taking it from any other.

    A reinstalled app or a device handed to another account can present a token
    Firebase already re-issued elsewhere. Clearing the previous holder in the
    same transaction is what keeps one physical device from receiving a second
    owner's alerts.
    """

    instant = now or datetime.now(UTC)
    record = await _locked_session(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
        device_id=device_id,
    )
    digest = hash_fcm_token(token)
    await db.execute(
        update(KAssetDeviceSession)
        .where(
            KAssetDeviceSession.fcm_token_hash == digest,
            KAssetDeviceSession.id != record.id,
        )
        .values(fcm_token=None, fcm_token_hash=None, fcm_token_updated_at=instant)
        .execution_options(synchronize_session=False)
    )
    record.fcm_token = token
    record.fcm_token_hash = digest
    record.fcm_token_updated_at = instant
    await db.commit()


async def clear_push_token(
    db: AsyncSession,
    *,
    session_id: str,
    owner_user_id: int,
    device_id: str,
    now: datetime | None = None,
) -> None:
    """Retire this session's token. Idempotent when none is registered."""

    record = await _locked_session(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
        device_id=device_id,
    )
    detach_fcm_token(record, now=now or datetime.now(UTC))
    await db.commit()


__all__ = [
    "clear_push_token",
    "detach_fcm_token",
    "hash_fcm_token",
    "register_push_token",
]
