"""KRX 스윙 SHADOW 관측 저장과 가상 성과 리포트(DB 경계).

쓰기 대상은 ``review.kasset_swing_shadow_runs``·``review.kasset_swing_shadow_signals``
뿐이다. 주문·추천·승격·포지션 모듈을 import하지 않는다.

관측 계약:

* 신호 세션 ``S``는 실제 시계 기준 마지막 완료 KRX 세션(``last_final_session_kr``)이다.
  과거 날짜를 지정하는 입력은 없다.
* ``S`` 다음 세션의 정규장 시작 이후에는 기록하지 않고 ``rejected`` run만 남긴다.
  가상 진입가(다음 세션 시가)가 이미 알려진 뒤의 기록은 전향 관측이 아니기 때문이다.
* 판정 시각(``evaluation_as_of``)은 ``S`` 정규장 종료 시각이고, 실제 기록 시각
  ``observed_at``은 따로 남긴다.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.extensions.kasset.automation.swing_shadow import (
    DEFAULT_SWING_SHADOW_CONFIG,
    SWING_CANDIDATES,
    SWING_SHADOW_MARKET,
    SWING_SHADOW_REPORT_SCHEMA_VERSION,
    SWING_SHADOW_SCHEMA_VERSION,
    CandidateStatus,
    HorizonOutcome,
    SwingBar,
    SwingShadowConfig,
    SwingSignal,
    bar_timestamp,
    decimal_text,
    evaluate_outcome,
    evaluate_swing_symbol,
    summarize_horizon,
)
from app.models.kasset_swing_shadow import (
    KAssetSwingShadowRun,
    KAssetSwingShadowSignal,
)
from app.services.daily_candles.read_service import last_final_session_kr
from app.services.daily_candles.sync_service import KR_COMMON_SHARE_UNIVERSE_SQL
from app.services.market_events.session_calendar import (
    next_trading_session,
    regular_session_bounds,
    trading_sessions_in_range,
)

logger = logging.getLogger(__name__)

TriggerSource = Literal["cli", "daily_candles_task"]

_SYMBOL_CHUNK = 500
_INSERT_CHUNK = 500
_SIGNAL_IDENTITY = "uq_kasset_swing_shadow_signals_identity"
# 실패가 아닌 run 상태. ``not_applicable``은 장기 SHADOW의 비코호트 세션 기록이다.
_NOT_FAILURES = ("completed", "not_applicable")

_BARS_SQL = text(
    """
    SELECT symbol, time, open, high, low, close, volume, value, source, ingested_at
    FROM public.kr_candles_1d
    WHERE venue = 'KRX'
      AND symbol IN :symbols
      AND time >= :start
      AND time < :end
    ORDER BY symbol, time
    """
).bindparams(bindparam("symbols", expanding=True))

REPORT_NOTICE = (
    "주문 없는 SHADOW 관측이다. 성과는 신호 관측 뒤 다음 유효 거래일 시가에 가상으로 "
    "진입했다고 가정한 가상 성과이며 실제 체결·계좌 수익이 아니다. 수익성 입증이 아니며 "
    "표본 부족 표기는 연구용 advisory일 뿐 매매·승격 gate가 아니다. 기간 단위는 달력일이 "
    "아니라 거래일이다."
)

REPORT_LIMITATIONS = (
    "kr_candles_1d에는 KRX 수정주가·액면분할 계수가 없어 수정 여부를 판별할 수 없다. "
    "세션 간 시가·종가가 직전 종가 대비 35%를 넘게 움직인 표본은 "
    "price_discontinuity로 성과 통계에서 뺀다(가격제한폭 30% 초과는 기업행동·자료 "
    "오류로 본다).",
    "가상 진입은 다음 세션 시가 전량 체결을 가정한다. kr_candles_1d는 NOT NULL만 "
    "보장하므로 진입~청산 봉의 가격 양수·OHLC 정합을 먼저 검사해 어긋나면 "
    "invalid_bar, 진입 세션 거래량 0은 entry_untradable, 청산 세션 거래량 0은 "
    "exit_untradable로 통계에서 뺀다. 중간 보유일 거래량 0은 보유 평가로 두고 "
    "zeroVolumeHoldingSessions로 센다. 거래가 있었더라도 시가 상·하한가 잠김이나 "
    "호가 공백으로 실제로 체결되지 않았을 수 있는 경우는 구분하지 못한다.",
    "일봉은 이후 재적재될 수 있다. 관측 시점 신호봉과 현재 봉의 가격·source가 다르면 "
    "signal_bar_revised로 세고 저장된 관측 시점 값은 바꾸지 않는다.",
    "유니버스는 관측 시점 kr_symbol_universe 보통주 정의다. 이후 상장폐지·거래정지된 "
    "종목의 성과 봉이 없으면 entry_missing/bar_missing으로 남는다.",
)


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    signal_session: date
    evaluation_as_of: datetime
    next_session_open: datetime
    week_complete: bool
    calendar_sessions: tuple[date, ...]


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def plan_observation(
    observed_at: datetime, lookback_sessions: int
) -> ObservationPlan | str:
    signal_session = last_final_session_kr(observed_at)
    if signal_session is None:
        return "no_final_session"
    bounds = regular_session_bounds("kr", signal_session)
    following = next_trading_session("kr", signal_session)
    following_bounds = (
        regular_session_bounds("kr", following) if following is not None else None
    )
    if bounds is None or following_bounds is None:
        return "calendar_unavailable"
    if observed_at >= following_bounds[0]:
        return "late_after_next_session_open"
    sessions = tuple(
        trading_sessions_in_range(
            "kr",
            signal_session - timedelta(days=lookback_sessions * 2 + 30),
            signal_session,
        )
    )
    if len(sessions) <= lookback_sessions or sessions[-1] != signal_session:
        return "calendar_unavailable"
    monday = signal_session - timedelta(days=signal_session.weekday())
    week = trading_sessions_in_range("kr", monday, monday + timedelta(days=6))
    return ObservationPlan(
        signal_session=signal_session,
        evaluation_as_of=bounds[1],
        next_session_open=following_bounds[0],
        week_complete=bool(week) and week[-1] == signal_session,
        calendar_sessions=sessions,
    )


async def load_universe(session: AsyncSession) -> list[str]:
    result = await session.execute(text(KR_COMMON_SHARE_UNIVERSE_SQL))
    symbols = [str(row.symbol).strip().upper() for row in result]
    return sorted({symbol for symbol in symbols if symbol})


def _bar_from_row(row: Any) -> SwingBar:
    timestamp: datetime = row.time
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    ingested = row.ingested_at
    if ingested is not None and ingested.tzinfo is None:
        ingested = ingested.replace(tzinfo=UTC)
    return SwingBar(
        session_date=timestamp.astimezone(KST).date(),
        open=Decimal(str(row.open)),
        high=Decimal(str(row.high)),
        low=Decimal(str(row.low)),
        close=Decimal(str(row.close)),
        volume=Decimal(str(row.volume)) if row.volume is not None else Decimal("0"),
        value=Decimal(str(row.value)) if row.value is not None else None,
        source=row.source,
        ingested_at=ingested,
    )


async def load_bars(
    session: AsyncSession,
    symbols: Sequence[str],
    *,
    first_session: date,
    last_session: date,
) -> dict[str, list[SwingBar]]:
    """``first_session``~``last_session`` KST 세션 봉을 종목별 오름차순으로 읽는다."""

    # 세션 라벨이 00:00 UTC든 00:00 KST든 같은 KST 날짜로 모이도록 하루 여유를 둔다.
    start = bar_timestamp(first_session) - timedelta(days=1)
    end = bar_timestamp(last_session) + timedelta(days=1)
    out: dict[str, list[SwingBar]] = defaultdict(list)
    for offset in range(0, len(symbols), _SYMBOL_CHUNK):
        chunk = list(symbols[offset : offset + _SYMBOL_CHUNK])
        result = await session.execute(
            _BARS_SQL, {"symbols": chunk, "start": start, "end": end}
        )
        for row in result:
            bar = _bar_from_row(row)
            if first_session <= bar.session_date <= last_session:
                out[str(row.symbol).strip().upper()].append(bar)
    return dict(out)


def _signal_row(
    signal: SwingSignal,
    *,
    run_id: int,
    observed_at: datetime,
    evaluation_as_of: datetime,
    config_fingerprint: str,
) -> dict[str, object]:
    bar = signal.signal_bar
    return {
        "run_id": run_id,
        "candidate": signal.candidate.value,
        "market": SWING_SHADOW_MARKET,
        "symbol": signal.symbol,
        "signal_session_date": signal.signal_session,
        "anchor_session_date": signal.anchor_session,
        "observed_at": observed_at,
        "evaluation_as_of": evaluation_as_of,
        "schema_version": SWING_SHADOW_SCHEMA_VERSION,
        "config_fingerprint": config_fingerprint,
        "signal_open": bar.open,
        "signal_high": bar.high,
        "signal_low": bar.low,
        "signal_close": bar.close,
        "signal_volume": bar.volume,
        "signal_value": bar.value,
        "signal_bar_source": bar.source,
        "signal_bar_ingested_at": bar.ingested_at,
        "trigger_price": signal.trigger_price,
        "stop_reference": signal.stop_reference,
        "evidence": signal.evidence,
    }


async def _record_terminal_run(
    session: AsyncSession,
    *,
    observed_at: datetime,
    trigger_source: TriggerSource,
    status: Literal["rejected", "failed"],
    reason: str,
    config: SwingShadowConfig,
    signal_session: date | None = None,
) -> int:
    run = KAssetSwingShadowRun(
        observed_at=observed_at,
        trigger_source=trigger_source,
        status=status,
        reason=reason,
        signal_session_date=signal_session,
        schema_version=SWING_SHADOW_SCHEMA_VERSION,
        config_fingerprint=config.fingerprint,
        config=config.fingerprint_payload(),
        exclusions={},
        candidate_counts={},
    )
    session.add(run)
    await session.commit()
    return run.id


async def observe_swing_shadow(
    session: AsyncSession,
    *,
    now: datetime,
    trigger_source: TriggerSource,
    config: SwingShadowConfig = DEFAULT_SWING_SHADOW_CONFIG,
) -> dict[str, object]:
    """마지막 완료 세션의 세 후보 신호를 판정해 run과 신규 신호를 저장한다."""

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
        return await _observe_planned(
            session,
            plan=plan,
            observed_at=observed_at,
            trigger_source=trigger_source,
            config=config,
        )
    except Exception as exc:
        await session.rollback()
        logger.exception("swing shadow observation failed")
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


async def _observe_planned(
    session: AsyncSession,
    *,
    plan: ObservationPlan,
    observed_at: datetime,
    trigger_source: TriggerSource,
    config: SwingShadowConfig,
) -> dict[str, object]:
    universe = await load_universe(session)
    bars = await load_bars(
        session,
        universe,
        first_session=plan.calendar_sessions[0],
        last_session=plan.signal_session,
    )
    exclusions: Counter[str] = Counter()
    found: dict[str, list[SwingSignal]] = {item.value: [] for item in SWING_CANDIDATES}
    no_signal: dict[str, Counter[str]] = {
        item.value: Counter() for item in SWING_CANDIDATES
    }
    not_applicable: dict[str, Counter[str]] = {
        item.value: Counter() for item in SWING_CANDIDATES
    }
    evaluated = 0
    for symbol in universe:
        symbol_bars = bars.get(symbol)
        if not symbol_bars:
            exclusions["no_bars"] += 1
            continue
        evaluation = evaluate_swing_symbol(
            symbol_bars,
            symbol=symbol,
            signal_session=plan.signal_session,
            calendar_sessions=plan.calendar_sessions,
            week_complete=plan.week_complete,
            evaluation_as_of=plan.evaluation_as_of,
            config=config,
        )
        if evaluation.excluded_reason is not None:
            exclusions[evaluation.excluded_reason] += 1
            continue
        evaluated += 1
        for item in evaluation.candidates:
            key = item.candidate.value
            if item.status is CandidateStatus.SIGNAL and item.signal is not None:
                found[key].append(item.signal)
            elif item.status is CandidateStatus.NOT_APPLICABLE:
                not_applicable[key][item.reason or "unspecified"] += 1
            else:
                no_signal[key][item.reason or "unspecified"] += 1

    fingerprint = config.fingerprint
    run = KAssetSwingShadowRun(
        observed_at=observed_at,
        trigger_source=trigger_source,
        status="completed",
        signal_session_date=plan.signal_session,
        evaluation_as_of=plan.evaluation_as_of,
        schema_version=SWING_SHADOW_SCHEMA_VERSION,
        config_fingerprint=fingerprint,
        config=config.fingerprint_payload(),
        universe_count=len(universe),
        evaluated_count=evaluated,
        exclusions=dict(sorted(exclusions.items())),
        candidate_counts={},
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
        for signals in found.values()
        for signal in signals
    ]
    for offset in range(0, len(rows), _INSERT_CHUNK):
        statement = (
            insert(KAssetSwingShadowSignal)
            .values(rows[offset : offset + _INSERT_CHUNK])
            .on_conflict_do_nothing(constraint=_SIGNAL_IDENTITY)
            .returning(KAssetSwingShadowSignal.candidate)
        )
        for candidate in (await session.execute(statement)).scalars():
            inserted[str(candidate)] += 1
    counts: dict[str, object] = {
        key: {
            "signals": len(found[key]),
            "inserted": inserted[key],
            "duplicates": len(found[key]) - inserted[key],
            "noSignalReasons": dict(sorted(no_signal[key].items())),
            "notApplicable": dict(sorted(not_applicable[key].items())),
        }
        for key in found
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
        "evaluatedCount": evaluated,
        "exclusions": dict(sorted(exclusions.items())),
        "candidates": counts,
    }


async def run_swing_shadow_after_daily_sync() -> dict[str, object]:
    """KR 일봉 동기화 성공 뒤 한 번 실행한다. 어떤 실패도 호출자에게 던지지 않는다."""

    try:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await observe_swing_shadow(
                session,
                now=datetime.now(UTC),
                trigger_source="daily_candles_task",
            )
    except Exception as exc:
        logger.exception("swing shadow observer could not run after daily sync")
        return {"status": "failed", "reason": type(exc).__name__}


def _signal_bar_from_record(record: KAssetSwingShadowSignal) -> SwingBar:
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


def outcome_to_json(outcome: HorizonOutcome) -> dict[str, object]:
    def text_or_none(value: Decimal | None) -> str | None:
        return decimal_text(value) if value is not None else None

    return {
        "status": outcome.status.value,
        "entrySession": outcome.entry_session.isoformat()
        if outcome.entry_session
        else None,
        "exitSession": outcome.exit_session.isoformat()
        if outcome.exit_session
        else None,
        "entryOpen": text_or_none(outcome.entry_open),
        "exitClose": text_or_none(outcome.exit_close),
        "grossReturn": text_or_none(outcome.gross_return),
        "netReturn": text_or_none(outcome.net_return),
        "mfe": text_or_none(outcome.mfe),
        "mae": text_or_none(outcome.mae),
        "zeroVolumeHoldingSessions": outcome.zero_volume_holding_sessions,
    }


def _horizon_summaries(
    outcomes: Mapping[int, list[HorizonOutcome]], config: SwingShadowConfig
) -> dict[str, object]:
    return {
        str(horizon): summarize_horizon(
            outcomes.get(horizon, []), min_mature_sample=config.min_mature_sample
        )
        for horizon in config.horizons
    }


class CoverageRun(Protocol):
    """``session_coverage``가 읽는 run 필드. 스윙·장기 run 모델이 만족한다."""

    @property
    def id(self) -> int: ...
    @property
    def status(self) -> str: ...
    @property
    def reason(self) -> str | None: ...
    @property
    def signal_session_date(self) -> date | None: ...
    @property
    def observed_at(self) -> datetime: ...
    @property
    def universe_count(self) -> int | None: ...
    @property
    def evaluated_count(self) -> int | None: ...
    @property
    def exclusions(self) -> dict[str, object]: ...
    @property
    def candidate_counts(self) -> dict[str, object]: ...


class CoverageRecord(Protocol):
    @property
    def signal_session_date(self) -> date: ...
    @property
    def candidate(self) -> str: ...


def _candidate_counts(run: CoverageRun, key: str) -> Mapping[str, object]:
    value = run.candidate_counts.get(key) if run.candidate_counts else None
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def session_coverage(
    expected_sessions: Sequence[date],
    runs: Sequence[CoverageRun],
    records: Sequence[CoverageRecord],
    candidate_keys: Sequence[str],
) -> dict[str, object]:
    """조회 기간 거래일마다 관측 상태를 하나로 정한다.

    같은 세션의 재실행은 합산하지 않고 마지막 ``completed`` run 하나를 대표로 쓴다.
    상태: ``not_observed``(run 없음), ``observation_failed``(rejected/failed만 있음),
    ``not_evaluated``(completed지만 평가 종목 0), ``evaluated_no_signal``,
    ``evaluated_with_signals``(대표 run이 그날 본 신호 > 0; 이전 세션과 같은 anchor라
    새로 저장되지 않은 중복 포함).
    """

    by_session: dict[date, list[CoverageRun]] = defaultdict(list)
    for run in runs:
        if run.signal_session_date is not None:
            by_session[run.signal_session_date].append(run)
    stored: dict[date, Counter[str]] = defaultdict(Counter)
    for record in records:
        stored[record.signal_session_date][record.candidate] += 1
    expected = set(expected_sessions)
    days = sorted(expected | set(by_session))
    state_counts: Counter[str] = Counter()
    sessions: list[dict[str, object]] = []
    for day in days:
        day_runs = sorted(by_session.get(day, []), key=lambda item: item.observed_at)
        completed = [run for run in day_runs if run.status == "completed"]
        entry: dict[str, object] = {
            "session": day.isoformat(),
            "expected": day in expected,
            "runCount": len(day_runs),
            "runStatusCounts": dict(
                sorted(Counter(r.status for r in day_runs).items())
            ),
            "failureReasons": dict(
                sorted(
                    Counter(
                        run.reason or "unspecified"
                        for run in day_runs
                        if run.status not in _NOT_FAILURES
                    ).items()
                )
            ),
        }
        if not day_runs:
            state = "not_observed"
        elif not completed and all(run.status == "not_applicable" for run in day_runs):
            state = "not_applicable"
        elif not completed:
            state = "observation_failed"
        else:
            representative = completed[-1]
            candidates: dict[str, object] = {}
            observed_signals = 0
            for key in candidate_keys:
                counts = _candidate_counts(representative, key)
                signals = _int(counts.get("signals"))
                observed_signals += signals
                candidates[key] = {
                    "signalsObserved": signals,
                    "storedNewSignals": stored[day][key],
                    "noSignalReasons": counts.get("noSignalReasons", {}),
                    "notApplicable": counts.get("notApplicable", {}),
                }
            evaluated = representative.evaluated_count or 0
            if evaluated == 0:
                state = "not_evaluated"
            elif observed_signals == 0:
                state = "evaluated_no_signal"
            else:
                state = "evaluated_with_signals"
            entry.update(
                {
                    "representativeRunId": representative.id,
                    "representativeObservedAt": representative.observed_at.astimezone(
                        UTC
                    ).isoformat(),
                    "universeCount": representative.universe_count,
                    "evaluatedCount": evaluated,
                    "exclusions": representative.exclusions,
                    "candidates": candidates,
                }
            )
        entry["state"] = state
        if day in expected:
            state_counts[state] += 1
        sessions.append(entry)
    observed_ok = {
        "not_evaluated",
        "evaluated_no_signal",
        "evaluated_with_signals",
    }
    return {
        "expectedSessions": [day.isoformat() for day in expected_sessions],
        "completedSessions": [
            str(item["session"]) for item in sessions if item["state"] in observed_ok
        ],
        "missingSessions": [
            str(item["session"])
            for item in sessions
            if item["expected"] and item["state"] not in observed_ok
        ],
        "notEvaluatedSessions": [
            str(item["session"])
            for item in sessions
            if item["state"] == "not_evaluated"
        ],
        "stateCounts": dict(sorted(state_counts.items())),
        "runStatusCounts": dict(sorted(Counter(run.status for run in runs).items())),
        "rejectedReasons": dict(
            sorted(
                Counter(
                    run.reason or "unspecified"
                    for run in runs
                    if run.status not in _NOT_FAILURES
                ).items()
            )
        ),
        "sessions": sessions,
    }


async def build_swing_shadow_report(
    session: AsyncSession,
    *,
    now: datetime,
    since: date,
    until: date | None = None,
    config: SwingShadowConfig = DEFAULT_SWING_SHADOW_CONFIG,
    include_signals: bool = False,
) -> dict[str, object]:
    """저장된 신호의 1/3/5/10거래일 가상 성과와 관측 커버리지를 읽기 전용으로 만든다."""

    generated_at = aware_utc(now)
    last_final = last_final_session_kr(generated_at)
    end = until or last_final or since
    records = list(
        (
            await session.scalars(
                select(KAssetSwingShadowSignal)
                .where(KAssetSwingShadowSignal.signal_session_date >= since)
                .where(KAssetSwingShadowSignal.signal_session_date <= end)
                .order_by(
                    KAssetSwingShadowSignal.signal_session_date,
                    KAssetSwingShadowSignal.candidate,
                    KAssetSwingShadowSignal.symbol,
                )
            )
        ).all()
    )
    runs = list(
        (
            await session.scalars(
                select(KAssetSwingShadowRun)
                .where(KAssetSwingShadowRun.signal_session_date >= since)
                .where(KAssetSwingShadowRun.signal_session_date <= end)
                .order_by(KAssetSwingShadowRun.observed_at)
            )
        ).all()
    )
    max_horizon = config.horizons[-1]
    forward: dict[date, tuple[date, ...]] = {}
    for session_date in {record.signal_session_date for record in records}:
        forward[session_date] = tuple(
            trading_sessions_in_range(
                "kr",
                session_date + timedelta(days=1),
                session_date + timedelta(days=max_horizon * 3 + 20),
            )[:max_horizon]
        )
    bars: dict[str, list[SwingBar]] = {}
    if records:
        last_needed = max(
            [record.signal_session_date for record in records]
            + [day for days in forward.values() for day in days]
        )
        bars = await load_bars(
            session,
            sorted({record.symbol for record in records}),
            first_session=min(record.signal_session_date for record in records),
            last_session=last_needed,
        )

    # 현재 설정 cohort는 run·신호가 0이어도 항상 낸다(미관측 세션을 숨기지 않는다).
    fingerprints = sorted(
        {record.config_fingerprint for record in records}
        | {run.config_fingerprint for run in runs}
        | {config.fingerprint}
    )
    expected_sessions = trading_sessions_in_range(
        "kr", since, min(end, last_final) if last_final else end
    )
    cohorts: list[dict[str, object]] = []
    signal_details: list[dict[str, object]] = []
    for fingerprint in fingerprints:
        cohort_runs = [run for run in runs if run.config_fingerprint == fingerprint]
        candidates: dict[str, object] = {}
        for candidate in SWING_CANDIDATES:
            cohort_records = [
                record
                for record in records
                if record.config_fingerprint == fingerprint
                and record.candidate == candidate.value
            ]
            outcomes: dict[int, list[HorizonOutcome]] = defaultdict(list)
            unrevised: dict[int, list[HorizonOutcome]] = defaultdict(list)
            revised_count = 0
            current_missing = 0
            for record in cohort_records:
                stored = _signal_bar_from_record(record)
                symbol_bars = {
                    bar.session_date: bar for bar in bars.get(record.symbol, [])
                }
                current = symbol_bars.get(record.signal_session_date)
                revised = (
                    current is not None and current.price_key() != stored.price_key()
                )
                if current is None:
                    current_missing += 1
                if revised:
                    revised_count += 1
                per_horizon: dict[str, object] = {}
                for horizon in config.horizons:
                    outcome = evaluate_outcome(
                        horizon=horizon,
                        signal_close=stored.close,
                        forward_sessions=forward[record.signal_session_date],
                        last_final_session=last_final,
                        bars_by_date=symbol_bars,
                        config=config,
                    )
                    outcomes[horizon].append(outcome)
                    if not revised:
                        unrevised[horizon].append(outcome)
                    per_horizon[str(horizon)] = outcome_to_json(outcome)
                if include_signals:
                    signal_details.append(
                        {
                            "configFingerprint": fingerprint,
                            "candidate": record.candidate,
                            "symbol": record.symbol,
                            "signalSession": record.signal_session_date.isoformat(),
                            "anchorSession": record.anchor_session_date.isoformat(),
                            "observedAt": record.observed_at.astimezone(
                                UTC
                            ).isoformat(),
                            "signalClose": decimal_text(stored.close),
                            "triggerPrice": decimal_text(Decimal(record.trigger_price)),
                            "signalBarRevised": revised,
                            "signalBarMissingNow": current is None,
                            "fill": "virtual_next_session_open",
                            "horizons": per_horizon,
                        }
                    )
            entry: dict[str, object] = {
                "signals": len(cohort_records),
                "signalBarRevised": revised_count,
                "signalBarMissingNow": current_missing,
                "horizons": _horizon_summaries(outcomes, config),
            }
            if revised_count:
                entry["horizonsExcludingRevised"] = _horizon_summaries(
                    unrevised, config
                )
            candidates[candidate.value] = entry
        cohorts.append(
            {
                "configFingerprint": fingerprint,
                "isCurrentConfig": fingerprint == config.fingerprint,
                "coverage": session_coverage(
                    expected_sessions,
                    cohort_runs,
                    [
                        record
                        for record in records
                        if record.config_fingerprint == fingerprint
                    ],
                    [item.value for item in SWING_CANDIDATES],
                ),
                "candidates": candidates,
            }
        )
    report: dict[str, object] = {
        "schemaVersion": SWING_SHADOW_REPORT_SCHEMA_VERSION,
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
            "fill": "virtual_next_session_open",
            "exit": "virtual_close_of_horizon_session_entry_day_is_1",
            "horizons": list(config.horizons),
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
    if include_signals:
        report["signals"] = signal_details
    return report


__all__ = [
    "REPORT_LIMITATIONS",
    "REPORT_NOTICE",
    "CoverageRecord",
    "CoverageRun",
    "ObservationPlan",
    "aware_utc",
    "build_swing_shadow_report",
    "load_bars",
    "load_universe",
    "observe_swing_shadow",
    "outcome_to_json",
    "plan_observation",
    "run_swing_shadow_after_daily_sync",
    "session_coverage",
]
