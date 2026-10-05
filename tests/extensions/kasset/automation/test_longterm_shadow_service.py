from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.extensions.kasset.automation import longterm_shadow_service
from app.extensions.kasset.automation.longterm_shadow import (
    DEFAULT_LONGTERM_SHADOW_CONFIG,
)
from app.extensions.kasset.automation.longterm_shadow_service import (
    build_longterm_shadow_report,
    observe_longterm_shadow,
)
from app.extensions.kasset.automation.swing_shadow import bar_timestamp, decimal_text
from app.models.kasset_longterm_shadow import (
    KAssetLongtermShadowRun,
    KAssetLongtermShadowSignal,
)
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.market_events.session_calendar import trading_sessions_in_range

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_A = "980001"  # 추세·모멘텀 1위, 재무 통과
_B = "980002"  # 추세 통과, 재무 없음
_C = "980003"  # 하락 추세(평가는 되지만 신호 없음)
_PREFERRED = "980004"  # 보통주 아님(유니버스 제외)
_SHORT = "980005"  # 200세션 봉뿐(insufficient_history)
_F = "980006"  # 추세 통과, 최신 분기 공시가 S 다음 날
_SYMBOLS = (_A, _B, _C, _PREFERRED, _SHORT, _F)
_SIGNAL_SESSION = date(2026, 9, 4)  # 금요일 = ISO 주 마지막 거래일
_MIDWEEK_CLOSE = datetime(2026, 9, 2, 17, 0, tzinfo=KST)
_FRIDAY_CLOSE = datetime(2026, 9, 4, 17, 0, tzinfo=KST)
_VALUE = Decimal("5000000000")
_FORWARD_ROWS = 25
_CONFIG = DEFAULT_LONGTERM_SHADOW_CONFIG


def _price(symbol: str, index: int) -> int:
    """세션 index(0..259 = 히스토리, 259 = S, 260+ = 이후)의 종가."""

    if symbol == _A:
        return 10000 + 12 * index
    if symbol == _F:
        return 10000 + 10 * index
    if symbol == _C:
        return 20000 - 20 * index
    return 10000 + 8 * index


def _row(day: date, o: int, h: int, low: int, c: int) -> dict[str, object]:
    return {
        "time": bar_timestamp(day),
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(low),
        "close": Decimal(c),
        "volume": Decimal(100000),
        "value": _VALUE,
    }


def _history_rows(symbol: str, days: list[date], first_index: int) -> list[dict]:
    rows = []
    for offset, day in enumerate(days):
        close = _price(symbol, first_index + offset)
        rows.append(_row(day, close, close + 10, close - 10, close))
    return rows


def _forward_rows(symbol: str, sessions: list[date]) -> list[dict]:
    rows = []
    for offset, day in enumerate(sessions):
        index = 260 + offset
        opened, closed = _price(symbol, index - 1), _price(symbol, index)
        rows.append(
            _row(
                day, opened, max(opened, closed) + 10, min(opened, closed) - 10, closed
            )
        )
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


_QUARTER_ENDS = (
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
    date(2026, 6, 30),
)


async def _insert_quarters(
    session: AsyncSession, symbol: str, *, last_filing: date | None = None
) -> None:
    rows = []
    for index, end in enumerate(_QUARTER_ENDS):
        recent = index >= 4
        filing = end + timedelta(days=45)
        if last_filing is not None and index == len(_QUARTER_ENDS) - 1:
            filing = last_filing
        rows.append(
            {
                "symbol": symbol,
                "fiscal_period": f"{end.year}Q{(end.month - 1) // 3 + 1}",
                "period_end_date": end,
                "filing_date": filing,
                "revenue": Decimal(120 if recent else 100),
                "net_income": Decimal(12 if recent else 10),
            }
        )
    await session.execute(
        text(
            "INSERT INTO public.financial_fundamentals_snapshots "
            "(market, symbol, fiscal_period, period_type, period_end_date, "
            "filing_date, source, source_collected_at, discrete_revenue, "
            "discrete_net_income, data_state) "
            "VALUES ('kr', :symbol, :fiscal_period, 'quarterly', :period_end_date, "
            ":filing_date, 'dart', now(), :revenue, :net_income, 'fresh')"
        ),
        rows,
    )


_RUN_WINDOW = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC))


async def _cleanup(session: AsyncSession) -> None:
    window_runs = select(KAssetLongtermShadowRun.id).where(
        KAssetLongtermShadowRun.observed_at >= _RUN_WINDOW[0],
        KAssetLongtermShadowRun.observed_at < _RUN_WINDOW[1],
    )
    await session.execute(
        delete(KAssetLongtermShadowSignal).where(
            KAssetLongtermShadowSignal.symbol.in_(_SYMBOLS)
            | KAssetLongtermShadowSignal.run_id.in_(window_runs)
        )
    )
    await session.execute(
        delete(KAssetLongtermShadowRun).where(
            KAssetLongtermShadowRun.id.in_(window_runs)
        )
    )
    await session.execute(
        text("DELETE FROM public.kr_candles_1d WHERE symbol = ANY(:symbols)"),
        {"symbols": list(_SYMBOLS)},
    )
    await session.execute(
        text(
            "DELETE FROM public.financial_fundamentals_snapshots "
            "WHERE symbol = ANY(:symbols)"
        ),
        {"symbols": list(_SYMBOLS)},
    )
    await session.execute(
        delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(_SYMBOLS))
    )
    await session.commit()


def _forward_sessions() -> list[date]:
    return trading_sessions_in_range(
        "kr", _SIGNAL_SESSION + timedelta(days=1), _SIGNAL_SESSION + timedelta(days=60)
    )[:_FORWARD_ROWS]


def _after_close(day: date) -> datetime:
    return datetime.combine(day, time(17, 0), tzinfo=KST)


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    await _cleanup(db_session)
    days = trading_sessions_in_range("kr", date(2025, 1, 1), _SIGNAL_SESSION)[-260:]
    assert len(days) == 260 and days[-1] == _SIGNAL_SESSION
    forward = _forward_sessions()
    assert len(forward) == _FORWARD_ROWS
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=symbol,
                name=f"longterm shadow fixture {symbol}",
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
    for symbol in (_A, _B, _C, _F, _PREFERRED):
        await _insert_bars(
            db_session,
            symbol,
            _history_rows(symbol, days, 0) + _forward_rows(symbol, forward),
        )
    await _insert_bars(db_session, _SHORT, _history_rows(_B, days[-200:], 60))
    await _insert_quarters(db_session, _A)
    # F의 최신 분기(2026Q2)는 S 다음 날 공시되어 S 시점에는 알 수 없다.
    await _insert_quarters(
        db_session, _F, last_filing=_SIGNAL_SESSION + timedelta(days=1)
    )
    await db_session.commit()
    try:
        yield db_session
    finally:
        await db_session.rollback()
        await _cleanup(db_session)


async def _signals(session: AsyncSession) -> list[KAssetLongtermShadowSignal]:
    return list(
        (
            await session.scalars(
                select(KAssetLongtermShadowSignal)
                .where(KAssetLongtermShadowSignal.symbol.in_(_SYMBOLS))
                .order_by(
                    KAssetLongtermShadowSignal.candidate,
                    KAssetLongtermShadowSignal.rank,
                )
            )
        ).all()
    )


async def test_cohort_is_stored_once_on_week_end_and_midweek_or_late_runs_store_nothing(
    seeded: AsyncSession,
) -> None:
    midweek = await observe_longterm_shadow(
        seeded, now=_MIDWEEK_CLOSE, trigger_source="cli"
    )
    assert midweek["status"] == "not_applicable"
    assert midweek["reason"] == "week_incomplete"
    assert midweek["signalSession"] == "2026-09-02"
    midweek_run = await seeded.get(KAssetLongtermShadowRun, midweek["runId"])
    assert midweek_run is not None
    assert midweek_run.status == "not_applicable"
    assert midweek_run.evaluated_symbols is None
    assert await _signals(seeded) == []

    first = await observe_longterm_shadow(
        seeded, now=_FRIDAY_CLOSE, trigger_source="cli"
    )

    assert first["status"] == "completed"
    assert first["signalSession"] == "2026-09-04"
    assert first["weekComplete"] is True
    assert first["exclusions"]["insufficient_history"] == 1
    assert first["evaluatedCount"] == 4
    candidates = first["candidates"]
    assert candidates["trend_momentum"]["passedFilter"] == 3
    assert candidates["trend_momentum"]["signals"] == 3
    assert candidates["trend_momentum"]["noSignalReasons"] == {"below_sma200": 1}
    assert candidates["quality_growth_trend"]["signals"] == 1
    assert candidates["quality_growth_trend"]["noSignalReasons"] == {
        "below_sma200": 1,
        "fundamentals_insufficient_quarters": 1,
        "fundamentals_missing": 1,
    }
    run = await seeded.get(KAssetLongtermShadowRun, first["runId"])
    assert run is not None
    assert run.evaluated_symbols == [_A, _B, _C, _F]

    stored = await _signals(seeded)
    assert [(row.candidate, row.symbol, row.rank) for row in stored] == [
        ("quality_growth_trend", _A, 1),
        ("trend_momentum", _A, 1),
        ("trend_momentum", _F, 2),
        ("trend_momentum", _B, 3),
    ]
    quality_a = stored[0]
    assert quality_a.signal_close == Decimal(_price(_A, 259))
    assert quality_a.momentum_12_1 == Decimal(_price(_A, 238)) / Decimal(
        _price(_A, 7)
    ) - Decimal(1)
    assert quality_a.signal_bar_source == "toss"
    assert quality_a.observed_at == _FRIDAY_CLOSE
    fundamentals = quality_a.evidence["fundamentals"]
    assert fundamentals["ttmNetIncome"] == "48.000000"
    assert fundamentals["priorTtmNetIncome"] == "40.000000"
    assert fundamentals["netIncomeGrowth"] == "0.200000"
    assert fundamentals["quarters"][0]["fiscalPeriod"] == "2026Q2"
    assert fundamentals["quarters"][0]["filingDate"] == "2026-08-14"
    assert "fundamentals" not in stored[1].evidence
    assert stored[1].evidence["trend"]["close"] == decimal_text(
        Decimal(_price(_A, 259))
    )

    # 토요일 실제 시각 재실행: 같은 완료 세션을 다시 평가하지만 신호는 늘지 않는다.
    saturday = await observe_longterm_shadow(
        seeded, now=datetime(2026, 9, 5, 10, 0, tzinfo=KST), trigger_source="cli"
    )
    assert saturday["status"] == "completed"
    assert saturday["signalSession"] == "2026-09-04"
    assert saturday["candidates"]["trend_momentum"]["inserted"] == 0
    assert saturday["candidates"]["trend_momentum"]["duplicates"] == 3
    assert [row.id for row in await _signals(seeded)] == [row.id for row in stored]
    assert {row.run_id for row in await _signals(seeded)} == {first["runId"]}

    late = await observe_longterm_shadow(
        seeded, now=datetime(2026, 9, 7, 9, 0, tzinfo=KST), trigger_source="cli"
    )
    assert late["status"] == "rejected"
    assert late["reason"] == "late_after_next_session_open"
    late_run = await seeded.get(KAssetLongtermShadowRun, late["runId"])
    assert late_run is not None
    assert late_run.status == "rejected"
    assert late_run.signal_session_date == _SIGNAL_SESSION
    assert len(await _signals(seeded)) == 4


async def test_observer_failure_is_recorded_as_a_failed_run_without_signals(
    seeded: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_session: AsyncSession) -> list[str]:
        raise RuntimeError("universe unavailable")

    monkeypatch.setattr(longterm_shadow_service, "load_universe", broken)

    result = await observe_longterm_shadow(
        seeded, now=_FRIDAY_CLOSE, trigger_source="daily_candles_task"
    )

    assert result["status"] == "failed"
    assert result["reason"] == "RuntimeError"
    run = await seeded.get(KAssetLongtermShadowRun, result["runId"])
    assert run is not None
    assert (run.status, run.reason, run.trigger_source) == (
        "failed",
        "RuntimeError",
        "daily_candles_task",
    )
    assert await _signals(seeded) == []


def _expected_net(symbol: str, horizon: int) -> Decimal:
    entry = Decimal(_price(symbol, 259)) * (
        1 + _CONFIG.buy_fee_rate + _CONFIG.slippage_rate
    )
    exit_ = Decimal(_price(symbol, 259 + horizon)) * (
        1 - _CONFIG.sell_fee_rate - _CONFIG.sell_tax_rate - _CONFIG.slippage_rate
    )
    return (exit_ / entry - 1).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def _mean(symbols: list[str], horizon: int) -> Decimal:
    nets = [_expected_net(symbol, horizon) for symbol in symbols]
    return sum(nets, start=Decimal(0)) / len(nets)


def _mean_text(symbols: list[str], horizon: int) -> str:
    return decimal_text(_mean(symbols, horizon))


def _detail(report: dict, candidate: str, horizon: int) -> dict:
    return next(
        item
        for item in report["cohortDetails"]
        if item["candidate"] == candidate and item["horizon"] == horizon
    )


async def _observe_week(session: AsyncSession) -> None:
    await observe_longterm_shadow(session, now=_MIDWEEK_CLOSE, trigger_source="cli")
    completed = await observe_longterm_shadow(
        session, now=_FRIDAY_CLOSE, trigger_source="cli"
    )
    assert completed["status"] == "completed"


async def test_report_compares_each_cohort_with_the_evaluated_universe_benchmark(
    seeded: AsyncSession,
) -> None:
    await _observe_week(seeded)

    report = await build_longterm_shadow_report(
        seeded,
        now=_after_close(_forward_sessions()[-1]),
        since=date(2026, 9, 1),
        until=_SIGNAL_SESSION,
        include_signals=True,
        include_cohorts=True,
    )

    assert report["basis"]["orders"] == "none"
    assert report["basis"]["benchmark"] == (
        "equal_weight_all_evaluated_symbols_same_session"
    )
    assert "실제 체결·계좌 수익이 아니다" in report["basis"]["notice"]
    cohort = next(item for item in report["cohorts"] if item["isCurrentConfig"])
    coverage = cohort["coverage"]
    assert coverage["expectedSessions"] == ["2026-09-04"]
    assert coverage["missingSessions"] == []
    assert coverage["stateCounts"] == {"evaluated_with_signals": 1}
    states = {item["session"]: item["state"] for item in coverage["sessions"]}
    assert states == {
        "2026-09-02": "not_applicable",
        "2026-09-04": "evaluated_with_signals",
    }
    assert coverage["rejectedReasons"] == {}

    trend = _detail(report, "trend_momentum", 20)
    assert trend["cohort"]["state"] == "mature"
    assert trend["cohort"]["members"] == 3
    assert trend["cohort"]["meanNetReturn"] == _mean_text([_A, _F, _B], 20)
    assert trend["benchmark"]["state"] == "mature"
    assert trend["benchmark"]["members"] == 4
    assert trend["benchmark"]["universeSymbols"] == 4
    assert trend["benchmark"]["meanNetReturn"] == _mean_text([_A, _B, _C, _F], 20)
    expected_excess = _mean([_A, _F, _B], 20) - _mean([_A, _B, _C, _F], 20)
    assert trend["excessReturn"] == decimal_text(expected_excess)
    assert expected_excess > 0

    quality = _detail(report, "quality_growth_trend", 20)
    assert quality["cohort"]["members"] == 1
    assert quality["cohort"]["meanNetReturn"] == _mean_text([_A], 20)
    assert quality["benchmark"]["members"] == 4

    # 60거래일 horizon은 아직 마지막 완료 세션 뒤라 코호트·벤치마크 모두 미성숙이다.
    pending = _detail(report, "trend_momentum", 60)
    assert pending["cohort"]["state"] == "pending"
    assert pending["benchmark"]["state"] == "pending"
    assert pending["excessReturn"] is None

    horizons = cohort["candidates"]["trend_momentum"]["horizons"]
    assert horizons["20"]["matureCohorts"] == 1
    assert horizons["20"]["sampleAdvisory"] == "insufficient_sample"
    assert horizons["20"]["stats"]["positiveExcessRatio"] == "1.000000"
    assert horizons["60"]["matureCohorts"] == 0
    assert horizons["60"]["stats"] is None
    assert horizons["60"]["cohortStateCounts"] == {"pending": 1}

    detail = {
        (item["candidate"], item["symbol"]): item
        for item in report["signals"]
        if item["symbol"] in _SYMBOLS
    }
    a_signal = detail[("trend_momentum", _A)]
    assert a_signal["signalBarRevised"] is False
    assert a_signal["rank"] == 1
    assert a_signal["horizons"]["20"]["status"] == "mature"
    assert (
        a_signal["horizons"]["20"]["entrySession"] == _forward_sessions()[0].isoformat()
    )
    assert a_signal["horizons"]["60"]["status"] == "pending"


async def test_cohort_with_an_unfinished_horizon_is_excluded_from_statistics(
    seeded: AsyncSession,
) -> None:
    await _observe_week(seeded)

    report = await build_longterm_shadow_report(
        seeded,
        now=_after_close(_forward_sessions()[9]),
        since=_SIGNAL_SESSION,
        until=_SIGNAL_SESSION,
        include_cohorts=True,
    )

    cohort = next(item for item in report["cohorts"] if item["isCurrentConfig"])
    horizon = cohort["candidates"]["trend_momentum"]["horizons"]["20"]
    assert horizon["cohortStateCounts"] == {"pending": 1}
    assert horizon["matureCohorts"] == 0
    assert horizon["stats"] is None
    assert _detail(report, "trend_momentum", 20)["cohort"]["meanNetReturn"] is None


async def test_revised_signal_bar_is_counted_and_separated_without_changing_stored_value(
    seeded: AsyncSession,
) -> None:
    await _observe_week(seeded)
    await seeded.execute(
        text(
            "UPDATE public.kr_candles_1d SET close = close + 1, high = high + 1 "
            "WHERE symbol = :symbol AND time = :time"
        ),
        {"symbol": _A, "time": bar_timestamp(_SIGNAL_SESSION)},
    )
    await seeded.commit()

    report = await build_longterm_shadow_report(
        seeded,
        now=_after_close(_forward_sessions()[-1]),
        since=_SIGNAL_SESSION,
        until=_SIGNAL_SESSION,
        include_signals=True,
    )

    cohort = next(item for item in report["cohorts"] if item["isCurrentConfig"])
    quality = cohort["candidates"]["quality_growth_trend"]
    assert quality["signalBarRevised"] == 1
    # 정정된 A만 있던 quality 코호트는 정정 제외 통계에서 빈 코호트가 된다.
    assert quality["horizonsExcludingRevised"]["20"]["cohortStateCounts"] == {
        "empty": 1
    }
    assert quality["horizons"]["20"]["cohortStateCounts"] == {"mature": 1}
    assert cohort["candidates"]["trend_momentum"]["signalBarRevised"] == 1
    a_signal = next(
        item
        for item in report["signals"]
        if item["symbol"] == _A and item["candidate"] == "quality_growth_trend"
    )
    assert a_signal["signalBarRevised"] is True
    assert a_signal["signalClose"] == decimal_text(Decimal(_price(_A, 259)))
    stored_close = (await _signals(seeded))[0].signal_close
    assert stored_close == Decimal(_price(_A, 259))


async def test_report_without_any_runs_still_lists_unobserved_cohort_sessions(
    seeded: AsyncSession,
) -> None:
    report = await build_longterm_shadow_report(
        seeded,
        now=_after_close(_forward_sessions()[-1]),
        since=date(2026, 9, 1),
        until=date(2026, 9, 10),
    )

    cohorts = [item for item in report["cohorts"] if item["isCurrentConfig"]]
    assert len(cohorts) == 1
    coverage = cohorts[0]["coverage"]
    # 09-04(금)만 주 마지막 거래일이고 09-10(목)은 그 주가 끝나지 않았다.
    assert coverage["expectedSessions"] == ["2026-09-04"]
    assert coverage["missingSessions"] == ["2026-09-04"]
    assert coverage["stateCounts"] == {"not_observed": 1}
    assert cohorts[0]["candidates"]["trend_momentum"]["signals"] == 0
