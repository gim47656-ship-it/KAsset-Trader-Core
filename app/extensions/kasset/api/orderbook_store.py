"""Lazy NH PLUG mock WebSocket orderbook snapshots for the Android API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.extensions.kasset.api.errors import MobileApiError
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.errors import NHPlugMockError
from app.services.brokers.nhplug.gating import mock_enabled

logger = logging.getLogger(__name__)

_MOCK_WEBSOCKET_URL: Final[str] = "wss://moapi.nhplug.com:17070/websocket"
_ORDERBOOK_TR_CD: Final[str] = "ob"
_SUBSCRIPTION_TTL_SECONDS: Final[float] = 60.0
_RECEIVE_POLL_SECONDS: Final[float] = 1.0
_MAX_RECONNECT_DELAY_SECONDS: Final[float] = 30.0
_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")

_ASK_PRICE_FIELDS: Final[tuple[str, ...]] = (
    "offer",
    "P_offer",
    "S_offer",
    "S4_offer",
    "S5_offer",
    "S6_offer",
    "S7_offer",
    "S8_offer",
    "S9_offer",
    "S10_offer",
)
_BID_PRICE_FIELDS: Final[tuple[str, ...]] = (
    "bid",
    "P_bid",
    "S_bid",
    "S4_bid",
    "S5_bid",
    "S6_bid",
    "S7_bid",
    "S8_bid",
    "S9_bid",
    "S10_bid",
)
_ASK_VOLUME_FIELDS: Final[tuple[str, ...]] = (
    "offerrem",
    "P_offerrem",
    "S_offerrem",
    "S4_offerrem",
    "S5_offerrem",
    "S6_offerrem",
    "S7_offerrem",
    "S8_offerrem",
    "S9_offerrem",
    "S10_offerrem",
)
_BID_VOLUME_FIELDS: Final[tuple[str, ...]] = (
    "bidrem",
    "P_bidrem",
    "S_bidrem",
    "S4_bidrem",
    "S5_bidrem",
    "S6_bidrem",
    "S7_bidrem",
    "S8_bidrem",
    "S9_bidrem",
    "S10_bidrem",
)


class _PermanentAuthenticationError(RuntimeError):
    """A redacted terminal OAuth failure."""


class _WebSocketAuthenticationError(RuntimeError):
    """The WebSocket server rejected the token in a control response."""


def normalize_orderbook_key(*, market: str, symbol: str) -> tuple[str, str]:
    """Normalize and validate the only orderbook key supported by this channel."""

    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip()
    if (
        normalized_market != "KRX"
        or _KRX_SYMBOL_RE.fullmatch(normalized_symbol) is None
    ):
        raise MobileApiError(
            422,
            "VALIDATION_ERROR",
            "NH PLUG 실시간 호가는 KRX 6자리 종목코드만 지원합니다.",
        )
    return normalized_market, normalized_symbol


class NHOrderbookSnapshotStore:
    """One lazy WebSocket connection shared by all requested KRX symbols."""

    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._runner_task: asyncio.Task[None] | None = None
        self._connection: ClientConnection | None = None
        self._connection_token: str | None = None
        self._subscribed: set[str] = set()
        self._last_requested: dict[str, float] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._auth_client: NHPlugAuthClient | None = None
        self._auth_credentials: tuple[str, str] | None = None
        self._auth_failure_credentials: tuple[str, str, str] | None = None

    async def get_snapshot(self, *, market: str, symbol: str) -> dict[str, Any]:
        """Touch a subscription and return its latest contract-shaped snapshot."""

        normalized_market, normalized_symbol = normalize_orderbook_key(
            market=market,
            symbol=symbol,
        )
        credentials = self._require_configuration()
        now = time.monotonic()

        async with self._state_lock:
            if self._auth_failure_credentials is not None:
                if self._auth_failure_credentials == credentials:
                    raise MobileApiError(
                        409,
                        "BROKER_NOT_CONNECTED",
                        "NH PLUG 실시간 호가 인증을 확인하지 못했습니다.",
                    )
                self._auth_failure_credentials = None
                self._auth_client = None
                self._auth_credentials = None

            previous_request = self._last_requested.get(normalized_symbol)
            if (
                previous_request is not None
                and now - previous_request >= _SUBSCRIPTION_TTL_SECONDS
            ):
                self._snapshots.pop(normalized_symbol, None)
            self._last_requested[normalized_symbol] = now

            if self._runner_task is None or self._runner_task.done():
                self._runner_task = asyncio.create_task(
                    self._run(),
                    name="nhplug-orderbook-websocket",
                )
            connection = self._connection
            connection_token = self._connection_token
            snapshot = self._snapshots.get(normalized_symbol)

        if connection is not None and connection_token is not None:
            await self._subscribe(connection, connection_token, normalized_symbol)

        if snapshot is None:
            return _empty_snapshot(
                market=normalized_market,
                symbol=normalized_symbol,
            )
        return _copy_snapshot(snapshot)

    async def close(self) -> None:
        """Stop the process-local connection task during API shutdown."""

        async with self._state_lock:
            task = self._runner_task
            self._runner_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._state_lock:
            self._connection = None
            self._connection_token = None
            self._subscribed.clear()
            self._last_requested.clear()
            self._snapshots.clear()
            self._auth_client = None
            self._auth_credentials = None
            self._auth_failure_credentials = None

    async def _run(self) -> None:
        reconnect_delay = 1.0
        force_refresh = False
        failed_token: str | None = None
        websocket_auth_retries = 0

        while True:
            try:
                credentials = self._require_configuration()
                token = await self._access_token(
                    credentials=credentials,
                    force_refresh=force_refresh,
                    failed_token=failed_token,
                )
                await self._clear_auth_failure()
                received_data = await self._connection_session(token)
                force_refresh = False
                failed_token = None
                if received_data:
                    reconnect_delay = 1.0
                    websocket_auth_retries = 0
            except asyncio.CancelledError:
                raise
            except MobileApiError:
                return
            except _WebSocketAuthenticationError:
                websocket_auth_retries += 1
                await self._mark_auth_failure(credentials)
                if websocket_auth_retries > 1:
                    return
                force_refresh = True
                failed_token = token
            except _PermanentAuthenticationError:
                await self._mark_auth_failure(credentials)
                return
            except Exception as exc:  # transient connect/read failures reconnect
                logger.warning(
                    "NH PLUG orderbook WebSocket disconnected (%s)",
                    type(exc).__name__,
                )

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(
                reconnect_delay * 2,
                _MAX_RECONNECT_DELAY_SECONDS,
            )

    async def _access_token(
        self,
        *,
        credentials: tuple[str, str, str],
        force_refresh: bool,
        failed_token: str | None,
    ) -> str:
        app_key, app_secret, _account_no = credentials
        auth_credentials = (app_key, app_secret)
        if self._auth_client is None or self._auth_credentials != auth_credentials:
            self._auth_client = NHPlugAuthClient(
                app_key=app_key,
                app_secret=app_secret,
            )
            self._auth_credentials = auth_credentials

        try:
            return await self._auth_client.get_access_token(
                force_refresh=force_refresh,
                failed_token=failed_token,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 401, 403}:
                raise _PermanentAuthenticationError from exc
            raise
        except NHPlugMockError as exc:
            raise _PermanentAuthenticationError from exc

    async def _connection_session(self, token: str) -> bool:
        received_data = False
        connection: ClientConnection | None = None
        try:
            async with connect(
                _MOCK_WEBSOCKET_URL,
                open_timeout=10,
                close_timeout=5,
                ping_interval=None,
            ) as active_connection:
                connection = active_connection
                async with self._state_lock:
                    self._connection = connection
                    self._connection_token = token
                    self._subscribed.clear()
                    active_symbols = self._active_symbols(time.monotonic())

                for symbol in active_symbols:
                    await self._subscribe(connection, token, symbol)

                while True:
                    try:
                        raw_frame = await asyncio.wait_for(
                            connection.recv(),
                            timeout=_RECEIVE_POLL_SECONDS,
                        )
                    except TimeoutError:
                        await self._expire_subscriptions(connection, token)
                        continue

                    payload = _decode_frame(raw_frame)
                    if payload is None:
                        continue
                    if _is_auth_rejection(payload):
                        raise _WebSocketAuthenticationError
                    snapshot = _snapshot_from_push(
                        payload,
                        received_at=datetime.now(UTC),
                    )
                    if snapshot is None:
                        continue
                    symbol = snapshot["symbol"]
                    now = time.monotonic()
                    async with self._state_lock:
                        last_requested = self._last_requested.get(symbol)
                        if (
                            last_requested is not None
                            and now - last_requested < _SUBSCRIPTION_TTL_SECONDS
                        ):
                            self._snapshots[symbol] = snapshot
                            received_data = True
        except ConnectionClosed:
            return received_data
        finally:
            async with self._state_lock:
                if self._connection is connection:
                    self._connection = None
                    self._connection_token = None
                    self._subscribed.clear()
        return received_data

    async def _subscribe(
        self,
        connection: ClientConnection,
        token: str,
        symbol: str,
    ) -> None:
        async with self._send_lock:
            async with self._state_lock:
                last_requested = self._last_requested.get(symbol)
                if (
                    connection is not self._connection
                    or symbol in self._subscribed
                    or last_requested is None
                    or time.monotonic() - last_requested
                    >= _SUBSCRIPTION_TTL_SECONDS
                ):
                    return
            try:
                await connection.send(
                    _subscription_message(
                        token=token,
                        symbol=symbol,
                        subscribe=True,
                    )
                )
            except (OSError, WebSocketException):
                return
            async with self._state_lock:
                if connection is self._connection:
                    self._subscribed.add(symbol)

    async def _expire_subscriptions(
        self,
        connection: ClientConnection,
        token: str,
    ) -> None:
        now = time.monotonic()
        async with self._state_lock:
            expired = [
                symbol
                for symbol, requested_at in self._last_requested.items()
                if now - requested_at >= _SUBSCRIPTION_TTL_SECONDS
            ]
            for symbol in expired:
                self._last_requested.pop(symbol, None)
                self._snapshots.pop(symbol, None)

        for symbol in expired:
            await self._unsubscribe(connection, token, symbol)

    async def _unsubscribe(
        self,
        connection: ClientConnection,
        token: str,
        symbol: str,
    ) -> None:
        async with self._send_lock:
            async with self._state_lock:
                if (
                    connection is not self._connection
                    or symbol not in self._subscribed
                    or symbol in self._last_requested
                ):
                    return
                self._subscribed.discard(symbol)
            try:
                await connection.send(
                    _subscription_message(
                        token=token,
                        symbol=symbol,
                        subscribe=False,
                    )
                )
            except (OSError, WebSocketException):
                return

    def _active_symbols(self, now: float) -> list[str]:
        return [
            symbol
            for symbol, requested_at in self._last_requested.items()
            if now - requested_at < _SUBSCRIPTION_TTL_SECONDS
        ]

    async def _mark_auth_failure(
        self,
        credentials: tuple[str, str, str],
    ) -> None:
        async with self._state_lock:
            self._auth_failure_credentials = credentials

    async def _clear_auth_failure(self) -> None:
        async with self._state_lock:
            self._auth_failure_credentials = None

    @staticmethod
    def _require_configuration() -> tuple[str, str, str]:
        if not mock_enabled():
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "NH PLUG 모의투자 실시간 호가 채널이 비활성화되어 있습니다.",
            )
        app_key = os.environ.get("NHPLUG_APP_KEY", "").strip()
        app_secret = os.environ.get("NHPLUG_APP_SECRET", "").strip()
        account_no = os.environ.get("NHPLUG_MOCK_ACCOUNT_NO", "").strip()
        if not (app_key and app_secret and account_no):
            raise MobileApiError(
                409,
                "BROKER_NOT_CONNECTED",
                "서버 공용 NH PLUG 실시간 호가 자격이 설정되지 않았습니다.",
            )
        return app_key, app_secret, account_no


def _subscription_message(*, token: str, symbol: str, subscribe: bool) -> str:
    return json.dumps(
        {
            "header": {
                "token": token,
                "tr_type": "1" if subscribe else "2",
            },
            "body": {
                "tr_cd": _ORDERBOOK_TR_CD,
                "tr_key": symbol,
            },
        },
        separators=(",", ":"),
    )


def _decode_frame(raw_frame: str | bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_frame)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot_from_push(
    payload: dict[str, Any],
    *,
    received_at: datetime,
) -> dict[str, Any] | None:
    header = payload.get("header")
    body = payload.get("body")
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    if header.get("tr_cd") != _ORDERBOOK_TR_CD:
        return None

    symbol = header.get("tr_key")
    if (
        not isinstance(symbol, str)
        or _KRX_SYMBOL_RE.fullmatch(symbol) is None
        or body.get("code") != symbol
    ):
        return None

    try:
        asks = [
            {
                "price": _decimal_string(body, price_field),
                "volume": _decimal_string(body, volume_field),
            }
            for price_field, volume_field in zip(
                _ASK_PRICE_FIELDS,
                _ASK_VOLUME_FIELDS,
                strict=True,
            )
        ]
        bids = [
            {
                "price": _decimal_string(body, price_field),
                "volume": _decimal_string(body, volume_field),
            }
            for price_field, volume_field in zip(
                _BID_PRICE_FIELDS,
                _BID_VOLUME_FIELDS,
                strict=True,
            )
        ]
        total_ask_volume = _decimal_string(body, "T_offerrem")
        total_bid_volume = _decimal_string(body, "T_bidrem")
    except ValueError:
        return None

    return {
        "symbol": symbol,
        "market": "KRX",
        "ready": True,
        "asOf": received_at.astimezone(UTC).isoformat(),
        "source": "NH_PLUG_WS",
        "asks": asks,
        "bids": bids,
        "totalAskVolume": total_ask_volume,
        "totalBidVolume": total_bid_volume,
    }


def _decimal_string(row: dict[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError(field_name)
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(field_name) from None
    if not number.is_finite() or number < 0:
        raise ValueError(field_name)
    return format(number, "f")


def _is_auth_rejection(payload: dict[str, Any]) -> bool:
    response_code = _find_string(payload, "rsp_cd")
    if response_code in {None, "", "0", "00000"}:
        return False
    message = " ".join(
        part
        for key in ("rsp_msg", "message", "msg")
        if (part := _find_string(payload, key))
    ).lower()
    return any(
        marker in message
        for marker in ("token", "access", "unauthor", "인증", "토큰", "만료")
    )


def _find_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value.strip()
    for nested in payload.values():
        if isinstance(nested, dict):
            found = _find_string(nested, key)
            if found is not None:
                return found
    return None


def _empty_snapshot(*, market: str, symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market": market,
        "ready": False,
        "asOf": None,
        "source": "NH_PLUG_WS",
        "asks": [],
        "bids": [],
        "totalAskVolume": "0",
        "totalBidVolume": "0",
    }


def _copy_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        **snapshot,
        "asks": [dict(level) for level in snapshot["asks"]],
        "bids": [dict(level) for level in snapshot["bids"]],
    }


nh_orderbook_store = NHOrderbookSnapshotStore()


def get_orderbook_store() -> NHOrderbookSnapshotStore:
    """FastAPI dependency hook for the process-local singleton store."""

    return nh_orderbook_store
