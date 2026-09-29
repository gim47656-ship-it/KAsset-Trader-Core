"""NH PLUG 국내 시세 실행 프로세스 — 주문 없음, DB 쓰기 없음.

``python -m app.extensions.kasset.nhplug.runner``로 ``nh-stream`` 컨테이너에서
돈다. ``KASSET_NH_STREAM_ENABLED``가 참일 때만 연결한다.

- Redis lease ``kasset:nh:owner``를 잡은 인스턴스 하나만 WebSocket을 연다.
  기존 토스 스트림(``kasset:stream:*``)과 키를 공유하지 않는다.
- KRX 정규장에만 연결한다. 장외에는 연결을 닫고 관찰창을 비운다.
- 구독 대상은 DB에서 읽는다(쓰기 없음): ① KAsset PAPER 계좌의 KRX 보유 종목,
  ② 실시간 관문을 요구하는 미집행 KRX BUY 추천. 보유가 먼저다.
- 종목당 ``oc``+``ob`` 두 건을 등록해 세션당 15종목, 최대 30종목이다. 초과분은
  구독하지 않는다(그 종목 신규 BUY는 관찰 부재로 막힌다).
- 매초 종목별 snapshot을 Redis에 짧은 TTL로 쓴다. 재시작·재연결이면 창이 비어
  60초 관찰을 다시 채운다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import ssl
import time as monotonic_time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

import httpx
import redis.asyncio as redis
from redis.exceptions import WatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.automation.market_session import current_regular_session
from app.extensions.kasset.automation.realtime_tape import (
    BookTick,
    TapeWindow,
    TradeTick,
)
from app.extensions.kasset.models import AndroidPaperAccount
from app.extensions.kasset.nhplug import protocol
from app.extensions.kasset.nhplug.tape_store import OWNER_KEY, RedisTapeStore
from app.extensions.kasset.nhplug.token_cache import (
    NhplugCredentials,
    NhplugTokenProvider,
    token_cache_path_from_env,
)
from app.models.ai_recommendations import AIRecommendation, RecommendationDecision
from app.models.paper_trading import PaperPosition
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)

ENABLED_ENV: Final = "KASSET_NH_STREAM_ENABLED"
LEASE_SECONDS: Final = 15.0
LEASE_RENEW_SECONDS: Final = 5.0
DEMAND_REFRESH_SECONDS: Final = 15.0
PUBLISH_INTERVAL_SECONDS: Final = 1.0
CLOSED_SESSION_POLL_SECONDS: Final = 15.0
#: 정규장 중 구독한 세션에서 이만큼 아무 프레임도 없으면 끊긴 것으로 본다.
#: NH는 heartbeat를 보내지 않으므로 ping 대신 데이터 침묵으로 판정한다.
SILENT_SESSION_SECONDS: Final = 60.0
BACKOFF_INITIAL_SECONDS: Final = 1.0
BACKOFF_MAX_SECONDS: Final = 30.0
SOURCE: Final = "kasset-automation"


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[], Awaitable[WebSocketLike]]
DemandLoader = Callable[[datetime], Awaitable[Sequence[str]]]


async def default_connector() -> WebSocketLike:
    import websockets

    # 운영 서버에서 기본 CA 검증으로 ACK까지 확인된 경로(2026-09-29). 검증을 끄는
    # 옵션은 두지 않는다. NH는 heartbeat가 없어 websockets 기본 ping을 끈다.
    return await websockets.connect(
        protocol.WS_URL,
        ssl=ssl.create_default_context(),
        open_timeout=15,
        close_timeout=5,
        ping_interval=None,
    )


async def load_demand(now: datetime) -> list[str]:
    """보유 → 실시간 BUY 추천 순으로 구독할 KRX 종목을 고른다(읽기 전용)."""

    async with AsyncSessionLocal() as db:
        return await demand_symbols(db, now=now)


async def demand_symbols(db: AsyncSession, *, now: datetime) -> list[str]:
    held = (
        await db.scalars(
            select(PaperPosition.symbol)
            .join(
                AndroidPaperAccount,
                AndroidPaperAccount.paper_account_id == PaperPosition.account_id,
            )
            .where(
                PaperPosition.quantity > 0,
                PaperPosition.instrument_type == InstrumentType.equity_kr,
            )
            .order_by(PaperPosition.symbol)
        )
    ).all()
    candidates = (
        await db.execute(
            select(AIRecommendation.symbol)
            .where(
                AIRecommendation.source == SOURCE,
                AIRecommendation.action == "BUY",
                AIRecommendation.market == "KRX",
                AIRecommendation.decision.in_(
                    (RecommendationDecision.PENDING, RecommendationDecision.APPROVED)
                ),
                AIRecommendation.paper_execution_status.is_(None),
                AIRecommendation.valid_until > now,
            )
            .order_by(AIRecommendation.created_at, AIRecommendation.id)
            .limit(200)
        )
    ).all()
    ordered: list[str] = []
    for symbol in held:
        _append_symbol(ordered, symbol)
    for (symbol,) in candidates:
        _append_symbol(ordered, symbol)
    await db.rollback()
    return ordered


def _append_symbol(ordered: list[str], raw: object) -> None:
    symbol = str(raw or "").strip()
    if len(symbol) == 6 and symbol not in ordered:
        ordered.append(symbol)


@dataclass(slots=True)
class _Slot:
    index: int
    subscribed: list[str] = field(default_factory=list)
    ws: WebSocketLike | None = None
    reader: asyncio.Task[None] | None = None
    last_frame_at: float = 0.0
    connected_at: float = 0.0
    backoff: float = BACKOFF_INITIAL_SECONDS
    retry_at: float = 0.0


class NhStreamRunner:
    def __init__(
        self,
        *,
        redis_client: redis.Redis,
        store: RedisTapeStore,
        token: Callable[[], Awaitable[str]],
        demand_loader: DemandLoader = load_demand,
        connector: Connector = default_connector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = monotonic_time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        instance_id: str | None = None,
    ) -> None:
        self._redis = redis_client
        self._store = store
        self._token = token
        self._demand_loader = demand_loader
        self._connector = connector
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._instance_id = instance_id or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._slots = [_Slot(index) for index in range(protocol.MAX_SESSIONS)]
        self._windows: dict[str, TapeWindow] = {}
        self._desired: list[str] = []
        self._next_demand_at = 0.0
        self._next_lease_at = 0.0
        self._owner = False
        self._last_send = 0.0
        self._published: set[str] = set()
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ lease
    async def _hold_lease(self) -> bool:
        now = self._monotonic()
        if now < self._next_lease_at:
            return self._owner
        self._next_lease_at = now + LEASE_RENEW_SECONDS
        try:
            if self._owner:
                self._owner = await self._renew_lease()
            else:
                self._owner = bool(
                    await self._redis.set(
                        OWNER_KEY,
                        self._instance_id,
                        nx=True,
                        px=int(LEASE_SECONDS * 1000),
                    )
                )
        except Exception:  # noqa: BLE001 - Redis 장애 중에는 소유권을 주장하지 않는다
            logger.warning("nh-stream lease check failed", exc_info=True)
            self._owner = False
        return self._owner

    async def _renew_lease(self) -> bool:
        async with self._redis.pipeline() as pipe:
            try:
                await pipe.watch(OWNER_KEY)
                current = await pipe.get(OWNER_KEY)
                if current is None or str(current) != self._instance_id:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.pexpire(OWNER_KEY, int(LEASE_SECONDS * 1000))
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def _release_lease(self) -> None:
        with contextlib.suppress(Exception):
            if str(await self._redis.get(OWNER_KEY) or "") == self._instance_id:
                await self._redis.delete(OWNER_KEY)
        self._owner = False

    # ------------------------------------------------------------------ loop
    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                interval = await self.tick()
                await self._sleep(interval)
        finally:
            await self._shutdown()

    async def tick(self) -> float:
        """한 번의 제어 주기. 다음 주기까지 기다릴 초를 돌려준다."""

        if not await self._hold_lease():
            await self._close_all("lease_not_held")
            return LEASE_RENEW_SECONDS
        now = self._clock()
        if current_regular_session("KRX", now) is None:
            await self._close_all("krx_session_closed")
            return CLOSED_SESSION_POLL_SECONDS
        monotonic_now = self._monotonic()
        if monotonic_now >= self._next_demand_at:
            self._next_demand_at = monotonic_now + DEMAND_REFRESH_SECONDS
            try:
                demand = list(await self._demand_loader(now))
            except Exception:  # noqa: BLE001 - 수요 조회 실패는 기존 구독 유지
                logger.warning("nh-stream demand load failed", exc_info=True)
            else:
                if len(demand) > protocol.MAX_SYMBOLS:
                    logger.warning(
                        "nh-stream demand exceeds registration budget: "
                        "demand=%d capacity=%d dropped=%s",
                        len(demand),
                        protocol.MAX_SYMBOLS,
                        demand[protocol.MAX_SYMBOLS :],
                    )
                self._desired = demand[: protocol.MAX_SYMBOLS]
        await self._reconcile_slots()
        await self._publish()
        return PUBLISH_INTERVAL_SECONDS

    # ------------------------------------------------------------------ slots
    def _assignment(self) -> list[list[str]]:
        """기존 배치를 최대한 유지하며 원하는 종목을 세션에 나눈다."""

        desired = set(self._desired)
        plan = [
            [symbol for symbol in slot.subscribed if symbol in desired]
            for slot in self._slots
        ]
        placed = {symbol for group in plan for symbol in group}
        for symbol in self._desired:
            if symbol in placed:
                continue
            for group in plan:
                if len(group) < protocol.SYMBOLS_PER_SESSION:
                    group.append(symbol)
                    placed.add(symbol)
                    break
        return plan

    async def _reconcile_slots(self) -> None:
        now = self._monotonic()
        for slot, wanted in zip(self._slots, self._assignment(), strict=True):
            if slot.ws is not None and slot.reader is not None and slot.reader.done():
                await self._drop_connection(slot, "reader_stopped")
            if (
                slot.ws is not None
                and slot.subscribed
                and now - max(slot.last_frame_at, slot.connected_at)
                > SILENT_SESSION_SECONDS
            ):
                await self._drop_connection(slot, "session_silent")
            if not wanted:
                if slot.ws is not None:
                    await self._unsubscribe(slot, list(slot.subscribed))
                    await self._drop_connection(slot, "no_demand")
                continue
            if slot.ws is None:
                if now < slot.retry_at:
                    continue
                if not await self._connect(slot):
                    continue
            removed = [symbol for symbol in slot.subscribed if symbol not in wanted]
            added = [symbol for symbol in wanted if symbol not in slot.subscribed]
            if removed:
                await self._unsubscribe(slot, removed)
            if added:
                await self._subscribe(slot, added)

    async def _connect(self, slot: _Slot) -> bool:
        try:
            # 토큰을 못 얻으면 연결하지 않는다. 구독 메시지마다 같은 캐시를 쓴다.
            await self._token()
            slot.ws = await self._connector()
        except Exception as exc:  # noqa: BLE001 - 다음 주기에 backoff 후 재시도
            logger.warning(
                "nh-stream connect failed: slot=%d error=%s retry_in=%.0fs",
                slot.index,
                type(exc).__name__,
                slot.backoff,
            )
            slot.ws = None
            slot.retry_at = self._monotonic() + slot.backoff
            slot.backoff = min(slot.backoff * 2, BACKOFF_MAX_SECONDS)
            return False
        slot.connected_at = self._monotonic()
        slot.last_frame_at = 0.0
        slot.subscribed = []
        slot.reader = asyncio.create_task(self._read(slot, slot.ws))
        logger.info("nh-stream connected: slot=%d", slot.index)
        return True

    async def _throttle(self) -> None:
        wait = self._last_send + protocol.SUBSCRIBE_INTERVAL_SECONDS - self._monotonic()
        if wait > 0:
            await self._sleep(wait)
        self._last_send = self._monotonic()

    async def _send(self, slot: _Slot, symbol: str, *, register: bool) -> None:
        assert slot.ws is not None
        token = await self._token()
        for channel in protocol.CHANNELS:
            await self._throttle()
            await slot.ws.send(
                protocol.subscribe_message(token, channel, symbol, register=register)
            )

    async def _subscribe(self, slot: _Slot, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            try:
                await self._send(slot, symbol, register=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "nh-stream subscribe failed: slot=%d symbol=%s error=%s",
                    slot.index,
                    symbol,
                    type(exc).__name__,
                )
                await self._drop_connection(slot, "subscribe_failed")
                return
            slot.subscribed.append(symbol)
            window = TapeWindow(symbol)
            self._windows[symbol] = window

    async def _unsubscribe(self, slot: _Slot, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            if slot.ws is not None:
                with contextlib.suppress(Exception):
                    await self._send(slot, symbol, register=False)
            if symbol in slot.subscribed:
                slot.subscribed.remove(symbol)
            self._windows.pop(symbol, None)

    async def _drop_connection(self, slot: _Slot, reason: str) -> None:
        if slot.reader is not None and not slot.reader.done():
            slot.reader.cancel()
            with contextlib.suppress(BaseException):
                await slot.reader
        slot.reader = None
        if slot.ws is not None:
            with contextlib.suppress(Exception):
                await slot.ws.close()
        slot.ws = None
        for symbol in slot.subscribed:
            # 끊겼던 구간을 관찰로 이어 붙이지 않는다. 재구독 뒤 새로 센다.
            self._windows.pop(symbol, None)
        slot.subscribed = []
        slot.retry_at = self._monotonic() + slot.backoff
        slot.backoff = min(slot.backoff * 2, BACKOFF_MAX_SECONDS)
        logger.info("nh-stream slot closed: slot=%d reason=%s", slot.index, reason)

    async def _close_all(self, reason: str) -> None:
        for slot in self._slots:
            if slot.ws is not None or slot.subscribed:
                await self._unsubscribe(slot, list(slot.subscribed))
                await self._drop_connection(slot, reason)
                slot.backoff = BACKOFF_INITIAL_SECONDS
                slot.retry_at = 0.0
        self._windows.clear()
        self._desired = []
        self._next_demand_at = 0.0
        await self._publish()

    async def _read(self, slot: _Slot, ws: WebSocketLike) -> None:
        while True:
            raw = await ws.recv()
            received_at = self._clock()
            slot.last_frame_at = self._monotonic()
            self.handle_message(slot, raw, received_at=received_at)

    def handle_message(
        self,
        slot: _Slot,
        raw: str | bytes,
        *,
        received_at: datetime,
    ) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        if protocol.is_ack(message):
            ok, code, text = protocol.ack_result(message)
            if not ok:
                header = message.get("header") or {}
                logger.warning(
                    "nh-stream subscription rejected: slot=%d tr_cd=%s rsp_cd=%s "
                    "rsp_msg=%s",
                    slot.index,
                    header.get("tr_cd") if isinstance(header, dict) else None,
                    code,
                    text,
                )
            else:
                # 정상 ACK도 연결이 살아 있다는 증거다.
                slot.backoff = BACKOFF_INITIAL_SECONDS
            return
        tick = protocol.parse_push(message, received_at=received_at)
        if tick is None:
            return
        window = self._windows.get(tick.symbol)
        if window is None:
            return
        if isinstance(tick, TradeTick):
            window.on_trade(tick)
        elif isinstance(tick, BookTick):
            window.on_book(tick)

    async def _publish(self) -> None:
        now = self._clock()
        live = set(self._windows)
        removed = self._published - live
        snapshots = [window.snapshot(now) for window in self._windows.values()]
        if not snapshots and not removed:
            return
        try:
            await self._store.publish(snapshots, removed=sorted(removed))
        except Exception:  # noqa: BLE001 - 발행 실패는 snapshot TTL 만료로 수렴
            logger.warning("nh-stream snapshot publish failed", exc_info=True)
            return
        self._published = live

    async def _shutdown(self) -> None:
        await self._close_all("shutdown")
        await self._release_lease()


def stream_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(ENABLED_ENV) or "").strip().lower() in {"1", "true", "yes"}


async def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)
    # docker stop(SIGTERM)을 받으면 main task를 취소해 구독 해제와 lease 반납을
    # 거친다. 그냥 죽으면 lease가 만료될 때까지 새 프로세스가 연결하지 못한다.
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if main_task is not None:
        for stop_signal in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(stop_signal, main_task.cancel)
    if not stream_enabled():
        logger.info("nh-stream disabled: %s is not true; idling", ENABLED_ENV)
        # 비활성 상태로 재시작 루프를 돌지 않도록 조용히 대기한다.
        await asyncio.Event().wait()
        return
    credentials = NhplugCredentials.from_env()
    client = redis.from_url(
        settings.get_redis_url(),
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        decode_responses=True,
    )
    async with httpx.AsyncClient() as http:
        provider = NhplugTokenProvider(
            credentials=credentials,
            cache_path=token_cache_path_from_env(),
            http_client=http,
        )
        runner = NhStreamRunner(
            redis_client=client,
            store=RedisTapeStore(client),
            token=provider.token,
        )
        try:
            await runner.run_forever()
        finally:
            await client.aclose()


if __name__ == "__main__":
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(main())
