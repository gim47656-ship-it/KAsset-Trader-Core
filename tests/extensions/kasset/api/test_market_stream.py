"""실시간 시세 스트림 계약 테스트.

실제 네트워킹은 쓰지 않는다. 상향은 스크립트 커넥션, 팬아웃은 인메모리 버스,
Redis 조정 계층만 `fakeredis`로 확인한다.

지키려는 계약은 다음이다.

* 토스 와이어 형태(선언형 full-replace, 텍스트 `PING`, 수신 프레임 디스패치)
* 토픽 예산 100건 초과 시 조용한 누락이 아니라 폴링 강등
* 구독자 0이 되면 상향 구독이 실제로 해제된다
* 재연결 백오프가 유계이며 냉각으로 끝난다
* 느린 클라이언트는 오래된 시세를 받지 않는다
* 인증 실패는 연결이 거부된다
* 스트림 시세 페이로드가 REST `Quote`와 같은 모델이다
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.paths import (
    is_android_compat_path,
    is_kasset_token_allowed_path,
)
from app.extensions.kasset.api.stream import contract, route
from app.extensions.kasset.api.stream import toss_protocol as protocol
from app.extensions.kasset.api.stream.bus import OWNER_KEY, StreamBus
from app.extensions.kasset.api.stream.runtime import (
    MarketStreamRuntime,
    get_stream_runtime,
)
from app.extensions.kasset.api.stream.session import SlowConsumer, StreamSession
from app.extensions.kasset.api.stream.topics import (
    MAX_UPSTREAM_TOPICS,
    REJECT_BAD_SYMBOL,
    REJECT_TOO_MANY,
    allocate_topics,
    parse_topic,
    parse_upstream_key,
)
from app.extensions.kasset.api.stream.upstream import TossUpstreamOwner

# --------------------------------------------------------------------------- #
# 테스트 대역
# --------------------------------------------------------------------------- #


class _FakeBus:
    """`StreamBus`와 같은 표면을 가진 인메모리 대역."""

    def __init__(self, *, instance_id: str = "test-instance") -> None:
        self.instance_id = instance_id
        self.lease_available = True
        self.aggregate: dict[str, int] = {}
        self.demand_published: list[dict[str, int]] = []
        self.state: dict[str, Any] | None = None
        self.released = 0
        # `TestClient` 는 앱을 별도 스레드의 루프에서 돌린다. `deque` 의
        # append/popleft 는 원자적이라 테스트 스레드에서 직접 밀어 넣을 수 있다.
        self.events: deque[dict[str, Any]] = deque()

    async def acquire_owner_lease(self) -> bool:
        return self.lease_available

    async def renew_owner_lease(self) -> bool:
        return self.lease_available

    async def release_owner_lease(self) -> None:
        self.released += 1

    async def publish_demand(self, topics) -> None:
        self.demand_published.append(dict(topics))
        self.aggregate = dict(topics)

    async def aggregate_demand(self) -> dict[str, int]:
        return dict(self.aggregate)

    async def publish_event(self, event) -> None:
        self.events.append(dict(event))

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self.events:
                yield self.events.popleft()
            else:
                await asyncio.sleep(0.005)

    async def store_state(self, state) -> None:
        self.state = dict(state)

    async def read_state(self) -> dict[str, Any] | None:
        return dict(self.state) if self.state is not None else None


class _ScriptedConnection:
    """보낸 프레임을 기록하고, 테스트가 밀어 넣은 프레임만 돌려주는 연결."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbound: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return await self._inbound.get()

    def push(self, payload: Any) -> None:
        self._inbound.put_nowait(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def declares(self) -> list[list[dict[str, Any]]]:
        return [json.loads(frame) for frame in self.sent if frame.startswith("[")]

    def declared_codes(self, index: int) -> set[str]:
        keys: set[str] = set()
        for element in self.declares()[index]:
            declare_type = element.get("type")
            if declare_type is None:
                continue
            for code in element["codes"]:
                keys.add(f"{declare_type}:{code}")
        return keys


def _owner(
    bus: _FakeBus,
    connection: _ScriptedConnection,
    **overrides: Any,
) -> TossUpstreamOwner:
    @contextlib.asynccontextmanager
    async def connector(_token: str) -> AsyncIterator[_ScriptedConnection]:
        yield connection

    async def token_provider(*, force_reissue: bool = False, failed_token=None) -> str:
        return "test-token"

    kwargs: dict[str, Any] = {
        "reconcile_interval_seconds": 0.0,
        "recv_poll_seconds": 0.01,
        "renew_interval_seconds": 1000.0,
        "keepalive_interval_seconds": 1000.0,
        "candidate_poll_seconds": 0.01,
    }
    kwargs.update(overrides)
    return TossUpstreamOwner(
        bus=bus, connector=connector, token_provider=token_provider, **kwargs
    )


async def _await_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


@contextlib.asynccontextmanager
async def _running(owner: TossUpstreamOwner) -> AsyncIterator[None]:
    task = asyncio.create_task(owner.run())
    try:
        yield
    finally:
        owner.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _baseline_quote(symbol: str = "TQQQ", market: str = "US") -> Quote:
    return krx_quotes.build_quote(
        market=market,
        symbol=symbol,
        name="ProShares UltraPro QQQ",
        currency="USD" if market == "US" else "KRW",
        price=Decimal("73.00"),
        previous_close=Decimal("72.00"),
        as_of=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        source=krx_quotes.TOSS_QUOTE_SOURCE,
    )


# --------------------------------------------------------------------------- #
# 토스 와이어 계약
# --------------------------------------------------------------------------- #


def test_declare_frame_matches_spec_shape() -> None:
    """명세 공식 예시와 같은 모양이어야 한다: 배열 1개 + id + type/codes."""

    frame = declare = protocol.declare_frame(
        ["trade:us:TSLA", "trade:us:AAPL", "trade:kr:005930"],
        request_id="req-1",
    )
    assert isinstance(frame, str)
    assert json.loads(declare) == [
        {"id": "req-1"},
        {"type": "trade:kr", "codes": ["005930"]},
        {"type": "trade:us", "codes": ["AAPL", "TSLA"]},
    ]


def test_declare_frame_release_all_is_an_empty_array() -> None:
    """전체 해제는 빈 배열이다. `unsubscribe` 액션은 존재하지 않는다."""

    assert json.loads(protocol.declare_frame([], request_id=None)) == []


def test_keepalive_is_plain_uppercase_text_not_json() -> None:
    assert protocol.PING_TEXT == "PING"
    # 서버 idle 한도 180초보다 확실히 짧아야 한다.
    assert protocol.KEEPALIVE_INTERVAL_SECONDS < protocol.IDLE_LIMIT_SECONDS


def test_parse_inbound_reads_spec_trade_frame() -> None:
    frame = protocol.parse_inbound(
        json.dumps(
            {
                "type": "message",
                "topic": "trade:us:AAPL",
                "data": {
                    "price": "243.26",
                    "volume": "8",
                    "timestamp": "2026-06-18T23:30:00.000+09:00",
                    "currency": "USD",
                },
            }
        )
    )
    assert isinstance(frame, protocol.TradeFrame)
    assert frame.topic.key == "quote:US:AAPL"
    assert frame.price == Decimal("243.26")
    assert frame.volume == Decimal("8")
    assert frame.as_of == datetime(2026, 6, 18, 14, 30, tzinfo=UTC)


def test_parse_inbound_reads_spec_orderbook_frame_with_null_timestamp() -> None:
    frame = protocol.parse_inbound(
        json.dumps(
            {
                "type": "message",
                "topic": "orderbook:kr:005930",
                "data": {
                    "timestamp": None,
                    "currency": "KRW",
                    "asks": [{"price": "72100", "volume": "8500"}],
                    "bids": [{"price": "72000", "volume": "1200"}],
                },
            }
        )
    )
    assert isinstance(frame, protocol.OrderbookFrame)
    # `timestamp` null 은 정상 입력이다. 서버 시각으로 위조하지 않는다.
    assert frame.as_of is None
    assert frame.asks[0].price == Decimal("72100")


def test_parse_inbound_tolerates_unknown_currency_enum() -> None:
    frame = protocol.parse_inbound(
        json.dumps(
            {
                "type": "message",
                "topic": "trade:us:AAPL",
                "data": {
                    "price": "1",
                    "volume": "1",
                    "timestamp": "2026-06-18T23:30:00+09:00",
                    "currency": "JPY",
                },
            }
        )
    )
    assert isinstance(frame, protocol.TradeFrame)
    assert frame.currency == "JPY"


def test_parse_inbound_reads_partial_reject_ack() -> None:
    frame = protocol.parse_inbound(
        json.dumps(
            {
                "type": "subscriptions",
                "id": "req-2",
                "subscribed": ["trade:kr:005930"],
                "rejected": [
                    {
                        "target": "trade:kr:999999",
                        "code": "stock-not-found",
                        "message": "해당 종목을 찾을 수 없습니다.",
                    }
                ],
            }
        )
    )
    assert isinstance(frame, protocol.SubscriptionsAck)
    assert frame.request_id == "req-2"
    assert frame.subscribed == ("trade:kr:005930",)
    assert frame.rejected[0].code == "stock-not-found"


def test_parse_inbound_ignores_channels_this_module_does_not_own() -> None:
    assert parse_upstream_key("personal:order:3") is None
    assert (
        protocol.parse_inbound(
            json.dumps(
                {"type": "message", "topic": "personal:order:3", "data": {"a": 1}}
            )
        )
        is None
    )


def test_error_codes_are_classified_by_required_response() -> None:
    assert protocol.classify_error("wrong-format") == "fatal"
    assert protocol.classify_error("invalid-type") == "fatal"
    assert protocol.classify_error("no-codes") == "fatal"
    assert protocol.classify_error("too-many-topics") == "capacity"
    assert protocol.classify_error("rate-limit-exceeded") == "throttle"
    assert protocol.classify_error("server-shutdown") == "reconnect"
    assert protocol.classify_error("too-many") == "connection-limit"
    assert protocol.classify_error("edge-blocked") == "blocked"
    assert protocol.classify_error("internal-error") == "retry"


# --------------------------------------------------------------------------- #
# 토픽 예산
# --------------------------------------------------------------------------- #


def test_topic_budget_caps_at_the_provider_limit_and_demotes_the_rest() -> None:
    """100건을 넘기면 조용히 누락시키지 않고 강등 목록으로 내보낸다."""

    demand = {f"quote:US:S{index:03d}": 1 for index in range(120)}
    demand["quote:US:TQQQ"] = 50

    budget = allocate_topics(demand)

    assert len(budget.streaming) == MAX_UPSTREAM_TOPICS
    assert "quote:US:TQQQ" in budget.streaming
    # 잃어버리는 토픽이 없다. 전량이 streaming + demoted 로 보존된다.
    assert len(budget.demoted) == len(demand) - MAX_UPSTREAM_TOPICS
    assert set(budget.streaming).isdisjoint(budget.demoted)
    assert set(budget.streaming) | set(budget.demoted) == set(demand)


def test_topic_budget_prefers_symbols_with_more_subscribers() -> None:
    demand = {
        "quote:US:AAA": 1,
        "quote:US:BBB": 9,
        "quote:US:CCC": 5,
    }
    budget = allocate_topics(demand, capacity=2)
    assert budget.streaming == ("quote:US:BBB", "quote:US:CCC")
    assert budget.demoted == ("quote:US:AAA",)


def test_topic_budget_drops_zero_subscriber_topics_entirely() -> None:
    budget = allocate_topics({"quote:US:AAA": 0, "quote:US:BBB": 1})
    assert budget.streaming == ("quote:US:BBB",)
    assert budget.demoted == ()


def test_topic_budget_demotes_provider_rejected_topics() -> None:
    budget = allocate_topics(
        {"quote:US:AAA": 3, "quote:US:BBB": 1}, blocked=("quote:US:AAA",)
    )
    assert budget.streaming == ("quote:US:BBB",)
    assert budget.demoted == ("quote:US:AAA",)


def test_topic_keys_map_one_to_one_with_upstream_keys() -> None:
    assert parse_topic("quote:US:TQQQ").upstream_key == "trade:us:TQQQ"
    assert parse_topic("orderbook:KRX:005930").upstream_key == "orderbook:kr:005930"
    assert parse_upstream_key("trade:kr:005930").key == "quote:KRX:005930"


# --------------------------------------------------------------------------- #
# 상향 소유자
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upstream_releases_subscription_when_subscriber_count_hits_zero() -> None:
    """구독자가 0이 되면 그 토픽을 뺀 새 배열을 선언한다(= 해제)."""

    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {"quote:US:TQQQ": 1, "orderbook:KRX:005930": 2}
    owner = _owner(bus, connection)

    async with _running(owner):
        await _await_until(lambda: len(connection.declares()) >= 1)
        assert connection.declared_codes(0) == {
            "trade:us:TQQQ",
            "orderbook:kr:005930",
        }
        connection.push(
            {
                "type": "subscriptions",
                "id": json.loads(connection.sent[0])[0]["id"],
                "subscribed": ["trade:us:TQQQ", "orderbook:kr:005930"],
                "rejected": [],
            }
        )
        await _await_until(
            lambda: (
                owner.confirmed_topics
                == frozenset({"quote:US:TQQQ", "orderbook:KRX:005930"})
            )
        )

        # 마지막 구독자가 화면을 떠났다.
        bus.aggregate = {}
        await _await_until(lambda: len(connection.declares()) >= 2)
        assert connection.declares()[-1] == [
            {"id": json.loads(connection.sent[-1])[0]["id"]}
        ]


@pytest.mark.asyncio
async def test_upstream_stops_redeclaring_provider_rejected_topics() -> None:
    """거부된 항목을 다시 선언하지 않고, 강등 목록으로 내보낸다."""

    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {"quote:US:TQQQ": 1, "quote:US:NOPE": 1}
    owner = _owner(bus, connection)

    async with _running(owner):
        await _await_until(lambda: len(connection.declares()) >= 1)
        request_id = json.loads(connection.sent[0])[0]["id"]
        connection.push(
            {
                "type": "subscriptions",
                "id": request_id,
                "subscribed": ["trade:us:TQQQ"],
                "rejected": [
                    {
                        "target": "trade:us:NOPE",
                        "code": "stock-not-found",
                        "message": "없음",
                    }
                ],
            }
        )
        await _await_until(lambda: "quote:US:NOPE" in owner.rejected_topics)
        await _await_until(
            lambda: bus.state is not None and bus.state["rejected"] == ["quote:US:NOPE"]
        )

        # 확정 집합이 이미 원하는 집합과 같으므로 추가 선언이 나가지 않는다.
        # 거부분을 계속 재선언하며 선언 빈도(5회/초)를 소모하지 않는다는 뜻이다.
        await asyncio.sleep(0.05)
        assert len(connection.declares()) == 1
        assert owner.confirmed_topics == frozenset({"quote:US:TQQQ"})
        assert bus.state is not None
        # 강등 목록으로 나가므로 앱은 그 종목만 REST 폴링으로 값을 받는다.
        assert "quote:US:NOPE" in bus.state["demoted"]
        assert bus.state["streaming"] == ["quote:US:TQQQ"]


@pytest.mark.asyncio
async def test_too_many_topics_shrinks_capacity_and_redeclares() -> None:
    """예산 초과 응답을 받으면 예산을 줄여 다시 선언한다(연결은 유지)."""

    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {
        "quote:US:AAA": 3,
        "quote:US:BBB": 2,
        "quote:US:CCC": 1,
    }
    owner = _owner(bus, connection, capacity=3)

    async with _running(owner):
        await _await_until(lambda: len(connection.declares()) >= 1)
        connection.push(
            {
                "type": "subscriptions",
                "id": json.loads(connection.sent[0])[0]["id"],
                "subscribed": ["trade:us:AAA", "trade:us:BBB", "trade:us:CCC"],
                "rejected": [],
            }
        )
        await _await_until(lambda: len(owner.confirmed_topics) == 3)
        connection.push(
            {
                "type": "error",
                "error": {"code": "too-many-topics", "message": "too many"},
            }
        )
        await _await_until(lambda: owner.capacity == 2)
        # 용량을 줄였더라도 즉시 연속 선언하지 않는다. full-replace 선언이 계속
        # 거부되는 조건에서 공급자 한도(5회/초)를 태우는 회귀를 막는다.
        await asyncio.sleep(protocol.DECLARE_MIN_INTERVAL_SECONDS / 2)
        assert len(connection.declares()) == 1
        await _await_until(lambda: len(connection.declares()) >= 2)
        # 구독자가 가장 적은 종목이 빠진다.
        assert connection.declared_codes(-1) == {"trade:us:AAA", "trade:us:BBB"}


@pytest.mark.asyncio
async def test_declare_rate_limit_does_not_drop_the_connection() -> None:
    """`rate-limit-exceeded`는 선언만 실패한다. 기존 구독과 연결을 유지한다."""

    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {"quote:US:TQQQ": 1}
    owner = _owner(bus, connection)

    async with _running(owner):
        await _await_until(lambda: len(connection.declares()) >= 1)
        connection.push(
            {
                "type": "error",
                "error": {"code": "rate-limit-exceeded", "message": "slow down"},
            }
        )
        connection.push(
            {
                "type": "message",
                "topic": "trade:us:TQQQ",
                "data": {
                    "price": "73.5",
                    "volume": "3",
                    "timestamp": "2026-08-28T12:00:00+00:00",
                    "currency": "USD",
                },
            }
        )
        # 연결이 유지되었으므로 틱이 팬아웃된다.
        await _await_until(lambda: owner.connect_attempts == 1)
        assert owner.cooldown_delays == []


@pytest.mark.asyncio
async def test_reconnect_backoff_is_bounded_and_ends_in_a_cooldown() -> None:
    """무한 즉시 재시도가 없어야 한다: 유계 백오프 후 냉각으로 내려간다."""

    bus = _FakeBus()

    @contextlib.asynccontextmanager
    async def failing_connector(_token: str):
        raise ConnectionError("upstream down")
        yield  # pragma: no cover — 위에서 항상 끊긴다

    async def token_provider(*, force_reissue: bool = False, failed_token=None) -> str:
        return "token"

    owner = TossUpstreamOwner(
        bus=bus,
        connector=failing_connector,
        token_provider=token_provider,
        max_consecutive_failures=4,
        cooldown_seconds=30.0,
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.004,
        candidate_poll_seconds=0.001,
    )

    task = asyncio.create_task(owner.run())
    try:
        await _await_until(lambda: bool(owner.cooldown_delays))
    finally:
        owner.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # 연결 시도가 유계다: 냉각 전까지 정확히 상한(4)만큼만 시도한다.
    assert owner.connect_attempts == 4
    # 연속 실패 상한(4)에 닿기까지 백오프는 3회다. 4번째에서 냉각으로 내려간다.
    assert len(owner.backoff_delays) == 3
    assert all(delay <= 0.004 for delay in owner.backoff_delays)
    assert owner.backoff_delays == sorted(owner.backoff_delays)
    assert owner.cooldown_delays == [30.0]


@pytest.mark.asyncio
async def test_fatal_protocol_error_stops_the_owner_instead_of_looping() -> None:
    """우리 프레임이 틀린 경우는 재시도해도 같다. 루프를 만들지 않는다."""

    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {"quote:US:TQQQ": 1}
    owner = _owner(bus, connection, backoff_base_seconds=0.001)

    task = asyncio.create_task(owner.run())
    try:
        await _await_until(lambda: len(connection.declares()) >= 1)
        connection.push(
            {
                "type": "error",
                "error": {"code": "invalid-type", "message": "unknown type"},
            }
        )
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        owner.stop()

    assert owner.connect_attempts == 1
    assert bus.state is not None
    assert bus.state["live"] is False
    assert bus.state["reason"] == "PROTOCOL_INVALID-TYPE"


@pytest.mark.asyncio
async def test_server_shutdown_reconnects_without_counting_as_a_failure() -> None:
    bus = _FakeBus()
    connection = _ScriptedConnection()
    bus.aggregate = {"quote:US:TQQQ": 1}
    owner = _owner(
        bus, connection, backoff_base_seconds=0.001, backoff_max_seconds=0.002
    )

    async with _running(owner):
        await _await_until(lambda: owner.connect_attempts >= 1)
        connection.push(
            {
                "type": "error",
                "error": {"code": "server-shutdown", "message": "재연결해주세요."},
            }
        )
        await _await_until(lambda: owner.connect_attempts >= 2)

    assert owner.cooldown_delays == []


@pytest.mark.asyncio
async def test_owner_never_connects_without_the_lease() -> None:
    """리스를 못 잡은 프로세스는 상향 연결을 만들지 않는다(계정 연결 상한 보호)."""

    bus = _FakeBus()
    bus.lease_available = False
    connection = _ScriptedConnection()
    owner = _owner(bus, connection)

    async with _running(owner):
        await asyncio.sleep(0.05)

    assert owner.connect_attempts == 0
    assert connection.sent == []


@pytest.mark.asyncio
async def test_owner_sends_plain_text_ping_for_keepalive() -> None:
    bus = _FakeBus()
    connection = _ScriptedConnection()
    owner = _owner(bus, connection, keepalive_interval_seconds=0.0)

    async with _running(owner):
        await _await_until(lambda: protocol.PING_TEXT in connection.sent)

    assert "PING" in connection.sent


@pytest.mark.asyncio
async def test_unanswered_keepalives_degrade_and_reconnect_the_upstream() -> None:
    """half-open 연결이 리스를 쥔 채 시세를 무기한 멈추면 안 된다."""

    bus = _FakeBus()
    bus.aggregate = {"quote:US:TQQQ": 1}
    connection = _ScriptedConnection()
    owner = _owner(
        bus,
        connection,
        keepalive_interval_seconds=0.01,
        max_missed_keepalives=2,
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.001,
    )

    async with _running(owner):
        # 첫 연결은 PING 2회에 아무 상향 프레임도 받지 못해 죽은 것으로
        # 판정된다. 리스를 놓고 백오프한 뒤 두 번째 연결을 연다.
        await _await_until(lambda: owner.connect_attempts >= 2)
        assert connection.sent.count(protocol.PING_TEXT) >= 2
        assert bus.state is not None
        assert bus.state["live"] is False
        assert bus.state["reason"] == "UPSTREAM_UNAVAILABLE"
        assert bus.released >= 1


@pytest.mark.asyncio
async def test_pong_resets_the_missed_keepalive_counter() -> None:
    """매 PING에 pong이 오면 같은 연결과 리스를 계속 유지한다."""

    bus = _FakeBus()
    bus.aggregate = {"quote:US:TQQQ": 1}
    connection = _ScriptedConnection()
    owner = _owner(
        bus,
        connection,
        keepalive_interval_seconds=0.01,
        max_missed_keepalives=2,
    )

    async with _running(owner):
        for expected in range(1, 6):
            await _await_until(
                lambda expected=expected: (
                    connection.sent.count(protocol.PING_TEXT) >= expected
                )
            )
            connection.push({"type": "pong"})
        await asyncio.sleep(0.005)
        assert owner.connect_attempts == 1


# --------------------------------------------------------------------------- #
# Redis 조정 계층
# --------------------------------------------------------------------------- #


class _PauseBeforeExecutePipeline:
    """WATCH 뒤 경쟁자가 키를 바꿀 때까지 EXEC를 멈추는 실제 pipeline 래퍼."""

    def __init__(
        self,
        inner: Any,
        *,
        execute_started: asyncio.Event,
        allow_execute: asyncio.Event,
    ) -> None:
        self._inner = inner
        self._execute_started = execute_started
        self._allow_execute = allow_execute

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *args: object):
        return await self._inner.__aexit__(*args)

    async def watch(self, key: str) -> None:
        await self._inner.watch(key)

    async def get(self, key: str):
        return await self._inner.get(key)

    async def unwatch(self) -> None:
        await self._inner.unwatch()

    def multi(self) -> None:
        self._inner.multi()

    def pexpire(self, key: str, milliseconds: int) -> None:
        self._inner.pexpire(key, milliseconds)

    def delete(self, key: str) -> None:
        self._inner.delete(key)

    async def execute(self):
        self._execute_started.set()
        await self._allow_execute.wait()
        return await self._inner.execute()


class _PausedPipelineRedis:
    """Redis 명령은 실제 fakeredis, pipeline EXEC 시점만 테스트가 제어한다."""

    def __init__(
        self,
        client: Any,
        *,
        execute_started: asyncio.Event,
        allow_execute: asyncio.Event,
    ) -> None:
        self._client = client
        self._execute_started = execute_started
        self._allow_execute = allow_execute

    def pipeline(self):
        return _PauseBeforeExecutePipeline(
            self._client.pipeline(),
            execute_started=self._execute_started,
            allow_execute=self._allow_execute,
        )


@pytest.mark.asyncio
async def test_only_one_process_can_hold_the_upstream_lease() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = StreamBus(redis_client=client, instance_id="proc-a")
    second = StreamBus(redis_client=client, instance_id="proc-b")

    assert await first.acquire_owner_lease() is True
    assert await second.acquire_owner_lease() is False
    # 소유자는 반복 호출로 갱신된다.
    assert await first.acquire_owner_lease() is True
    # 남의 리스는 갱신할 수 없다.
    assert await second.renew_owner_lease() is False

    await first.release_owner_lease()
    assert await second.acquire_owner_lease() is True


@pytest.mark.asyncio
async def test_lease_renewal_loses_a_real_watch_multi_race() -> None:
    """WATCH와 EXEC 사이 소유권이 바뀌면 WatchError를 False로 닫아야 한다."""

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await client.set(OWNER_KEY, "proc-a", px=15_000)
    execute_started = asyncio.Event()
    allow_execute = asyncio.Event()
    paused_client = _PausedPipelineRedis(
        client,
        execute_started=execute_started,
        allow_execute=allow_execute,
    )
    owner = StreamBus(  # type: ignore[arg-type]
        redis_client=paused_client, instance_id="proc-a"
    )

    renewal = asyncio.create_task(owner.renew_owner_lease())
    await asyncio.wait_for(execute_started.wait(), timeout=0.5)

    # WATCH가 잡힌 뒤 다른 프로세스가 소유권을 가져간다. 실제 fakeredis
    # transaction은 EXEC에서 WatchError를 내고, StreamBus가 그 분기를 닫는다.
    await client.set(OWNER_KEY, "proc-b", px=15_000)
    allow_execute.set()

    assert await asyncio.wait_for(renewal, timeout=0.5) is False
    assert await client.get(OWNER_KEY) == "proc-b"


@pytest.mark.asyncio
async def test_demand_is_summed_across_processes_and_prunes_dead_ones() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = StreamBus(redis_client=client, instance_id="proc-a")
    second = StreamBus(
        redis_client=client, instance_id="proc-b", demand_ttl_seconds=-1.0
    )

    await first.publish_demand({"quote:US:TQQQ": 2})
    await second.publish_demand({"quote:US:TQQQ": 5, "quote:US:AAPL": 1})

    # `proc-b` 의 필드는 이미 만료되었으므로 합계에 들어가지 않는다.
    assert await first.aggregate_demand() == {"quote:US:TQQQ": 2}

    await first.publish_demand({})
    assert await first.aggregate_demand() == {}


# --------------------------------------------------------------------------- #
# 클라이언트 세션 (백압 / conflation)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_slow_client_receives_only_the_latest_quote_per_topic() -> None:
    """시세는 누적 가치가 없다. 밀린 클라이언트에게 오래된 값을 보내지 않는다."""

    sent: list[str] = []
    gate = asyncio.Event()

    async def send(message: str) -> None:
        await gate.wait()
        sent.append(message)

    session = StreamSession(send=send)
    session.declare(["quote:US:TQQQ", "quote:KRX:005930"])

    session.offer("quote:US:TQQQ", "tqqq-73.00")
    session.offer("quote:US:TQQQ", "tqqq-73.20")
    session.offer("quote:US:TQQQ", "tqqq-73.45")
    session.offer("quote:KRX:005930", "samsung-72000")

    gate.set()
    await session.flush()

    assert sent == ["tqqq-73.45", "samsung-72000"]
    assert session.conflated == 2


@pytest.mark.asyncio
async def test_pending_messages_never_exceed_the_subscription_count() -> None:
    """대기 큐가 무한히 자라지 않는다는 것이 conflation 의 핵심 성질이다."""

    session = StreamSession(send=_never_returns)
    session.declare(["quote:US:TQQQ"])
    for index in range(1000):
        session.offer("quote:US:TQQQ", f"tick-{index}")

    assert session.conflated == 999
    assert len(session._pending) == 1  # noqa: SLF001 — 상한이 이 테스트의 계약이다


@pytest.mark.asyncio
async def test_stalled_client_is_reported_as_a_slow_consumer() -> None:
    session = StreamSession(send=_never_returns, send_timeout=0.01)
    session.declare(["quote:US:TQQQ"])
    session.offer("quote:US:TQQQ", "tick")

    with pytest.raises(SlowConsumer):
        await session.flush()


@pytest.mark.asyncio
async def test_control_messages_are_bounded_and_keep_order() -> None:
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    session = StreamSession(send=send, control_limit=2)
    session.push_control("a")
    session.push_control("b")
    session.push_control("c")
    await session.flush()

    assert sent == ["b", "c"]


def test_client_declaration_is_full_replace() -> None:
    """화면을 나가면 새 선언만으로 구독이 해제된다."""

    session = StreamSession(send=_never_returns)
    accepted, rejected, released = session.declare(
        ["quote:US:TQQQ", "orderbook:US:TQQQ"]
    )
    assert accepted == ("orderbook:US:TQQQ", "quote:US:TQQQ")
    assert rejected == ()
    assert released == frozenset()

    accepted, _, released = session.declare(["quote:US:TQQQ"])
    assert accepted == ("quote:US:TQQQ",)
    assert released == frozenset({"orderbook:US:TQQQ"})

    accepted, _, released = session.declare([])
    assert accepted == ()
    assert released == frozenset({"quote:US:TQQQ"})


def test_client_topic_cap_rejects_the_overflow_only() -> None:
    session = StreamSession(send=_never_returns, max_topics=2)
    accepted, rejected, _ = session.declare(
        ["quote:US:AAA", "quote:US:BBB", "quote:US:CCC", "quote:US:bad"]
    )
    assert accepted == ("quote:US:AAA", "quote:US:BBB")
    assert ("quote:US:CCC", REJECT_TOO_MANY) in rejected
    assert ("quote:US:bad", REJECT_BAD_SYMBOL) in rejected


async def _never_returns(_message: str) -> None:
    await asyncio.Event().wait()


# --------------------------------------------------------------------------- #
# 런타임 (수요 집계 / 강등 통보 / REST 모델 일치)
# --------------------------------------------------------------------------- #


def _runtime(bus: _FakeBus, baseline: Quote | None = None) -> MarketStreamRuntime:
    async def resolver(_topic) -> Quote | None:
        return baseline

    return MarketStreamRuntime(
        bus=bus,
        baseline_resolver=resolver,
        start_owner=False,
        demand_debounce_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_runtime_demand_drops_to_zero_when_the_last_client_leaves() -> None:
    bus = _FakeBus()
    runtime = _runtime(bus, _baseline_quote())
    first = StreamSession(send=_collect([]))
    second = StreamSession(send=_collect([]))
    runtime.register(first)
    runtime.register(second)

    await runtime.declare(first, ["quote:US:TQQQ"])
    await runtime.declare(second, ["quote:US:TQQQ"])
    assert runtime.demand == {"quote:US:TQQQ": 2}

    runtime.unregister(second)
    assert runtime.demand == {"quote:US:TQQQ": 1}

    runtime.unregister(first)
    # 상향 예산이 회수된다: 수요 맵에서 아예 사라져야 한다.
    assert dict(runtime.demand) == {}


@pytest.mark.asyncio
async def test_runtime_tells_the_client_which_topics_to_poll_when_degraded() -> None:
    bus = _FakeBus()
    bus.state = {
        "live": False,
        "reason": "UPSTREAM_UNAVAILABLE",
        "streaming": [],
        "demoted": [],
        "rejected": [],
    }
    runtime = _runtime(bus, _baseline_quote())
    sent: list[str] = []
    session = StreamSession(send=_collect(sent))
    runtime._absorb_state(bus.state)  # noqa: SLF001 — 소유자 상태 주입
    runtime.register(session)

    await runtime.declare(session, ["quote:US:TQQQ", "orderbook:KRX:005930"])
    await session.flush()

    statuses = [
        json.loads(message)
        for message in sent
        if json.loads(message)["type"] == contract.MESSAGE_STATUS
    ]
    assert statuses[-1]["upstream"] == "DEGRADED"
    assert statuses[-1]["reason"] == "UPSTREAM_UNAVAILABLE"
    assert statuses[-1]["pollingTopics"] == [
        "orderbook:KRX:005930",
        "quote:US:TQQQ",
    ]
    assert statuses[-1]["pollIntervalSeconds"] == (
        contract.POLL_FALLBACK_INTERVAL_SECONDS
    )


@pytest.mark.asyncio
async def test_runtime_reports_budget_demoted_topics_as_polling_topics() -> None:
    bus = _FakeBus()
    state = {
        "live": True,
        "reason": None,
        "streaming": ["quote:US:TQQQ"],
        "demoted": ["orderbook:KRX:005930"],
        "rejected": [],
    }
    runtime = _runtime(bus, _baseline_quote())
    sent: list[str] = []
    session = StreamSession(send=_collect(sent))
    runtime._absorb_state(state)  # noqa: SLF001 — 소유자 상태 주입
    runtime.register(session)

    await runtime.declare(session, ["quote:US:TQQQ", "orderbook:KRX:005930"])
    await session.flush()

    statuses = [
        json.loads(message)
        for message in sent
        if json.loads(message)["type"] == contract.MESSAGE_STATUS
    ]
    assert statuses[-1]["upstream"] == "LIVE"
    assert statuses[-1]["reason"] == contract.REASON_TOPIC_BUDGET
    assert statuses[-1]["pollingTopics"] == ["orderbook:KRX:005930"]


def test_stream_quote_payload_matches_the_rest_quote_model() -> None:
    """앱이 폴링 결과와 스트림 결과를 같은 모델로 다룰 수 있어야 한다."""

    baseline = _baseline_quote()
    streamed = contract.quote_from_tick(
        baseline,
        price=Decimal("74.40"),
        as_of=datetime(2026, 8, 28, 12, 23, 4, tzinfo=UTC),
        source=protocol.TOSS_STREAM_SOURCE,
    )

    assert set(streamed.model_dump(by_alias=True)) == set(
        baseline.model_dump(by_alias=True)
    )
    assert streamed.price == "74.40"
    assert streamed.previous_close == "72.00"
    # 등락 계산은 REST 와 같은 생성점(`build_quote`)을 쓴다.
    assert streamed.change_amount == "2.40"
    assert streamed.change_rate == "3.33"
    assert streamed.as_of == "2026-08-28T12:23:04Z"
    assert streamed.source == "TOSS_API_WS"


def test_stream_orderbook_payload_matches_the_rest_orderbook_field_names() -> None:
    from app.extensions.kasset.api.schemas import OrderbookResponse

    book = contract.orderbook_from_frame(
        parse_topic("orderbook:KRX:005930"),
        asks=[(Decimal("72100"), Decimal("8500"))],
        bids=[(Decimal("72000"), Decimal("1200"))],
        as_of=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        source=protocol.TOSS_STREAM_SOURCE,
    )
    payload = book.model_dump(by_alias=True)

    rest_fields = {
        field.alias or name for name, field in OrderbookResponse.model_fields.items()
    }
    assert set(payload) == rest_fields
    assert payload["totalAskVolume"] == "8500"
    assert payload["totalBidVolume"] == "1200"
    assert payload["ready"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "-1"),
        ("price", "NaN"),
        ("price", "1e3"),
        ("volume", "-1"),
        ("volume", "NaN"),
        ("volume", "1e3"),
    ],
)
def test_stream_orderbook_level_rejects_non_rest_decimal_notation(
    field: str, value: str
) -> None:
    """REST와 스트림 호가는 같은 decimal 문자열 표기만 허용한다."""

    payload = {"price": "72100.50", "volume": "8500"}
    payload[field] = value

    with pytest.raises(ValidationError):
        contract.StreamOrderbookLevel.model_validate(payload)


def _collect(sink: list[str]):
    async def send(message: str) -> None:
        sink.append(message)

    return send


# --------------------------------------------------------------------------- #
# WebSocket 엔드포인트
# --------------------------------------------------------------------------- #


class _ReceiveOnlyWebSocket:
    """클라이언트 입력은 없지만 서버 close는 관찰하는 WebSocket 대역."""

    def __init__(self) -> None:
        self.receive_started = asyncio.Event()
        self.receive_cancelled = asyncio.Event()
        self.closed: list[tuple[int, str]] = []
        self._never = asyncio.Event()

    async def receive_text(self) -> str:
        self.receive_started.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


@pytest.mark.asyncio
async def test_receive_only_client_closes_immediately_when_sender_times_out() -> None:
    """클라이언트가 다음 프레임을 보내지 않아도 4409와 수요 회수가 진행된다."""

    websocket = _ReceiveOnlyWebSocket()

    async def stalled_sender() -> None:
        await websocket.receive_started.wait()
        raise SlowConsumer("stream client send timed out")

    sender = asyncio.create_task(stalled_sender())
    await asyncio.wait_for(
        route._serve(  # noqa: SLF001 — 비동기 경쟁 조건의 직접 회귀 테스트
            websocket,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            sender,
        ),
        timeout=0.5,
    )

    assert websocket.closed == [
        (contract.CLOSE_SLOW_CONSUMER, "전송이 지연되어 연결을 닫습니다.")
    ]
    # FIRST_COMPLETED 뒤 남은 receive task도 반드시 회수해야 한다.
    assert websocket.receive_cancelled.is_set()


def _app(runtime: MarketStreamRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 주입한 런타임의 백그라운드 태스크를 앱 종료와 함께 정리한다.
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(lifespan=lifespan)
    install_android_compat_api(app)
    app.dependency_overrides[get_stream_runtime] = lambda: runtime
    return app


def test_stream_path_is_reachable_with_a_kasset_token() -> None:
    assert is_kasset_token_allowed_path("/api/v1/market/stream")
    assert is_android_compat_path("/api/v1/market/stream")


def test_stream_rejects_an_invalid_token_with_a_distinguishable_close_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """토큰 만료와 서버 장애를 앱이 구분할 수 있어야 한다."""

    async def deny(_token: str):
        raise MobileApiError(401, "UNAUTHORIZED", "세션이 만료되었습니다.")

    monkeypatch.setattr(route, "authenticate_stream_token", deny)
    runtime = _runtime(_FakeBus())

    with TestClient(_app(runtime)) as client:
        with client.websocket_connect("/api/v1/market/stream") as websocket:
            websocket.send_text(json.dumps({"type": "auth", "accessToken": "expired"}))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_text()

    assert excinfo.value.code == contract.CLOSE_UNAUTHORIZED


def test_stream_rejects_a_non_auth_first_frame_without_a_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def never(_token: str):
        calls.append(_token)
        raise AssertionError("must not authenticate")

    monkeypatch.setattr(route, "authenticate_stream_token", never)
    runtime = _runtime(_FakeBus())

    with TestClient(_app(runtime)) as client:
        with client.websocket_connect("/api/v1/market/stream") as websocket:
            websocket.send_text(
                json.dumps({"type": "subscribe", "topics": ["quote:US:TQQQ"]})
            )
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_text()

    assert excinfo.value.code == contract.CLOSE_UNAUTHORIZED
    assert calls == []


def test_stream_delivers_a_tick_as_a_rest_shaped_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """헤더 인증 → 선언 → 팬아웃 틱 수신까지 한 번에 확인한다."""

    async def allow(token: str):
        assert token == "good-token"
        return SimpleNamespace(
            user=SimpleNamespace(id=7, role="trader", is_active=True)
        )

    monkeypatch.setattr(route, "authenticate_stream_token", allow)
    bus = _FakeBus()
    bus.state = {
        "live": True,
        "reason": None,
        "streaming": ["quote:US:TQQQ"],
        "demoted": [],
        "rejected": [],
    }
    runtime = _runtime(bus, _baseline_quote())

    with TestClient(_app(runtime)) as client:
        with client.websocket_connect(
            "/api/v1/market/stream",
            headers={"Authorization": "Bearer good-token"},
        ) as websocket:
            ready = json.loads(websocket.receive_text())
            # 접속 시점에 앱이 알아야 할 것: 프로토콜 버전, 상향 상태, 상한.
            assert ready == {
                "type": contract.MESSAGE_READY,
                "protocol": contract.STREAM_PROTOCOL_VERSION,
                "upstream": "LIVE",
                "maxTopics": 24,
                "pingIntervalSeconds": 30,
                "pollIntervalSeconds": contract.POLL_FALLBACK_INTERVAL_SECONDS,
            }
            websocket.send_text(
                json.dumps({"type": "subscribe", "topics": ["quote:US:TQQQ"]})
            )
            messages = _drain_until(websocket, contract.MESSAGE_SUBSCRIBED)
            assert messages[-1]["topics"] == ["quote:US:TQQQ"]

            # 토스는 구독 직후 초기 스냅샷을 주지 않는다. 서버가 기존 REST
            # 해석 경로로 만든 baseline 을 먼저 한 번 내려 준다.
            baseline = _drain_until(websocket, contract.MESSAGE_QUOTE)[-1]
            assert baseline["quote"]["price"] == "73.00"
            assert baseline["quote"]["source"] == krx_quotes.TOSS_QUOTE_SOURCE

            # 소유자가 게시한 것처럼 팬아웃 채널에 틱을 하나 넣는다.
            bus.events.append(
                {
                    "kind": "tick",
                    "topic": "quote:US:TQQQ",
                    "trade": {
                        "price": "74.40",
                        "volume": "8",
                        "currency": "USD",
                        "asOf": "2026-08-28T12:23:04+00:00",
                    },
                }
            )

            quote = _drain_until(websocket, contract.MESSAGE_QUOTE)[-1]

    assert quote["topic"] == "quote:US:TQQQ"
    assert quote["quote"]["price"] == "74.40"
    # 전일 종가·종목명·통화는 baseline 에서 재사용하고 등락만 다시 계산한다.
    assert quote["quote"]["previousClose"] == "72.00"
    assert quote["quote"]["changeAmount"] == "2.40"
    assert quote["quote"]["name"] == "ProShares UltraPro QQQ"
    assert quote["quote"]["asOf"] == "2026-08-28T12:23:04Z"
    assert quote["quote"]["source"] == "TOSS_API_WS"


def _drain_until(websocket, message_type: str, *, limit: int = 20) -> list[dict]:
    seen: list[dict] = []
    for _ in range(limit):
        payload = json.loads(websocket.receive_text())
        seen.append(payload)
        if payload["type"] == message_type:
            return seen
    raise AssertionError(f"{message_type} not received; saw {seen}")
