from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.extensions.kasset.automation.swing_shadow import (
    DEFAULT_SWING_SHADOW_CONFIG,
    SWING_SHADOW_SCHEMA_VERSION,
    bar_timestamp,
)
from app.extensions.kasset.automation.swing_shadow_service import (
    build_swing_shadow_report,
    observe_swing_shadow,
)
from app.models.kasset_swing_shadow import (
    KAssetSwingShadowRun,
    KAssetSwingShadowSignal,
)
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.market_events.session_calendar import trading_sessions_in_range

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_BOX = "990001"
_PULLBACK = "990003"
_PREFERRED = "990004"
_STALE = "990005"
_SYMBOLS = (_BOX, _PULLBACK, _PREFERRED, _STALE)
_SIGNAL_SESSION = date(2026, 9, 4)
_FRIDAY_CLOSE = datetime(2026, 9, 4, 17, 0, tzinfo=KST)
_VALUE = Decimal("5000000000")


def _row(day: date, o: object, h: object, low: object, c: object, v: object = 100000):
    return {
        "time": bar_timestamp(day),
        "open": Decimal(str(o)),
        "high": Decimal(str(h)),
        "low": Decimal(str(low)),
        "close": Decimal(str(c)),
        "volume": Decimal(str(v)),
        "value": _VALUE,
    }


def _box_rows(days: list[date]) -> list[dict[str, object]]:
    rows = []
    for index, day in enumerate(days[:95]):
        close = 10100 if index % 2 else 9900
        rows.append(
            _row(day, 10000, max(10000, close) + 150, min(10000, close) - 150, close)
        )
    rows += [
        _row(days[95], 10300, 10900, 10250, 10800, 300000),
        _row(days[96], 10750, 10800, 10500, 10550),
        _row(days[97], 10500, 10560, 10300, 10480),
        _row(days[98], 10480, 10520, 10420, 10450),
        _row(days[99], 10460, 10700, 10440, 10650),
    ]
    return rows


def _pullback_rows(days: list[date]) -> list[dict[str, object]]:
    rows = []
    close = Decimal("10000")
    for day in days[:97]:
        close += 100
        rows.append(_row(day, close - 5, close + 10, close - 10, close))
    base = close
    rows += [
        _row(days[97], base, base, base - 500, base - 300),
        _row(days[98], base - 300, base - 150, base - 450, base - 200),
        _row(days[99], base - 150, base + 200, base - 180, base + 150, 150000),
    ]
    return rows


def _forward_rows(sessions: list[date], start: Decimal) -> list[dict[str, object]]:
    rows = []
    price = start
    for day in sessions:
        rows.append(_row(day, price + 10, price + 120, price - 60, price + 50))
        price += 50
    return rows


async def _insert_bars(
    session: AsyncSession, symbol: str, rows: list[dict[str, object]]
) -> None:
    await session.execute(
        text(
            "INSERT INTO public.kr_candles_1d "
            "(time, symbol, venue, open, high, low, close, volume, value, source) "
            "VALUES (:time, :symbol, 'KRX', :open, :high, :low, :close, :volume, "
            ":value, 'toss')"
        ),
        [{**row, "symbol": symbol} for row in rows],
    )


_RUN_WINDOW = (datetime(2026, 9, 4, tzinfo=UTC), datetime(2026, 9, 24, tzinfo=UTC))


async def _cleanup(session: AsyncSession) -> None:
    window_runs = select(KAssetSwingShadowRun.id).where(
        KAssetSwingShadowRun.observed_at >= _RUN_WINDOW[0],
        KAssetSwingShadowRun.observed_at < _RUN_WINDOW[1],
    )
    await session.execute(
        delete(KAssetSwingShadowSignal).where(
            KAssetSwingShadowSignal.symbol.in_(_SYMBOLS)
            | KAssetSwingShadowSignal.run_id.in_(window_runs)
        )
    )
    await session.execute(
        delete(KAssetSwingShadowRun).where(KAssetSwingShadowRun.id.in_(window_runs))
    )
    await session.execute(
        text("DELETE FROM public.kr_candles_1d WHERE symbol = ANY(:symbols)"),
        {"symbols": list(_SYMBOLS)},
    )
    await session.execute(
        delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(_SYMBOLS))
    )
    await session.commit()


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    await _cleanup(db_session)
    days = trading_sessions_in_range("kr", date(2026, 3, 1), _SIGNAL_SESSION)[-100:]
    assert days[-1] == _SIGNAL_SESSION
    forward = trading_sessions_in_range("kr", date(2026, 9, 5), date(2026, 9, 11))
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=symbol,
                name=f"swing shadow fixture {symbol}",
                exchange="KOSPI",
                nxt_eligible=False,
                is_active=True,
                security_type="STOCK",
                is_common_share=symbol != _PREFERRED,
            )
            for symbol in _SYMBOLS
        ]
    )
    await db_session.flush()
    box = _box_rows(days)
    await _insert_bars(db_session, _BOX, box + _forward_rows(forward, Decimal("10650")))
    pullback = _pullback_rows(days)
    await _insert_bars(
        db_session,
        _PULLBACK,
        pullback + _forward_rows(forward[:3], pullback[-1]["close"]),  # type: ignore[arg-type]
    )
    await _insert_bars(db_session, _PREFERRED, _box_rows(days))
    await _insert_bars(db_session, _STALE, _box_rows(days)[:-1])
    await db_session.commit()
    try:
        yield db_session
    finally:
        await db_session.rollback()
        await _cleanup(db_session)


async def _signals(session: AsyncSession) -> list[KAssetSwingShadowSignal]:
    return list(
        (
            await session.scalars(
                select(KAssetSwingShadowSignal)
                .where(KAssetSwingShadowSignal.symbol.in_(_SYMBOLS))
                .order_by(KAssetSwingShadowSignal.symbol)
            )
        ).all()
    )


async def test_observation_persists_once_and_rejects_after_next_open(
    seeded: AsyncSession,
) -> None:
    first = await observe_swing_shadow(seeded, now=_FRIDAY_CLOSE, trigger_source="cli")

    assert first["status"] == "completed"
    assert first["signalSession"] == "2026-09-04"
    assert first["evaluationAsOf"] == "2026-09-04T06:30:00+00:00"
    assert first["weekComplete"] is True
    assert first["exclusions"]["stale_latest_bar"] >= 1
    stored = await _signals(seeded)
    assert [(row.symbol, row.candidate) for row in stored] == [
        (_BOX, "box_breakout_retest"),
        (_PULLBACK, "uptrend_first_pullback"),
    ]
    box = stored[0]
    assert box.anchor_session_date == date(2026, 8, 31)
    assert box.signal_close == Decimal("10650")
    assert box.signal_bar_source == "toss"
    assert box.signal_bar_ingested_at is not None
    assert box.observed_at == _FRIDAY_CLOSE
    assert box.run_id == first["runId"]

    # 토요일 실제 시각 재실행: 같은 완료 세션을 다시 평가하지만 신호는 늘지 않는다.
    saturday = await observe_swing_shadow(
        seeded, now=datetime(2026, 9, 5, 10, 0, tzinfo=KST), trigger_source="cli"
    )
    assert saturday["status"] == "completed"
    assert saturday["signalSession"] == "2026-09-04"
    assert saturday["candidates"]["box_breakout_retest"]["duplicates"] >= 1
    assert [row.id for row in await _signals(seeded)] == [row.id for row in stored]
    assert {row.run_id for row in await _signals(seeded)} == {first["runId"]}

    late = await observe_swing_shadow(
        seeded, now=datetime(2026, 9, 7, 9, 0, tzinfo=KST), trigger_source="cli"
    )
    assert late["status"] == "rejected"
    assert late["reason"] == "late_after_next_session_open"
    late_run = await seeded.get(KAssetSwingShadowRun, late["runId"])
    assert late_run is not None
    assert late_run.status == "rejected"
    assert late_run.signal_session_date == _SIGNAL_SESSION
    assert len(await _signals(seeded)) == 2


async def test_report_separates_mature_pending_missing_and_revised(
    seeded: AsyncSession,
) -> None:
    await observe_swing_shadow(seeded, now=_FRIDAY_CLOSE, trigger_source="cli")
    # 관측 뒤 신호봉이 재적재돼 가격이 바뀐 경우와 적재 시각만 바뀐 경우를 만든다.
    await seeded.execute(
        text(
            "UPDATE public.kr_candles_1d SET close = 10660 "
            "WHERE symbol = :symbol AND time = :time"
        ),
        {"symbol": _BOX, "time": bar_timestamp(_SIGNAL_SESSION)},
    )
    await seeded.execute(
        text(
            "UPDATE public.kr_candles_1d SET ingested_at = now() + interval '1 hour' "
            "WHERE symbol = :symbol AND time = :time"
        ),
        {"symbol": _PULLBACK, "time": bar_timestamp(_SIGNAL_SESSION)},
    )
    await seeded.commit()

    report = await build_swing_shadow_report(
        seeded,
        now=datetime(2026, 9, 11, 17, 0, tzinfo=KST),
        since=_SIGNAL_SESSION,
        include_signals=True,
    )

    assert report["basis"]["fill"] == "virtual_next_session_open"
    assert report["basis"]["orders"] == "none"
    assert "실제 체결·계좌 수익이 아니다" in report["basis"]["notice"]
    details = {
        (item["symbol"], item["candidate"]): item
        for item in report["signals"]
        if item["symbol"] in _SYMBOLS
    }
    box = details[(_BOX, "box_breakout_retest")]
    assert box["signalBarRevised"] is True
    assert box["signalClose"] == "10650.000000"
    assert box["horizons"]["1"]["status"] == "mature"
    assert box["horizons"]["1"]["entrySession"] == "2026-09-07"
    assert box["horizons"]["1"]["entryOpen"] == "10660.000000"
    assert box["horizons"]["5"]["status"] == "mature"
    assert box["horizons"]["10"]["status"] == "pending"
    pullback = details[(_PULLBACK, "uptrend_first_pullback")]
    assert pullback["signalBarRevised"] is False
    assert pullback["horizons"]["3"]["status"] == "mature"
    assert pullback["horizons"]["5"]["status"] == "bar_missing"

    cohort = next(
        item
        for item in report["cohorts"]
        if item["candidates"]["box_breakout_retest"]["signals"] >= 1
    )
    assert "2026-09-07" in cohort["coverage"]["missingSessions"]
    assert "2026-09-04" in cohort["coverage"]["completedSessions"]
    box_summary = cohort["candidates"]["box_breakout_retest"]
    assert box_summary["signalBarRevised"] >= 1
    assert "horizonsExcludingRevised" in box_summary
    assert box_summary["horizons"]["10"]["statusCounts"]["pending"] >= 1
    assert box_summary["horizons"]["1"]["sampleAdvisory"] == "insufficient_sample"

    stored_close = await seeded.scalar(
        select(func.max(KAssetSwingShadowSignal.signal_close)).where(
            KAssetSwingShadowSignal.symbol == _BOX
        )
    )
    assert stored_close == Decimal("10650")


def _run(
    session_date: date,
    *,
    status: str = "completed",
    observed_hour: int = 17,
    evaluated: int | None = 100,
    universe: int | None = 120,
    box_signals: int = 0,
    reason: str | None = None,
) -> KAssetSwingShadowRun:
    config = DEFAULT_SWING_SHADOW_CONFIG
    observed = datetime.combine(session_date, datetime.min.time(), tzinfo=KST).replace(
        hour=observed_hour
    )
    completed = status == "completed"
    return KAssetSwingShadowRun(
        observed_at=observed,
        trigger_source="cli",
        status=status,
        reason=reason,
        signal_session_date=session_date,
        evaluation_as_of=bar_timestamp(session_date) if completed else None,
        schema_version=SWING_SHADOW_SCHEMA_VERSION,
        config_fingerprint=config.fingerprint,
        config=config.fingerprint_payload(),
        universe_count=universe if completed else None,
        evaluated_count=evaluated if completed else None,
        exclusions={"stale_latest_bar": 20} if completed else {},
        candidate_counts={
            "box_breakout_retest": {
                "signals": box_signals,
                "inserted": 0,
                "duplicates": box_signals,
                "noSignalReasons": {"no_recent_box_breakout": 7},
                "notApplicable": {},
            }
        }
        if completed
        else {},
    )


@pytest_asyncio.fixture
async def clean_runs(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    await _cleanup(db_session)
    try:
        yield db_session
    finally:
        await db_session.rollback()
        await _cleanup(db_session)


async def test_report_coverage_separates_unobserved_failed_and_zero_signal_days(
    clean_runs: AsyncSession,
) -> None:
    # 2026-09-14(월)~18(금) 다섯 거래일.
    clean_runs.add_all(
        [
            _run(date(2026, 9, 15), status="failed", reason="OperationalError"),
            _run(date(2026, 9, 16), evaluated=0, universe=20),
            _run(date(2026, 9, 17), evaluated=100, observed_hour=16),
            _run(date(2026, 9, 17), evaluated=90, observed_hour=18),
            _run(date(2026, 9, 18), evaluated=50, box_signals=1),
        ]
    )
    await clean_runs.commit()

    report = await build_swing_shadow_report(
        clean_runs,
        now=datetime(2026, 9, 18, 17, 30, tzinfo=KST),
        since=date(2026, 9, 14),
        until=date(2026, 9, 18),
    )

    cohort = next(item for item in report["cohorts"] if item["isCurrentConfig"])
    coverage = cohort["coverage"]
    states = {item["session"]: item for item in coverage["sessions"]}
    assert {day: item["state"] for day, item in states.items()} == {
        "2026-09-14": "not_observed",
        "2026-09-15": "observation_failed",
        "2026-09-16": "not_evaluated",
        "2026-09-17": "evaluated_no_signal",
        "2026-09-18": "evaluated_with_signals",
    }
    assert states["2026-09-15"]["failureReasons"] == {"OperationalError": 1}
    assert states["2026-09-16"]["exclusions"] == {"stale_latest_bar": 20}
    # 재실행은 합산하지 않고 마지막 completed run 하나만 분모로 쓴다.
    assert states["2026-09-17"]["runCount"] == 2
    assert states["2026-09-17"]["evaluatedCount"] == 90
    assert states["2026-09-17"]["candidates"]["box_breakout_retest"][
        "noSignalReasons"
    ] == {"no_recent_box_breakout": 7}
    assert coverage["missingSessions"] == ["2026-09-14", "2026-09-15"]
    assert coverage["notEvaluatedSessions"] == ["2026-09-16"]
    assert coverage["stateCounts"] == {
        "evaluated_no_signal": 1,
        "evaluated_with_signals": 1,
        "not_evaluated": 1,
        "not_observed": 1,
        "observation_failed": 1,
    }


async def test_report_without_any_runs_still_lists_unobserved_sessions(
    clean_runs: AsyncSession,
) -> None:
    report = await build_swing_shadow_report(
        clean_runs,
        now=datetime(2026, 9, 23, 17, 0, tzinfo=KST),
        since=date(2026, 9, 21),
        until=date(2026, 9, 23),
    )

    cohorts = [item for item in report["cohorts"] if item["isCurrentConfig"]]
    assert len(cohorts) == 1
    coverage = cohorts[0]["coverage"]
    assert coverage["expectedSessions"] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    assert coverage["missingSessions"] == coverage["expectedSessions"]
    assert coverage["stateCounts"] == {"not_observed": 3}
    assert cohorts[0]["candidates"]["box_breakout_retest"]["signals"] == 0
