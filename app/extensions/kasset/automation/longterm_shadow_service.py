"""KRX 장기 추세·재무성장 SHADOW 관측 저장과 코호트 성과 리포트(DB 경계).

쓰기 대상은 ``review.kasset_longterm_shadow_runs``·``review.kasset_longterm_shadow_signals``
뿐이다. 주문·추천·승격·포지션 모듈을 import하지 않는다. 일봉 로딩·대상 SQL·calendar
계획·성과 계산은 스윙 SHADOW와 같은 구현을 쓴다.

관측 계약(스윙과 동일):

* 신호 세션 ``S``는 실제 시계 기준 마지막 완료 KRX 세션(``last_final_session_kr``)이다.
  과거 날짜를 지정하는 입력은 없다.
* ``S`` 다음 세션의 정규장 시작 이후에는 기록하지 않고 ``rejected`` run만 남긴다.
* ``S``가 calendar상 그 ISO 주의 마지막 거래일일 때만 두 후보를 판정한다. 아니면
  ``not_applicable``/``week_incomplete`` run만 남긴다.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Literal

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.longterm_shadow import (
    DEFAULT_LONGTERM_SHADOW_CONFIG,
    LONGTERM_CANDIDATES,
    LONGTERM_SHADOW_MARKET,
    LONGTERM_SHADOW_REPORT_SCHEMA_VERSION,
    LONGTERM_SHADOW_SCHEMA_VERSION,
    CohortPair,
    CohortSummary,
    LongtermShadowConfig,
    LongtermSignal,
    LongtermSymbolEvaluation,
    QuarterFact,
    evaluate_longterm_symbol,
    select_cohort,
    summarize_candidate_horizon,
    summarize_cohort,
    trend_passing_symbols,
)
from app.extensions.kasset.automation.swing_shadow import (
    HorizonOutcome,
    OutcomeStatus,
    SwingBar,
    decimal_text,
    evaluate_outcome,
)
from app.extensions.kasset.automation.swing_shadow_service import (
    ObservationPlan,
    aware_utc,
    load_bars,
    load_universe,
    outcome_to_json,
    plan_observation,
    session_coverage,
)
from app.models.kasset_longterm_shadow import (
    KAssetLongtermShadowRun,
    KAssetLongtermShadowSignal,
)
from app.services.daily_candles.read_service import last_final_session_kr
from app.services.market_events.session_calendar import trading_sessions_in_range

logger = logging.getLogger(__name__)

TriggerSource = Literal["cli", "daily_candles_task"]

# 한 번에 일봉을 메모리에 올리는 종목 수. 260세션 × 종목이라 스윙(80세션)보다 작게 끊는다.
_EVAL_CHUNK = 250
_SYMBOL_CHUNK = 500
_INSERT_CHUNK = 500
_SIGNAL_IDENTITY = "uq_kasset_longterm_shadow_signals_identity"

_FACTS_SQL = text(
    """
    SELECT symbol, fiscal_period, period_end_date, filing_date, source,
           discrete_revenue, discrete_net_income
    FROM public.financial_fundamentals_snapshots
    WHERE market = 'kr'
      AND period_type = 'quarterly'
      AND data_state = 'fresh'
      AND filing_date IS NOT NULL
      AND filing_date <= :session
      AND symbol IN :symbols
    ORDER BY symbol, period_end_date DESC
    """
).bindparams(bindparam("symbols", expanding=True))

REPORT_NOTICE = (
    "주문 없는 SHADOW 관측이다. 성과는 코호트 세션 다음 유효 거래일 시가에 가상으로 "
    "진입했다고 가정한 가상 성과이며 실제 체결·계좌 수익이 아니다. 수익성 입증이 아니며 "
    "표본 부족 표기는 연구용 advisory일 뿐 매매·승격 gate가 아니다. 기간 단위는 달력일이 "
    "아니라 거래일이다. 코호트는 매주 시작하는 격자형이라 보유 기간이 겹치는 코호트 "
    "간 성과는 독립 표본이 아니다."
)

REPORT_LIMITATIONS = (
    "kr_candles_1d에는 KRX 수정주가·액면분할 계수가 없다. 260세션 안에서 세션 간 "
    "시가·종가가 직전 종가 대비 35%를 넘게 움직인 종목은 관측에서 제외하고, 성과 구간의 "
    "35% 초과 변동 표본은 price_discontinuity로 통계에서 뺀다. 그보다 작은 기업행동은 "
    "구분하지 못해 12-1 모멘텀과 성과가 왜곡될 수 있다.",
    "재무 테이블은 정정 이력을 보존하지 않는다. 신호에는 관측 당시 읽은 분기 값·공시일을 "
    "그대로 저장하지만, 과거 시점을 재현해 검증한 것은 아니다. filing_date <= S 기준이라 "
    "공시 시각(장중/장후)은 구분하지 않는다.",
    "유니버스는 관측 시점 kr_symbol_universe 보통주 정의(현재 상장 종목)라 생존 편향이 "
    "있다. 이후 상장폐지·거래정지된 종목의 성과 봉이 없으면 entry_missing/bar_missing으로 "
    "남고 통계에서 빠진다.",
    "가상 진입은 다음 세션 시가 전량 체결을 가정한다. 진입~청산 봉의 가격 양수·OHLC 정합을 "
    "먼저 검사해 어긋나면 invalid_bar, 진입 세션 거래량 0은 entry_untradable, 청산 세션 "
    "거래량 0은 exit_untradable로 통계에서 뺀다. 시가 상·하한가 잠김처럼 거래가 있었어도 "
    "체결되지 않았을 수 있는 경우는 구분하지 못한다.",
    "벤치마크는 같은 세션 관측에서 대상·품질 필터를 통과한 전 종목(evaluated_symbols)의 "
    "동일가중 평균이다. 벤치마크 구성원이 후보 코호트 종목을 포함하고, 코호트 간 보유 기간이 "
    "겹쳐 초과수익의 표준오차를 이 리포트로 판단할 수 없다.",
    "일봉은 이후 재적재될 수 있다. 관측 시점 신호봉과 현재 봉의 가격·source가 다르면 "
    "signal_bar_revised로 세고 저장된 관측 시점 값은 바꾸지 않는다.",
)


# ---- 관측 ---------------------------------------------------------------------


def _signal_row(
    signal: LongtermSignal,
    *,
    run_id: int,
    observed_at: datetime,
    evaluation_as_of: datetime,
    config_fingerprint: str,
) -> dict[str, object]:
    bar = signal.metrics.signal_bar
    return {
        "run_id": run_id,
        "candidate": signal.candidate.value,
        "market": LONGTERM_SHADOW_MARKET,
        "symbol": signal.symbol,
        "signal_session_date": signal.signal_session,
        "observed_at": observed_at,
        "evaluation_as_of": evaluation_as_of,
        "schema_version": LONGTERM_SHADOW_SCHEMA_VERSION,
        "config_fingerprint": config_fingerprint,
        "rank": signal.rank,
        "momentum_12_1": signal.momentum,
        "signal_open": bar.open,
        "signal_high": bar.high,
        "signal_low": bar.low,
        "signal_close": bar.close,
        "signal_volume": bar.volume,
        "signal_value": bar.value,
        "signal_bar_source": bar.source,
        "signal_bar_ingested_at": bar.ingested_at,
        "evidence": signal.evidence,
    }


async def _record_terminal_run(
    session: AsyncSession,
    *,
    observed_at: datetime,
    trigger_source: TriggerSource,
    status: Literal["rejected", "failed", "not_applicable"],
    reason: str,
    config: LongtermShadowConfig,
    signal_session: date | None = None,
    evaluation_as_of: datetime | None = None,
) -> int:
    run = KAssetLongtermShadowRun(
        observed_at=observed_at,
        trigger_source=trigger_source,
        status=status,
        reason=reason,
        signal_session_date=signal_session,
        evaluation_as_of=evaluation_as_of,
        schema_version=LONGTERM_SHADOW_SCHEMA_VERSION,
        config_fingerprint=config.fingerprint,
        config=config.fingerprint_payload(),
        exclusions={},
        candidate_counts={},
    )
    session.add(run)
    await session.commit()
    return run.id


async def observe_longterm_shadow(
    session: AsyncSession,
    *,
    now: datetime,
    trigger_source: TriggerSource,
    config: LongtermShadowConfig = DEFAULT_LONGTERM_SHADOW_CONFIG,
) -> dict[str, object]:
    """마지막 완료 세션이 주 마지막 거래일이면 두 후보의 상위 종목을 저장한다."""

    observed_at = aware_utc(now)
    plan = plan_observation(observed_at, config.lookback_sessions)
    if isinstance(plan, str):
        run_id = await _record_terminal_run(
            session,
            observed_at=observed_at,
            trigger_source=trigger_source,
            status="rejected",
            reason=plan,
            config=config,
            signal_session=last_final_session_kr(observed_at),
        )
        return {
            "status": "rejected",
            "reason": plan,
            "runId": run_id,
            "observedAt": observed_at.isoformat(),
        }

    try:
        if not plan.week_complete:
            run_id = await _record_terminal_run(
                session,
                observed_at=observed_at,
                trigger_source=trigger_source,
                status="not_applicable",
                reason="week_incomplete",
                config=config,
                signal_session=plan.signal_session,
                evaluation_as_of=plan.evaluation_as_of,
            )
            return {
                "status": "not_applicable",
                "reason": "week_incomplete",
                "runId": run_id,
                "signalSession": plan.signal_session.isoformat(),
                "observedAt": observed_at.isoformat(),
            }
        return await _observe_planned(
            session,
            plan=plan,
            observed_at=observed_at,
            trigger_source=trigger_source,
            config=config,
        )
    except Exception as exc:
        await session.rollback()
        logger.exception("longterm shadow observation failed")
        run_id = await _record_terminal_run(
            session,
            observed_at=observed_at,
            trigger_source=trigger_source,
            status="failed",
            reason=type(exc).__name__,
            config=config,
            signal_session=plan.signal_session,
        )
        return {
            "status": "failed",
            "reason": type(exc).__name__,
            "runId": run_id,
            "signalSession": plan.signal_session.isoformat(),
            "observedAt": observed_at.isoformat(),
        }


async def _load_quarter_facts(
    session: AsyncSession, symbols: Sequence[str], *, signal_session: date
) -> dict[str, list[QuarterFact]]:
    """``signal_session``까지 공시된 fresh 분기 행만 읽는다(미래 공시 제외)."""

    out: dict[str, list[QuarterFact]] = defaultdict(list)
    for offset in range(0, len(symbols), _SYMBOL_CHUNK):
        chunk = list(symbols[offset : offset + _SYMBOL_CHUNK])
        result = await session.execute(
            _FACTS_SQL, {"symbols": chunk, "session": signal_session}
        )
        for row in result:
            out[str(row.symbol).strip().upper()].append(
                QuarterFact(
                    fiscal_period=row.fiscal_period,
                    period_end_date=row.period_end_date,
                    filing_date=row.filing_date,
                    source=row.source,
                    discrete_revenue=Decimal(str(row.discrete_revenue))
                    if row.discrete_revenue is not None
                    else None,
                    discrete_net_income=Decimal(str(row.discrete_net_income))
                    if row.discrete_net_income is not None
                    else None,
                )
            )
    return dict(out)


async def _observe_planned(
    session: AsyncSession,
    *,
    plan: ObservationPlan,
    observed_at: datetime,
    trigger_source: TriggerSource,
    config: LongtermShadowConfig,
) -> dict[str, object]:
    universe = await load_universe(session)
    first_session = plan.calendar_sessions[-config.lookback_sessions]
    exclusions: Counter[str] = Counter()
    evaluations: list[LongtermSymbolEvaluation] = []
    for offset in range(0, len(universe), _EVAL_CHUNK):
        chunk = universe[offset : offset + _EVAL_CHUNK]
        bars = await load_bars(
            session,
            chunk,
            first_session=first_session,
            last_session=plan.signal_session,
        )
        for symbol in chunk:
            symbol_bars = bars.get(symbol)
            if not symbol_bars:
                exclusions["no_bars"] += 1
                continue
            evaluation = evaluate_longterm_symbol(
                symbol_bars,
                symbol=symbol,
                signal_session=plan.signal_session,
                calendar_sessions=plan.calendar_sessions,
                config=config,
            )
            if evaluation.excluded_reason is not None:
                exclusions[evaluation.excluded_reason] += 1
                continue
            evaluations.append(evaluation)

    facts = await _load_quarter_facts(
        session,
        trend_passing_symbols(evaluations),
        signal_session=plan.signal_session,
    )
    selections = select_cohort(
        evaluations, facts, signal_session=plan.signal_session, config=config
    )

    fingerprint = config.fingerprint
    run = KAssetLongtermShadowRun(
        observed_at=observed_at,
        trigger_source=trigger_source,
        status="completed",
        signal_session_date=plan.signal_session,
        evaluation_as_of=plan.evaluation_as_of,
        schema_version=LONGTERM_SHADOW_SCHEMA_VERSION,
        config_fingerprint=fingerprint,
        config=config.fingerprint_payload(),
        universe_count=len(universe),
        evaluated_count=len(evaluations),
        exclusions=dict(sorted(exclusions.items())),
        candidate_counts={},
        evaluated_symbols=sorted(item.symbol for item in evaluations),
    )
    session.add(run)
    await session.flush()

    inserted: Counter[str] = Counter()
    rows = [
        _signal_row(
            signal,
            run_id=run.id,
            observed_at=observed_at,
            evaluation_as_of=plan.evaluation_as_of,
            config_fingerprint=fingerprint,
        )
        for selection in selections.values()
        for signal in selection.signals
    ]
    for offset in range(0, len(rows), _INSERT_CHUNK):
        statement = (
            insert(KAssetLongtermShadowSignal)
            .values(rows[offset : offset + _INSERT_CHUNK])
            .on_conflict_do_nothing(constraint=_SIGNAL_IDENTITY)
            .returning(KAssetLongtermShadowSignal.candidate)
        )
        for candidate in (await session.execute(statement)).scalars():
            inserted[str(candidate)] += 1
    counts: dict[str, object] = {
        selection.candidate.value: {
            "passedFilter": selection.passed,
            "signals": len(selection.signals),
            "inserted": inserted[selection.candidate.value],
            "duplicates": len(selection.signals) - inserted[selection.candidate.value],
            "noSignalReasons": selection.no_signal_reasons,
            "notApplicable": {},
        }
        for selection in selections.values()
    }
    run.candidate_counts = counts
    await session.commit()
    return {
        "status": "completed",
        "runId": run.id,
        "triggerSource": trigger_source,
        "observedAt": observed_at.isoformat(),
        "signalSession": plan.signal_session.isoformat(),
        "evaluationAsOf": plan.evaluation_as_of.isoformat(),
        "nextSessionOpen": plan.next_session_open.isoformat(),
        "weekComplete": plan.week_complete,
        "configFingerprint": fingerprint,
        "universeCount": len(universe),
        "evaluatedCount": len(evaluations),
        "exclusions": dict(sorted(exclusions.items())),
        "candidates": counts,
    }


async def run_longterm_shadow_after_daily_sync() -> dict[str, object]:
    """KR 일봉 동기화 성공 뒤 한 번 실행한다. 어떤 실패도 호출자에게 던지지 않는다."""

    try:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await observe_longterm_shadow(
                session,
                now=datetime.now(UTC),
                trigger_source="daily_candles_task",
            )
    except Exception as exc:
        logger.exception("longterm shadow observer could not run after daily sync")
        return {"status": "failed", "reason": type(exc).__name__}


# ---- 리포트 -------------------------------------------------------------------


def _signal_bar_from_record(record: KAssetLongtermShadowSignal) -> SwingBar:
    return SwingBar(
        session_date=record.signal_session_date,
        open=Decimal(record.signal_open),
        high=Decimal(record.signal_high),
        low=Decimal(record.signal_low),
        close=Decimal(record.signal_close),
        volume=Decimal(record.signal_volume),
        value=Decimal(record.signal_value) if record.signal_value is not None else None,
        source=record.signal_bar_source,
        ingested_at=record.signal_bar_ingested_at,
    )


def _cohort_sessions(since: date, until: date) -> list[date]:
    """``[since, until]`` 안에서 calendar상 ISO 주의 마지막 거래일인 세션."""

    monday = since - timedelta(days=since.weekday())
    sunday = until + timedelta(days=6 - until.weekday())
    last_of_week: dict[tuple[int, int], date] = {}
    for day in trading_sessions_in_range("kr", monday, sunday):
        last_of_week[day.isocalendar()[:2]] = day
    return sorted(day for day in last_of_week.values() if since <= day <= until)


def _text_or_none(value: Decimal | None) -> str | None:
    return decimal_text(value) if value is not None else None


def _summary_json(summary: CohortSummary) -> dict[str, object]:
    return {
        "state": summary.state,
        "members": summary.members,
        "matureCount": summary.mature_count,
        "statusCounts": summary.status_counts,
        "meanNetReturn": _text_or_none(summary.mean_net),
        "meanGrossReturn": _text_or_none(summary.mean_gross),
        "meanMfe": _text_or_none(summary.mean_mfe),
        "meanMae": _text_or_none(summary.mean_mae),
    }


def _horizon_stats(
    pairs: Mapping[int, list[CohortPair]], config: LongtermShadowConfig
) -> dict[str, object]:
    return {
        str(horizon): summarize_candidate_horizon(
            pairs.get(horizon, []), min_mature_cohorts=config.min_mature_cohorts
        )
        for horizon in config.horizons
    }


def _outcome(
    by_symbol: Mapping[str, Mapping[date, SwingBar]],
    forward: Sequence[date],
    last_final: date | None,
    config: LongtermShadowConfig,
    symbol: str,
    signal_close: Decimal,
    horizon: int,
) -> HorizonOutcome:
    return evaluate_outcome(
        horizon=horizon,
        signal_close=signal_close,
        forward_sessions=forward,
        last_final_session=last_final,
        bars_by_date=by_symbol.get(symbol, {}),
        config=config,
    )


def _forward_sessions(signal_session: date, horizon: int) -> tuple[date, ...]:
    """``signal_session`` 뒤 거래일 최대 ``horizon``개.

    조회 끝이 거래소 calendar 범위(현재 + 약 1년)를 넘으면 calendar가 구간 전체를 빈
    값으로 돌려주므로(fail-closed) 필요한 만큼만 좁게 잡는다. 주 5일에 공휴일 여유를 더한
    값이다.
    """

    window_days = horizon * 7 // 5 + 45
    return tuple(
        trading_sessions_in_range(
            "kr",
            signal_session + timedelta(days=1),
            signal_session + timedelta(days=window_days),
        )[:horizon]
    )


async def _fingerprint_cohorts(
    session: AsyncSession,
    *,
    fingerprint: str,
    records: Sequence[KAssetLongtermShadowSignal],
    runs: Sequence[KAssetLongtermShadowRun],
    last_final: date | None,
    config: LongtermShadowConfig,
    include_signals: bool,
    include_cohorts: bool,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    """한 설정 지문의 후보별 코호트·벤치마크 성과. 세션 하나씩 일봉을 읽어 메모리를 묶는다."""

    benchmark_symbols: dict[date, list[str]] = {}
    for run in sorted(runs, key=lambda item: item.observed_at):
        if (
            run.status == "completed"
            and run.signal_session_date is not None
            and run.signal_session_date not in benchmark_symbols
            and run.evaluated_symbols is not None
        ):
            benchmark_symbols[run.signal_session_date] = list(run.evaluated_symbols)
    sessions = sorted(
        benchmark_symbols.keys() | {r.signal_session_date for r in records}
    )
    max_horizon = config.horizons[-1]

    pairs: dict[str, dict[int, list[CohortPair]]] = {
        item.value: defaultdict(list) for item in LONGTERM_CANDIDATES
    }
    pairs_unrevised: dict[str, dict[int, list[CohortPair]]] = {
        item.value: defaultdict(list) for item in LONGTERM_CANDIDATES
    }
    revised_counts: Counter[str] = Counter()
    missing_now: Counter[str] = Counter()
    cohort_details: list[dict[str, object]] = []
    signal_details: list[dict[str, object]] = []

    for signal_session in sessions:
        members = [r for r in records if r.signal_session_date == signal_session]
        universe = benchmark_symbols.get(signal_session)
        forward = _forward_sessions(signal_session, max_horizon)
        needed = sorted({r.symbol for r in members} | set(universe or ()))
        last_needed = (
            min(forward[-1], last_final) if forward and last_final else signal_session
        )
        bars = await load_bars(
            session,
            needed,
            first_session=signal_session,
            last_session=last_needed,
        )
        by_symbol = {
            symbol: {bar.session_date: bar for bar in items}
            for symbol, items in bars.items()
        }

        outcome = partial(_outcome, by_symbol, forward, last_final, config)

        benchmark: dict[int, CohortSummary] = {}
        for horizon in config.horizons:
            outcomes: list[HorizonOutcome] = []
            for symbol in universe or ():
                current = by_symbol.get(symbol, {}).get(signal_session)
                outcomes.append(
                    outcome(symbol, current.close, horizon)
                    if current is not None
                    else HorizonOutcome(horizon, OutcomeStatus.BAR_MISSING)
                )
            benchmark[horizon] = (
                summarize_cohort(outcomes)
                if universe is not None
                else CohortSummary("benchmark_unavailable", 0, {}, 0)
            )

        for candidate in LONGTERM_CANDIDATES:
            key = candidate.value
            cohort_records = [r for r in members if r.candidate == key]
            outcomes_by_horizon: dict[int, list[tuple[bool, HorizonOutcome]]] = (
                defaultdict(list)
            )
            for record in cohort_records:
                stored = _signal_bar_from_record(record)
                current = by_symbol.get(record.symbol, {}).get(signal_session)
                revised = (
                    current is not None and current.price_key() != stored.price_key()
                )
                if current is None:
                    missing_now[key] += 1
                if revised:
                    revised_counts[key] += 1
                per_horizon: dict[str, object] = {}
                for horizon in config.horizons:
                    item = outcome(record.symbol, stored.close, horizon)
                    outcomes_by_horizon[horizon].append((revised, item))
                    per_horizon[str(horizon)] = outcome_to_json(item)
                if include_signals:
                    signal_details.append(
                        {
                            "configFingerprint": fingerprint,
                            "candidate": key,
                            "symbol": record.symbol,
                            "signalSession": signal_session.isoformat(),
                            "rank": record.rank,
                            "momentum12m1": decimal_text(Decimal(record.momentum_12_1)),
                            "observedAt": record.observed_at.astimezone(
                                UTC
                            ).isoformat(),
                            "signalClose": decimal_text(stored.close),
                            "signalBarRevised": revised,
                            "signalBarMissingNow": current is None,
                            "evidence": record.evidence,
                            "fill": "virtual_next_session_open",
                            "horizons": per_horizon,
                        }
                    )
            for horizon in config.horizons:
                items = outcomes_by_horizon.get(horizon, [])
                cohort = summarize_cohort([item for _, item in items])
                pairs[key][horizon].append(
                    CohortPair(signal_session, cohort, benchmark[horizon])
                )
                pairs_unrevised[key][horizon].append(
                    CohortPair(
                        signal_session,
                        summarize_cohort([item for flag, item in items if not flag]),
                        benchmark[horizon],
                    )
                )
                if include_cohorts:
                    pair = pairs[key][horizon][-1]
                    cohort_details.append(
                        {
                            "configFingerprint": fingerprint,
                            "candidate": key,
                            "signalSession": signal_session.isoformat(),
                            "horizon": horizon,
                            "cohort": _summary_json(cohort),
                            "benchmark": {
                                **_summary_json(benchmark[horizon]),
                                "universeSymbols": len(universe)
                                if universe is not None
                                else None,
                            },
                            "excessReturn": _text_or_none(pair.excess),
                        }
                    )

    candidates: dict[str, object] = {}
    for candidate in LONGTERM_CANDIDATES:
        key = candidate.value
        entry: dict[str, object] = {
            "signals": sum(1 for r in records if r.candidate == key),
            "signalBarRevised": revised_counts[key],
            "signalBarMissingNow": missing_now[key],
            "horizons": _horizon_stats(pairs[key], config),
        }
        if revised_counts[key]:
            entry["horizonsExcludingRevised"] = _horizon_stats(
                pairs_unrevised[key], config
            )
        candidates[key] = entry
    return candidates, cohort_details, signal_details


async def build_longterm_shadow_report(
    session: AsyncSession,
    *,
    now: datetime,
    since: date,
    until: date | None = None,
    config: LongtermShadowConfig = DEFAULT_LONGTERM_SHADOW_CONFIG,
    include_signals: bool = False,
    include_cohorts: bool = False,
) -> dict[str, object]:
    """저장된 코호트의 20/60/120거래일 가상 성과·벤치마크·초과수익과 커버리지를 읽기 전용으로 만든다."""

    generated_at = aware_utc(now)
    last_final = last_final_session_kr(generated_at)
    end = until or last_final or since
    records = list(
        (
            await session.scalars(
                select(KAssetLongtermShadowSignal)
                .where(KAssetLongtermShadowSignal.signal_session_date >= since)
                .where(KAssetLongtermShadowSignal.signal_session_date <= end)
                .order_by(
                    KAssetLongtermShadowSignal.signal_session_date,
                    KAssetLongtermShadowSignal.candidate,
                    KAssetLongtermShadowSignal.rank,
                )
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(KAssetLongtermShadowRun)
                .where(KAssetLongtermShadowRun.signal_session_date >= since)
                .where(KAssetLongtermShadowRun.signal_session_date <= end)
                .order_by(KAssetLongtermShadowRun.observed_at)
            )
        ).all()
    )

    # 현재 설정 cohort는 run·신호가 0이어도 항상 낸다(미관측 세션을 숨기지 않는다).
    fingerprints = sorted(
        {record.config_fingerprint for record in records}
        | {run.config_fingerprint for run in runs}
        | {config.fingerprint}
    )
    expected_sessions = _cohort_sessions(
        since, min(end, last_final) if last_final else end
    )
    cohorts: list[dict[str, object]] = []
    signal_details: list[dict[str, object]] = []
    cohort_details: list[dict[str, object]] = []
    for fingerprint in fingerprints:
        fingerprint_runs = [r for r in runs if r.config_fingerprint == fingerprint]
        fingerprint_records = [
            r for r in records if r.config_fingerprint == fingerprint
        ]
        candidates, details, signals = await _fingerprint_cohorts(
            session,
            fingerprint=fingerprint,
            records=fingerprint_records,
            runs=fingerprint_runs,
            last_final=last_final,
            config=config,
            include_signals=include_signals,
            include_cohorts=include_cohorts,
        )
        cohort_details.extend(details)
        signal_details.extend(signals)
        cohorts.append(
            {
                "configFingerprint": fingerprint,
                "isCurrentConfig": fingerprint == config.fingerprint,
                "coverage": session_coverage(
                    expected_sessions,
                    fingerprint_runs,
                    fingerprint_records,
                    [item.value for item in LONGTERM_CANDIDATES],
                ),
                "candidates": candidates,
            }
        )
    report: dict[str, object] = {
        "schemaVersion": LONGTERM_SHADOW_REPORT_SCHEMA_VERSION,
        "generatedAt": generated_at.isoformat(),
        "period": {
            "since": since.isoformat(),
            "until": end.isoformat(),
            "lastFinalSession": last_final.isoformat() if last_final else None,
            "unit": "trading_sessions",
        },
        "basis": {
            "mode": "SHADOW",
            "orders": "none",
            "cohortCadence": "last_trading_session_of_iso_week",
            "fill": "virtual_next_session_open",
            "exit": "virtual_close_of_horizon_session_entry_day_is_1",
            "horizons": list(config.horizons),
            "weighting": "equal_weight_mature_members",
            "benchmark": "equal_weight_all_evaluated_symbols_same_session",
            "excessReturn": "cohort_mean_net_minus_benchmark_mean_net",
            "costs": {
                "buyFeeRate": decimal_text(config.buy_fee_rate),
                "sellFeeRate": decimal_text(config.sell_fee_rate),
                "sellTaxRate": decimal_text(config.sell_tax_rate),
                "slippageRatePerSide": decimal_text(config.slippage_rate),
            },
            "outcomeConfigFingerprint": config.fingerprint,
            "notice": REPORT_NOTICE,
        },
        "limitations": list(REPORT_LIMITATIONS),
        "cohorts": cohorts,
    }
    if include_cohorts:
        report["cohortDetails"] = cohort_details
    if include_signals:
        report["signals"] = signal_details
    return report


__all__ = [
    "REPORT_LIMITATIONS",
    "REPORT_NOTICE",
    "build_longterm_shadow_report",
    "observe_longterm_shadow",
    "run_longterm_shadow_after_daily_sync",
]
