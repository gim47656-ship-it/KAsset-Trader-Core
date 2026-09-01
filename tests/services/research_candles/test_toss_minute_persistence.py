from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from app.jobs import toss_minute_candles as job
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage
from app.services.research_candles.toss_minute_repository import (
    TossMinuteCandleRepository,
)
from app.services.research_candles.toss_minute_source import (
    TOSS_MINUTE_VALUE_SEMANTICS,
    TossMinuteCandleRow,
    TossMinuteCandleSource,
    classify_toss_minute_segment,
)
from app.tasks import toss_minute_candles_tasks as task_module

KST = ZoneInfo("Asia/Seoul")
pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, candles: list[TossCandle]) -> None:
        self.candle_rows = candles
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage:
        self.calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "before": before,
                "adjusted": adjusted,
            }
        )
        return TossCandlesPage(candles=self.candle_rows, next_before=None)

    async def aclose(self) -> None:
        self.closed = True


class _MemoryRepository:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.rows: dict[tuple[datetime, str], TossMinuteCandleRow] = {}

    async def active_symbol_count(self) -> int:
        return len(self.symbols)

    async def active_symbol_batch(
        self, *, total: int, offset: int, limit: int
    ) -> list[str]:
        assert total == len(self.symbols)
        return [
            self.symbols[(offset + index) % total] for index in range(min(limit, total))
        ]

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int:
        for row in rows:
            self.rows[(row.time_utc, row.symbol)] = row
        return len({(row.time_utc, row.symbol) for row in rows})


class _Source:
    def __init__(
        self,
        rows_by_symbol: dict[str, list[TossMinuteCandleRow] | Exception],
    ) -> None:
        self.rows_by_symbol = rows_by_symbol

    async def fetch(
        self, *, symbol: str, retrieved_at: datetime, batch_id: str
    ) -> list[TossMinuteCandleRow]:
        value = self.rows_by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return [
            replace(row, retrieved_at=retrieved_at, batch_id=batch_id) for row in value
        ]

    async def close(self) -> None:
        return None


def _row(*, symbol: str = "005930", close: str = "101") -> TossMinuteCandleRow:
    close_value = Decimal(close)
    return TossMinuteCandleRow(
        time_utc=datetime(2026, 9, 1, 1, 5, tzinfo=UTC),
        session_date_kst=datetime(2026, 9, 1, tzinfo=KST).date(),
        symbol=symbol,
        session_segment="KRX_REGULAR",
        source="TOSS",
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=close_value,
        volume=Decimal("3"),
        value=close_value * Decimal("3"),
        value_semantics=TOSS_MINUTE_VALUE_SEMANTICS,
        is_padding=False,
        pre_nxt=None,
        retrieved_at=datetime(2026, 9, 1, 1, 5, 30, tzinfo=UTC),
        batch_id="initial",
    )


@pytest.mark.asyncio
async def test_source_preserves_current_partial_timezone_segment_and_value_semantics() -> (
    None
):
    client = _Client(
        [
            TossCandle(
                timestamp="2026-09-01T10:05:45",
                open_price=Decimal("100"),
                high_price=Decimal("102"),
                low_price=Decimal("99"),
                close_price=Decimal("101"),
                volume=Decimal("3"),
                currency="KRW",
            )
        ]
    )
    source = TossMinuteCandleSource(client)

    rows = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 10, 5, 50, tzinfo=KST),
        batch_id="tick",
    )

    assert client.calls == [
        {
            "symbol": "005930",
            "interval": "1m",
            "count": 200,
            "before": None,
            "adjusted": None,
        }
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.time_utc == datetime(2026, 9, 1, 1, 5, tzinfo=UTC)
    assert row.session_date_kst.isoformat() == "2026-09-01"
    assert row.session_segment == "KRX_REGULAR"
    assert row.value == Decimal("303")
    assert row.value_semantics == "CLOSE_X_VOLUME_SYNTHETIC"
    assert row.is_padding is False
    assert row.pre_nxt is None


@pytest.mark.asyncio
async def test_source_accepts_the_next_minute_when_the_request_crosses_boundary() -> (
    None
):
    client = _Client(
        [
            TossCandle(
                timestamp="2026-09-01T10:06:00",
                open_price=Decimal("100"),
                high_price=Decimal("102"),
                low_price=Decimal("99"),
                close_price=Decimal("101"),
                volume=Decimal("3"),
                currency="KRW",
            )
        ]
    )
    source = TossMinuteCandleSource(client)

    rows = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 10, 5, 59, tzinfo=KST),
        batch_id="minute-boundary",
    )

    assert [row.time_utc for row in rows] == [datetime(2026, 9, 1, 1, 6, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_source_rejects_only_the_unclassifiable_row() -> None:
    client = _Client(
        [
            TossCandle(
                timestamp="2026-09-01T07:59:00",
                open_price=Decimal("100"),
                high_price=Decimal("100"),
                low_price=Decimal("100"),
                close_price=Decimal("100"),
                volume=Decimal("0"),
                currency="KRW",
            ),
            TossCandle(
                timestamp="2026-09-01T10:05:00",
                open_price=Decimal("100"),
                high_price=Decimal("102"),
                low_price=Decimal("99"),
                close_price=Decimal("101"),
                volume=Decimal("3"),
                currency="KRW",
            ),
        ]
    )
    source = TossMinuteCandleSource(client)

    rows = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 10, 6, tzinfo=KST),
        batch_id="partial-rejection",
    )

    assert [row.time_utc for row in rows] == [datetime(2026, 9, 1, 1, 5, tzinfo=UTC)]


@pytest.mark.parametrize(
    ("clock", "segment"),
    [
        ((8, 0), "NXT_PRE"),
        ((8, 59), "NXT_PRE"),
        ((9, 0), "KRX_REGULAR"),
        ((15, 30), "KRX_REGULAR"),
        ((15, 31), "NXT_POST"),
        ((20, 0), "NXT_POST"),
    ],
)
def test_segment_boundaries_are_kst_clock_labels(
    clock: tuple[int, int], segment: str
) -> None:
    assert (
        classify_toss_minute_segment(datetime(2026, 9, 1, *clock, tzinfo=KST))
        == segment
    )


@pytest.mark.asyncio
async def test_regular_tick_upserts_rows_idempotently_and_updates_partial_revision() -> (
    None
):
    repository = _MemoryRepository(["005930"])
    tick = datetime(2026, 9, 1, 10, 5, 50, tzinfo=KST)

    first = await job._collect_batch(
        repository=repository,
        source=_Source({"005930": [_row(close="101")]}),
        now=tick,
        batch_size=20,
    )
    second = await job._collect_batch(
        repository=repository,
        source=_Source({"005930": [_row(close="102")]}),
        now=tick,
        batch_size=20,
    )

    assert first["status"] == second["status"] == "completed"
    assert first["batch_id"] == second["batch_id"]
    assert len(repository.rows) == 1
    stored = repository.rows[(datetime(2026, 9, 1, 1, 5, tzinfo=UTC), "005930")]
    assert stored.close == Decimal("102")
    assert stored.value == Decimal("306")


@pytest.mark.asyncio
async def test_tick_selects_only_one_bounded_active_universe_batch() -> None:
    symbols = [f"{number:06d}" for number in range(25)]
    repository = _MemoryRepository(symbols)
    result = await job._collect_batch(
        repository=repository,
        source=_Source({symbol: [] for symbol in symbols}),
        now=datetime(2026, 9, 1, 10, 6, tzinfo=KST),
        batch_size=20,
    )

    assert result["status"] == "completed"
    assert result["symbols_total"] == 25
    assert result["symbols_selected"] == 20
    assert result["empty_symbols"] == 20


@pytest.mark.asyncio
async def test_provider_failure_isolated_as_partial_batch() -> None:
    repository = _MemoryRepository(["000660", "005930"])
    result = await job._collect_batch(
        repository=repository,
        source=_Source(
            {
                "000660": [_row(symbol="000660")],
                "005930": RuntimeError("provider unavailable"),
            }
        ),
        now=datetime(2026, 9, 1, 10, 5, tzinfo=KST),
        batch_size=20,
    )

    assert result["status"] == "partial"
    assert result["symbols_succeeded"] == 1
    assert result["symbols_failed"] == 1
    assert len(repository.rows) == 1
    assert (datetime(2026, 9, 1, 1, 5, tzinfo=UTC), "000660") in repository.rows


@pytest.mark.asyncio
async def test_repository_uses_target_unique_key_for_idempotent_upsert() -> None:
    capture = SimpleNamespace(statements=[])

    async def execute(statement: object) -> None:
        capture.statements.append(statement)

    session = SimpleNamespace(execute=execute)
    repository = TossMinuteCandleRepository(session)  # type: ignore[arg-type]
    count = await repository.upsert([_row(), _row()])

    compiled = str(
        capture.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    )
    assert count == 1
    assert "INSERT INTO research.kr_candles_1m_toss" in compiled
    conflict_clause = (
        "ON CONFLICT ON CONSTRAINT uq_research_kr_candles_1m_toss_time_symbol"
    )
    assert conflict_clause in compiled
    assert "DO UPDATE SET" in compiled


@pytest.mark.asyncio
async def test_non_market_tick_is_noop_before_database_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "is_trading_session", lambda market, day: False)

    def forbidden_factory() -> Any:
        raise AssertionError("non-market tick touched a dependency")

    result = await job.run_toss_minute_candle_sync(
        now=datetime(2026, 9, 1, 10, 5, tzinfo=KST),
        session_factory=forbidden_factory,
        source_factory=forbidden_factory,
    )

    assert result == {
        "status": "noop",
        "reason": "non_trading_day",
        "rows_upserted": 0,
    }


@pytest.mark.asyncio
async def test_weekday_after_session_is_noop_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "is_trading_session", lambda market, day: True)

    def forbidden_factory() -> Any:
        raise AssertionError("outside-session tick touched a dependency")

    result = await job.run_toss_minute_candle_sync(
        now=datetime(2026, 9, 1, 20, 1, tzinfo=KST),
        session_factory=forbidden_factory,
        source_factory=forbidden_factory,
    )

    assert result == {
        "status": "noop",
        "reason": "outside_toss_session",
        "rows_upserted": 0,
    }


def test_taskiq_schedule_is_registered_and_discovered() -> None:
    import app.tasks as task_package

    assert task_module in task_package.TASKIQ_TASK_MODULES
    task = task_module.sync_toss_minute_candles_task
    assert task.task_name == "research.candles.kr.toss.1m.sync"
    assert task.labels.get("schedule") == [
        {"cron": "* 8-19 * * 1-5", "cron_offset": "Asia/Seoul"},
        {"cron": "0 20 * * 1-5", "cron_offset": "Asia/Seoul"},
    ]


def test_kasset_worker_and_scheduler_discover_toss_minute_task() -> None:
    compose = (
        Path(__file__).resolve().parents[3] / "docker-compose.kasset.yml"
    ).read_text(encoding="utf-8")
    task_module_path = '"app.tasks.toss_minute_candles_tasks"'
    assert compose.count(task_module_path) == 2
