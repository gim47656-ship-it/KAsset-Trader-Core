"""토스 실시간 WS 단일 소유자.

**왜 Redis 리스 선출인가.** 토스는 계정당 동시 연결을 2개로 제한하고, 초과하면
새 연결을 수락한 뒤 *가장 오래된 연결을 조용히 끊는다*. 즉 `api` 프로세스마다
연결을 만들면 서로를 죽이며 무한 재연결 루프가 된다. 단일 소유자는 선택이 아니라
필수다.

전용 컨테이너를 세우는 대안도 있었지만 리스 선출을 골랐다.

1. 어느 쪽을 골라도 수요 집계용 Redis 계층은 필요하다(구독자는 여러 프로세스에
   흩어져 있다). 리스는 그 위에 키 하나를 더 얹는 것뿐이다.
2. 새 배포 표면이 늘지 않는다. 컨테이너·이미지·헬스체크·재시작 정책이 없다.
3. 전용 컨테이너는 그것이 죽는 순간 스트림 전체가 죽는 단일 장애점이다. 리스는
   살아 있는 아무 `api` 프로세스가 리스 만료(15초) 후 이어받는다.
4. 연결 2개 중 1개만 쓰는 이유도 여기 있다. 리더 교대 순간에는 이전 소유자의
   연결이 아직 닫히지 않았을 수 있어 두 연결이 겹칠 수 있다. 1개만 쓰면 그 겹침이
   계정 한도 안에서 흡수되고, "가장 오래된 연결이 밀려나는" 동작이 오히려
   교대를 마무리해 준다.

**선언 상태 기계.** 구독은 선언형 full-replace이고 선언 실패는 in-band 프레임이다.
선언 전체 실패(`error`)면 서버 쪽 구독은 **직전 선언 그대로 유지**된다. 그래서
"보낸 것"과 "ack로 확정된 것"을 따로 들고 있어야 한다. 하나로 합치면 실패한
선언을 확정으로 착각해 재조정이 멈춘다.

**personal:order 는 이 슬라이스 범위가 아니다.** 이유를 남긴다. 주문 이벤트는
LOSSLESS 채널이라 미소비분을 건너뛸 수 없고(2초 이상 막히면 서버가 연결을
종료한다), 여기서 쓰는 최신값 대체(conflation)를 적용하면 안 된다. 또 LOSSLESS는
연결 세션 안에서만 보장되므로 재연결 뒤에는 `GET /api/v1/orders` 재동기화가
필수다. 시세 팬아웃과 전달 보장이 정반대이므로 같은 세션 파이프라인에 얹으면 안
되고, 별도 슬라이스에서 전용 경로로 붙여야 한다. 다만 구독 한도 100건은
`personal:order` 의 `accountSeq` 까지 합산하므로, 예산 상한은 이미 그 몫을
고려해 시세 쪽에서 100을 다 쓰지 않도록 `capacity` 를 주입 가능하게 두었다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Final, Protocol

from app.extensions.kasset.api.stream import bus as bus_module
from app.extensions.kasset.api.stream import toss_protocol as protocol
from app.extensions.kasset.api.stream.bus import StreamBus
from app.extensions.kasset.api.stream.topics import (
    MAX_UPSTREAM_TOPICS,
    TopicBudget,
    allocate_topics,
    parse_topic,
    parse_upstream_key,
)

logger = logging.getLogger(__name__)

# 재연결 백오프. 명세 권고(1s → 2s → 4s ..., jitter)를 그대로 쓰고 상한을 둔다.
BACKOFF_BASE_SECONDS: Final[float] = 1.0
BACKOFF_MAX_SECONDS: Final[float] = 60.0
# 연속 실패 상한. 이 횟수를 넘으면 즉시 재시도를 멈추고 냉각기로 내려간다.
# 무한 재시도 루프를 만들지 않기 위한 유계 장치다.
MAX_CONSECUTIVE_FAILURES: Final[int] = 6
COOLDOWN_SECONDS: Final[float] = 300.0
# 엣지 차단·허용 IP 미등록·동시 연결 한도는 재시도로 풀리지 않는다. 긴 냉각으로 보낸다.
BLOCKED_COOLDOWN_SECONDS: Final[float] = 600.0

# 수신 폴링 간격. recv 를 이 간격으로 깨워 선언 재조정·리스 갱신·PING 을 처리한다.
RECV_POLL_SECONDS: Final[float] = 0.5
# 구독 재조정(=선언) 간격. 선언 빈도 한도는 5회/초이고, full-replace 구조에서는
# 화면 전환마다 즉시 선언하면 쉽게 닿는다. 그래서 재조정을 이 주기로 코얼레스해
# 초당 최대 1회만 선언한다.
RECONCILE_INTERVAL_SECONDS: Final[float] = 1.0
# ack/error 를 기다리는 시간. 이 시간이 지나면 응답을 못 받은 선언은 버리고
# 다시 선언한다(응답이 영구히 안 오면 재조정이 멈추기 때문).
DECLARE_ACK_TIMEOUT_SECONDS: Final[float] = 5.0
# 공급자가 거부한 토픽을 다시 시도해 볼 때까지의 시간. 종목 마스터가 갱신되면
# 풀릴 수 있으므로 영구 차단하지 않는다.
REJECT_COOLDOWN_SECONDS: Final[float] = 1800.0
# 평문 `PING` 뒤 아무 상향 프레임도 받지 못한 연속 주기 상한. 첫 PING 뒤
# 약 120초(60초 × 2) 동안 무응답이면 half-open 연결로 판정한다.
MAX_MISSED_KEEPALIVES: Final[int] = 2

# 강등 사유(하향 `status.reason` 으로 그대로 나간다).
REASON_UNAVAILABLE: Final[str] = "UPSTREAM_UNAVAILABLE"
REASON_BLOCKED: Final[str] = "UPSTREAM_BLOCKED"


class UpstreamConnection(Protocol):
    """테스트에서 갈아끼울 수 있는 최소 연결 인터페이스."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], contextlib.AbstractAsyncContextManager[UpstreamConnection]]
TokenProvider = Callable[..., Awaitable[str]]


class UpstreamHandshakeError(RuntimeError):
    """handshake 단계 HTTP 실패. 연결 이후 실패는 전부 in-band 프레임이다."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"handshake rejected with HTTP {status_code}")
        self.status_code = status_code


class _FatalUpstream(RuntimeError):
    """우리 프레임이 틀렸다는 뜻. 재시도해도 결과가 같다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Blocked(RuntimeError):
    """엣지 차단·허용 IP·동시 연결 한도. 긴 냉각이 유일한 대응이다."""


class _StaleConnection(RuntimeError):
    """PING에 응답하지 않는 half-open 연결. 즉시 강등 후 재연결한다."""


class TossUpstreamOwner:
    """리스를 잡은 프로세스에서만 토스 WS 연결을 열고 틱을 팬아웃한다."""

    def __init__(
        self,
        *,
        bus: StreamBus,
        connector: Connector,
        token_provider: TokenProvider,
        clock: Callable[[], float] = time.monotonic,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        blocked_cooldown_seconds: float = BLOCKED_COOLDOWN_SECONDS,
        candidate_poll_seconds: float = bus_module.OWNER_CANDIDATE_SECONDS,
        reconcile_interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
        recv_poll_seconds: float = RECV_POLL_SECONDS,
        keepalive_interval_seconds: float = protocol.KEEPALIVE_INTERVAL_SECONDS,
        max_missed_keepalives: int = MAX_MISSED_KEEPALIVES,
        renew_interval_seconds: float = bus_module.OWNER_RENEW_SECONDS,
        capacity: int = MAX_UPSTREAM_TOPICS,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = BACKOFF_MAX_SECONDS,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._bus = bus
        self._connector = connector
        self._token_provider = token_provider
        self._clock = clock
        self._max_consecutive_failures = max(1, max_consecutive_failures)
        self._cooldown_seconds = cooldown_seconds
        self._blocked_cooldown_seconds = blocked_cooldown_seconds
        self._candidate_poll_seconds = candidate_poll_seconds
        self._reconcile_interval_seconds = reconcile_interval_seconds
        self._recv_poll_seconds = recv_poll_seconds
        self._keepalive_interval_seconds = keepalive_interval_seconds
        self._max_missed_keepalives = max(1, max_missed_keepalives)
        self._renew_interval_seconds = renew_interval_seconds
        self._capacity = max(1, min(capacity, MAX_UPSTREAM_TOPICS))
        self._backoff_base_seconds = max(backoff_base_seconds, 0.0)
        self._backoff_max_seconds = max(backoff_max_seconds, self._backoff_base_seconds)
        self._jitter = jitter
        self._stop = asyncio.Event()

        # ack 로 확정된 구독 집합(다운스트림 토픽 키).
        self._confirmed: frozenset[str] = frozenset()
        # 보냈지만 아직 ack/error 를 못 받은 선언. `(id, 원하는 집합, 보낸 시각)`.
        self._inflight: tuple[str, frozenset[str], float] | None = None
        # 공급자가 거부한 토픽 → 재시도 가능 시각.
        self._rejected: dict[str, float] = {}
        self._declare_sequence = 0
        # 토큰 재발급 신호. handshake 401 을 받은 다음 연결에서만 강제 재발급한다.
        self._force_token_reissue = False
        self._last_token: str | None = None

        # 관측용. 재연결 백오프가 유계인지, 냉각으로 내려갔는지 확인할 수 있다.
        self.connect_attempts = 0
        self.backoff_delays: list[float] = []
        self.cooldown_delays: list[float] = []

    # --- 공개 API -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def confirmed_topics(self) -> frozenset[str]:
        return self._confirmed

    @property
    def rejected_topics(self) -> frozenset[str]:
        return frozenset(self._rejected)

    async def run(self) -> None:
        """리더 후보 루프. 리스를 잡은 동안에만 상향 연결을 유지한다."""

        failures = 0
        while not self._stop.is_set():
            if not await self._bus.acquire_owner_lease():
                await self._wait(self._candidate_poll_seconds)
                continue
            produced = False
            blocked = False
            try:
                produced = await self._session()
            except _FatalUpstream as exc:
                # 재시도는 같은 결과를 낳고 선언 빈도 한도만 태운다. 소유권을
                # 넘기고 이 프로세스에서는 멈춘다(배포·수정이 유일한 해결이다).
                logger.error(
                    "toss stream upstream fatal protocol failure (%s): owner stops",
                    exc.reason,
                )
                await self._publish_state(live=False, reason=exc.reason)
                return
            except _Blocked as exc:
                logger.warning("toss stream upstream blocked (%s)", exc)
                blocked = True
            except _StaleConnection:
                # 표준 WS ping을 끈 대신 평문 PING 왕복 하나로 dead-peer를
                # 판정한다. 앱이 즉시 REST 폴링으로 강등되도록 상태도 바로 내린다.
                logger.warning(
                    "toss stream keepalive unanswered: reconnecting upstream"
                )
                await self._publish_state(live=False, reason=REASON_UNAVAILABLE)
            except UpstreamHandshakeError as exc:
                blocked = self._on_handshake_failure(exc)
            except Exception as exc:  # noqa: BLE001 — 전송 실패는 재연결로 수습한다
                logger.warning(
                    "toss stream upstream disconnected (%s)", type(exc).__name__
                )
            finally:
                # 구독은 연결에 매인다. 다음 세션은 처음부터 다시 선언한다.
                self._confirmed = frozenset()
                self._inflight = None
                await self._bus.release_owner_lease()

            if self._stop.is_set():
                return
            if blocked:
                await self._publish_state(live=False, reason=REASON_BLOCKED)
                self.cooldown_delays.append(self._blocked_cooldown_seconds)
                await self._wait(self._blocked_cooldown_seconds)
                failures = 0
                continue
            failures = 0 if produced else failures + 1
            if failures >= self._max_consecutive_failures:
                # 유계 장치: 짧은 백오프를 무한히 반복하지 않고 냉각기로 내려간다.
                await self._publish_state(live=False, reason=REASON_UNAVAILABLE)
                self.cooldown_delays.append(self._cooldown_seconds)
                await self._wait(self._cooldown_seconds)
                failures = 0
                continue
            delay = self._backoff(failures)
            self.backoff_delays.append(delay)
            await self._wait(delay)

    def _on_handshake_failure(self, exc: UpstreamHandshakeError) -> bool:
        """handshake HTTP 실패 분기. `True` 면 긴 냉각으로 보낸다."""

        if exc.status_code == 401:
            # 토큰 없음·무효·만료. 다음 연결에서 강제 재발급한다.
            self._force_token_reissue = True
            logger.warning("toss stream handshake 401: forcing token reissue")
            return False
        if exc.status_code == 403:
            # 허용 IP 미등록. 재시도로 풀리지 않는다(WTS 설정이 필요하다).
            logger.error("toss stream handshake 403: allowed-IP registration required")
            return True
        logger.warning("toss stream handshake failed status=%d", exc.status_code)
        return False

    # --- 세션 ----------------------------------------------------------------

    async def _session(self) -> bool:
        """연결 1회. 데이터를 한 건이라도 받았으면 `True`."""

        token = await self._token_provider(
            force_reissue=self._force_token_reissue,
            failed_token=self._last_token if self._force_token_reissue else None,
        )
        self._force_token_reissue = False
        self._last_token = token
        self.connect_attempts += 1

        produced = False
        # 첫 재조정은 즉시 돌려 현재 수요를 선언한다.
        next_reconcile = 0.0
        now = self._clock()
        next_renew = now + self._renew_interval_seconds
        next_ping = now + self._keepalive_interval_seconds
        keepalive_pending = False
        missed_keepalives = 0
        declare_blocked_until = 0.0

        async with self._connector(token) as connection:
            while not self._stop.is_set():
                now = self._clock()
                if now >= next_reconcile and now >= declare_blocked_until:
                    await self.reconcile(connection)
                    next_reconcile = now + self._reconcile_interval_seconds
                if now >= next_renew:
                    if not await self._bus.renew_owner_lease():
                        # 리스를 잃었다. 다른 프로세스가 소유자다. 즉시 물러난다.
                        logger.info("toss stream owner lease lost: releasing upstream")
                        return produced
                    next_renew = now + self._renew_interval_seconds
                if now >= next_ping:
                    # 이전 PING 이후 pong 또는 데이터가 한 건도 없었다. 새 PING을
                    # 보내기 전에 실패 횟수를 올려, 약 interval × threshold 동안
                    # 무응답인 연결만 half-open으로 판정한다.
                    if keepalive_pending:
                        missed_keepalives += 1
                        if missed_keepalives >= self._max_missed_keepalives:
                            raise _StaleConnection
                    # 명세: 클라이언트로부터의 수신이 180초 없으면 서버가 끊는다.
                    # 서버가 보내는 데이터는 그 타이머를 리셋하지 않는다.
                    await connection.send(protocol.PING_TEXT)
                    keepalive_pending = True
                    next_ping = now + self._keepalive_interval_seconds

                try:
                    raw = await asyncio.wait_for(
                        connection.recv(), timeout=self._recv_poll_seconds
                    )
                except TimeoutError:
                    continue
                # pong뿐 아니라 임의의 상향 프레임도 peer가 살아 있다는 증거다.
                # 파싱할 수 없는 프레임이라도 TCP 왕복은 확인되었으므로 reset한다.
                keepalive_pending = False
                missed_keepalives = 0

                frame = protocol.parse_inbound(raw)
                if frame is None or isinstance(frame, protocol.PongFrame):
                    continue
                if isinstance(frame, protocol.SubscriptionsAck):
                    self._apply_ack(frame)
                    continue
                if isinstance(frame, protocol.UpstreamError):
                    outcome = self._apply_error(frame)
                    if outcome == "reconnect":
                        # `server-shutdown` 은 프레임 직후 연결이 닫힌다. 정상적인
                        # 배포 신호이므로 실패로 세지 않고 즉시 재연결한다.
                        return True
                    if outcome == "redeclare":
                        # `too-many-topics`가 연속으로 와도 선언 빈도 5회/초를
                        # 태우지 않는다. 기존 재조정 주기 또는 명세상 안전한 최소
                        # 간격(0.5초) 중 긴 쪽을 반드시 기다린다.
                        next_reconcile = self._clock() + max(
                            self._reconcile_interval_seconds,
                            protocol.DECLARE_MIN_INTERVAL_SECONDS,
                        )
                    elif outcome == "throttle":
                        declare_blocked_until = (
                            self._clock() + protocol.RATE_LIMIT_BACKOFF_SECONDS
                        )
                    continue

                produced = True
                await self._publish_tick(frame)
        return produced

    async def reconcile(self, connection: UpstreamConnection) -> None:
        """수요를 읽어 예산을 배분하고, 달라졌으면 구독 전체를 다시 선언한다."""

        now = self._clock()
        if self._inflight is not None:
            _, _, sent_at = self._inflight
            if now - sent_at < DECLARE_ACK_TIMEOUT_SECONDS:
                # 응답을 기다리는 중이다. 겹쳐 보내면 선언 빈도만 태운다.
                return
            logger.warning("toss stream declare ack timed out: redeclaring")
            self._inflight = None

        demand = await self._bus.aggregate_demand()
        budget = self._budget(demand)
        desired = frozenset(budget.streaming)
        if desired == self._confirmed:
            await self._publish_state(
                live=True,
                reason=None,
                streaming=budget.streaming,
                demoted=budget.demoted,
            )
            return

        self._declare_sequence += 1
        request_id = f"{self._bus.instance_id}:{self._declare_sequence}"
        upstream = sorted(parse_topic(key).upstream_key for key in budget.streaming)
        await connection.send(protocol.declare_frame(upstream, request_id=request_id))
        self._inflight = (request_id, desired, now)

    def _budget(self, demand: Mapping[str, int]) -> TopicBudget:
        now = self._clock()
        for key in [key for key, until in self._rejected.items() if until <= now]:
            self._rejected.pop(key, None)
        return allocate_topics(
            demand, capacity=self._capacity, blocked=tuple(self._rejected)
        )

    def _apply_ack(self, ack: protocol.SubscriptionsAck) -> None:
        """ack 를 확정 상태로 반영하고 거부분을 차단 목록에 넣는다.

        명세: 원인을 고치지 않은 재선언은 같은 이유로 다시 거부된다. 그대로 두면
        매 주기마다 같은 거부를 받으며 선언 빈도만 소모한다. 차단된 토픽은 다음
        재조정에서 선언 목록에서 빠지고 강등 목록으로 나가므로, 앱은 그 종목만
        REST 폴링으로 값을 받는다.
        """

        if ack.rejected:
            until = self._clock() + REJECT_COOLDOWN_SECONDS
            for item in ack.rejected:
                key = _downstream_key(item.target)
                if key is None:
                    continue
                self._rejected[key] = until
                logger.info(
                    "toss stream topic rejected target=%s code=%s",
                    item.target,
                    item.code,
                )

        confirmed = {
            key
            for key in (_downstream_key(target) for target in ack.subscribed)
            if key is not None
        }
        if self._inflight is not None and (
            ack.request_id is None or ack.request_id == self._inflight[0]
        ):
            self._inflight = None
        self._confirmed = frozenset(confirmed)

    def _apply_error(self, error: protocol.UpstreamError) -> str:
        """선언 전체 실패를 처리한다. 서버 쪽 구독은 직전 선언 그대로 유지된다."""

        action = protocol.classify_error(error.code)
        if self._inflight is not None and (
            error.request_id is None or error.request_id == self._inflight[0]
        ):
            # 이 선언은 반영되지 않았다. 확정 집합은 건드리지 않는다.
            self._inflight = None
        if action == "fatal":
            raise _FatalUpstream(f"PROTOCOL_{(error.code or 'unknown').upper()}")
        if action in {"blocked", "connection-limit"}:
            raise _Blocked(error.code or "unknown")
        if action == "reconnect":
            logger.info("toss stream upstream server-shutdown: reconnecting")
            return "reconnect"
        if action == "capacity":
            self._shrink_capacity()
            return "redeclare"
        if action == "throttle":
            logger.info("toss stream declare rate limited: backing off ~1s")
            return "throttle"
        logger.warning("toss stream upstream error code=%s", error.code or "unknown")
        return "retry"

    def _shrink_capacity(self) -> None:
        """`too-many-topics`. 확정된 구독 수보다 작게 줄이고 다시 선언한다."""

        ceiling = len(self._confirmed) if self._confirmed else self._capacity
        reduced = max(1, min(self._capacity, ceiling) - 1)
        if reduced < self._capacity:
            logger.warning(
                "toss stream topic capacity reduced %d -> %d", self._capacity, reduced
            )
        self._capacity = reduced

    # --- 팬아웃 --------------------------------------------------------------

    async def _publish_tick(self, frame: Any) -> None:
        if isinstance(frame, protocol.TradeFrame):
            payload: dict[str, Any] = {
                "kind": bus_module.EVENT_TICK,
                "topic": frame.topic.key,
                "trade": {
                    "price": str(frame.price),
                    "volume": str(frame.volume),
                    "currency": frame.currency,
                    "asOf": frame.as_of.isoformat(),
                },
            }
        elif isinstance(frame, protocol.OrderbookFrame):
            payload = {
                "kind": bus_module.EVENT_TICK,
                "topic": frame.topic.key,
                "orderbook": {
                    "currency": frame.currency,
                    "asOf": frame.as_of.isoformat() if frame.as_of else None,
                    "asks": [
                        [str(level.price), str(level.volume)] for level in frame.asks
                    ],
                    "bids": [
                        [str(level.price), str(level.volume)] for level in frame.bids
                    ],
                },
            }
        else:  # pragma: no cover — 위 두 분기가 전부다
            return
        await self._bus.publish_event(payload)

    async def _publish_state(
        self,
        *,
        live: bool,
        reason: str | None,
        streaming: tuple[str, ...] = (),
        demoted: tuple[str, ...] = (),
    ) -> None:
        state = {
            "kind": bus_module.EVENT_STATE,
            "live": live,
            "reason": reason,
            "streaming": list(streaming),
            "demoted": list(demoted),
            # 공급자가 거부한 토픽. 프로세스들이 강등 사유를 "예산 초과"와
            # "공급자 거부"로 구분해 앱에 알릴 수 있게 따로 실어 보낸다.
            "rejected": sorted(self._rejected),
            "ownerId": self._bus.instance_id,
        }
        await self._bus.store_state(state)
        await self._bus.publish_event(state)

    # --- 유계 백오프 ----------------------------------------------------------

    def _backoff(self, failures: int) -> float:
        """1s → 2s → 4s ... 상한 `backoff_max_seconds`. jitter 포함, 항상 유계다."""

        exponent = min(max(failures, 1) - 1, 16)
        base = min(
            self._backoff_base_seconds * (2**exponent), self._backoff_max_seconds
        )
        return min(base + base * self._jitter() * 0.5, self._backoff_max_seconds)

    async def _wait(self, seconds: float) -> None:
        """중단 신호를 기다리며 쉰다. 종료가 지연되지 않게 하는 것이 목적이다."""

        delay = max(seconds, 0.0)
        if delay <= 0.0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)


def _downstream_key(upstream_target: str) -> str | None:
    topic = parse_upstream_key(upstream_target)
    return topic.key if topic is not None else None


@contextlib.asynccontextmanager
async def connect_toss_stream(token: str) -> AsyncIterator[UpstreamConnection]:
    """운영 커넥터. handshake 헤더로만 인증한다(명세: 인증은 handshake 1회).

    handshake 실패는 HTTP 상태로 오므로 여기서 `UpstreamHandshakeError` 로 바꿔
    상위 루프가 401(토큰 재발급) / 403(허용 IP) / 503(백오프)을 구분하게 한다.
    """

    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    try:
        async with connect(
            protocol.TOSS_STREAM_URL,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=10,
            close_timeout=5,
            # keepalive 는 명세의 텍스트 `PING` 으로 직접 보낸다. 표준 ping 과
            # 이중으로 돌리면 유휴 판정 기준이 두 개가 된다.
            ping_interval=None,
        ) as connection:
            yield connection
    except InvalidStatus as exc:
        raise UpstreamHandshakeError(exc.response.status_code) from exc
