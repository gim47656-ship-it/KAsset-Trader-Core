"""AES-256-GCM broker credential vault for the Android facade."""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.models import BrokerCredential

_VERSION = "v1"
_NONCE_BYTES = 12


@dataclass(frozen=True, slots=True)
class RevealedBrokerCredential:
    credential_id: str
    provider: str
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None


class CredentialVault:
    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()

    async def store_nh(
        self,
        db: AsyncSession,
        *,
        app_key: str,
        app_secret: str,
        account_no: str,
    ) -> BrokerCredential:
        values = {
            "app_key": self._required(app_key, "App Key"),
            "app_secret": self._required(app_secret, "App Secret"),
            "account_no": self._required(account_no, "Account Number"),
        }
        async with self._write_lock:
            record = await self.record(db, "NH", for_update=True)
            credential_id = record.id if record is not None else str(uuid4())
            encrypted = {
                field_name: self._encrypt(
                    value,
                    credential_id=credential_id,
                    provider="NH",
                    field_name=field_name,
                )
                for field_name, value in values.items()
            }
            if record is None:
                record = BrokerCredential(
                    id=credential_id,
                    provider="NH",
                    encrypted_app_key=encrypted["app_key"],
                    encrypted_app_secret=encrypted["app_secret"],
                    encrypted_account_no=encrypted["account_no"],
                    last_verified_at=None,
                )
                db.add(record)
            else:
                record.encrypted_app_key = encrypted["app_key"]
                record.encrypted_app_secret = encrypted["app_secret"]
                record.encrypted_account_no = encrypted["account_no"]
                record.last_verified_at = None
            await db.commit()
            await db.refresh(record)
            return record

    async def record(
        self, db: AsyncSession, provider: str, *, for_update: bool = False
    ) -> BrokerCredential | None:
        stmt = select(BrokerCredential).where(
            BrokerCredential.provider == provider.strip().upper()
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def reveal_nh(self, db: AsyncSession) -> RevealedBrokerCredential:
        record = await self.record(db, "NH")
        if record is None:
            raise MobileApiError(
                409, "BROKER_NOT_CONNECTED", "NH PLUG 연결 정보가 등록되지 않았습니다."
            )
        return RevealedBrokerCredential(
            credential_id=record.id,
            provider=record.provider,
            app_key=self._decrypt(
                record.encrypted_app_key,
                credential_id=record.id,
                provider=record.provider,
                field_name="app_key",
            ),
            app_secret=self._decrypt(
                record.encrypted_app_secret,
                credential_id=record.id,
                provider=record.provider,
                field_name="app_secret",
            ),
            account_no=self._decrypt(
                record.encrypted_account_no,
                credential_id=record.id,
                provider=record.provider,
                field_name="account_no",
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_verified_at=record.last_verified_at,
        )

    async def delete_nh(self, db: AsyncSession) -> str | None:
        async with self._write_lock:
            record = await self.record(db, "NH", for_update=True)
            if record is None:
                return None
            credential_id = record.id
            await db.delete(record)
            await db.commit()
            return credential_id

    async def mark_verified(
        self, db: AsyncSession, *, checked_at: datetime
    ) -> BrokerCredential:
        record = await self.record(db, "NH", for_update=True)
        if record is None:
            raise MobileApiError(
                409, "BROKER_NOT_CONNECTED", "NH PLUG 연결 정보가 등록되지 않았습니다."
            )
        record.last_verified_at = checked_at.astimezone(UTC)
        await db.commit()
        await db.refresh(record)
        return record

    def _encrypt(
        self,
        plaintext: str,
        *,
        credential_id: str,
        provider: str,
        field_name: str,
    ) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._master_key()).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self._aad(credential_id, provider, field_name),
        )
        return f"{_VERSION}.{base64.b64encode(nonce + ciphertext).decode('ascii')}"

    def _decrypt(
        self,
        packed: str,
        *,
        credential_id: str,
        provider: str,
        field_name: str,
    ) -> str:
        try:
            version, encoded = packed.split(".", 1)
            if version != _VERSION:
                raise ValueError("unsupported credential version")
            payload = base64.b64decode(encoded, validate=True)
            if len(payload) <= _NONCE_BYTES:
                raise ValueError("credential payload is too short")
            plaintext = AESGCM(self._master_key()).decrypt(
                payload[:_NONCE_BYTES],
                payload[_NONCE_BYTES:],
                self._aad(credential_id, provider, field_name),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError, binascii.Error) as err:
            raise MobileApiError(
                500,
                "CREDENTIAL_VAULT_ERROR",
                "저장된 브로커 연결 정보를 읽지 못했습니다.",
            ) from err

    @staticmethod
    def _aad(credential_id: str, provider: str, field_name: str) -> bytes:
        return f"kasset:{_VERSION}:{credential_id}:{provider}:{field_name}".encode()

    @staticmethod
    def _required(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise MobileApiError(
                422, "VALIDATION_ERROR", f"{label} 값을 입력해 주세요."
            )
        if len(normalized) > 2048:
            raise MobileApiError(422, "VALIDATION_ERROR", f"{label} 값이 너무 깁니다.")
        return normalized

    @staticmethod
    def mask(value: str, *, visible: int = 4) -> str:
        if not value:
            return ""
        return "••••" + value[-visible:]

    @staticmethod
    def _master_key() -> bytes:
        configured = settings.CREDENTIAL_MASTER_KEY
        if configured is None or not configured.get_secret_value().strip():
            raise MobileApiError(
                503,
                "CONFIGURATION_ERROR",
                "서버의 Credential Vault가 준비되지 않았습니다.",
            )
        raw = configured.get_secret_value().strip()
        try:
            key = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as err:
            raise MobileApiError(
                503,
                "CONFIGURATION_ERROR",
                "서버의 Credential Vault 설정이 올바르지 않습니다.",
            ) from err
        if len(key) != 32:
            raise MobileApiError(
                503,
                "CONFIGURATION_ERROR",
                "서버의 Credential Vault 설정이 올바르지 않습니다.",
            )
        return key


credential_vault = CredentialVault()
