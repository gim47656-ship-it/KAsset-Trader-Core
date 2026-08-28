"""Android-facing NH PLUG mock read-only adapter."""

from __future__ import annotations

import asyncio
import os
import re
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
from app.extensions.kasset.api.paper import iso_z
from app.extensions.kasset.api.paper_schemas import (
    Balance,
    CashBalance,
    Position,
    PositionsResponse,
    Quote,
)
from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockDisabled,
    NHPlugMockError,
)
from app.services.brokers.nhplug.gating import mock_enabled

_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")


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

    async def verify(self, db: AsyncSession, owner_user_id: int) -> datetime:
        await self._prepare_client(db, owner_user_id, require_prior_verification=False)
        checked_at = datetime.now(UTC).replace(microsecond=0)
        await credential_vault.mark_verified(db, owner_user_id, checked_at=checked_at)
        return checked_at

    async def balance(self, db: AsyncSession, owner_user_id: int) -> Balance:
        context = await self.prepare_read(db, owner_user_id)
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
        updated_at = iso_z()
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

    async def positions(
        self, db: AsyncSession, owner_user_id: int
    ) -> PositionsResponse:
        context = await self.prepare_read(db, owner_user_id)
        try:
            payload = await context.client.fetch_balance(
                act_no=context.credential.account_no
            )
        except (NHPlugMockError, httpx.HTTPError) as err:
            raise MobileApiError(
                502,
                "NH_POSITIONS_FAILED",
                "NH PLUG 모의투자 보유종목을 조회하지 못했습니다.",
            ) from err
        updated_at = iso_z()
        positions: list[Position] = []
        for row in _array_block(payload, "Output_1"):
            quantity = _decimal_string(row, "itg_bnc_qty")
            if Decimal(quantity) == 0:
                continue
            name_value = row.get("iem_nm")
            name = name_value.strip() if isinstance(name_value, str) else None
            positions.append(
                Position(
                    broker="NH",
                    account_id=context.credential.credential_id,
                    market="KRX",
                    symbol=_required_string(row, "iem_cd"),
                    name=name or None,
                    currency="KRW",
                    quantity=quantity,
                    average_price=_decimal_string(row, "phs_pr"),
                    current_price=_decimal_string(row, "now_pr"),
                    market_value=_decimal_string(row, "eal_amt"),
                    unrealized_pnl=_decimal_string(row, "eal_pls_amt"),
                    unrealized_pnl_rate=_decimal_string(row, "pft_rt"),
                    updated_at=updated_at,
                )
            )
        return PositionsResponse(positions=positions)

    async def quote(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        market: str,
        symbol: str,
    ) -> Quote:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip()
        if (
            normalized_market != "KRX"
            or _KRX_SYMBOL_RE.fullmatch(normalized_symbol) is None
        ):
            raise MobileApiError(
                422,
                "VALIDATION_ERROR",
                "NH PLUG 조회는 KRX 6자리 종목코드만 지원합니다.",
            )
        context = await self.prepare_read(db, owner_user_id)
        try:
            payload = await context.client.fetch_quote(
                market=normalized_market,
                symbol=normalized_symbol,
            )
        except (NHPlugMockError, httpx.HTTPError) as err:
            raise MobileApiError(
                502,
                "NH_QUOTE_FAILED",
                "NH PLUG 모의투자 현재가를 조회하지 못했습니다.",
            ) from err
        return _quote_from_payload(
            payload, market=normalized_market, symbol=normalized_symbol
        )

    async def prepare_read(
        self, db: AsyncSession, owner_user_id: int
    ) -> VerifiedNHClient:
        return await self._prepare_client(
            db, owner_user_id, require_prior_verification=True
        )

    async def _prepare_client(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        require_prior_verification: bool,
    ) -> VerifiedNHClient:
        if not mock_enabled():
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "NH PLUG 모의투자 조회 기능이 서버에서 비활성화되어 있습니다.",
            )
        credential = await credential_vault.reveal_nh(db, owner_user_id)
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


class NHSharedMarketData:
    """계좌 연동과 무관한 서버 공용 KRX 시세 채널.

    사용자별 볼트 자격은 계좌·주문에만 쓰고, 시세는 서버 env 자격
    (NHPLUG_APP_KEY/SECRET + NHPLUG_MOCK_ACCOUNT_NO)으로 모두에게 제공한다.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cached: NHPlugMockClient | None = None

    async def _client(self) -> NHPlugMockClient:
        if not mock_enabled():
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "NH PLUG 모의투자 조회 기능이 서버에서 비활성화되어 있습니다.",
            )
        app_key = os.environ.get("NHPLUG_APP_KEY", "").strip()
        app_secret = os.environ.get("NHPLUG_APP_SECRET", "").strip()
        account_no = os.environ.get("NHPLUG_MOCK_ACCOUNT_NO", "").strip()
        if not (app_key and app_secret and account_no):
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "서버 공용 NH PLUG 시세 자격이 설정되지 않았습니다.",
            )
        async with self._lock:
            if self._cached is not None:
                return self._cached
            try:
                auth_client = NHPlugAuthClient(
                    app_key=app_key, app_secret=app_secret
                )
                client = NHPlugMockClient(
                    app_key=app_key,
                    app_secret=app_secret,
                    token_provider=auth_client.get_access_token,
                )
                payload = await client.list_accounts()
                allowlist = MockAccountAllowlist.from_acctinfo_response(
                    payload=payload,
                    configured_account_no=account_no,
                )
                client.bind_account_allowlist(allowlist)
            except (NHPlugMockError, httpx.HTTPError) as err:
                raise MobileApiError(
                    502,
                    "NH_CONNECTION_FAILED",
                    "NH PLUG 모의투자 서버 연결을 확인하지 못했습니다.",
                ) from err
            self._cached = client
            return client

    async def quote(self, *, market: str, symbol: str) -> Quote:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip()
        if (
            normalized_market != "KRX"
            or _KRX_SYMBOL_RE.fullmatch(normalized_symbol) is None
        ):
            raise MobileApiError(
                422,
                "VALIDATION_ERROR",
                "NH PLUG 조회는 KRX 6자리 종목코드만 지원합니다.",
            )
        client = await self._client()
        try:
            payload = await client.fetch_quote(
                market=normalized_market,
                symbol=normalized_symbol,
            )
        except (NHPlugMockError, httpx.HTTPError) as err:
            self._cached = None
            raise MobileApiError(
                502,
                "NH_QUOTE_FAILED",
                "NH PLUG 모의투자 현재가를 조회하지 못했습니다.",
            ) from err
        return _quote_from_payload(
            payload, market=normalized_market, symbol=normalized_symbol
        )


def _quote_from_payload(
    payload: dict[str, Any], *, market: str, symbol: str
) -> Quote:
    row = _object_block(payload, "Output_0")
    response_symbol = _required_string(row, "iem_cd")
    if response_symbol != symbol:
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    price = Decimal(_decimal_string(row, "stck_prpr"))
    change = _signed_change(
        Decimal(_decimal_string(row, "prdy_vrss")),
        row.get("prdy_vrss_sign"),
    )
    change_rate = _signed_change(
        Decimal(_decimal_string(row, "prdy_ctrt")),
        row.get("prdy_vrss_sign"),
    )
    name_value = row.get("iem_nm")
    name = name_value.strip() if isinstance(name_value, str) else None
    return Quote(
        broker="NH",
        market=market,
        symbol=response_symbol,
        name=name or None,
        currency="KRW",
        price=format(price, "f"),
        previous_close=format(price - change, "f"),
        change_amount=format(change, "f"),
        change_rate=format(change_rate, "f"),
        as_of=iso_z(),
        source="NH_PLUG_MOCK",
    )


nh_market_data = NHSharedMarketData()


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


def _array_block(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    block = payload.get(name, [])
    if not isinstance(block, list) or not all(isinstance(row, dict) for row in block):
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    return block


def _required_string(row: dict[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MobileApiError(
            502,
            "NH_RESPONSE_INVALID",
            "NH PLUG 모의투자 응답 형식이 올바르지 않습니다.",
        )
    return value.strip()


def _signed_change(value: Decimal, sign_code: Any) -> Decimal:
    code = str(sign_code).strip()
    magnitude = abs(value)
    if code in {"1", "2", "6", "7"}:
        return magnitude
    if code in {"4", "5", "8", "9"}:
        return -magnitude
    if code in {"0", "3"}:
        return Decimal("0")
    return value


nh_adapter = NHAndroidAdapter()
