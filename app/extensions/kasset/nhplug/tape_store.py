"""실시간 관찰 snapshot의 Redis 저장과 실행 경로의 관문.

``nh-stream`` 프로세스가 종목별 snapshot을 매초 ``kasset:nh:tape:{symbol}``에
짧은 TTL로 쓴다. 추천 선택·제출 직전 재확인·1분 청산 task는 여기서 읽기만
한다. Redis 오류·키 없음·형식 오류는 모두 "snapshot 없음"으로 취급해 신규 BUY를
막고(fail-closed), 보유 청산 경로는 이 저장소와 무관하게 동작한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Final, Protocol

import redis.asyncio as redis

from app.core.config import settings
from app.extensions.kasset.automation.realtime_tape import (
    DEFAULT_REALTIME_TAPE_CONFIG,
    RealtimeEntryCheck,
    RealtimeTapeConfig,
    TapeSnapshot,
    evaluate_realtime_entry,
)

logger = logging.getLogger(__name__)

KEY_PREFIX: Final = "kasset:nh"
OWNER_KEY: Final = f"{KEY_PREFIX}:owner"
SNAPSHOT_TTL_SECONDS: Final = 10


def snapshot_key(symbol: str) -> str:
    return f"{KEY_PREFIX}:tape:{symbol}"


class RedisTapeStore:
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    @classmethod
    def from_settings(cls) -> RedisTapeStore:
        return cls(
            redis.from_url(
                settings.get_redis_url(),
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        )

    async def publish(
        self,
        snapshots: Iterable[TapeSnapshot],
        *,
        removed: Iterable[str] = (),
    ) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            for snapshot in snapshots:
                pipe.set(
                    snapshot_key(snapshot.symbol),
                    json.dumps(snapshot.as_json(), separators=(",", ":")),
                    ex=SNAPSHOT_TTL_SECONDS,
                )
            for symbol in removed:
                pipe.delete(snapshot_key(symbol))
            await pipe.execute()

    async def read_many(self, symbols: Iterable[str]) -> dict[str, TapeSnapshot]:
        ordered = sorted({str(symbol) for symbol in symbols if str(symbol)})
        if not ordered:
            return {}
        try:
            raw_values = await self._redis.mget([snapshot_key(s) for s in ordered])
        except Exception:  # noqa: BLE001 - 관찰 부재는 신규 BUY 차단으로 수렴한다
            logger.warning("realtime tape snapshot read failed", exc_info=True)
            return {}
        result: dict[str, TapeSnapshot] = {}
        for symbol, raw in zip(ordered, raw_values, strict=True):
            if raw is None:
                continue
            try:
                payload = json.loads(raw if isinstance(raw, str) else bytes(raw))
                snapshot = TapeSnapshot.from_json(payload)
            except (TypeError, ValueError):
                logger.warning("realtime tape snapshot is malformed: symbol=%s", symbol)
                continue
            if snapshot.symbol == symbol:
                result[symbol] = snapshot
        return result

    async def aclose(self) -> None:
        await self._redis.aclose()


class TapeSnapshotReader(Protocol):
    async def read_many(self, symbols: Iterable[str]) -> Mapping[str, TapeSnapshot]: ...


class RealtimeEntryGate:
    """추천 선택과 제출 직전에 같은 판정을 호출하는 관문."""

    def __init__(
        self,
        reader: TapeSnapshotReader | None,
        *,
        config: RealtimeTapeConfig = DEFAULT_REALTIME_TAPE_CONFIG,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader
        self._config = config
        self._clock = clock

    async def check(self, symbol: str) -> RealtimeEntryCheck:
        snapshot: TapeSnapshot | None = None
        if self._reader is not None:
            snapshots = await self._reader.read_many((symbol,))
            snapshot = snapshots.get(symbol)
        # 판정 시각은 읽은 직후의 실제 시각이다. sweep의 절삭된 ``now``를 쓰면
        # 신선도가 최대 1초 느슨해진다.
        return evaluate_realtime_entry(snapshot, now=self._clock(), config=self._config)


__all__ = [
    "OWNER_KEY",
    "SNAPSHOT_TTL_SECONDS",
    "RealtimeEntryGate",
    "RedisTapeStore",
    "TapeSnapshotReader",
    "snapshot_key",
]
