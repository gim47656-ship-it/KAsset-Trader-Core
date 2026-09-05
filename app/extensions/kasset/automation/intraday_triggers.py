"""완료된 정규장 장중 bar만 쓰는 순수 진입 방아쇠 계산기와 정책.

계산기는 전부 순수 함수다. 공급자 호출, 세션 조회, DB 접근은
:mod:`app.extensions.kasset.automation.intraday_data`가 맡고 이 모듈은 이미
"완료·정규장·검증됨"으로 좁혀진 bar만 받는다.

동시간대 상대거래량은 과거 거래일의 같은 시각 기준선과 비교하며, 같은 세션
내부의 직전 bar를 기준선으로 삼는 기존 상대거래량과 의미가 다르다.

fail-closed 규칙은 하나다: 값을 만들 수 없으면 값을 추정하지 않고
``UNAVAILABLE``과 그 사유를 남긴다. 가격/거래량 bar가 stale이거나 부분
bar이면 개별 trigger가 아니라 판정 전체가 막힌다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final, Literal

from app.extensions.kasset.automation.contracts import Action, PriceBar

INTRADAY_TRIGGER_SCHEMA_VERSION: Final = "kasset.intraday-triggers.v1"
NO_CHASE_SCHEMA_VERSION: Final = "kasset.no-chase.v1"

#: Trigger 이름. 감사 원장과 정책이 같은 문자열을 쓴다.
OPENING_RANGE_BREAKOUT: Final = "opening_range_breakout"
SESSION_VWAP_RECLAIM: Final = "session_vwap_reclaim"
RELATIVE_VOLUME_5M: Final = "relative_volume_5m"
RELATIVE_VOLUME_20M: Final = "relative_volume_20m"
SAME_TIME_RELATIVE_VOLUME_5M: Final = "same_time_relative_volume_5m"
SAME_TIME_RELATIVE_VOLUME_20M: Final = "same_time_relative_volume_20m"
INTRADAY_RELATIVE_STRENGTH: Final = "intraday_relative_strength"

#: 지수 분봉을 공용 경로에서 실제로 받지 못했을 때의 사유 코드.
INDEX_INTRADAY_UNAVAILABLE: Final = "index_intraday_unavailable"

_ZERO = Decimal("0")
_QUANTUM = Decimal("0.000001")
_THREE = Decimal("3")


class TriggerStatus(StrEnum):
    """Trigger 하나의 닫힌 상태 집합."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class TriggerDecisionStatus(StrEnum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TriggerResult:
    """Trigger 하나의 상태, 값, 임계값, 출처, 관측 시각."""

    code: str
    status: TriggerStatus
    value: str | None
    threshold: str | None
    source: str | None
    as_of: datetime | None
    detail: str
    unavailable_reason: str | None = None
    blocked_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.status is TriggerStatus.ACTIVE

    @property
    def available(self) -> bool:
        return self.status is not TriggerStatus.UNAVAILABLE

    def as_evidence(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": self.status.value,
            "value": self.value,
            "threshold": self.threshold,
            "source": self.source,
            "asOf": _timestamp_text(self.as_of),
            "detail": self.detail,
            "unavailableReason": self.unavailable_reason,
            "blockedReason": self.blocked_reason,
        }


@dataclass(frozen=True, slots=True)
class SameTimeVolumeBaseline:
    """과거 한 거래일의 동시간대 완료 구간 거래량."""

    session_date: date
    volume: Decimal


def same_time_baseline_median(
    baseline: Sequence[SameTimeVolumeBaseline],
) -> Decimal | None:
    """과거 거래일의 동시간대 거래량 중앙값을 계산한다."""

    volumes: list[Decimal] = []
    seen_dates: set[date] = set()
    for sample in baseline:
        if sample.session_date in seen_dates:
            raise ValueError(
                "duplicate same-time baseline session_date: "
                f"{sample.session_date.isoformat()}"
            )
        seen_dates.add(sample.session_date)
        if sample.volume < _ZERO:
            raise ValueError(
                "same-time baseline volume must not be negative: "
                f"{sample.session_date.isoformat()}={sample.volume}"
            )
        volumes.append(sample.volume)

    if not volumes:
        return None
    ordered = sorted(volumes)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


@dataclass(frozen=True, slots=True)
class IntradayTriggerConfig:
    """장중 추격매수 방지 임계값."""

    pivot_buffer_ratio: Decimal = Decimal("0.002")
    max_extension_ratio: Decimal = Decimal("0.02")
    gap_up_atr_multiple: Decimal = Decimal("1.0")
    gap_up_min_ratio: Decimal = Decimal("0.03")
    decision_ttl: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        for field_name in (
            "pivot_buffer_ratio",
            "max_extension_ratio",
            "gap_up_atr_multiple",
            "gap_up_min_ratio",
        ):
            if getattr(self, field_name) < _ZERO:
                raise ValueError(f"{field_name} must not be negative")
        if self.max_extension_ratio < self.pivot_buffer_ratio:
            raise ValueError("max_extension_ratio must be at least pivot_buffer_ratio")
        if self.decision_ttl <= timedelta(0):
            raise ValueError("decision_ttl must be positive")


DEFAULT_INTRADAY_TRIGGER_CONFIG = IntradayTriggerConfig()


@dataclass(frozen=True, slots=True)
class _NoChaseEvidence:
    pivot_buffer_ratio: Decimal
    max_extension_ratio: Decimal
    session_open_price: Decimal | None
    previous_close: Decimal | None
    atr_14: Decimal | None
    gap_up_blocked: bool
    gap_up_unavailable: str | None

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": NO_CHASE_SCHEMA_VERSION,
            "pivotBuffer": _decimal_text(self.pivot_buffer_ratio),
            "maxExtension": _decimal_text(self.max_extension_ratio),
            "gapUp": {
                "open": _optional_decimal_text(self.session_open_price),
                "prevClose": _optional_decimal_text(self.previous_close),
                "atr14": _optional_decimal_text(self.atr_14),
                "blocked": self.gap_up_blocked,
                "unavailable": self.gap_up_unavailable,
            },
        }


@dataclass(frozen=True, slots=True)
class IntradayTriggerPolicy:
    """진입에 필요한 trigger 조합을 이름으로 못 박은 정책.

    - ``directional``: 이 중 하나 이상이 ACTIVE여야 한다(ORB 또는 VWAP 회복/붕괴).
    - ``volume``: 이 중 하나 이상이 ACTIVE여야 한다(완료 bar 상대거래량).
    - ``directional_confirmation``: available이면 반드시 같은 방향이어야 하고,
      UNAVAILABLE이면 나머지 trigger를 막지 않되 근거에 사유를 남긴다.
    """

    directional: tuple[str, ...] = (OPENING_RANGE_BREAKOUT, SESSION_VWAP_RECLAIM)
    volume: tuple[str, ...] = (RELATIVE_VOLUME_5M, RELATIVE_VOLUME_20M)
    directional_confirmation: tuple[str, ...] = (INTRADAY_RELATIVE_STRENGTH,)

    def __post_init__(self) -> None:
        for field_name in ("directional", "volume"):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} triggers must not be empty")
        names = (*self.directional, *self.volume, *self.directional_confirmation)
        if len(set(names)) != len(names):
            raise ValueError("a trigger cannot appear in two policy groups")

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": INTRADAY_TRIGGER_SCHEMA_VERSION,
            "directionalAnyOf": list(self.directional),
            "volumeAnyOf": list(self.volume),
            "directionalConfirmationIfAvailable": list(self.directional_confirmation),
            "rule": (
                "(any directional trigger) AND (any completed-bar relative volume "
                "trigger) AND (every available confirmation trigger agrees)"
            ),
        }


DEFAULT_INTRADAY_TRIGGER_POLICY = IntradayTriggerPolicy()


@dataclass(frozen=True, slots=True)
class IntradayTriggerDecision:
    """한 후보·한 방향의 장중 방아쇠 판정."""

    schema_version: str
    symbol: str
    market: Literal["KRX", "US"]
    direction: Action
    status: TriggerDecisionStatus
    triggers: tuple[TriggerResult, ...]
    policy: IntradayTriggerPolicy
    evaluated_at: datetime
    valid_until: datetime
    data_as_of: datetime | None
    blocked_reason: str | None
    no_chase: _NoChaseEvidence

    @property
    def triggered(self) -> bool:
        return self.status is TriggerDecisionStatus.TRIGGERED

    def is_expired(self, now: datetime) -> bool:
        return _aware_utc(now, "now") >= self.valid_until

    def expire(self, now: datetime) -> IntradayTriggerDecision:
        """소비 시점에 TTL이 지난 활성 판정을 비활성 판정으로 바꾼다."""

        if not self.triggered or not self.is_expired(now):
            return self
        return replace(
            self,
            status=TriggerDecisionStatus.NOT_TRIGGERED,
            blocked_reason="expired",
        )

    def as_evidence(self) -> dict[str, object]:
        return {
            "title": "Completed intraday entry triggers",
            "source": "kasset_intraday_triggers",
            "kind": "intraday_triggers",
            "schemaVersion": self.schema_version,
            "symbol": self.symbol,
            "market": self.market,
            "direction": self.direction.value,
            "status": self.status.value,
            "evaluatedAt": _timestamp_text(self.evaluated_at),
            "validUntil": _timestamp_text(self.valid_until),
            "dataAsOf": _timestamp_text(self.data_as_of),
            "blockedReason": self.blocked_reason,
            "policy": self.policy.as_evidence(),
            "triggers": [item.as_evidence() for item in self.triggers],
            "noChase": self.no_chase.as_evidence(),
        }

    def compact_reason(self) -> str:
        """감사 원장의 짧은 사유 칸에 넣을 trigger별 상태 요약."""

        states = ",".join(f"{item.code}={item.status.value}" for item in self.triggers)
        return f"{self.status.value}[{states}]"[:128]


def opening_range_breakout(
    bars: Sequence[PriceBar],
    *,
    direction: Action,
    session_open: datetime,
    opening_range: timedelta,
    bar_interval: timedelta,
    source: str,
    config: IntradayTriggerConfig = DEFAULT_INTRADAY_TRIGGER_CONFIG,
) -> TriggerResult:
    """개장 구간 고/저를 완료 bar 종가가 방향대로 깼는가."""

    ordered = _ordered(bars)
    if not ordered:
        return _unavailable(
            OPENING_RANGE_BREAKOUT,
            reason="no_completed_session_bars",
            detail="the regular session produced no completed bars yet",
            source=source,
        )
    boundary = _aware_utc(session_open, "session_open") + opening_range
    opening = tuple(
        bar
        for bar in ordered
        if _aware_utc(bar.timestamp, "bar.timestamp") + bar_interval <= boundary
    )
    if not opening:
        return _unavailable(
            OPENING_RANGE_BREAKOUT,
            reason="opening_range_incomplete",
            detail=(
                "no completed bar closed inside the opening range ending at "
                f"{_timestamp_text(boundary)}"
            ),
            source=source,
        )
    after = tuple(
        bar
        for bar in ordered
        if _aware_utc(bar.timestamp, "bar.timestamp") + bar_interval > boundary
    )
    if not after:
        return _unavailable(
            OPENING_RANGE_BREAKOUT,
            reason="no_completed_bar_after_opening_range",
            detail="the opening range has not been followed by a completed bar",
            source=source,
        )
    range_high = max(bar.high for bar in opening)
    range_low = min(bar.low for bar in opening)
    latest = after[-1]
    basis = range_high if direction is Action.BUY else range_low
    pivot_threshold = (
        basis * (Decimal("1") + config.pivot_buffer_ratio)
        if direction is Action.BUY
        else basis * (Decimal("1") - config.pivot_buffer_ratio)
    )
    extension_threshold = (
        basis * (Decimal("1") + config.max_extension_ratio)
        if direction is Action.BUY
        else basis * (Decimal("1") - config.max_extension_ratio)
    )
    too_extended = (
        latest.close > extension_threshold
        if direction is Action.BUY
        else latest.close < extension_threshold
    )
    broke = (
        latest.close >= pivot_threshold
        if direction is Action.BUY
        else latest.close <= pivot_threshold
    )
    return TriggerResult(
        code=OPENING_RANGE_BREAKOUT,
        status=(
            TriggerStatus.BLOCKED
            if too_extended
            else (TriggerStatus.ACTIVE if broke else TriggerStatus.INACTIVE)
        ),
        value=_decimal_text(latest.close),
        threshold=_decimal_text(pivot_threshold),
        source=source,
        as_of=_aware_utc(latest.timestamp, "bar.timestamp") + bar_interval,
        detail=(
            f"opening range high={_decimal_text(range_high)} "
            f"low={_decimal_text(range_low)} extension="
            f"{_decimal_text(extension_threshold)} over "
            f"{len(opening)} completed bars"
        ),
        blocked_reason="too_extended" if too_extended else None,
    )


def session_vwap(bars: Sequence[PriceBar]) -> tuple[Decimal, ...]:
    """세션 시작에서 리셋되는 누적 VWAP 시계열.

    전형가 ``(high + low + close) / 3``을 거래량으로 가중한 누적 평균이다.
    누적 거래량이 0인 구간은 값을 만들 수 없으므로 그 시점의 종가를 쓰지 않고
    직전 VWAP을 유지하며, 첫 bar부터 0이면 그 bar의 전형가를 쓴다.
    """

    ordered = _ordered(bars)
    weighted = _ZERO
    volume = _ZERO
    output: list[Decimal] = []
    for bar in ordered:
        typical = (bar.high + bar.low + bar.close) / _THREE
        weighted += typical * bar.volume
        volume += bar.volume
        if volume > _ZERO:
            output.append(weighted / volume)
        else:
            output.append(output[-1] if output else typical)
    return tuple(output)


def session_vwap_reclaim(
    bars: Sequence[PriceBar],
    *,
    direction: Action,
    bar_interval: timedelta,
    source: str,
    config: IntradayTriggerConfig = DEFAULT_INTRADAY_TRIGGER_CONFIG,
) -> TriggerResult:
    """세션 VWAP을 방향대로 되찾았거나 그 위/아래로 뻗었는가.

    BUY는 회복(reclaim), SELL은 붕괴(breakdown)로 대칭이다. 직전 완료 bar가
    반대쪽에 있었으면 ``reclaim``, 계속 같은 쪽이면서 세션 극단 종가를 새로
    쓰면 ``breakout``으로 본다.
    """

    ordered = _ordered(bars)
    if len(ordered) < 2:
        return _unavailable(
            SESSION_VWAP_RECLAIM,
            reason="insufficient_completed_session_bars",
            detail="session VWAP needs at least two completed bars",
            source=source,
        )
    curve = session_vwap(ordered)
    latest, previous = ordered[-1], ordered[-2]
    latest_vwap, previous_vwap = curve[-1], curve[-2]
    if direction is Action.BUY:
        reclaimed = previous.close <= previous_vwap
        extreme = latest.close >= max(bar.close for bar in ordered)
        pivot_threshold = latest_vwap * (Decimal("1") + config.pivot_buffer_ratio)
        extension_threshold = latest_vwap * (Decimal("1") + config.max_extension_ratio)
        beyond = latest.close >= pivot_threshold
        too_extended = latest.close > extension_threshold
    else:
        reclaimed = previous.close >= previous_vwap
        extreme = latest.close <= min(bar.close for bar in ordered)
        pivot_threshold = latest_vwap * (Decimal("1") - config.pivot_buffer_ratio)
        extension_threshold = latest_vwap * (Decimal("1") - config.max_extension_ratio)
        beyond = latest.close <= pivot_threshold
        too_extended = latest.close < extension_threshold
    pattern = "reclaim" if reclaimed else ("breakout" if extreme else "none")
    active = beyond and pattern != "none"
    return TriggerResult(
        code=SESSION_VWAP_RECLAIM,
        status=(
            TriggerStatus.BLOCKED
            if too_extended
            else (TriggerStatus.ACTIVE if active else TriggerStatus.INACTIVE)
        ),
        value=_decimal_text(latest.close),
        threshold=_decimal_text(pivot_threshold),
        source=source,
        as_of=_aware_utc(latest.timestamp, "bar.timestamp") + bar_interval,
        detail=(
            f"pattern={pattern} previousClose={_decimal_text(previous.close)} "
            f"previousVwap={_decimal_text(previous_vwap)} extension="
            f"{_decimal_text(extension_threshold)} over "
            f"{len(ordered)} completed bars"
        ),
        blocked_reason="too_extended" if too_extended else None,
    )


def relative_volume(
    bars: Sequence[PriceBar],
    *,
    code: str,
    window_bars: int,
    baseline_bars: int,
    threshold: Decimal,
    bar_interval: timedelta,
    source: str,
) -> TriggerResult:
    """완료 bar 상대거래량.

    최근 ``window_bars``개 완료 bar의 평균 거래량을 그 앞 ``baseline_bars``개
    완료 bar의 평균 거래량으로 나눈다. 두 구간을 모두 완료 bar로만 채울 수
    없으면 값을 만들지 않는다.
    """

    if window_bars < 1 or baseline_bars < 1:
        raise ValueError("relative volume windows must be positive")
    ordered = _ordered(bars)
    required = window_bars + baseline_bars
    if len(ordered) < required:
        return _unavailable(
            code,
            reason="insufficient_completed_session_bars",
            detail=(
                f"{required} completed bars are required and "
                f"{len(ordered)} are available"
            ),
            source=source,
        )
    window = ordered[-window_bars:]
    baseline = ordered[-required:-window_bars]
    baseline_mean = sum((bar.volume for bar in baseline), start=_ZERO) / Decimal(
        len(baseline)
    )
    if baseline_mean <= _ZERO:
        return _unavailable(
            code,
            reason="zero_baseline_volume",
            detail="the baseline window traded no volume, so no ratio exists",
            source=source,
        )
    window_mean = sum((bar.volume for bar in window), start=_ZERO) / Decimal(
        len(window)
    )
    ratio = window_mean / baseline_mean
    latest = ordered[-1]
    return TriggerResult(
        code=code,
        status=(TriggerStatus.ACTIVE if ratio >= threshold else TriggerStatus.INACTIVE),
        value=_decimal_text(ratio),
        threshold=_decimal_text(threshold),
        source=source,
        as_of=_aware_utc(latest.timestamp, "bar.timestamp") + bar_interval,
        detail=(
            f"window={window_bars} bars mean={_decimal_text(window_mean)} "
            f"baseline={baseline_bars} bars mean={_decimal_text(baseline_mean)}"
        ),
    )


def same_time_relative_volume(
    bars: Sequence[PriceBar],
    baseline: Sequence[SameTimeVolumeBaseline],
    *,
    code: str,
    window_bars: int,
    minimum_days: int,
    threshold: Decimal,
    bar_interval: timedelta,
    source: str,
) -> TriggerResult:
    """과거 거래일의 동시간대 거래량 중앙값과 비교하는 상대거래량.

    :func:`relative_volume`이 같은 세션 내부의 직전 완료 bar를 기준선으로
    삼는 것과 달리, 이 계산은 과거 거래일의 동일 시각 완료 구간만 비교한다.
    """

    if window_bars < 1 or minimum_days < 1:
        raise ValueError("same-time relative volume windows must be positive")
    ordered = _ordered(bars)
    if len(ordered) < window_bars:
        return _unavailable(
            code,
            reason="insufficient_completed_session_bars",
            detail=(
                f"{window_bars} completed bars are required and "
                f"{len(ordered)} are available"
            ),
            source=source,
        )

    baseline_median = same_time_baseline_median(baseline)
    sample_days = len(baseline)
    if baseline_median is None or sample_days < minimum_days:
        return _unavailable(
            code,
            reason="insufficient_baseline_days",
            detail=(
                f"{minimum_days} baseline days are required and "
                f"{sample_days} are available"
            ),
            source=source,
        )
    if baseline_median <= _ZERO:
        return _unavailable(
            code,
            reason="zero_baseline_volume",
            detail="the same-time baseline median is zero, so no ratio exists",
            source=source,
        )

    today_volume = sum(
        (bar.volume for bar in ordered[-window_bars:]),
        start=_ZERO,
    )
    ratio = today_volume / baseline_median
    latest = ordered[-1]
    return TriggerResult(
        code=code,
        status=(TriggerStatus.ACTIVE if ratio >= threshold else TriggerStatus.INACTIVE),
        value=_decimal_text(ratio),
        threshold=_decimal_text(threshold),
        source=source,
        as_of=_aware_utc(latest.timestamp, "bar.timestamp") + bar_interval,
        detail=(
            f"window={window_bars} bars "
            f"todayVolume={_decimal_text(today_volume)} "
            f"baselineMedian={_decimal_text(baseline_median)} "
            f"sampleDays={sample_days}"
        ),
    )


def intraday_relative_strength(
    bars: Sequence[PriceBar],
    index_bars: Sequence[PriceBar] | None,
    *,
    direction: Action,
    threshold: Decimal,
    bar_interval: timedelta,
    source: str,
    index_source: str | None,
    unavailable_reason: str | None = None,
) -> TriggerResult:
    """같은 완료 bar 창에서 지수보다 강한가(BUY) 또는 약한가(SELL).

    지수 분봉이 실제로 없으면 값을 추정하지도, 일봉과 섞지도 않는다. 그 대신
    ``UNAVAILABLE``과 사유만 남긴다.
    """

    if index_bars is None or not index_bars:
        return _unavailable(
            INTRADAY_RELATIVE_STRENGTH,
            reason=unavailable_reason or INDEX_INTRADAY_UNAVAILABLE,
            detail=(
                "no completed regular-session index bars were returned by the "
                "shared market-data path"
            ),
            source=index_source,
        )
    ordered = _ordered(bars)
    ordered_index = _ordered(index_bars)
    if not ordered:
        return _unavailable(
            INTRADAY_RELATIVE_STRENGTH,
            reason="no_completed_session_bars",
            detail="the candidate has no completed session bars",
            source=source,
        )
    overlap = _overlapping_window(ordered, ordered_index)
    if overlap is None:
        return _unavailable(
            INTRADAY_RELATIVE_STRENGTH,
            reason="index_window_mismatch",
            detail=(
                "candidate and index completed bars do not share a common "
                "session window"
            ),
            source=index_source,
        )
    symbol_window, index_window = overlap
    symbol_return = _window_return(symbol_window)
    index_return = _window_return(index_window)
    if symbol_return is None or index_return is None:
        return _unavailable(
            INTRADAY_RELATIVE_STRENGTH,
            reason="non_positive_reference_price",
            detail="a window open price was not positive, so no return exists",
            source=index_source,
        )
    excess = symbol_return - index_return
    required = threshold if direction is Action.BUY else -threshold
    active = excess >= required if direction is Action.BUY else excess <= required
    latest = symbol_window[-1]
    return TriggerResult(
        code=INTRADAY_RELATIVE_STRENGTH,
        status=TriggerStatus.ACTIVE if active else TriggerStatus.INACTIVE,
        value=_decimal_text(excess),
        threshold=_decimal_text(required),
        source=index_source,
        as_of=_aware_utc(latest.timestamp, "bar.timestamp") + bar_interval,
        detail=(
            f"symbolReturn={_decimal_text(symbol_return)} "
            f"indexReturn={_decimal_text(index_return)} over "
            f"{len(symbol_window)} shared completed bars"
        ),
    )


def decide_intraday_triggers(
    triggers: Sequence[TriggerResult],
    *,
    symbol: str,
    market: Literal["KRX", "US"],
    direction: Action,
    evaluated_at: datetime,
    policy: IntradayTriggerPolicy = DEFAULT_INTRADAY_TRIGGER_POLICY,
    config: IntradayTriggerConfig = DEFAULT_INTRADAY_TRIGGER_CONFIG,
    session_open_price: Decimal | None = None,
    previous_close: Decimal | None = None,
    atr_14: Decimal | None = None,
    blocked_reason: str | None = None,
) -> IntradayTriggerDecision:
    """명시적 정책과 추격매수 금지 조건으로 진입 방아쇠를 판정한다."""

    moment = _aware_utc(evaluated_at, "evaluated_at")
    valid_until = moment + config.decision_ttl
    ordered = tuple(triggers)
    by_code = {item.code: item for item in ordered}
    data_as_of = max(
        (item.as_of for item in ordered if item.as_of is not None),
        default=None,
    )
    no_chase = _no_chase_evidence(
        direction=direction,
        config=config,
        session_open_price=session_open_price,
        previous_close=previous_close,
        atr_14=atr_14,
    )

    def decision(
        status: TriggerDecisionStatus, reason: str | None
    ) -> IntradayTriggerDecision:
        return IntradayTriggerDecision(
            schema_version=INTRADAY_TRIGGER_SCHEMA_VERSION,
            symbol=symbol,
            market=market,
            direction=direction,
            status=status,
            triggers=ordered,
            policy=policy,
            evaluated_at=moment,
            valid_until=valid_until,
            data_as_of=data_as_of,
            blocked_reason=reason,
            no_chase=no_chase,
        )

    if blocked_reason is not None:
        return decision(TriggerDecisionStatus.UNAVAILABLE, blocked_reason)
    if direction not in {Action.BUY, Action.SELL}:
        return decision(TriggerDecisionStatus.UNAVAILABLE, "no_directional_setup")

    no_chase_blocks = [
        item.blocked_reason
        for item in ordered
        if item.status is TriggerStatus.BLOCKED and item.blocked_reason is not None
    ]
    if no_chase.gap_up_blocked:
        no_chase_blocks.append("gap_up_no_chase")
    if no_chase_blocks:
        return decision(
            TriggerDecisionStatus.BLOCKED,
            ",".join(dict.fromkeys(no_chase_blocks)),
        )

    failures: list[str] = []
    directional = tuple(by_code.get(code) for code in policy.directional)
    if all(item is None or not item.available for item in directional):
        failures.append("directional_triggers_unavailable")
    elif not any(item is not None and item.active for item in directional):
        failures.append("no_directional_trigger")

    volume = tuple(by_code.get(code) for code in policy.volume)
    if all(item is None or not item.available for item in volume):
        failures.append("relative_volume_unavailable")
    elif not any(item is not None and item.active for item in volume):
        failures.append("relative_volume_not_confirmed")

    for code in policy.directional_confirmation:
        item = by_code.get(code)
        if item is None or not item.available:
            # 확인용 trigger가 없다는 사실만으로 나머지 유효 trigger를 막지
            # 않는다. 사유는 이미 trigger 근거에 남아 있다.
            continue
        if not item.active:
            failures.append(f"{code}_disagrees")

    if not failures:
        return decision(TriggerDecisionStatus.TRIGGERED, None)
    unavailable_failure = any(item.endswith("_unavailable") for item in failures)
    return decision(
        (
            TriggerDecisionStatus.UNAVAILABLE
            if unavailable_failure
            else TriggerDecisionStatus.NOT_TRIGGERED
        ),
        ",".join(failures),
    )


def _overlapping_window(
    bars: Sequence[PriceBar],
    index_bars: Sequence[PriceBar],
) -> tuple[tuple[PriceBar, ...], tuple[PriceBar, ...]] | None:
    """두 계열이 공유하는 완료 bar timestamp 창만 남긴다."""

    index_by_timestamp = {
        _aware_utc(bar.timestamp, "bar.timestamp"): bar for bar in index_bars
    }
    shared = [
        (bar, index_by_timestamp[timestamp])
        for bar in bars
        if (timestamp := _aware_utc(bar.timestamp, "bar.timestamp"))
        in index_by_timestamp
    ]
    if len(shared) < 2:
        return None
    return tuple(item[0] for item in shared), tuple(item[1] for item in shared)


def _window_return(bars: Sequence[PriceBar]) -> Decimal | None:
    reference = bars[0].open
    if not reference.is_finite() or reference <= _ZERO:
        return None
    return bars[-1].close / reference - Decimal("1")


def _ordered(bars: Sequence[PriceBar]) -> tuple[PriceBar, ...]:
    return tuple(
        sorted(bars, key=lambda bar: _aware_utc(bar.timestamp, "bar.timestamp"))
    )


def _no_chase_evidence(
    *,
    direction: Action,
    config: IntradayTriggerConfig,
    session_open_price: Decimal | None,
    previous_close: Decimal | None,
    atr_14: Decimal | None,
) -> _NoChaseEvidence:
    unavailable: str | None = None
    blocked = False
    if direction is Action.BUY:
        missing: list[str] = []
        if session_open_price is None:
            missing.append("session_open_unavailable")
        if previous_close is None or previous_close <= _ZERO:
            missing.append("previous_close_unavailable")
        if atr_14 is None:
            missing.append("atr14_unavailable")
        if missing:
            unavailable = ",".join(missing)
        else:
            assert session_open_price is not None
            assert previous_close is not None
            assert atr_14 is not None
            gap = session_open_price - previous_close
            threshold = max(
                config.gap_up_atr_multiple * atr_14,
                config.gap_up_min_ratio * previous_close,
            )
            blocked = gap >= threshold
    return _NoChaseEvidence(
        pivot_buffer_ratio=config.pivot_buffer_ratio,
        max_extension_ratio=config.max_extension_ratio,
        session_open_price=session_open_price,
        previous_close=previous_close,
        atr_14=atr_14,
        gap_up_blocked=blocked,
        gap_up_unavailable=unavailable,
    )


def _unavailable(
    code: str,
    *,
    reason: str,
    detail: str,
    source: str | None,
) -> TriggerResult:
    return TriggerResult(
        code=code,
        status=TriggerStatus.UNAVAILABLE,
        value=None,
        threshold=None,
        source=source,
        as_of=None,
        detail=detail,
        unavailable_reason=reason,
    )


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_INTRADAY_TRIGGER_POLICY",
    "DEFAULT_INTRADAY_TRIGGER_CONFIG",
    "INDEX_INTRADAY_UNAVAILABLE",
    "INTRADAY_RELATIVE_STRENGTH",
    "INTRADAY_TRIGGER_SCHEMA_VERSION",
    "IntradayTriggerConfig",
    "NO_CHASE_SCHEMA_VERSION",
    "IntradayTriggerDecision",
    "IntradayTriggerPolicy",
    "OPENING_RANGE_BREAKOUT",
    "RELATIVE_VOLUME_20M",
    "RELATIVE_VOLUME_5M",
    "SAME_TIME_RELATIVE_VOLUME_20M",
    "SAME_TIME_RELATIVE_VOLUME_5M",
    "SESSION_VWAP_RECLAIM",
    "SameTimeVolumeBaseline",
    "TriggerDecisionStatus",
    "TriggerResult",
    "TriggerStatus",
    "decide_intraday_triggers",
    "intraday_relative_strength",
    "opening_range_breakout",
    "relative_volume",
    "same_time_baseline_median",
    "same_time_relative_volume",
    "session_vwap",
    "session_vwap_reclaim",
]
