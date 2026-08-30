from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api import router as mod

_APPLIED_REVISION = "20260830_news_translation"


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Session:
    """조회만 흉내내는 최소 세션. 쓰기 메서드를 아예 두지 않아,

    상태 조회 경로가 실수로 commit/flush를 호출하면 AttributeError로 드러난다.
    """

    def __init__(
        self,
        *,
        value: object = _APPLIED_REVISION,
        error: Exception | None = None,
    ) -> None:
        self._value = value
        self._error = error
        self.statements: list[str] = []
        self.rollbacks = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        if self._error is not None:
            raise self._error
        return _Result(self._value)

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def clear_revision_cache() -> None:
    mod._migration_revision_cache = None


@pytest.fixture(autouse=True)
def stub_status_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod.broker_registry,
        "list_brokers",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    provider="NH",
                    connected=True,
                    last_verified_at="2026-08-30T00:00:00Z",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        mod.runtime_state,
        "get",
        AsyncMock(
            return_value=SimpleNamespace(
                trading_mode="PAPER",
                kill_switch_enabled=False,
            )
        ),
    )
    monkeypatch.setattr(
        mod.runtime_state,
        "get_global",
        AsyncMock(return_value=SimpleNamespace(kill_switch_enabled=False)),
    )


@pytest.mark.asyncio
async def test_system_status_reports_the_applied_migration_revision() -> None:
    session = _Session()

    status = await mod._build_system_status(session, 101)

    assert status.database.status == "ok"
    assert status.database.migration_revision == _APPLIED_REVISION
    assert status.trading_mode == "PAPER"
    assert [broker.provider for broker in status.brokers] == ["NH"]


@pytest.mark.asyncio
async def test_migration_revision_query_is_a_read_only_select() -> None:
    session = _Session()

    await mod._build_system_status(session, 101)

    assert session.statements == ["SELECT version_num FROM alembic_version"]
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_missing_revision_row_leaves_the_field_null_not_blank() -> None:
    session = _Session(value=None)

    status = await mod._build_system_status(session, 101)

    assert status.database.migration_revision is None
    assert status.database.status == "ok"


@pytest.mark.asyncio
async def test_revision_query_failure_keeps_the_rest_of_the_status_alive() -> None:
    session = _Session(error=RuntimeError("relation alembic_version does not exist"))

    status = await mod._build_system_status(session, 101)

    assert status.database.migration_revision is None
    assert status.database.status == "ok"
    assert status.trading_mode == "PAPER"
    # 실패한 조회는 트랜잭션을 오염시키므로 곧바로 되돌려 뒤따르는 조회를 살린다.
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_revision_is_cached_so_every_request_does_not_hit_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr(mod.time, "monotonic", lambda: now)
    session = _Session()

    for _ in range(3):
        await mod._build_system_status(session, 101)
    assert len(session.statements) == 1

    now = 1000.0 + mod._MIGRATION_REVISION_TTL_SECONDS + 0.1
    await mod._build_system_status(session, 101)
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_failed_revision_lookup_is_retried_sooner_than_a_successful_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr(mod.time, "monotonic", lambda: now)
    session = _Session(error=RuntimeError("connection reset"))

    await mod._build_system_status(session, 101)
    assert len(session.statements) == 1

    now = 1000.0 + mod._MIGRATION_REVISION_MISS_TTL_SECONDS + 0.1
    await mod._build_system_status(session, 101)
    assert len(session.statements) == 2
