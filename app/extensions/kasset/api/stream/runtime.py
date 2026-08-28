"""프로세스 단위 스트림 런타임.

한 `api` 프로세스가 담당하는 일은 셋이다.

1. **로컬 수요 집계** — 자기 프로세스에 붙은 클라이언트들의 토픽 구독 수를 세어
   Redis에 게시한다. 소유자는 이 합계로 상향 예산을 배분한다.
2. **틱 디스패치** — 팬아웃 채널에서 받은 틱을 그 토픽을 구독한 로컬 세션에만
   넣는다. 와이어 메시지는 토픽당 1회만 직렬화해 세션들이 같은 문자열을 공유한다.
3. **베이스라인 해석** — 토스 체결 프레임에는 전일 종가·종목명·통화가 없고, 구독
   직후 초기 스냅샷도 오지 않는다. 그래서 구독 시점에 기존 REST 경로
   (`krx_quotes.resolve_quotes`)로 한 번 해석해 baseline `Quote`를 만들고,
   이후 tick은 그 baseline 위에 가격·시각만 갈아 끼운다. 등락 계산을 스트림 쪽에서
   따로 구현하지 않으므로 폴링 값과 스트림 값이 갈라지지 않는다.

**기존 `orderbook_store.py`와의 관계.** 상태 저장소를 두 개 만들지 않는다.
`orderbook_store`는 REST `GET /api/v1/market/orderbook`의 NH PLUG KRX 채널 전용
으로 그대로 남고, 스트림은 그 저장소를 읽지도 쓰지도 않는다. 스트림 쪽 "마지막
값"은 세션 mailbox 안에만 있는 전송 버퍼이고, 조회 가능한 상태 저장소가 아니다.
같은 KRX 호가를 두 공급자에서 받을 수는 있으나 `source` 필드가 어느 채널의 값인지
항상 구분해 주며, 스트림이 NH WS 연결을 추가로 열지는 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import redis.asyncio as redis

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.stream import bus as bus_module
from app.extensions.kasset.api.stream import toss_protocol as protocol
from app.extensions.kasset.api.stream.bus import StreamBus
from app.extensions.kasset.api.stream.contract import (
    REASON_TOPIC_BUDGET,
    REASON_TOPIC_REJECTED,
    REASON_UPSTREAM_DOWN,
    UpstreamState,
    orderbook_from_frame,
    orderbook_message,
    quote_from_tick,
    quote_message,
)
from app.extensions.kasset.api.stream.session import StreamSession
from app.extensions.kasset.api.stream.topics import Topic, parse_topic
from app.extensions.kasset.api.stream.upstream import (
    TossUpstreamOwner,
    connect_toss_stream,
)

logger = logging.getLogger(__name__)

# baseline 은 전일 종가·종목명·통화만 담는다. 하루 단위로만 바뀌므로 짧게 캐시해도
# 같은 종목을 여는 클라이언트마다 DB·공급자를 두드리지 않는다.
BASELINE_TTL_SECONDS: Final[float] = 300.0
# 수요 게시 디바운스. full-replace 구조에서 화면 전환마다 즉시 반영하면 상향
# 선언 빈도(5회/초)에 쉽게 닿는다. 여기서 한 번 코얼레스하고, 소유자 쪽에서
# 재조정 주기로 한 번 더 코얼레스한다.
DEMAND_DEBOUNCE_SECONDS: Final[float] = 0.2

BaselineResolver = Callable[[Topic], Awaitable[Quote | None]]


class MarketStreamRuntime:
    """프로세스 하나가 들고 있는 스트림 런타임. 여러 번 시작해도 한 번만 뜬다."""

    def __init__(
        self,
        *,
        bus: StreamBus | None = None,
        baseline_resolver: BaselineResolver | None = None,
        owner: TossUpstreamOwner | None = None,
        start_owner: bool = True,
        demand_debounce_seconds: float = DEMAND_DEBOUNCE_SECONDS,
        baseline_ttl_seconds: float = BASELINE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._baseline_resolver = baseline_resolver or _resolve_baseline
        self._owner = owner
        self._start_owner = start_owner
        self._demand_debounce_seconds = demand_debounce_seconds
        self._baseline_ttl_seconds = baseline_ttl_seconds
        self._clock = clock

        self._sessions: set[StreamSession] = set()
        self._demand: dict[str, int] = {}
        self._baselines: dict[str, tuple[float, Quote]] = {}
        self._baseline_locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._demand_dirty = asyncio.Event()
        self._redis_owned: redis.Redis | None = None
        # 소유자가 게시한 최신 상태. 새로 붙은 클라이언트에게 즉시 알려 준다.
        self._live = False
        self._streaming: frozenset[str] = frozenset()
        self._rejected: frozenset[str] = frozenset()
        self._reason: str | None = REASON_UPSTREAM_DOWN

    # --- 수명 ----------------------------------------------------------------

    async def ensure_started(self) -> None:
        """첫 클라이언트가 붙을 때 게으르게 시작한다. 재호출은 무해하다.

        여기서 예외를 올리면 앱의 WebSocket 연결 자체가 깨진다. 상향이 없는
        상태(토스 비활성·Redis 장애)에서도 연결은 살려 두고 `DEGRADED` 상태로
        폴링을 지시하는 것이 옳으므로, 시작 실패는 강등으로 흡수한다.
        """

        async with self._start_lock:
            if self._tasks:
                return
            if self._bus is None:
                self._bus = self._build_bus()
            if self._owner is None and self._start_owner:
                self._owner = self._build_owner(self._bus)
            # 늦게 시작한 프로세스도 소유자가 게시한 최신 상태를 즉시 반영한다.
            # Redis가 없으면 기본값(강등)이 그대로 유지된다.
            try:
                state = await self._bus.read_state()
            except Exception as exc:  # noqa: BLE001 — 리더 태스크가 다시 시도한다
                logger.warning(
                    "kasset stream state unavailable at startup (%s)",
                    type(exc).__name__,
                )
                state = None
            if state is not None:
                self._absorb_state(state)
            self._tasks.append(
                asyncio.create_task(self._run_reader(), name="kasset-stream-reader")
            )
            self._tasks.append(
                asyncio.create_task(
                    self._run_demand_publisher(), name="kasset-stream-demand"
                )
            )
            if self._owner is not None:
                self._tasks.append(
                    asyncio.create_task(self._owner.run(), name="kasset-stream-owner")
                )

    async def aclose(self) -> None:
        """API 종료 경로. 소유자 리스를 남기지 않고 즉시 놓는다."""

        async with self._start_lock:
            tasks = list(self._tasks)
            self._tasks.clear()
            if self._owner is not None:
                self._owner.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for session in list(self._sessions):
                session.close()
            self._sessions.clear()
            self._demand.clear()
            self._baselines.clear()
            if self._bus is not None:
                with contextlib.suppress(Exception):
                    await self._bus.publish_demand({})
                with contextlib.suppress(Exception):
                    await self._bus.release_owner_lease()
            if self._redis_owned is not None:
                with contextlib.suppress(Exception):
                    await self._redis_owned.aclose()
                self._redis_owned = None
            self._owner = None
            self._bus = None

    def _build_bus(self) -> StreamBus:
        client = redis.from_url(
            settings.get_redis_url(),
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            decode_responses=True,
        )
        self._redis_owned = client
        # 인스턴스 식별자는 프로세스 단위여야 한다. 같은 컨테이너에 uvicorn 워커가
        # 여러 개 떠도 각자 다른 수요 필드를 쓰고, 리스 소유자도 하나로 판정된다.
        return StreamBus(
            redis_client=client, instance_id=f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )

    @staticmethod
    def _build_owner(bus: StreamBus) -> TossUpstreamOwner | None:
        """상향 소유자 후보. 토스가 꺼져 있으면 후보가 되지 않는다.

        게이트는 새로 만들지 않고 기존 `TOSS_API_ENABLED` 자격 검증을 그대로
        쓴다. 자격이 없으면 상향은 없고, 앱은 `status`의 `pollingTopics`를 보고
        기존 REST 폴링으로 값을 받는다.
        """

        from app.services.brokers.toss.auth import TossOAuthTokenManager
        from app.services.brokers.toss.errors import (
            TossApiDisabled,
            TossMissingCredentials,
        )

        try:
            manager = TossOAuthTokenManager.from_settings()
        except (TossApiDisabled, TossMissingCredentials) as exc:
            logger.info(
                "kasset stream upstream disabled (%s): clients stay on REST polling",
                type(exc).__name__,
            )
            return None

        async def token_provider(
            *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            return await manager.get_access_token(
                force_reissue=force_reissue, failed_token=failed_token
            )

        return TossUpstreamOwner(
            bus=bus,
            connector=connect_toss_stream,
            token_provider=token_provider,
        )

    # --- 세션 등록 ------------------------------------------------------------

    @property
    def upstream_state(self) -> UpstreamState:
        """접속 시점 상향 상태. `ready` 프레임에 실린다."""

        return "LIVE" if self._live else "DEGRADED"

    def register(self, session: StreamSession) -> None:
        # 구독이 없는 시점에는 `status`를 보내지 않는다. 폴링할 토픽이 없어서
        # 담을 내용이 없고, 앱이 "전부 스트리밍 중"으로 오해할 여지만 남는다.
        # 접속 시점 상향 상태는 `ready` 프레임이 전달한다.
        self._sessions.add(session)

    def unregister(self, session: StreamSession) -> None:
        """세션이 떠나면 그 구독분을 로컬 수요에서 뺀다.

        구독자가 0이 된 토픽은 수요 맵에서 사라지고, 소유자의 다음 재조정에서
        선언 목록에서 빠진다(full-replace라 빠진 항목이 곧 해제다).
        """

        if session not in self._sessions:
            return
        self._sessions.discard(session)
        self._release(session.topics)
        session.close()

    async def declare(
        self, session: StreamSession, requested: Iterable[object]
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """클라이언트 선언을 반영하고 수락된 토픽의 baseline을 즉시 보낸다."""

        previous = session.topics
        accepted, rejected, released = session.declare(requested)
        self._release(released)
        added = frozenset(accepted) - previous
        for key in added:
            self._demand[key] = self._demand.get(key, 0) + 1
        if added or released:
            self._demand_dirty.set()

        self._notify_status(session)
        for key in sorted(added):
            topic = session.topic(key)
            if topic is None or topic.kind != "quote":
                # 호가는 구독 직후 초기 스냅샷이 없다(상향도 없다). 앱은 화면을
                # 열 때 받은 REST 스냅샷 위에 첫 푸시를 얹는다.
                continue
            baseline = await self._baseline(topic)
            if baseline is not None:
                session.offer(key, quote_message(topic, baseline))
        return accepted, rejected

    def _release(self, keys: Iterable[str]) -> None:
        changed = False
        for key in keys:
            remaining = self._demand.get(key, 0) - 1
            if remaining > 0:
                self._demand[key] = remaining
            else:
                self._demand.pop(key, None)
            changed = True
        if changed:
            self._demand_dirty.set()

    @property
    def demand(self) -> Mapping[str, int]:
        return self._demand

    # --- 상태 통보 ------------------------------------------------------------

    def _notify_status(self, session: StreamSession) -> None:
        """이 세션이 폴링으로 메워야 하는 토픽을 계산해 알린다.

        상향이 살아 있어도 예산 초과·공급자 거부로 스트리밍하지 않는 토픽이
        있을 수 있다. 그 경우에도 앱이 값을 못 보는 일이 없게 폴링 목록으로
        내보내고, 사유를 구분해 준다.
        """

        if not self._live:
            session.offer_status(
                upstream="DEGRADED",
                reason=self._reason or REASON_UPSTREAM_DOWN,
                polling_topics=sorted(session.topics),
            )
            return
        polling = sorted(session.topics - self._streaming)
        reason: str | None = None
        if polling:
            reason = (
                REASON_TOPIC_REJECTED
                if self._rejected.intersection(polling)
                else REASON_TOPIC_BUDGET
            )
        session.offer_status(upstream="LIVE", reason=reason, polling_topics=polling)

    def _absorb_state(self, state: Mapping[str, Any]) -> None:
        self._live = bool(state.get("live"))
        self._streaming = _string_set(state.get("streaming"))
        self._rejected = _string_set(state.get("rejected"))
        reason = state.get("reason")
        self._reason = reason if isinstance(reason, str) else None

    # --- 백그라운드 태스크 -----------------------------------------------------

    async def _run_demand_publisher(self) -> None:
        """로컬 수요를 게시한다. 변경은 디바운스, 무변경은 하트비트로 갱신한다."""

        assert self._bus is not None
        while True:
            try:
                await asyncio.wait_for(
                    self._demand_dirty.wait(),
                    timeout=bus_module.DEMAND_HEARTBEAT_SECONDS,
                )
                self._demand_dirty.clear()
                if self._demand_debounce_seconds > 0:
                    await asyncio.sleep(self._demand_debounce_seconds)
                    self._demand_dirty.clear()
            except TimeoutError:
                pass
            try:
                await self._bus.publish_demand(self._demand)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 다음 주기에 다시 시도한다
                logger.warning(
                    "kasset stream demand publish failed (%s)", type(exc).__name__
                )

    async def _run_reader(self) -> None:
        """팬아웃 채널을 읽어 로컬 세션에 넣는다."""

        assert self._bus is not None
        while True:
            try:
                async for event in self._bus.listen():
                    await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 구독을 다시 건다
                logger.warning(
                    "kasset stream fanout reader restarting (%s)", type(exc).__name__
                )
            await asyncio.sleep(1.0)

    async def _handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("kind")
        if kind == bus_module.EVENT_STATE:
            self._absorb_state(event)
            for session in list(self._sessions):
                self._notify_status(session)
            return
        if kind != bus_module.EVENT_TICK:
            return
        key = event.get("topic")
        if not isinstance(key, str):
            return
        listeners = [session for session in self._sessions if key in session.topics]
        if not listeners:
            return
        message = await self._tick_message(key, event)
        if message is None:
            return
        for session in listeners:
            session.offer(key, message)

    async def _tick_message(self, key: str, event: Mapping[str, Any]) -> str | None:
        """틱 하나를 와이어 메시지로 만든다. 토픽당 1회만 직렬화한다."""

        try:
            topic = parse_topic(key)
        except Exception:  # noqa: BLE001 — 우리 키만 오는 자리다
            return None

        trade = event.get("trade")
        if isinstance(trade, dict):
            baseline = await self._baseline(topic)
            if baseline is None:
                return None
            price = _decimal(trade.get("price"))
            as_of = _isoformat(trade.get("asOf"))
            if price is None or as_of is None:
                return None
            return quote_message(
                topic,
                quote_from_tick(
                    baseline,
                    price=price,
                    as_of=as_of,
                    source=protocol.TOSS_STREAM_SOURCE,
                ),
            )

        book = event.get("orderbook")
        if isinstance(book, dict):
            asks = _levels(book.get("asks"))
            bids = _levels(book.get("bids"))
            if asks is None or bids is None:
                return None
            return orderbook_message(
                topic,
                orderbook_from_frame(
                    topic,
                    asks=asks,
                    bids=bids,
                    as_of=_isoformat(book.get("asOf")),
                    source=protocol.TOSS_STREAM_SOURCE,
                ),
            )
        return None

    # --- baseline ------------------------------------------------------------

    async def _baseline(self, topic: Topic) -> Quote | None:
        cached = self._baselines.get(topic.key)
        now = self._clock()
        if cached is not None and now - cached[0] < self._baseline_ttl_seconds:
            return cached[1]
        lock = self._baseline_locks.setdefault(topic.key, asyncio.Lock())
        async with lock:
            cached = self._baselines.get(topic.key)
            now = self._clock()
            if cached is not None and now - cached[0] < self._baseline_ttl_seconds:
                return cached[1]
            try:
                quote = await self._baseline_resolver(topic)
            except Exception as exc:  # noqa: BLE001 — 다음 틱에서 다시 시도한다
                logger.warning(
                    "kasset stream baseline unavailable topic=%s (%s)",
                    topic.key,
                    type(exc).__name__,
                )
                return cached[1] if cached is not None else None
            if quote is None:
                return None
            self._baselines[topic.key] = (self._clock(), quote)
            return quote


async def _resolve_baseline(topic: Topic) -> Quote | None:
    """기존 REST 시세 해석 경로를 그대로 쓴다.

    WebSocket 연결 수명 동안 DB 세션을 붙잡아 두면 커넥션 풀이 고갈되므로, 여기서
    필요한 순간에만 짧게 세션을 열고 닫는다.
    """

    async with AsyncSessionLocal() as db:
        quotes = await krx_quotes.resolve_quotes(
            db, market=topic.market, symbols=[topic.symbol]
        )
    return quotes[0] if quotes else None


def _decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except Exception:  # noqa: BLE001 — 팬아웃 페이로드 방어
        return None
    return value if value.is_finite() else None


def _isoformat(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _string_set(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def _levels(raw: object) -> list[tuple[Decimal, Decimal]] | None:
    if not isinstance(raw, list):
        return None
    levels: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            continue
        price = _decimal(item[0])
        volume = _decimal(item[1])
        if price is None or volume is None:
            continue
        levels.append((price, volume))
    return levels


market_stream_runtime = MarketStreamRuntime()


def get_stream_runtime() -> MarketStreamRuntime:
    """FastAPI 의존성 훅. 테스트는 이 훅을 덮어 자기 런타임을 주입한다."""

    return market_stream_runtime
