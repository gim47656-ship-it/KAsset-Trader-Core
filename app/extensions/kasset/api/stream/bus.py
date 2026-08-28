"""Redis 기반 스트림 조정 계층.

`api` 프로세스가 여러 개여도 토스 연결은 전역 1개여야 한다(계정당 2개 상한).
이 모듈은 그 조정에 필요한 네 가지만 담당한다.

1. **소유자 리스** — `SET NX PX` + CAS 갱신. 리스를 든 프로세스만 상향 연결을 만든다.
2. **구독 수요 집계** — 프로세스별 필드를 가진 해시 하나. 각 프로세스가 자기
   로컬 수요를 주기적으로 써 넣고, 소유자가 합산해 토픽 예산을 배분한다.
   필드에 만료 시각을 함께 담아, 죽은 프로세스의 수요는 소유자가 읽을 때 지운다.
3. **틱 팬아웃** — pub/sub 채널 1개. 소유자가 publish, 모든 프로세스가 구독한다.
4. **스트림 상태** — 강등 여부·스트리밍 토픽 목록. 늦게 붙은 프로세스도 키에서
   즉시 읽을 수 있게 pub/sub과 키를 함께 쓴다.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Any, Final

import redis.asyncio as redis
from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

_PREFIX: Final[str] = "kasset:stream"
OWNER_KEY: Final[str] = f"{_PREFIX}:owner"
DEMAND_KEY: Final[str] = f"{_PREFIX}:demand"
STATE_KEY: Final[str] = f"{_PREFIX}:state"
EVENTS_CHANNEL: Final[str] = f"{_PREFIX}:events"

# 리스 수명과 갱신 주기. 갱신 주기는 수명의 1/3로 두어 한 번 놓쳐도 살아남는다.
OWNER_LEASE_SECONDS: Final[float] = 15.0
OWNER_RENEW_SECONDS: Final[float] = 5.0
# 리스를 못 잡은 프로세스가 다시 시도하는 주기.
OWNER_CANDIDATE_SECONDS: Final[float] = 5.0
# 수요 필드 수명. 하트비트를 두 번 놓쳐도 살아 있게 잡는다.
DEMAND_TTL_SECONDS: Final[float] = 30.0
DEMAND_HEARTBEAT_SECONDS: Final[float] = 5.0
STATE_TTL_SECONDS: Final[int] = 60

EVENT_TICK: Final[str] = "tick"
EVENT_STATE: Final[str] = "state"

# 리스 갱신·해제는 소유권 확인과 원자적이어야 한다. 확인 없이 PEXPIRE/DEL 하면
# 이미 남에게 넘어간 리스를 되찾거나 남의 리스를 지운다. WATCH/MULTI 낙관적
# 락으로 그 원자성을 얻는다(Lua 없이도 되고, 스크립트 캐시에 의존하지 않는다).


class StreamBus:
    """스트림 조정에 쓰는 Redis 접근을 한곳에 모은 얇은 계층."""

    def __init__(
        self,
        *,
        redis_client: redis.Redis,
        instance_id: str,
        lease_seconds: float = OWNER_LEASE_SECONDS,
        demand_ttl_seconds: float = DEMAND_TTL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._instance_id = instance_id
        self._lease_seconds = lease_seconds
        self._demand_ttl_seconds = demand_ttl_seconds

    @property
    def instance_id(self) -> str:
        return self._instance_id

    # --- 소유자 리스 ----------------------------------------------------------

    async def acquire_owner_lease(self) -> bool:
        """리스를 잡으면 `True`. 이미 소유자면 갱신으로 처리해 그대로 유지한다."""

        acquired = await self._redis.set(
            OWNER_KEY,
            self._instance_id,
            nx=True,
            px=int(self._lease_seconds * 1000),
        )
        if acquired:
            return True
        return await self.renew_owner_lease()

    async def renew_owner_lease(self) -> bool:
        """내가 소유자일 때만 리스 수명을 연장한다."""

        return await self._compare_and_swap(extend=True)

    async def release_owner_lease(self) -> None:
        """내가 소유자일 때만 리스를 놓는다. 실패는 리스 만료로 수습된다."""

        with suppress(Exception):
            await self._compare_and_swap(extend=False)

    async def _compare_and_swap(self, *, extend: bool) -> bool:
        try:
            async with self._redis.pipeline() as pipe:
                await pipe.watch(OWNER_KEY)
                current = await pipe.get(OWNER_KEY)
                if current is None or _text(current) != self._instance_id:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                if extend:
                    pipe.pexpire(OWNER_KEY, int(self._lease_seconds * 1000))
                else:
                    pipe.delete(OWNER_KEY)
                await pipe.execute()
                return True
        except WatchError:
            # 그 사이 다른 프로세스가 키를 바꿨다. 소유권이 없다는 뜻이다.
            return False

    # --- 구독 수요 ------------------------------------------------------------

    async def publish_demand(self, topics: Mapping[str, int]) -> None:
        """이 프로세스의 로컬 수요를 게시한다. 빈 수요는 필드를 지운다."""

        if not topics:
            await self._redis.hdel(DEMAND_KEY, self._instance_id)
            return
        payload = json.dumps(
            {
                "expiresAt": time.time() + self._demand_ttl_seconds,
                "topics": dict(topics),
            },
            separators=(",", ":"),
        )
        await self._redis.hset(DEMAND_KEY, self._instance_id, payload)

    async def aggregate_demand(self) -> dict[str, int]:
        """살아 있는 프로세스의 수요를 합산한다. 만료된 필드는 지운다."""

        raw = await self._redis.hgetall(DEMAND_KEY)
        if not raw:
            return {}
        now = time.time()
        total: dict[str, int] = {}
        stale: list[str] = []
        for field, value in raw.items():
            instance = _text(field)
            try:
                entry = json.loads(value)
                expires_at = float(entry["expiresAt"])
                topics = entry["topics"]
            except (KeyError, TypeError, ValueError):
                stale.append(instance)
                continue
            if expires_at <= now or not isinstance(topics, dict):
                stale.append(instance)
                continue
            for key, count in topics.items():
                if not isinstance(key, str):
                    continue
                try:
                    parsed = int(count)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    total[key] = total.get(key, 0) + parsed
        if stale:
            await self._redis.hdel(DEMAND_KEY, *stale)
        return total

    # --- 팬아웃 --------------------------------------------------------------

    async def publish_event(self, event: Mapping[str, Any]) -> None:
        await self._redis.publish(
            EVENTS_CHANNEL, json.dumps(event, separators=(",", ":"))
        )

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        """팬아웃 채널을 구독해 이벤트를 순서대로 흘린다."""

        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(EVENTS_CHANNEL)
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    event = json.loads(raw["data"])
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    yield event
        finally:
            # 종료 경로다. 여기서 예외를 올리면 원래 종료 사유를 가린다.
            with suppress(Exception):
                await pubsub.unsubscribe(EVENTS_CHANNEL)
            with suppress(Exception):
                await pubsub.aclose()

    # --- 상태 ----------------------------------------------------------------

    async def store_state(self, state: Mapping[str, Any]) -> None:
        await self._redis.set(
            STATE_KEY,
            json.dumps(state, separators=(",", ":")),
            ex=STATE_TTL_SECONDS,
        )

    async def read_state(self) -> dict[str, Any] | None:
        raw = await self._redis.get(STATE_KEY)
        if not raw:
            return None
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return state if isinstance(state, dict) else None


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
