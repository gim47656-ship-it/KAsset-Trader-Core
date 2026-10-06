from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import toss_minute_candles as job
from app.models.kr_candles_1m_toss import KRTossMinuteCandle
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage
from app.services.research_candles.toss_minute_gap_repair import (
    plan_session_gaps,
    repair_session_gaps,
)
from app.services.research_candles.toss_minute_repository import (
    TossMinuteCandleRepository,
    TossMinuteCoverage,
)
from app.services.research_candles.toss_minute_source import (
    TOSS_MINUTE_VALUE_SEMANTICS,
    TossMinuteCandleRow,
    TossMinuteCandleSource,
    TossMinutePage,
    UnclassifiableTossMinute,
    classify_toss_minute_segment,
)
from app.tasks import toss_minute_candles_tasks as task_module
from scripts import repair_toss_minute_gaps as repair_cli

KST = ZoneInfo("Asia/Seoul")
pytestmark = pytest.mark.unit


def _candle(at: datetime, *, close: str = "101", volume: str = "3") -> TossCandle:
    return TossCandle(
        timestamp=at.isoformat(),
        open_price=Decimal("100"),
        high_price=Decimal("102"),
        low_price=Decimal("99"),
        close_price=Decimal(close),
        volume=Decimal(volume),
        currency="KRW",
    )


def _minutes(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=offset) for offset in range(count)]


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


class _HistoryClient:
    """Toss ``/candles`` over a fixed bar history.

    Pages are newest first, ``before`` is an inclusive upper bound and
    ``next_before`` names the newest bar older than the page, as the provider
    contract describes.
    """

    def __init__(self, bars: Mapping[str, list[datetime] | Exception]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str | None]] = []

    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage:
        self.calls.append((symbol, before))
        history = self.bars[symbol]
        if isinstance(history, Exception):
            raise history
        bound = datetime.fromisoformat(before) if before is not None else None
        eligible = [at for at in history if bound is None or at <= bound]
        page = eligible[-(count or 100) :]
        older = eligible[: len(eligible) - len(page)]
        return TossCandlesPage(
            candles=[_candle(at) for at in reversed(page)],
            next_before=older[-1].isoformat() if older else None,
        )

    async def aclose(self) -> None:
        return None


class _MemoryRepository:
    """In-memory stand-in with the coverage order of ``stalest_active_symbols``."""

    def __init__(
        self, symbols: list[str], rows: list[TossMinuteCandleRow] | None = None
    ) -> None:
        self.symbols = symbols
        self.rows: dict[tuple[datetime, str], TossMinuteCandleRow] = {
            (row.time_utc, row.symbol): row for row in rows or []
        }
        self.selections: list[list[str]] = []

    async def stalest_active_symbols(
        self, *, limit: int, since: datetime, exclude: Any = ()
    ) -> list[TossMinuteCoverage]:
        coverage: list[TossMinuteCoverage] = []
        for symbol in self.symbols:
            if symbol in exclude:
                continue
            stored = [
                row
                for (time_utc, row_symbol), row in self.rows.items()
                if row_symbol == symbol and time_utc >= since
            ]
            latest = max(stored, key=lambda row: row.time_utc, default=None)
            coverage.append(
                TossMinuteCoverage(
                    symbol=symbol,
                    latest_time_utc=latest.time_utc if latest else None,
                    retrieved_at=latest.retrieved_at if latest else None,
                )
            )
        coverage.sort(
            key=lambda item: (
                item.retrieved_at is not None,
                item.retrieved_at or datetime.min.replace(tzinfo=UTC),
                item.symbol,
            )
        )
        chosen = coverage[:limit]
        self.selections.append([item.symbol for item in chosen])
        return chosen

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int:
        for row in rows:
            self.rows[(row.time_utc, row.symbol)] = row
        return len({(row.time_utc, row.symbol) for row in rows})

    def stored_minutes(self, symbol: str) -> list[datetime]:
        return sorted(
            time_utc for time_utc, row_symbol in self.rows if row_symbol == symbol
        )


class _Source:
    def __init__(
        self,
        rows_by_symbol: dict[str, list[TossMinuteCandleRow] | Exception],
    ) -> None:
        self.rows_by_symbol = rows_by_symbol

    async def fetch(
        self,
        *,
        symbol: str,
        retrieved_at: datetime,
        batch_id: str,
        before: str | None = None,
    ) -> TossMinutePage:
        value = self.rows_by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return TossMinutePage(
            rows=[
                replace(row, retrieved_at=retrieved_at, batch_id=batch_id)
                for row in value
            ],
            next_before=None,
        )

    async def close(self) -> None:
        return None


def _row(
    *,
    symbol: str = "005930",
    close: str = "101",
    time_utc: datetime = datetime(2026, 9, 1, 1, 5, tzinfo=UTC),
    retrieved_at: datetime = datetime(2026, 9, 1, 1, 5, 30, tzinfo=UTC),
) -> TossMinuteCandleRow:
    close_value = Decimal(close)
    return TossMinuteCandleRow(
        time_utc=time_utc,
        session_date_kst=time_utc.astimezone(KST).date(),
        symbol=symbol,
        session_segment=classify_toss_minute_segment(time_utc.astimezone(KST)),
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
        retrieved_at=retrieved_at,
        batch_id="initial",
    )


def _history_source(client: _HistoryClient) -> TossMinuteCandleSource:
    # Every history used below ends well before this response time.
    return TossMinuteCandleSource(
        client, clock=lambda: datetime(2026, 10, 1, 12, tzinfo=UTC)
    )


@pytest.mark.asyncio
async def test_source_preserves_current_partial_timezone_segment_and_value_semantics() -> (
    None
):
    # Toss may send an offset-less timestamp; it is a KST wall-clock minute.
    client = _Client(
        [
            replace(
                _candle(datetime(2026, 9, 1, 10, 5, 45, tzinfo=KST)),
                timestamp="2026-09-01T10:05:45",
            )
        ]
    )
    source = TossMinuteCandleSource(client)

    page = await source.fetch(
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
    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.time_utc == datetime(2026, 9, 1, 1, 5, tzinfo=UTC)
    assert row.session_date_kst.isoformat() == "2026-09-01"
    assert row.session_segment == "KRX_REGULAR"
    assert row.value == Decimal("303")
    assert row.value_semantics == "CLOSE_X_VOLUME_SYNTHETIC"
    assert row.is_padding is False
    assert row.pre_nxt is None


@pytest.mark.asyncio
async def test_source_forwards_before_cursor_and_returns_next_cursor() -> None:
    history = _minutes(datetime(2026, 9, 1, 9, 1, tzinfo=KST), 300)
    client = _HistoryClient({"005930": history})
    source = _history_source(client)

    page = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 14, 1, tzinfo=KST),
        batch_id="older",
        before=history[150].isoformat(),
    )

    assert client.calls == [("005930", history[150].isoformat())]
    # ``before`` is inclusive: the bar at the cursor is the newest one returned.
    assert [row.time_utc for row in page.rows] == [
        at.astimezone(UTC) for at in history[:151]
    ]
    assert page.oldest_time_utc == history[0].astimezone(UTC)
    assert page.next_before is None


@pytest.mark.asyncio
async def test_source_accepts_the_in_progress_bar_when_the_batch_crosses_a_minute() -> (
    None
):
    # 2026-10-06 13:45 KST: a batch that started at 13:45:48 got its response
    # after 13:46:00, when the in-progress bar is labelled with its 13:47 end.
    client = _Client([_candle(datetime(2026, 9, 1, 10, 7, tzinfo=KST))])
    source = TossMinuteCandleSource(
        client, clock=lambda: datetime(2026, 9, 1, 10, 6, 1, tzinfo=KST)
    )

    page = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 10, 5, 48, tzinfo=KST),
        batch_id="minute-boundary",
    )

    assert [row.time_utc for row in page.rows] == [
        datetime(2026, 9, 1, 1, 7, tzinfo=UTC)
    ]


@pytest.mark.asyncio
async def test_source_still_rejects_a_bar_beyond_the_response_minute() -> None:
    client = _Client([_candle(datetime(2026, 9, 1, 10, 8, tzinfo=KST))])
    source = TossMinuteCandleSource(
        client, clock=lambda: datetime(2026, 9, 1, 10, 6, 1, tzinfo=KST)
    )

    with pytest.raises(UnclassifiableTossMinute, match="future_minute"):
        await source.fetch(
            symbol="005930",
            retrieved_at=datetime(2026, 9, 1, 10, 5, 48, tzinfo=KST),
            batch_id="corrupt-clock",
        )


@pytest.mark.asyncio
async def test_source_rejects_only_the_unclassifiable_row() -> None:
    client = _Client(
        [
            _candle(datetime(2026, 9, 1, 7, 59, tzinfo=KST), close="100", volume="0"),
            _candle(datetime(2026, 9, 1, 10, 5, tzinfo=KST)),
        ]
    )
    source = TossMinuteCandleSource(client)

    page = await source.fetch(
        symbol="005930",
        retrieved_at=datetime(2026, 9, 1, 10, 6, tzinfo=KST),
        batch_id="partial-rejection",
    )

    assert [row.time_utc for row in page.rows] == [
        datetime(2026, 9, 1, 1, 5, tzinfo=UTC)
    ]


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
        backoff=job._RetryBackoff(),
    )
    second = await job._collect_batch(
        repository=repository,
        source=_Source({"005930": [_row(close="102")]}),
        now=tick,
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    assert first["status"] == second["status"] == "completed"
    assert first["batch_id"] == second["batch_id"] == "toss-1m-20260901T0105Z"
    assert len(repository.rows) == 1
    stored = repository.rows[(datetime(2026, 9, 1, 1, 5, tzinfo=UTC), "005930")]
    assert stored.close == Decimal("102")
    assert stored.value == Decimal("306")


@pytest.mark.asyncio
async def test_tick_selects_one_bounded_batch_of_never_collected_symbols() -> None:
    symbols = [f"{number:06d}" for number in range(25)]
    repository = _MemoryRepository(symbols)
    result = await job._collect_batch(
        repository=repository,
        source=_Source({symbol: [] for symbol in symbols}),
        now=datetime(2026, 9, 1, 10, 6, tzinfo=KST),
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    assert result["status"] == "completed"
    assert result["symbols_selected"] == 20
    assert result["empty_symbols"] == 20
    assert result["toss_calls"] == 20
    assert repository.selections == [symbols[:20]]


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
        backoff=job._RetryBackoff(),
    )

    assert result["status"] == "partial"
    assert result["symbols_succeeded"] == 1
    assert result["symbols_failed"] == 1
    assert len(repository.rows) == 1
    assert (datetime(2026, 9, 1, 1, 5, tzinfo=UTC), "000660") in repository.rows


@pytest.mark.asyncio
async def test_failed_and_skipped_minute_symbols_are_selected_on_the_next_run() -> None:
    symbols = ["000010", "000020", "000030", "000040", "000050"]
    repository = _MemoryRepository(symbols)
    backoff = job._RetryBackoff()

    def rows_for(symbol: str) -> list[TossMinuteCandleRow]:
        return [_row(symbol=symbol)]

    async def run(at: datetime, source: _Source) -> dict[str, Any]:
        return await job._collect_batch(
            repository=repository,
            source=source,
            now=at,
            batch_size=2,
            backoff=backoff,
        )

    await run(
        datetime(2026, 9, 1, 10, 0, 5, tzinfo=KST),
        _Source({"000010": rows_for("000010"), "000020": TimeoutError("toss")}),
    )
    await run(
        datetime(2026, 9, 1, 10, 1, 5, tzinfo=KST),
        _Source({symbol: rows_for(symbol) for symbol in ["000020", "000030"]}),
    )
    # The 10:02 run never happened (worker restart). Selection resumes from the
    # symbols still waiting instead of jumping to a wall-clock offset.
    await run(
        datetime(2026, 9, 1, 10, 3, 5, tzinfo=KST),
        _Source({symbol: rows_for(symbol) for symbol in ["000040", "000050"]}),
    )

    assert repository.selections == [
        ["000010", "000020"],
        ["000020", "000030"],
        ["000040", "000050"],
    ]


@pytest.mark.asyncio
async def test_symbol_that_keeps_failing_backs_off_instead_of_taking_every_slot() -> (
    None
):
    dead, live = "000001", ["000002", "000003", "000004"]
    session_bars = _minutes(datetime(2026, 9, 1, 9, 1, tzinfo=KST), 5)
    client = _HistoryClient(
        {
            dead: RuntimeError("Toss API error status=404 code='stock-not-found'"),
            **dict.fromkeys(live, session_bars),
        }
    )
    repository = _MemoryRepository([dead, *live])
    backoff = job._RetryBackoff()

    for minute in range(7):
        await job._collect_batch(
            repository=repository,
            source=_history_source(client),
            now=datetime(2026, 9, 1, 10, minute, 5, tzinfo=KST),
            batch_size=1,
            backoff=backoff,
        )

    assert repository.selections == [
        [dead],  # 10:00 first miss
        [dead],  # 10:01 retried on the next run, second miss: wait 2 minutes
        ["000002"],
        [dead],  # 10:03 third miss: wait 4 minutes
        ["000003"],
        ["000004"],
        ["000002"],  # 10:06 oldest successful fetch comes round again
    ]


@pytest.mark.asyncio
async def test_gap_beyond_the_latest_page_is_filled_with_before_cursor() -> None:
    history = _minutes(datetime(2026, 9, 1, 9, 1, tzinfo=KST), 300)  # 09:01-14:00
    stored = [
        _row(
            time_utc=at.astimezone(UTC),
            close="1",
            retrieved_at=datetime(2026, 9, 1, 0, 40, 5, tzinfo=UTC),
        )
        for at in history[:40]  # 09:01-09:40, the last bar still in progress
    ]
    client = _HistoryClient({"005930": history})
    repository = _MemoryRepository(["005930"], stored)

    result = await job._collect_batch(
        repository=repository,
        source=_history_source(client),
        now=datetime(2026, 9, 1, 14, 0, 10, tzinfo=KST),
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    assert client.calls == [
        ("005930", None),
        ("005930", "2026-09-01T10:40:00+09:00"),
    ]
    assert result["toss_calls"] == 2
    assert result["gap_fill_pages"] == 1
    assert result["unfilled_gaps"] == {}
    assert repository.stored_minutes("005930") == [at.astimezone(UTC) for at in history]
    # The bar that was in progress at 09:40 is rewritten with the final value.
    revised = repository.rows[(history[39].astimezone(UTC), "005930")]
    assert revised.close == Decimal("101")

    client.calls.clear()
    again = await job._collect_batch(
        repository=repository,
        source=_history_source(client),
        now=datetime(2026, 9, 1, 14, 1, 10, tzinfo=KST),
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    assert client.calls == [("005930", None)]
    assert again["toss_calls"] == 1
    assert again["gap_fill_pages"] == 0


@pytest.mark.asyncio
async def test_gap_wider_than_the_symbol_page_cap_is_reported_for_repair() -> None:
    history = [
        *_minutes(datetime(2026, 9, 1, 8, 1, tzinfo=KST), 720),
        *_minutes(datetime(2026, 9, 2, 8, 1, tzinfo=KST), 720),
    ]
    last_stored = history[29]  # 2026-09-01 08:30
    stored = [
        _row(
            time_utc=last_stored.astimezone(UTC),
            retrieved_at=last_stored.astimezone(UTC),
        )
    ]
    client = _HistoryClient({"005930": history})
    repository = _MemoryRepository(["005930"], stored)

    result = await job._collect_batch(
        repository=repository,
        source=_history_source(client),
        now=datetime(2026, 9, 2, 20, 0, 10, tzinfo=KST),
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    pages = 1 + job.TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES
    oldest_fetched = history[-200 * pages]
    assert result["toss_calls"] == pages
    assert result["gap_fill_pages"] == job.TOSS_MINUTE_GAP_FILL_SYMBOL_PAGES
    assert result["unfilled_gaps"] == {
        "005930": {
            "reason": "symbol_page_cap",
            "stored_through": last_stored.astimezone(UTC).isoformat(),
            "fetched_from": oldest_fetched.astimezone(UTC).isoformat(),
        }
    }
    assert repository.stored_minutes("005930") == [
        last_stored.astimezone(UTC),
        *(at.astimezone(UTC) for at in history[-200 * pages :]),
    ]


@pytest.mark.asyncio
async def test_run_page_budget_caps_toss_calls_per_minute() -> None:
    symbols = [f"{number:06d}" for number in range(1, 21)]
    history = _minutes(datetime(2026, 9, 1, 8, 1, tzinfo=KST), 600)  # 08:01-18:00
    stored = [
        _row(
            symbol=symbol,
            time_utc=history[9].astimezone(UTC),
            retrieved_at=history[9].astimezone(UTC) + timedelta(seconds=index),
        )
        for index, symbol in enumerate(symbols)
    ]
    client = _HistoryClient(dict.fromkeys(symbols, history))
    repository = _MemoryRepository(symbols, stored)

    result = await job._collect_batch(
        repository=repository,
        source=_history_source(client),
        now=datetime(2026, 9, 1, 18, 0, 10, tzinfo=KST),
        batch_size=20,
        backoff=job._RetryBackoff(),
    )

    # Each symbol needs two extra pages; the first ten use the run's budget.
    assert result["toss_calls"] == job.TOSS_MINUTE_GAP_FILL_RUN_PAGES + len(symbols)
    assert result["toss_calls"] == 40
    assert result["gap_fill_pages"] == job.TOSS_MINUTE_GAP_FILL_RUN_PAGES
    assert set(result["unfilled_gaps"]) == set(symbols[10:])
    assert {gap["reason"] for gap in result["unfilled_gaps"].values()} == {
        "run_page_budget"
    }
    assert repository.stored_minutes(symbols[0]) == [
        at.astimezone(UTC) for at in history
    ]


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stalest_active_symbols_orders_by_last_fetch_on_postgres(
    db_session: AsyncSession,
) -> None:
    fresh, older, never, inactive, out_of_window, excluded = (
        "T9M001",
        "T9M002",
        "T9M003",
        "T9M004",
        "T9M005",
        "T9M006",
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=symbol,
                name=f"toss minute {symbol}",
                exchange="KOSPI",
                is_active=symbol != inactive,
            )
            for symbol in (fresh, older, never, inactive, out_of_window, excluded)
        ]
    )
    await db_session.flush()
    bar = datetime(2026, 9, 1, 6, 30, tzinfo=UTC)  # 15:30 KST
    repository = TossMinuteCandleRepository(db_session)
    await repository.upsert(
        [
            _row(symbol=fresh, time_utc=bar - timedelta(minutes=1), retrieved_at=bar),
            # The newest bar of a closed session keeps advancing its fetch time.
            _row(symbol=fresh, time_utc=bar, retrieved_at=bar + timedelta(minutes=50)),
            _row(symbol=older, time_utc=bar, retrieved_at=bar + timedelta(minutes=5)),
            _row(symbol=excluded, time_utc=bar, retrieved_at=bar),
            _row(
                symbol=out_of_window,
                time_utc=bar - timedelta(days=30),
                retrieved_at=bar - timedelta(days=30),
            ),
        ]
    )
    # Re-delivering a bar updates it in place instead of adding a row.
    await repository.upsert(
        [_row(symbol=older, close="105", time_utc=bar, retrieved_at=bar)]
    )

    selected = await repository.stalest_active_symbols(
        limit=10_000,
        since=bar - timedelta(days=14),
        exclude={excluded},
    )

    ours = [item for item in selected if item.symbol.startswith("T9M")]
    assert [item.symbol for item in ours] == [never, out_of_window, older, fresh]
    assert ours[0].latest_time_utc is None and ours[0].retrieved_at is None
    assert ours[2] == TossMinuteCoverage(
        symbol=older, latest_time_utc=bar, retrieved_at=bar
    )
    assert ours[3] == TossMinuteCoverage(
        symbol=fresh, latest_time_utc=bar, retrieved_at=bar + timedelta(minutes=50)
    )
    stored = (
        await db_session.execute(
            select(KRTossMinuteCandle.close).where(KRTossMinuteCandle.symbol == older)
        )
    ).scalars()
    assert list(stored) == [Decimal("105")]


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


SESSION = date(2026, 9, 30)
REGULAR_0930 = _minutes(datetime(2026, 9, 30, 9, 1, tzinfo=KST), 390)  # 09:01-15:30
HOLE_0930 = [*REGULAR_0930[59:259], *REGULAR_0930[359:370]]  # 10:00-13:19, 15:00-15:10
KRX_NAMES = ["000270", "000660", "005930", "035420", "373220"]
NXT_NAMES = ["000100", "000120", "000150"]


def _day_bars(day: date, *, nxt: bool = False) -> list[datetime]:
    """Toss bars of one session: KRX names carry after-hours padding to 20:00,
    NXT names also 08:01-09:00."""

    at = datetime(day.year, day.month, day.day, 9, 1, tzinfo=KST)
    bars = [*_minutes(at, 390), *_minutes(at.replace(hour=15, minute=31), 270)]
    if nxt:
        bars = [*_minutes(at.replace(hour=8), 60), *bars]
    return bars


class _SessionRepository:
    """Stored minutes per session date, answering the repair queries."""

    def __init__(self, stored: Mapping[date, Mapping[str, list[datetime]]]) -> None:
        self.stored = {
            day: {
                symbol: {at.astimezone(UTC) for at in minutes}
                for symbol, minutes in by_symbol.items()
            }
            for day, by_symbol in stored.items()
        }
        self.upserted: list[TossMinuteCandleRow] = []

    def _bars(self, day: date, symbols: Any = None) -> list[tuple[str, str, datetime]]:
        return [
            (symbol, classify_toss_minute_segment(minute.astimezone(KST)), minute)
            for symbol, minutes in self.stored.get(day, {}).items()
            if symbols is None or symbol in symbols
            for minute in minutes
        ]

    async def neighbour_session_dates(
        self, *, session_date: date
    ) -> tuple[date | None, date | None]:
        earlier = [day for day in self.stored if day < session_date]
        later = [day for day in self.stored if day > session_date]
        return max(earlier, default=None), min(later, default=None)

    async def session_segment_counts(
        self, *, session_date: date
    ) -> list[tuple[str, str, int]]:
        counts = Counter(
            (symbol, segment) for symbol, segment, _ in self._bars(session_date)
        )
        return [(symbol, segment, n) for (symbol, segment), n in counts.items()]

    async def session_minute_presence(
        self, *, session_date: date, symbols: Any = None
    ) -> list[tuple[str, datetime, int]]:
        counts = Counter(
            (segment, minute)
            for _, segment, minute in self._bars(session_date, symbols)
        )
        return [(segment, minute, n) for (segment, minute), n in counts.items()]

    async def session_minutes(
        self, *, session_date: date, symbols: Any
    ) -> list[tuple[str, datetime]]:
        by_symbol = self.stored.get(session_date, {})
        return [
            (symbol, minute)
            for symbol in symbols
            for minute in by_symbol.get(symbol, set())
        ]

    async def upsert(self, rows: list[TossMinuteCandleRow]) -> int:
        self.upserted.extend(rows)
        for row in rows:
            by_symbol = self.stored.setdefault(row.session_date_kst, {})
            by_symbol.setdefault(row.symbol, set()).add(row.time_utc)
        return len(rows)


def _stored_sessions() -> _SessionRepository:
    days: dict[date, dict[str, list[datetime]]] = {
        day: {
            **{symbol: _day_bars(day) for symbol in KRX_NAMES},
            **{symbol: _day_bars(day, nxt=True) for symbol in NXT_NAMES},
        }
        for day in (date(2026, 9, 29), SESSION, date(2026, 10, 1))
    }
    on_day = days[SESSION]
    hole = set(HOLE_0930)
    nine = datetime(2026, 9, 30, 9, 0, tzinfo=KST)
    on_day["005930"] = [at for at in on_day["005930"] if at not in hole]
    # The whole regular session is lost; only the neighbours say it is expected.
    on_day["373220"] = [at for at in on_day["373220"] if at not in REGULAR_0930]
    # 09:00 is a grid minute for NXT names only.
    on_day["000150"] = [at for at in on_day["000150"] if at != nine]
    on_day["000270"] = [nine, *on_day["000270"]]
    return _SessionRepository(days)


def _repair_client() -> _HistoryClient:
    return _HistoryClient(
        {
            "000150": [
                *_day_bars(date(2026, 9, 29), nxt=True),
                *_day_bars(SESSION, nxt=True),
            ],
            **{
                symbol: [*_day_bars(date(2026, 9, 29)), *_day_bars(SESSION)]
                for symbol in ("005930", "373220")
            },
        }
    )


@pytest.mark.asyncio
async def test_gap_plan_finds_missing_minutes_and_backward_page_calls() -> None:
    plan = await plan_session_gaps(_stored_sessions(), session_date=SESSION)
    summary = plan.summary(top=1)

    assert summary["neighbour_session_dates"] == ["2026-09-29", "2026-10-01"]
    assert summary["symbol_classes"] == [
        {
            "segments": ["KRX_REGULAR", "NXT_POST"],
            "symbols": 5,
            "grid_minutes": {"KRX_REGULAR": 390, "NXT_POST": 270},
        },
        {
            "segments": ["KRX_REGULAR", "NXT_POST", "NXT_PRE"],
            "symbols": 3,
            "grid_minutes": {"KRX_REGULAR": 391, "NXT_POST": 270, "NXT_PRE": 59},
        },
    ]
    assert summary["symbols_expected"] == 8
    assert summary["symbols_without_rows"] == 0
    gaps = {gap.symbol: gap for gap in plan.gaps}
    assert sorted(gaps) == ["000150", "005930", "373220"]
    assert gaps["000150"].missing == (datetime(2026, 9, 30, 0, 0, tzinfo=UTC),)
    assert gaps["005930"].missing == tuple(at.astimezone(UTC) for at in HOLE_0930)
    assert gaps["005930"].runs() == 2
    # 15:10 back 200 grid minutes reaches 11:51; 11:50 then reaches 09-29.
    assert gaps["005930"].page_cursors() == [
        datetime(2026, 9, 30, 15, 10, tzinfo=KST),
        datetime(2026, 9, 30, 11, 50, tzinfo=KST),
    ]
    assert gaps["373220"].missing == tuple(at.astimezone(UTC) for at in REGULAR_0930)
    assert summary["missing_minutes"] == 1 + 211 + 390
    assert summary["estimated_calls"] == 1 + 2 + 2
    assert summary["largest_gaps"] == [
        {
            "symbol": "373220",
            "missing_minutes": 390,
            "gap_runs": 1,
            "estimated_calls": 2,
            "first_missing_kst": "2026-09-30T09:01:00+09:00",
            "last_missing_kst": "2026-09-30T15:30:00+09:00",
        }
    ]


@pytest.mark.asyncio
async def test_gap_plan_keeps_minutes_most_symbols_lost_together() -> None:
    # 2026-09-30: one outage removed the same stretch from most symbols, so the
    # date's own majority no longer had those minutes.
    lost = set(_minutes(datetime(2026, 9, 30, 10, 0, tzinfo=KST), 30))
    days = {
        day: {symbol: _day_bars(day) for symbol in KRX_NAMES}
        for day in (date(2026, 9, 29), SESSION, date(2026, 10, 1))
    }
    for symbol in KRX_NAMES[:4]:
        days[SESSION][symbol] = [at for at in days[SESSION][symbol] if at not in lost]

    plan = await plan_session_gaps(_SessionRepository(days), session_date=SESSION)

    assert {segment: len(m) for segment, m in plan.classes[0].grid.items()} == {
        "KRX_REGULAR": 390,
        "NXT_POST": 270,
    }
    assert {gap.symbol: len(gap.missing) for gap in plan.gaps} == dict.fromkeys(
        KRX_NAMES[:4], 30
    )


@pytest.mark.asyncio
async def test_repair_walks_before_cursor_and_writes_only_that_session() -> None:
    repository = _stored_sessions()
    plan = await plan_session_gaps(repository, session_date=SESSION)
    client = _repair_client()
    commits: list[int] = []

    async def commit() -> None:
        commits.append(len(repository.upserted))

    outcome = await repair_session_gaps(
        plan=plan,
        source=_history_source(client),
        repository=repository,
        commit=commit,
        max_calls=None,
        now=datetime(2026, 10, 6, 11, 0, tzinfo=UTC),
    )

    assert client.calls == [
        ("000150", "2026-09-30T09:00:00+09:00"),
        ("005930", "2026-09-30T15:10:00+09:00"),
        ("005930", "2026-09-30T11:50:00+09:00"),
        ("373220", "2026-09-30T15:30:00+09:00"),
        ("373220", "2026-09-30T12:10:00+09:00"),
    ]
    assert outcome.summary() == {
        "calls": 5,
        "rows_upserted": 60 + 370 + 390,
        "minutes_filled": 602,
        "minutes_not_returned": 0,
        "symbols_repaired": 3,
        "symbols_failed": 0,
        "failed_symbols": {},
        "stopped_at_call_cap": False,
    }
    assert commits == [60, 430, 820]
    assert {row.session_date_kst for row in repository.upserted} == {SESSION}
    assert (await plan_session_gaps(repository, session_date=SESSION)).gaps == ()


@pytest.mark.asyncio
async def test_repair_stops_at_the_call_cap_and_keeps_what_it_fetched() -> None:
    repository = _stored_sessions()
    plan = await plan_session_gaps(repository, session_date=SESSION)

    async def commit() -> None:
        return None

    outcome = await repair_session_gaps(
        plan=plan,
        source=_history_source(_repair_client()),
        repository=repository,
        commit=commit,
        max_calls=2,
    )

    assert outcome.calls == 2
    assert outcome.stopped_at_call_cap is True
    # 000150's 09:00, then 005930's 15:00-15:10 and 11:51-13:19.
    assert outcome.minutes_filled == 1 + 100
    remaining = await plan_session_gaps(repository, session_date=SESSION)
    assert {gap.symbol: len(gap.missing) for gap in remaining.gaps} == {
        "005930": 111,
        "373220": 390,
    }


@pytest.mark.asyncio
async def test_repair_cli_defaults_to_dry_run_without_toss_or_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _stored_sessions()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def commit(self) -> None:
            raise AssertionError("dry-run committed")

    def forbidden() -> TossMinuteCandleSource:
        raise AssertionError("dry-run built a Toss client")

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _Session)
    monkeypatch.setattr(
        "app.services.research_candles.toss_minute_repository."
        "TossMinuteCandleRepository",
        lambda session: repository,
    )
    monkeypatch.setattr(TossMinuteCandleSource, "from_settings", forbidden)

    args = repair_cli.parse_args(["--session-date", "2026-09-30"])
    exit_code = await repair_cli.run(args)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert args.commit is False
    assert output["mode"] == "dry-run"
    assert output["missing_minutes"] == 602
    assert output["estimated_calls"] == 5
    assert "repair" not in output
    assert repository.upserted == []
