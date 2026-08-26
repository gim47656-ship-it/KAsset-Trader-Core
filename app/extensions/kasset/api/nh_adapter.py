"""Android-facing NH PLUG mock read-only adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.credential_vault import (
    RevealedBrokerCredential,
    credential_vault,
)
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import Balance, CashBalance
from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockDisabled,
    NHPlugMockError,
)
from app.services.brokers.nhplug.gating import mock_enabled


@dataclass(frozen=True, slots=True)
class VerifiedNHClient:
    client: NHPlugMockClient = field(repr=False)
    credential: RevealedBrokerCredential = field(repr=False)


class NHAndroidAdapter:
    def __init__(self) -> None:
        self._auth_lock = asyncio.Lock()
        self._cached_auth: tuple[str, datetime, NHPlugAuthClient] | None = None

    async def invalidate_auth_cache(self, credential_id: str | None = None) -> None:
        async with self._auth_lock:
            if (
                credential_id is None
                or self._cached_auth is None
                or self._cached_auth[0] == credential_id
            ):
                self._cached_auth = None

    async def verify(self, db: AsyncSession) -> datetime:
        await self._prepare_client(db, require_prior_verification=False)
        checked_at = datetime.now(UTC).replace(microsecond=0)
        await credential_vault.mark_verified(db, checked_at=checked_at)
        return checked_at

    async def balance(self, db: AsyncSession) -> Balance:
        context = await self.prepare_read(db)
        try:
            payload = await context.client.fetch_balance(
                act_no=context.credential.account_no
            )
        except (NHPlugMockError, httpx.HTTPError) as err:
            raise MobileApiError(
                502,
                "NH_BALANCE_FAILED",
                "NH PLUG 모의투자 잔고를 조회하지 못했습니다.",
            ) from err
        summary = _object_block(payload, "Output_0")
        updated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        return Balance(
            broker="NH",
            account_id=context.credential.credential_id,
            base_currency="KRW",
            cash=[
                CashBalance(
                    currency="KRW",
                    cash=_decimal_string(summary, "dca"),
                    available=_decimal_string(summary, "orr_pbl_amt"),
                )
            ],
            evaluation_amount=_decimal_string(summary, "tot_eal_amt"),
            total_assets=_decimal_string(summary, "tot_aet_amt"),
            unrealized_pnl=_decimal_string(summary, "tot_eal_pls"),
            updated_at=updated_at,
        )

    async def prepare_read(self, db: AsyncSession) -> VerifiedNHClient:
        return await self._prepare_client(db, require_prior_verification=True)

    async def _prepare_client(
        self,
        db: AsyncSession,
        *,
        require_prior_verification: bool,
    ) -> VerifiedNHClient:
        if not mock_enabled():
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "NH PLUG 모의투자 조회 기능이 서버에서 비활성화되어 있습니다.",
            )
        credential = await credential_vault.reveal_nh(db)
        if require_prior_verification and credential.last_verified_at is None:
            raise MobileApiError(
                409,
                "BROKER_NOT_VERIFIED",
                "NH PLUG 계좌 연결 확인이 필요합니다.",
            )
        try:
            auth_client = await self._auth_client(credential)
            client = NHPlugMockClient(
                app_key=credential.app_key,
                app_secret=credential.app_secret,
                token_provider=auth_client.get_access_token,
            )
            account_payload = await client.list_accounts()
            allowlist = MockAccountAllowlist.from_acctinfo_response(
                payload=account_payload,
                configured_account_no=credential.account_no,
            )
            client.bind_account_allowlist(allowlist)
            return VerifiedNHClient(client=client, credential=credential)
        except NHPlugMockDisabled as err:
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "NH PLUG 모의투자 조회 기능이 서버에서 비활성화되어 있습니다.",
            ) from err
        except NHPlugMockAccountRejected as err:
            raise MobileApiError(
                409,
                "NH_ACCOUNT_NOT_ALLOWED",
                "등록한 계좌가 NH PLUG 모의투자 계좌로 확인되지 않았습니다.",
            ) from err
        except (NHPlugMockError, httpx.HTTPError) as err:
            raise MobileApiError(
                502,
                "NH_CONNECTION_FAILED",
                "NH PLUG 모의투자 서버 연결을 확인하지 못했습니다.",
            ) from err

    async def _auth_client(
        self, credential: RevealedBrokerCredential
    ) -> NHPlugAuthClient:
        async with self._auth_lock:
            cached = self._cached_auth
            if (
                cached is not None
                and cached[0] == credential.credential_id
                and cached[1] == credential.updated_at
            ):
                return cached[2]
            auth_client = NHPlugAuthClient(
                app_key=credential.app_key,
                app_secret=credential.app_secret,
            )
            self._cached_auth = (
                credential.credential_id,
                credential.updated_at,
                auth_client,
            )
            return auth_client


def _object_block(payload: dict[str, Any], name: str) -> dict[str, Any]:
    block = payload.get(name)
    if not isinstance(block, dict):
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    return block


def _decimal_string(row: dict[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as err:
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        ) from err
    if not number.is_finite():
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    return format(number, "f")


nh_adapter = NHAndroidAdapter()
