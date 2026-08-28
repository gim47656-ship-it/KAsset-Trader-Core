"""앱 클라이언트 1개의 구독 상태와 송신 파이프라인.

**왜 큐가 아니라 최신값 대체(conflation)인가.** 상향 계약이 그렇게 정의되어 있다.
토스는 `trade`·`orderbook` 채널을 LOSSY로 명시한다 — 수신이 밀리면 중간 프레임이
유실될 수 있고, 항상 최신 상태가 우선이며 유실 감지용 sequence를 주지 않는다.
그러니 하향에서 큐를 쌓아 30초 전 체결가를 순서대로 재생하는 것은 쓸모가 없고
서버 메모리만 먹는다. 토픽별로 **마지막 메시지 하나만** 들고 있다가 보낸다.

메모리 상한은 구조적으로 잡힌다. 대기 중 메시지 수는 그 연결의 구독 토픽 수
(`MAX_CLIENT_TOPICS`) + 제어 메시지 유계 FIFO 이상 커질 수 없다.

이 파이프라인은 시세 전용이다. LOSSLESS 채널(`personal:order`)을 여기에 얹으면
안 된다 — 이유는 `upstream.py` 모듈 주석에 남겼다.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable

from app.extensions.kasset.api.stream.contract import (
    STATUS_MAILBOX_KEY,
    UpstreamState,
    status_message,
)
from app.extensions.kasset.api.stream.topics import (
    MAX_CLIENT_TOPICS,
    REJECT_TOO_MANY,
    Topic,
    TopicRejected,
    parse_topic,
)

logger = logging.getLogger(__name__)

# 한 번의 send 가 이 시간을 넘기면 그 클라이언트는 소비 능력이 없는 것으로 본다.
# conflation 이 메모리는 막아 주지만, 영구히 막힌 소켓은 태스크를 붙잡아 둔다.
SEND_TIMEOUT_SECONDS: float = 5.0
# ack·error 같은 제어 메시지는 합칠 수 없다(각각 다른 요청에 대한 응답이다).
# 대신 유계 FIFO 로 두어 폭주해도 메모리가 늘지 않게 한다.
CONTROL_QUEUE_LIMIT: int = 16

Sender = Callable[[str], Awaitable[None]]


class SlowConsumer(RuntimeError):
    """송신이 시간 안에 끝나지 않았다. 연결을 닫는 것이 유일한 대응이다."""


class StreamSession:
    """클라이언트 1개의 구독 집합 + 최신값 대체 송신 큐."""

    def __init__(
        self,
        *,
        send: Sender,
        max_topics: int = MAX_CLIENT_TOPICS,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
        control_limit: int = CONTROL_QUEUE_LIMIT,
    ) -> None:
        self._send = send
        self._max_topics = max(1, max_topics)
        self._send_timeout = send_timeout
        self._topics: dict[str, Topic] = {}
        # 토픽 키 → 아직 보내지 않은 최신 메시지. 같은 키가 다시 들어오면 덮는다.
        self._pending: dict[str, str] = {}
        self._control: deque[str] = deque(maxlen=max(1, control_limit))
        self._wake = asyncio.Event()
        self._closed = False
        # 관측용: conflation 으로 건너뛴 프레임 수.
        self.conflated = 0

    @property
    def topics(self) -> frozenset[str]:
        return frozenset(self._topics)

    @property
    def closed(self) -> bool:
        return self._closed

    def topic(self, key: str) -> Topic | None:
        return self._topics.get(key)

    # --- 구독 -----------------------------------------------------------------

    def declare(
        self, requested: Iterable[object]
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], frozenset[str]]:
        """선언형 full-replace. 이 호출의 인자가 곧 이 연결의 구독 전체다.

        반환값은 `(수락된 토픽, (토픽, 거부코드) 목록, 해제된 토픽)`이다. 화면을
        나갈 때 앱이 새 목록(또는 빈 목록)을 선언하면 빠진 토픽이 곧 해제분이고,
        호출부는 그만큼 상향 수요를 줄인다. 놓친 `unsubscribe` 때문에 상향 예산이
        새는 경로가 아예 없고, 상향 토스 프로토콜과 같은 모델이라 변환도 얇다.
        """

        accepted: dict[str, Topic] = {}
        rejected: list[tuple[str, str]] = []
        for raw in requested:
            try:
                topic = parse_topic(raw)
            except TopicRejected as exc:
                rejected.append((exc.topic, exc.code))
                continue
            if topic.key in accepted:
                continue
            if len(accepted) >= self._max_topics:
                rejected.append((topic.key, REJECT_TOO_MANY))
                continue
            accepted[topic.key] = topic

        released = frozenset(self._topics) - frozenset(accepted)
        self._topics = accepted
        for key in released:
            self._pending.pop(key, None)
        return tuple(sorted(accepted)), tuple(rejected), released

    # --- 송신 -----------------------------------------------------------------

    def offer(self, topic_key: str, message: str) -> None:
        """토픽 데이터를 최신값으로 대체 등록한다. 구독하지 않은 토픽은 버린다."""

        if self._closed or topic_key not in self._topics:
            return
        if topic_key in self._pending:
            self.conflated += 1
        self._pending[topic_key] = message
        self._wake.set()

    def offer_status(
        self,
        *,
        upstream: UpstreamState,
        reason: str | None,
        polling_topics: Iterable[str],
    ) -> None:
        """강등 시그널. 최신 상태만 의미가 있으므로 데이터와 같이 합쳐진다."""

        if self._closed:
            return
        self._pending[STATUS_MAILBOX_KEY] = status_message(
            upstream=upstream, reason=reason, polling_topics=polling_topics
        )
        self._wake.set()

    def push_control(self, message: str) -> None:
        """ack·error·pong. 합치지 않고 순서대로 보낸다(유계 FIFO)."""

        if self._closed:
            return
        self._control.append(message)
        self._wake.set()

    async def run(self) -> None:
        """송신 루프. 깨어날 때마다 제어 메시지 → 최신 데이터 순으로 비운다."""

        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._closed:
                return
            await self.flush()

    async def flush(self) -> None:
        """지금 대기 중인 것만 보낸다. 테스트가 결정적으로 관찰할 수 있는 단위다."""

        while self._control:
            await self._deliver(self._control.popleft())
        if not self._pending:
            return
        # 스냅샷을 떠서 비운다. 보내는 동안 새로 들어온 값은 다음 회차에 최신값으로
        # 다시 합쳐지므로, 오래된 프레임이 최신 프레임을 앞지르지 않는다.
        batch = self._pending
        self._pending = {}
        for message in batch.values():
            await self._deliver(message)

    async def _deliver(self, message: str) -> None:
        try:
            await asyncio.wait_for(self._send(message), timeout=self._send_timeout)
        except TimeoutError as exc:
            raise SlowConsumer("stream client send timed out") from exc

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        self._control.clear()
        self._topics.clear()
        self._wake.set()
