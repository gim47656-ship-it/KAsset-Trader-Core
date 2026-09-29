"""NH 시세 러너·토큰 캐시·snapshot 저장·1분 KRX 주기의 동작 계약."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest

from app.core.config import settings
from app.extensions.kasset.automation import realtime_kr
from app.extensions.kasset.automation.realtime_tape import (
    KST,
    TapeSnapshot,
    evaluate_realtime_entry,
)
from app.extensions.kasset.nhplug import protocol
from app.extensions.kasset.nhplug.runner import NhStreamRunner
from app.extensions.kasset.nhplug.tape_store import (
    RealtimeEntryGate,
    RedisTapeStore,
    snapshot_key,
)
from app.extensions.kasset.nhplug.token_cache import (
    NhplugCredentials,
    NhplugTokenError,
    NhplugTokenProvider,
    read_token_cache,
    validate_auth_url,
)
from app.tasks import kasset_paper_automation_tasks

IN_SESSION = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)  # 11:00 KST
AFTER_CLOSE = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)  # 21:00 KST
_CREDENTIALS = NhplugCredentials(
    app_key="test-app-key",
    app_secret="test-app-secret",
    auth_url="https://api.nhplug.com:8443",
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fcntl is POSIX-only")


# ---------------------------------------------------------------- token cache
def _token_client(
    responses: list[httpx.Response],
    calls: list[httpx.Request],
    *,
    delay: float = 0.0,
) -> httpx.AsyncClient:
    async def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if delay:
            await asyncio.sleep(delay)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _write_cache(path: Path, token: str, expires_at: object) -> None:
    path.write_text(json.dumps({"access_token": token, "expires_at": expires_at}))


@posix_only
@pytest.mark.asyncio
async def test_valid_cached_token_is_reused_without_any_issue_request(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "token.json"
    _write_cache(cache, "cached-token", 2_000_000_000)
    calls: list[httpx.Request] = []
    async with _token_client([], calls) as client:
        provider = NhplugTokenProvider(
            credentials=_CREDENTIALS,
            cache_path=cache,
            http_client=client,
            clock=lambda: 1_900_000_000.0,
        )
        assert await provider.token() == "cached-token"
        assert await provider.token() == "cached-token"
    assert calls == []
    assert provider.issued_count == 0


@posix_only
@pytest.mark.asyncio
async def test_expired_cache_issues_once_even_with_two_processes(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "token.json"
    _write_cache(cache, "old-token", 1_000.0)
    calls: list[httpx.Request] = []
    responses = [
        httpx.Response(200, json={"access_token": "new-token", "expires_in": 86400})
    ]
    async with _token_client(responses, calls, delay=0.05) as client:
        # 프로세스 둘을 흉내 낸다: 메모리·asyncio 잠금을 공유하지 않는 두 공급자.
        first, second = (
            NhplugTokenProvider(
                credentials=_CREDENTIALS,
                cache_path=cache,
                http_client=client,
                clock=lambda: 10_000.0,
            )
            for _ in range(2)
        )
        tokens = await asyncio.gather(first.token(), second.token())

    assert tokens == ["new-token", "new-token"]
    assert len(calls) == 1
    assert calls[0].url.path == "/oauth2/token"
    assert calls[0].url.params["grant_type"] == "client_credentials"
    stored = read_token_cache(cache)
    assert stored is not None
    assert stored.access_token == "new-token"
    assert stored.expires_at == 10_000.0 + 86400
    # 기존 파일의 epoch 숫자 표기를 그대로 유지한다.
    assert isinstance(json.loads(cache.read_text())["expires_at"], float)
    assert os.stat(cache).st_mode & 0o777 == 0o600


@posix_only
@pytest.mark.asyncio
async def test_unreadable_cache_and_rate_limit_never_churn_tokens(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    calls: list[httpx.Request] = []
    async with _token_client([], calls) as client:
        provider = NhplugTokenProvider(
            credentials=_CREDENTIALS, cache_path=broken, http_client=client
        )
        with pytest.raises(NhplugTokenError):
            await provider.token()
    assert calls == []

    expired = tmp_path / "expired.json"
    _write_cache(expired, "old-token", "2020-01-01T00:00:00+00:00")
    responses = [httpx.Response(429, json={"rsp_msg": "too many"})]
    async with _token_client(responses, calls) as client:
        provider = NhplugTokenProvider(
            credentials=_CREDENTIALS, cache_path=expired, http_client=client
        )
        with pytest.raises(NhplugTokenError, match="HTTP 429"):
            await provider.token()
    assert len(calls) == 1
    assert json.loads(expired.read_text())["access_token"] == "old-token"

    with pytest.raises(NhplugTokenError):
        validate_auth_url("https://api.nhplug.com.evil.example:8443")
    assert "test-app-secret" not in repr(_CREDENTIALS)


# ---------------------------------------------------------------- runner
class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        self.closed = True


class _Harness:
    def __init__(self, redis_client: fakeredis.aioredis.FakeRedis, demand: list[str]):
        self.now = IN_SESSION
        self.mono = 0.0
        self.sockets: list[_FakeWebSocket] = []
        self.demand = demand
        self.store = RedisTapeStore(redis_client)
        self.redis = redis_client

    def runner(self, instance_id: str) -> NhStreamRunner:
        async def _connect() -> _FakeWebSocket:
            socket = _FakeWebSocket()
            self.sockets.append(socket)
            return socket

        async def _sleep(seconds: float) -> None:
            self.mono += seconds

        async def _demand(_now: datetime) -> list[str]:
            return list(self.demand)

        async def _token() -> str:
            return "tok"

        return NhStreamRunner(
            redis_client=self.redis,
            store=self.store,
            token=_token,
            demand_loader=_demand,
            connector=_connect,  # type: ignore[arg-type]
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleep=_sleep,
            instance_id=instance_id,
        )


def _push(channel: str, symbol: str, moment: datetime, **body: object) -> str:
    stamp = moment.astimezone(KST).strftime("%H:%M:%S")
    key = "time" if channel == protocol.TRADE_CHANNEL else "hotime"
    return json.dumps(
        {
            "header": {"tr_cd": channel, "tr_key": symbol},
            "body": {"code": symbol, key: stamp, **body},
        }
    )


@asynccontextmanager
async def _redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runner_respects_registration_budget_and_holding_priority() -> None:
    holdings = [str(100000 + index) for index in range(20)]
    candidates = [str(200000 + index) for index in range(15)]
    async with _redis() as client:
        harness = _Harness(client, holdings + candidates)
        owner = harness.runner("owner-a")
        standby = harness.runner("owner-b")
        try:
            await owner.tick()
            assert await standby.tick() == pytest.approx(5.0)

            # 세션은 최대 2개, 세션마다 등록 30건(15종목 × oc/ob).
            assert len(harness.sockets) == protocol.MAX_SESSIONS
            per_socket = [socket.sent for socket in harness.sockets]
            assert [len(sent) for sent in per_socket] == [30, 30]
            subscribed = {
                str(message["body"]["tr_key"])  # type: ignore[index]
                for sent in per_socket
                for message in sent
            }
            channels = {
                str(message["body"]["tr_cd"])  # type: ignore[index]
                for sent in per_socket
                for message in sent
            }
            assert channels == {"oc", "ob"}
            assert set(holdings) <= subscribed
            assert len(subscribed) == protocol.MAX_SYMBOLS
            assert not set(candidates[-5:]) & subscribed
            assert all(
                message["header"] == {"token": "tok", "tr_type": "1"}
                for sent in per_socket
                for message in sent
            )
        finally:
            await owner._shutdown()
            await standby._shutdown()


@pytest.mark.asyncio
async def test_runner_replay_warms_up_publishes_and_resets_on_reconnect() -> None:
    symbol = "005930"
    async with _redis() as client:
        harness = _Harness(client, [symbol])
        runner = harness.runner("owner")
        try:
            await runner.tick()
            slot = runner._slots[0]
            ack = json.dumps(
                {
                    "header": {"tr_type": "1", "tr_cd": "oc", "rsp_cd": "00000"},
                    "body": {"tr_key": [symbol]},
                }
            )
            runner.handle_message(slot, ack, received_at=harness.now)
            buy, sell, volume, price = 5_000, 4_000, 10_000, 70_000
            for second in range(0, 63):
                moment = IN_SESSION + timedelta(seconds=second)
                harness.now = moment
                runner.handle_message(
                    slot,
                    _push("ob", symbol, moment, bid="70000", offer="70100"),
                    received_at=moment,
                )
                if second % 2 == 0:
                    buy, sell, volume, price = (
                        buy + 30,
                        sell + 20,
                        volume + 50,
                        price + 5,
                    )
                    runner.handle_message(
                        slot,
                        _push(
                            "oc",
                            symbol,
                            moment,
                            price=str(price),
                            volume=str(volume),
                            bidvolall=str(buy),
                            offvolall=str(sell),
                            avgprice="69900",
                        ),
                        received_at=moment,
                    )
            harness.now = IN_SESSION + timedelta(seconds=62.5)
            await runner._publish()
            snapshots = await harness.store.read_many([symbol])
            gate = RealtimeEntryGate(harness.store, clock=lambda: harness.now)
            assert evaluate_realtime_entry(snapshots[symbol], now=harness.now).ready
            assert (await gate.check(symbol)).ready

            # 끊긴 연결의 관찰은 이어 붙이지 않는다.
            await runner._drop_connection(slot, "test_disconnect")
            await runner._publish()
            assert await client.get(snapshot_key(symbol)) is None
            assert not (await gate.check(symbol)).ready

            harness.mono += 60
            await runner.tick()
            assert len(harness.sockets) == 2
            snapshot = (await harness.store.read_many([symbol]))[symbol]
            assert (
                "warmup_incomplete"
                in evaluate_realtime_entry(snapshot, now=harness.now).reasons
            )
        finally:
            await runner._shutdown()


@pytest.mark.asyncio
async def test_runner_disconnects_outside_the_krx_regular_session() -> None:
    async with _redis() as client:
        harness = _Harness(client, ["005930"])
        harness.now = AFTER_CLOSE
        runner = harness.runner("owner")
        try:
            assert await runner.tick() == pytest.approx(15.0)
            assert harness.sockets == []
        finally:
            await runner._shutdown()


@pytest.mark.asyncio
async def test_tape_store_ignores_malformed_snapshots() -> None:
    async with _redis() as client:
        store = RedisTapeStore(client)
        await client.set(snapshot_key("005930"), "{broken")
        await client.set(snapshot_key("000660"), json.dumps({"schemaVersion": "x"}))
        assert await store.read_many(["005930", "000660", "035420"]) == {}


# ---------------------------------------------------------------- minute cycle
class _RecordingReader:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    async def read_many(self, symbols: Iterable[str]) -> Mapping[str, TapeSnapshot]:
        self._calls.append(("read", sorted(symbols)))
        return {}


@pytest.mark.asyncio
async def test_minute_cycle_orders_protection_trend_then_krx_only_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _Manager:
        def __init__(self, _db: object, **_kwargs: object) -> None:
            pass

        async def run_owner(self, owner_id: int, *, markets: object) -> tuple[str, ...]:
            calls.append(("protective", owner_id, sorted(markets)))  # type: ignore[call-overload]
            return ("stop-1",)

        async def run_realtime_trend_exits(
            self, owner_id: int, *, snapshots: object
        ) -> tuple[str, ...]:
            calls.append(("trend", owner_id))
            return ()

    @asynccontextmanager
    async def _session() -> AsyncIterator[object]:
        yield object()

    async def _owners(_db: object, _now: datetime) -> list[int]:
        return [7]

    async def _held(_db: object, _owner: int) -> list[str]:
        return ["005930"]

    async def _execute(**kwargs: object) -> dict[str, object]:
        calls.append(("execute", sorted(kwargs["markets"])))  # type: ignore[call-overload]
        assert isinstance(kwargs["realtime_gate"], RealtimeEntryGate)
        return {"enabled": True, "owners": 0, "outcomes": []}

    monkeypatch.setattr(settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", True)
    monkeypatch.setattr(realtime_kr, "PaperPositionManagerService", _Manager)
    monkeypatch.setattr(realtime_kr, "_session", _session)
    monkeypatch.setattr(realtime_kr, "_auto_paper_owner_ids", _owners)
    monkeypatch.setattr(realtime_kr, "_held_krx_symbols", _held)
    monkeypatch.setattr(
        realtime_kr,
        "current_strategy_artifact",
        lambda: SimpleNamespace(fingerprint="a" * 64),
    )

    result = await realtime_kr.run_kr_realtime_once(
        now=IN_SESSION,
        tape_reader=_RecordingReader(calls),
        clock=lambda: IN_SESSION,
        execute=_execute,
    )

    assert calls == [
        ("protective", 7, ["KRX"]),
        ("read", ["005930"]),
        ("trend", 7),
        ("execute", ["KRX"]),
    ]
    assert result["owners"] == [
        {
            "ownerUserId": 7,
            "protectiveExitIds": ["stop-1"],
            "realtimeTrendExitIds": [],
            "observedSymbols": [],
        }
    ]

    def _forbidden_session() -> object:
        raise AssertionError("closed session must not touch the database")

    monkeypatch.setattr(realtime_kr, "_session", _forbidden_session)
    assert await realtime_kr.run_kr_realtime_once(
        now=AFTER_CLOSE, tape_reader=_RecordingReader(calls)
    ) == {"enabled": True, "skipped": "krx_regular_session_closed"}


def test_minute_task_is_scheduled_only_during_kst_regular_hours() -> None:
    task = kasset_paper_automation_tasks.kasset_realtime_kr_run
    assert task.task_name == "kasset.realtime.kr.run"
    assert task.labels["schedule"] == [
        {"cron": "* 9-15 * * 1-5", "cron_offset": "Asia/Seoul"}
    ]
