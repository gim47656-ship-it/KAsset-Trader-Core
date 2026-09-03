from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.extensions.kasset.automation.intraday_triggers import SameTimeVolumeBaseline
from app.services.research_candles.rvol_shadow_repository import (
    RvolShadowObservation,
    RvolShadowRepository,
)
from app.services.research_candles.same_time_volume_profile import (
    load_same_time_bucket_volumes,
)

pytestmark = pytest.mark.unit


class _Rows:
    def __init__(self, rows: list[tuple[str, date, Decimal]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, date, Decimal]]:
        return self._rows


class _CaptureSession:
    def __init__(self, rows: list[tuple[str, date, Decimal]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.rows)


class _NoDatabaseSession:
    async def execute(self, statement: Any) -> None:
        raise AssertionError(f"DB에 접근하면 안 됩니다: {statement}")


def _compile_postgres(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    return " ".join(str(compiled).split()), compiled.params


@pytest.mark.asyncio
async def test_empty_requests_returns_without_database_access() -> None:
    result = await load_same_time_bucket_volumes(
        _NoDatabaseSession(),  # type: ignore[arg-type]
        requests={},
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    assert result == {}


@pytest.mark.asyncio
async def test_empty_buckets_returns_without_database_access() -> None:
    result = await load_same_time_bucket_volumes(
        _NoDatabaseSession(),  # type: ignore[arg-type]
        requests={"005930": []},
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    assert result == {}


@pytest.mark.asyncio
async def test_nonpositive_lookback_returns_without_database_access() -> None:
    result = await load_same_time_bucket_volumes(
        _NoDatabaseSession(),  # type: ignore[arg-type]
        requests={"005930": [time(9, 0)]},
        before_session_date=date(2026, 9, 3),
        lookback_days=0,
    )

    assert result == {}


@pytest.mark.asyncio
async def test_profile_uses_one_aggregate_query_and_sorts_observed_days() -> None:
    session = _CaptureSession(
        [
            ("005930", date(2026, 9, 2), Decimal("450")),
            ("000660", date(2026, 9, 1), Decimal("80")),
            ("005930", date(2026, 9, 1), Decimal("300")),
        ]
    )

    result = await load_same_time_bucket_volumes(
        session,  # type: ignore[arg-type]
        requests={
            "005930": [time(9, 0), time(9, 5), time(9, 10), time(9, 15)],
            "000660": [time(9, 0), time(9, 5), time(9, 10), time(9, 15)],
            "035420": [time(9, 0), time(9, 5), time(9, 10), time(9, 15)],
        },
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    assert len(session.statements) == 1
    compact_sql, _ = _compile_postgres(session.statements[0])
    assert "WITH recent_session_dates AS" not in compact_sql
    assert "SELECT DISTINCT" not in compact_sql
    assert "time_utc AT TIME ZONE 'Asia/Seoul'" in compact_sql
    assert "date_trunc" in compact_sql
    assert "sum(research.kr_candles_1m_toss.volume)" in compact_sql
    assert (
        "GROUP BY research.kr_candles_1m_toss.symbol, "
        "research.kr_candles_1m_toss.session_date_kst"
    ) in compact_sql
    assert "session_segment =" in compact_sql

    assert result["005930"] == [
        SameTimeVolumeBaseline(date(2026, 9, 1), Decimal("300")),
        SameTimeVolumeBaseline(date(2026, 9, 2), Decimal("450")),
    ]
    assert result["000660"] == [SameTimeVolumeBaseline(date(2026, 9, 1), Decimal("80"))]
    assert result["035420"] == []


@pytest.mark.asyncio
async def test_profile_joins_each_symbol_to_only_its_requested_buckets() -> None:
    session = _CaptureSession(
        [
            ("A", date(2026, 9, 2), Decimal("10")),
            ("B", date(2026, 9, 2), Decimal("60")),
        ]
    )

    result = await load_same_time_bucket_volumes(
        session,  # type: ignore[arg-type]
        requests={
            "A": [time(10, 10)],
            "B": [time(10, 0), time(10, 5), time(10, 10)],
        },
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    compact_sql, params = _compile_postgres(session.statements[0])
    assert "symbol = requested_buckets.symbol" in compact_sql
    assert "requested_buckets.bucket_start_kst" in compact_sql
    bound_values = list(params.values())
    assert bound_values.count("A") == 1
    assert bound_values.count("B") == 3
    assert bound_values.count(time(10, 0)) == 1
    assert bound_values.count(time(10, 5)) == 1
    assert bound_values.count(time(10, 10)) == 2
    assert result["A"] == [SameTimeVolumeBaseline(date(2026, 9, 2), Decimal("10"))]
    assert result["B"] == [SameTimeVolumeBaseline(date(2026, 9, 2), Decimal("60"))]


@pytest.mark.asyncio
async def test_profile_applies_lookback_independently_per_symbol() -> None:
    session = _CaptureSession(
        [
            ("A", date(2026, 9, 2), Decimal("30")),
            ("A", date(2026, 8, 31), Decimal("10")),
            ("B", date(2026, 8, 20), Decimal("40")),
            ("A", date(2026, 9, 1), Decimal("20")),
            ("B", date(2026, 9, 1), Decimal("50")),
        ]
    )

    result = await load_same_time_bucket_volumes(
        session,  # type: ignore[arg-type]
        requests={"A": [time(10, 10)], "B": [time(10, 10)]},
        before_session_date=date(2026, 9, 3),
        lookback_days=2,
    )

    assert result["A"] == [
        SameTimeVolumeBaseline(date(2026, 9, 1), Decimal("20")),
        SameTimeVolumeBaseline(date(2026, 9, 2), Decimal("30")),
    ]
    assert result["B"] == [
        SameTimeVolumeBaseline(date(2026, 8, 20), Decimal("40")),
        SameTimeVolumeBaseline(date(2026, 9, 1), Decimal("50")),
    ]


@pytest.mark.asyncio
async def test_profile_excludes_padding_only_days_but_keeps_real_zero_volume() -> None:
    session = _CaptureSession([("005930", date(2026, 9, 2), Decimal("0"))])

    result = await load_same_time_bucket_volumes(
        session,  # type: ignore[arg-type]
        requests={"005930": [time(10, 10)]},
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    compact_sql, _ = _compile_postgres(session.statements[0])
    assert (
        "HAVING bool_or(research.kr_candles_1m_toss.is_padding IS false)" in compact_sql
    )
    assert result["005930"] == [SameTimeVolumeBaseline(date(2026, 9, 2), Decimal("0"))]


@pytest.mark.asyncio
async def test_profile_physically_limits_the_calendar_date_range() -> None:
    session = _CaptureSession()

    await load_same_time_bucket_volumes(
        session,  # type: ignore[arg-type]
        requests={"005930": [time(10, 10)]},
        before_session_date=date(2026, 9, 3),
        lookback_days=20,
    )

    compact_sql, params = _compile_postgres(session.statements[0])
    assert "session_date_kst <" in compact_sql
    assert "session_date_kst >=" in compact_sql
    assert date(2026, 9, 3) in params.values()
    assert date(2026, 7, 10) in params.values()


@pytest.mark.asyncio
async def test_shadow_repository_empty_input_returns_without_database_access() -> None:
    repository = RvolShadowRepository(  # type: ignore[arg-type]
        _NoDatabaseSession()
    )

    assert await repository.record_many([]) == 0


@pytest.mark.asyncio
async def test_shadow_repository_inserts_observation_without_commit() -> None:
    session = _CaptureSession()
    repository = RvolShadowRepository(session)  # type: ignore[arg-type]
    row = RvolShadowObservation(
        observed_at=datetime(2026, 9, 3, 1, 12, tzinfo=UTC),
        cycle_trace_id="cycle-1",
        owner_user_id=7,
        symbol="005930",
        market="KR",
        direction="BUY",
        bucket_start_kst=time(10, 10),
        completed_bars=14,
        session_decision_status="inactive",
        session_decision_reason="relative_volume_not_satisfied",
        same_time_baseline_median_5m=Decimal("100"),
        same_time_baseline_median_20m=Decimal("400"),
        session_rvol_5m=Decimal("0.615"),
        session_status_5m="inactive",
        session_rvol_20m=Decimal("0.721"),
        session_status_20m="inactive",
        same_time_rvol_5m=None,
        same_time_status_5m="unavailable:insufficient_baseline_days",
        same_time_sample_days_5m=2,
        same_time_rvol_20m=None,
        same_time_status_20m="unavailable:insufficient_baseline_days",
        same_time_sample_days_20m=2,
    )

    assert await repository.record_many([row]) == 1
    assert len(session.statements) == 1
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    assert "INSERT INTO review.kasset_intraday_rvol_shadow" in compiled
    assert "same_time_sample_days_20m" in compiled
