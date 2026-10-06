"""Real TimescaleDB proof for the Toss 1-minute compression migration.

Only a real hypertable can show that the migration's ordering is right
(policy off -> decompress -> compression off on downgrade), that the policy
fires at night and leaves the active chunk alone, and that the repair CLI's
``INSERT ... ON CONFLICT DO UPDATE`` keeps working on a compressed chunk.
CI's plain PostgreSQL has no TimescaleDB, so the test skips there; the
server-side runner (``docs/runbooks/server-pytest-runner.md``) executes it.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import time
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
_TABLE = "research.kr_candles_1m_toss"


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename[:-3], _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORPUS = _load("20260804_toss_phase2_corpus.py")
COMPRESSION = _load("20261006_toss_minute_compression.py")


def _run(connection: Connection, migration: ModuleType, direction: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


_SEED = """
INSERT INTO research.kr_candles_1m_toss (
    time_utc, session_date_kst, symbol, session_segment, open, high, low, close,
    volume, value, is_padding, retrieved_at, batch_id
)
SELECT ts, (ts AT TIME ZONE 'Asia/Seoul')::date, symbol, 'KRX_REGULAR',
       100, 101, 99, 100, 10, 1000, false, ts, 'seed'
FROM unnest(ARRAY['2026-01-05 00:00+00', '2026-01-12 00:00+00']::timestamptz[])
         AS start_at,
     LATERAL generate_series(start_at, start_at + INTERVAL '5 hours',
                             INTERVAL '1 minute') AS ts,
     unnest(ARRAY['005930', '000660', '035420']) AS symbol
"""

_UPSERT = f"""
INSERT INTO {_TABLE} (
    time_utc, session_date_kst, symbol, session_segment, open, high, low, close,
    volume, value, is_padding, retrieved_at, batch_id
) VALUES
    ('2026-01-05 00:10:00+00', '2026-01-05', '005930', 'KRX_REGULAR',
     100, 101, 99, 100, 10, 1000, false, now(), 'repair'),
    ('2026-01-05 02:00:00+00', '2026-01-05', '005930', 'KRX_REGULAR',
     100, 101, 99, 100, 10, 1000, false, now(), 'repair')
ON CONFLICT ON CONSTRAINT uq_research_kr_candles_1m_toss_time_symbol
DO UPDATE SET batch_id = EXCLUDED.batch_id, retrieved_at = EXCLUDED.retrieved_at
"""


@pytest_asyncio.fixture
async def hypertable_engine() -> AsyncIterator[AsyncEngine]:
    base = make_url(settings.DATABASE_URL)
    if base.get_backend_name() != "postgresql":
        pytest.skip("Toss compression migration needs PostgreSQL")
    name = f"toss_compression_{uuid4().hex}"
    admin = await asyncpg.connect(
        user=base.username,
        password=base.password,
        host=base.host,
        port=base.port,
        database="postgres",
    )
    engine: AsyncEngine | None = None
    try:
        if not await admin.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb'"
        ):
            pytest.skip("TimescaleDB is not installed on this PostgreSQL server")
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = create_async_engine(
            base.set(database=name).render_as_string(hide_password=False)
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE EXTENSION IF NOT EXISTS timescaledb")
                )
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
        except DBAPIError:
            pytest.skip("TimescaleDB cannot be created in this PostgreSQL server")
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        assert name.startswith("toss_compression_"), name  # never drop anything else
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _scalar(engine: AsyncEngine, sql: str, **params: object):
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar_one()


async def _chunks(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT count(*) FILTER (WHERE is_compressed), count(*) "
                    "FROM timescaledb_information.chunks "
                    "WHERE hypertable_name = 'kr_candles_1m_toss'"
                )
            )
        ).one()
    return row[0], row[1]


async def _run_policy(engine: AsyncEngine) -> None:
    job_id = await _scalar(
        engine,
        "SELECT job_id FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_compression'",
    )
    # The policy commits between chunks, so it cannot run inside a transaction.
    async with engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("CALL run_job(:job_id)"), {"job_id": job_id})


async def _policy_state(engine: AsyncEngine) -> tuple[bool, int, time] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT schedule_interval = INTERVAL '1 day', max_retries, "
                    "(next_start AT TIME ZONE 'Asia/Seoul')::time "
                    "FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression'"
                )
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


@pytest.mark.asyncio
async def test_compression_policy_round_trip_on_a_real_hypertable(
    hypertable_engine: AsyncEngine,
) -> None:
    engine = hypertable_engine
    async with engine.begin() as connection:
        await connection.run_sync(_run, CORPUS, "upgrade")
        # Two chunks long past the 7 day horizon (2026-01-05, 2026-01-12) and one
        # row in the active chunk, which the policy must never touch.
        await connection.execute(text(_SEED))
        # A hole inside the first week: the repair CLI fills exactly this kind.
        await connection.execute(
            text(
                f"DELETE FROM {_TABLE} WHERE symbol = '005930' "
                "AND time_utc = '2026-01-05 02:00:00+00'"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {_TABLE} (time_utc, session_date_kst, symbol, "
                "session_segment, open, high, low, close, volume, value, "
                "is_padding, retrieved_at, batch_id) VALUES (now(), "
                "(now() AT TIME ZONE 'Asia/Seoul')::date, '005930', 'KRX_REGULAR', "
                "100, 101, 99, 100, 10, 1000, false, now(), 'active')"
            )
        )
    total = await _scalar(engine, f"SELECT count(*) FROM {_TABLE}")
    assert await _chunks(engine) == (0, 3)

    async with engine.begin() as connection:
        await connection.run_sync(_run, COMPRESSION, "upgrade")

    # Enabling compression must not compress anything by itself.
    assert await _chunks(engine) == (0, 3)
    # Once a day at 02:30 KST, i.e. outside 08:50-20:10 KST.
    assert await _policy_state(engine) == (True, COMPRESSION.MAX_RETRIES, time(2, 30))

    await _run_policy(engine)
    # Only the two old chunks; the chunk holding "now" stays writable rowstore.
    assert await _chunks(engine) == (2, 3)
    assert await _scalar(engine, f"SELECT count(*) FROM {_TABLE}") == total

    # The repair CLI's upsert: one conflicting row inside a compressed chunk and
    # one brand-new minute (the hole) inside the same compressed symbol batch.
    async with engine.begin() as connection:
        await connection.execute(text(_UPSERT))
    assert await _scalar(engine, f"SELECT count(*) FROM {_TABLE}") == total + 1
    assert (
        await _scalar(
            engine,
            f"SELECT count(*) FROM {_TABLE} WHERE batch_id = 'repair' "
            "AND symbol = '005930'",
        )
        == 2
    )
    assert (
        await _scalar(
            engine,
            f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} "
            "GROUP BY time_utc, symbol HAVING count(*) > 1) duplicates",
        )
        == 0
    )
    await _run_policy(engine)  # recompresses the chunk the upsert touched
    assert await _chunks(engine) == (2, 3)
    assert await _scalar(engine, f"SELECT count(*) FROM {_TABLE}") == total + 1

    async with engine.begin() as connection:
        await connection.run_sync(_run, COMPRESSION, "downgrade")

    assert await _policy_state(engine) is None
    assert await _chunks(engine) == (0, 3)
    assert not await _scalar(
        engine,
        "SELECT compression_enabled FROM timescaledb_information.hypertables "
        "WHERE hypertable_name = 'kr_candles_1m_toss'",
    )
    assert await _scalar(engine, f"SELECT count(*) FROM {_TABLE}") == total + 1

    async with engine.begin() as connection:
        await connection.run_sync(_run, COMPRESSION, "upgrade")
    assert await _policy_state(engine) == (True, COMPRESSION.MAX_RETRIES, time(2, 30))
