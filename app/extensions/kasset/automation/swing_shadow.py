"""KRX 스윙 SHADOW 3후보 판정과 가상 성과 계산(순수 함수, DB·네트워크·시계 없음).

이 모듈은 주문·추천·승격 경로와 연결되지 않는다. 판정 입력은 호출자가 이미
완료로 확정한 세션(``signal_session``)까지의 일봉과 거래소 calendar 세션 목록이다.
``signal_session`` 뒤의 봉은 계산 전에 버린다.

후보(v1):

* ``weekly_compression_breakout`` — 완료 주봉만 사용한다. 직전 6주 base 폭이 좁고
  최근 3주 변동폭이 그 앞 3주보다 줄었으며, 이번 완료 주 종가가 base 고점을 처음
  넘고 주 거래량이 base 평균의 1.5배 이상이며 10주 종가 평균 위인 경우.
* ``uptrend_first_pullback`` — 기존 ``shadow_setups`` First Pullback의 ``confirmed``
  판정을 그대로 쓰고, 첫 접촉(``contact_label == "first"``)과 SMA20>SMA50,
  상승하는 SMA50, 종가>SMA50 상승 추세 조건을 더한 경우.
* ``box_breakout_retest`` — 40세션 박스(폭 20% 이하)를 거래량을 동반해 종가로
  돌파한 뒤 2~10세션 안에 박스 상단까지 되돌아와 지지받고 신호일에 양봉으로
  박스 위에서 다시 오른 경우.

성과는 신호일 종가가 아니라 다음 유효 거래일 시가의 가상 진입 기준이다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from hashlib import sha256
from statistics import median
from typing import Literal

from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.shadow_setups import (
    DEFAULT_SHADOW_SETUP_CONFIG,
    ShadowSetupConfig,
    ShadowStatus,
    evaluate_shadow_setups,
)
from app.services.halt_detection import HaltBar, classify_bars

SWING_SHADOW_SCHEMA_VERSION = "kasset.swing-shadow.v1"
SWING_SHADOW_CONFIG_SCHEMA_VERSION = "kasset.swing-shadow-config.v1"
SWING_SHADOW_REPORT_SCHEMA_VERSION = "kasset.swing-shadow-report.v1"
SWING_SHADOW_MARKET: Literal["KRX"] = "KRX"

_ZERO = Decimal("0")
_ONE = Decimal("1")
_QUANTUM = Decimal("0.000001")


class SwingCandidate(StrEnum):
    WEEKLY_COMPRESSION_BREAKOUT = "weekly_compression_breakout"
    UPTREND_FIRST_PULLBACK = "uptrend_first_pullback"
    BOX_BREAKOUT_RETEST = "box_breakout_retest"


SWING_CANDIDATES: tuple[SwingCandidate, ...] = tuple(SwingCandidate)


class CandidateStatus(StrEnum):
    SIGNAL = "signal"
    NO_SIGNAL = "no_signal"
    NOT_APPLICABLE = "not_applicable"


class OutcomeStatus(StrEnum):
    MATURE = "mature"
    PENDING = "pending"
    ENTRY_MISSING = "entry_missing"
    BAR_MISSING = "bar_missing"
    PRICE_DISCONTINUITY = "price_discontinuity"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    INVALID_BAR = "invalid_bar"
    ENTRY_UNTRADABLE = "entry_untradable"
    EXIT_UNTRADABLE = "exit_untradable"


def _bar_is_valid(bar: SwingBar) -> bool:
    """가격 양수·OHLC 정합·거래량 비음수. ``kr_candles_1d``는 NOT NULL만 보장한다."""

    return (
        all(
            value.is_finite() and value > _ZERO
            for value in (bar.open, bar.high, bar.low, bar.close)
        )
        and bar.volume.is_finite()
        and bar.volume >= _ZERO
        and bar.high >= max(bar.open, bar.close, bar.low)
        and bar.low <= min(bar.open, bar.close)
    )


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def decimal_text(value: Decimal) -> str:
    """JSON·DB 근거용 고정 소수 문자열(지수 표기 없음)."""

    return format(_quantize(value), "f")


@dataclass(frozen=True, slots=True)
class SwingShadowConfig:
    """스윙 SHADOW 전용 불변 설정. 활성 전략 artifact와 지문을 공유하지 않는다."""

    lookback_sessions: int = 80
    min_close: Decimal = Decimal("1000")
    turnover_sessions: int = 20
    min_average_turnover: Decimal = Decimal("1000000000")
    max_abs_session_move: Decimal = Decimal("0.35")
    weekly_base_weeks: int = 6
    weekly_contraction_weeks: int = 3
    weekly_max_base_width: Decimal = Decimal("0.15")
    weekly_volume_ratio: Decimal = Decimal("1.5")
    weekly_trend_weeks: int = 10
    pullback_fast_sma: int = 20
    pullback_slow_sma: int = 50
    pullback_slope_sessions: int = 10
    box_lookback_sessions: int = 40
    box_max_width: Decimal = Decimal("0.20")
    box_breakout_min_age: int = 2
    box_breakout_max_age: int = 10
    box_volume_sessions: int = 20
    box_volume_ratio: Decimal = Decimal("1.5")
    box_retest_tolerance: Decimal = Decimal("0.02")
    box_hold_tolerance: Decimal = Decimal("0.03")
    horizons: tuple[int, ...] = (1, 3, 5, 10)
    buy_fee_rate: Decimal = Decimal("0.00015")
    sell_fee_rate: Decimal = Decimal("0.00015")
    sell_tax_rate: Decimal = Decimal("0.0018")
    slippage_rate: Decimal = Decimal("0.001")
    min_mature_sample: int = 30
    shadow_setup_config: ShadowSetupConfig = field(
        default_factory=lambda: DEFAULT_SHADOW_SETUP_CONFIG
    )

    def __post_init__(self) -> None:
        for name in (
            "min_close",
            "min_average_turnover",
            "max_abs_session_move",
            "weekly_max_base_width",
            "weekly_volume_ratio",
            "box_max_width",
            "box_volume_ratio",
            "box_retest_tolerance",
            "box_hold_tolerance",
            "buy_fee_rate",
            "sell_fee_rate",
            "sell_tax_rate",
            "slippage_rate",
        ):
            value = _decimal(getattr(self, name))
            if not value.is_finite() or value < _ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        for name in (
            "lookback_sessions",
            "turnover_sessions",
            "weekly_base_weeks",
            "weekly_contraction_weeks",
            "weekly_trend_weeks",
            "pullback_fast_sma",
            "pullback_slow_sma",
            "pullback_slope_sessions",
            "box_lookback_sessions",
            "box_breakout_min_age",
            "box_volume_sessions",
            "min_mature_sample",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.weekly_contraction_weeks * 2 != self.weekly_base_weeks:
            raise ValueError("weekly base must split into two contraction halves")
        if self.pullback_fast_sma >= self.pullback_slow_sma:
            raise ValueError("fast SMA must be shorter than slow SMA")
        if self.box_breakout_max_age < self.box_breakout_min_age:
            raise ValueError("box breakout age window is empty")
        if not self.horizons or any(h < 1 for h in self.horizons):
            raise ValueError("horizons must be positive trading-session counts")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be strictly increasing")
        daily_need = self.pullback_slow_sma + self.pullback_slope_sessions
        box_need = (
            self.box_breakout_max_age
            + max(self.box_lookback_sessions, self.box_volume_sessions)
            + 1
        )
        # 첫 주가 잘릴 수 있으므로 필요한 완료 주 수보다 한 주를 더 확보한다.
        weekly_need = (max(self.weekly_trend_weeks, self.weekly_base_weeks + 2) + 1) * 5
        if self.lookback_sessions < max(
            daily_need, box_need, weekly_need, self.turnover_sessions
        ):
            raise ValueError("lookback_sessions does not cover every candidate")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": SWING_SHADOW_CONFIG_SCHEMA_VERSION,
            "signalSchemaVersion": SWING_SHADOW_SCHEMA_VERSION,
            "market": SWING_SHADOW_MARKET,
            "lookbackSessions": self.lookback_sessions,
            "minClose": decimal_text(self.min_close),
            "turnoverSessions": self.turnover_sessions,
            "minAverageTurnover": decimal_text(self.min_average_turnover),
            "maxAbsSessionMove": decimal_text(self.max_abs_session_move),
            "weeklyBaseWeeks": self.weekly_base_weeks,
            "weeklyContractionWeeks": self.weekly_contraction_weeks,
            "weeklyMaxBaseWidth": decimal_text(self.weekly_max_base_width),
            "weeklyVolumeRatio": decimal_text(self.weekly_volume_ratio),
            "weeklyTrendWeeks": self.weekly_trend_weeks,
            "pullbackFastSma": self.pullback_fast_sma,
            "pullbackSlowSma": self.pullback_slow_sma,
            "pullbackSlopeSessions": self.pullback_slope_sessions,
            "boxLookbackSessions": self.box_lookback_sessions,
            "boxMaxWidth": decimal_text(self.box_max_width),
            "boxBreakoutMinAge": self.box_breakout_min_age,
            "boxBreakoutMaxAge": self.box_breakout_max_age,
            "boxVolumeSessions": self.box_volume_sessions,
            "boxVolumeRatio": decimal_text(self.box_volume_ratio),
            "boxRetestTolerance": decimal_text(self.box_retest_tolerance),
            "boxHoldTolerance": decimal_text(self.box_hold_tolerance),
            "horizons": list(self.horizons),
            "buyFeeRate": decimal_text(self.buy_fee_rate),
            "sellFeeRate": decimal_text(self.sell_fee_rate),
            "sellTaxRate": decimal_text(self.sell_tax_rate),
            "slippageRate": decimal_text(self.slippage_rate),
            "minMatureSample": self.min_mature_sample,
            "shadowSetupConfigFingerprint": self.shadow_setup_config.fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return sha256(encoded).hexdigest()


DEFAULT_SWING_SHADOW_CONFIG = SwingShadowConfig()


@dataclass(frozen=True, slots=True)
class SwingBar:
    """KRX 일봉 한 개. ``session_date``는 KST 세션 날짜다."""

    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal | None = None
    source: str | None = None
    ingested_at: datetime | None = None

    def price_key(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, str | None]:
        """가격 정정 비교용 의미 값(메타데이터 ``ingested_at`` 제외)."""

        return (self.open, self.high, self.low, self.close, self.volume, self.source)


@dataclass(frozen=True, slots=True)
class SwingSignal:
    candidate: SwingCandidate
    symbol: str
    signal_session: date
    anchor_session: date
    trigger_price: Decimal
    stop_reference: Decimal | None
    signal_bar: SwingBar
    evidence: dict[str, object]


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate: SwingCandidate
    status: CandidateStatus
    reason: str | None
    signal: SwingSignal | None = None


@dataclass(frozen=True, slots=True)
class SymbolEvaluation:
    symbol: str
    excluded_reason: str | None
    candidates: tuple[CandidateResult, ...]
    future_bars_ignored: int = 0

    @property
    def signals(self) -> tuple[SwingSignal, ...]:
        return tuple(item.signal for item in self.candidates if item.signal is not None)


def bar_timestamp(session_date: date) -> datetime:
    """기존 일봉 저장 관례(세션 날짜 00:00 UTC 라벨)의 PriceBar 시각."""

    return datetime.combine(session_date, time.min, tzinfo=UTC)


def evaluate_swing_symbol(
    bars: Sequence[SwingBar],
    *,
    symbol: str,
    signal_session: date,
    calendar_sessions: Sequence[date],
    week_complete: bool,
    evaluation_as_of: datetime,
    config: SwingShadowConfig = DEFAULT_SWING_SHADOW_CONFIG,
) -> SymbolEvaluation:
    """완료 세션 ``signal_session``까지의 일봉으로 세 후보를 판정한다.

    ``calendar_sessions``는 ``signal_session``으로 끝나는 오름차순 거래소 세션이며
    ``lookback_sessions``보다 길어야 첫 주가 잘렸는지 판단할 수 있다.
    ``week_complete``는 calendar 기준으로 ``signal_session``이 그 ISO 주의 마지막
    거래일인지다. ``evaluation_as_of``는 그 세션의 정규장 종료 시각이다.
    """

    normalized = symbol.strip().upper()
    sessions = tuple(calendar_sessions)
    if not sessions or sessions[-1] != signal_session:
        raise ValueError("calendar_sessions must end at signal_session")
    if len(sessions) < config.lookback_sessions:
        raise ValueError("calendar_sessions must cover lookback_sessions")

    retained = [bar for bar in bars if bar.session_date <= signal_session]
    future_ignored = len(bars) - len(retained)

    def excluded(reason: str) -> SymbolEvaluation:
        return SymbolEvaluation(
            symbol=normalized,
            excluded_reason=reason,
            candidates=(),
            future_bars_ignored=future_ignored,
        )

    retained.sort(key=lambda item: item.session_date)
    dates = [bar.session_date for bar in retained]
    if len(set(dates)) != len(dates):
        return excluded("duplicate_bar")
    if not all(_bar_is_valid(bar) for bar in retained):
        return excluded("invalid_ohlcv")
    if not retained or retained[-1].session_date != signal_session:
        return excluded("stale_latest_bar")

    covered = sessions[-config.lookback_sessions :]
    session_set = set(sessions)
    by_date = {bar.session_date: bar for bar in retained}
    if any(
        bar.session_date >= covered[0] and bar.session_date not in session_set
        for bar in retained
    ):
        return excluded("off_calendar_bar")
    missing = [day for day in covered if day not in by_date]
    if missing:
        first_bar = retained[0].session_date
        if all(day < first_bar for day in missing):
            return excluded("insufficient_history")
        return excluded("data_gap")
    window = tuple(by_date[day] for day in covered)

    if _has_discontinuity(window, config.max_abs_session_move):
        return excluded("price_discontinuity")
    halt = classify_bars(
        [
            HaltBar(
                close=bar.close,
                high=bar.high,
                low=bar.low,
                open=bar.open,
                volume=bar.volume,
            )
            for bar in window
        ]
    )
    if halt.suspected:
        return excluded("halted_suspect")
    latest = window[-1]
    if latest.close < config.min_close:
        return excluded("below_min_close")
    turnover = window[-config.turnover_sessions :]
    if any(bar.value is None for bar in turnover):
        return excluded("turnover_unavailable")
    average_turnover = _mean(tuple(_decimal(bar.value) for bar in turnover))
    if average_turnover < config.min_average_turnover:
        return excluded("below_min_turnover")

    weekly_first_partial = sessions[0] >= covered[0] or _same_week(
        sessions[-config.lookback_sessions - 1], covered[0]
    )
    results = (
        _weekly_compression_breakout(
            window,
            symbol=normalized,
            week_complete=week_complete,
            first_week_partial=weekly_first_partial,
            config=config,
        ),
        _uptrend_first_pullback(
            window,
            symbol=normalized,
            evaluation_as_of=evaluation_as_of,
            config=config,
        ),
        _box_breakout_retest(window, symbol=normalized, config=config),
    )
    return SymbolEvaluation(
        symbol=normalized,
        excluded_reason=None,
        candidates=results,
        future_bars_ignored=future_ignored,
    )


def _same_week(left: date, right: date) -> bool:
    return left.isocalendar()[:2] == right.isocalendar()[:2]


def _has_discontinuity(bars: Sequence[SwingBar], limit: Decimal) -> bool:
    for previous, current in zip(bars, bars[1:], strict=False):
        for price in (current.open, current.close):
            if abs(price / previous.close - _ONE) > limit:
                return True
    return False


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=_ZERO) / Decimal(len(values))


def _sma(values: Sequence[Decimal], period: int, end: int) -> Decimal:
    """``values[end - period + 1 : end + 1]`` 단순평균."""

    return _mean(values[end - period + 1 : end + 1])


def _no_signal(candidate: SwingCandidate, reason: str) -> CandidateResult:
    return CandidateResult(candidate, CandidateStatus.NO_SIGNAL, reason)


@dataclass(frozen=True, slots=True)
class _WeekBar:
    sessions: tuple[date, ...]
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _weekly_bars(bars: Sequence[SwingBar], *, drop_first: bool) -> list[_WeekBar]:
    groups: list[list[SwingBar]] = []
    for bar in bars:
        if groups and _same_week(groups[-1][-1].session_date, bar.session_date):
            groups[-1].append(bar)
        else:
            groups.append([bar])
    if drop_first and groups:
        groups = groups[1:]
    return [
        _WeekBar(
            sessions=tuple(item.session_date for item in group),
            open=group[0].open,
            high=max(item.high for item in group),
            low=min(item.low for item in group),
            close=group[-1].close,
            volume=sum((item.volume for item in group), start=_ZERO),
        )
        for group in groups
    ]


def _weekly_compression_breakout(
    bars: Sequence[SwingBar],
    *,
    symbol: str,
    week_complete: bool,
    first_week_partial: bool,
    config: SwingShadowConfig,
) -> CandidateResult:
    candidate = SwingCandidate.WEEKLY_COMPRESSION_BREAKOUT
    if not week_complete:
        return CandidateResult(
            candidate, CandidateStatus.NOT_APPLICABLE, "week_incomplete"
        )
    weeks = _weekly_bars(bars, drop_first=first_week_partial)
    base_n = config.weekly_base_weeks
    half = config.weekly_contraction_weeks
    needed = max(config.weekly_trend_weeks, base_n + 2)
    if len(weeks) < needed:
        return _no_signal(candidate, "insufficient_weeks")
    current = weeks[-1]
    base = weeks[-base_n - 1 : -1]
    base_high = max(week.high for week in base)
    base_low = min(week.low for week in base)
    base_width = base_high / base_low - _ONE
    if base_width > config.weekly_max_base_width:
        return _no_signal(candidate, "base_too_wide")
    older = base[:half]
    recent = base[half:]
    older_range = _mean(tuple((w.high - w.low) / w.close for w in older))
    recent_range = _mean(tuple((w.high - w.low) / w.close for w in recent))
    if not recent_range < older_range:
        return _no_signal(candidate, "no_range_contraction")
    if not current.close > base_high:
        return _no_signal(candidate, "no_weekly_close_breakout")
    # 직전 주가 이미 그 앞 base 고점을 종가로 넘었다면 이번 주는 연속 돌파다.
    prior_base = weeks[-base_n - 2 : -2]
    previous_week = weeks[-2]
    if previous_week.close > max(week.high for week in prior_base):
        return _no_signal(candidate, "not_first_breakout_week")
    base_volume = _mean(tuple(week.volume for week in base))
    volume_ratio = current.volume / base_volume if base_volume > _ZERO else _ZERO
    if volume_ratio < config.weekly_volume_ratio:
        return _no_signal(candidate, "weekly_volume_below_ratio")
    trend_average = _mean(tuple(w.close for w in weeks[-config.weekly_trend_weeks :]))
    if not current.close > trend_average:
        return _no_signal(candidate, "below_weekly_trend_average")
    latest = bars[-1]
    evidence: dict[str, object] = {
        "weekSessions": [day.isoformat() for day in current.sessions],
        "weekOpen": decimal_text(current.open),
        "weekHigh": decimal_text(current.high),
        "weekLow": decimal_text(current.low),
        "weekClose": decimal_text(current.close),
        "weekVolume": decimal_text(current.volume),
        "baseWeeksStart": base[0].sessions[0].isoformat(),
        "baseWeeksEnd": base[-1].sessions[-1].isoformat(),
        "baseHigh": decimal_text(base_high),
        "baseLow": decimal_text(base_low),
        "baseWidth": decimal_text(base_width),
        "olderRangeRatio": decimal_text(older_range),
        "recentRangeRatio": decimal_text(recent_range),
        "weeklyVolumeRatio": decimal_text(volume_ratio),
        "weeklyTrendAverage": decimal_text(trend_average),
    }
    return CandidateResult(
        candidate,
        CandidateStatus.SIGNAL,
        None,
        SwingSignal(
            candidate=candidate,
            symbol=symbol,
            signal_session=latest.session_date,
            anchor_session=current.sessions[-1],
            trigger_price=_quantize(base_high),
            stop_reference=_quantize(base_low),
            signal_bar=latest,
            evidence=evidence,
        ),
    )


def _uptrend_first_pullback(
    bars: Sequence[SwingBar],
    *,
    symbol: str,
    evaluation_as_of: datetime,
    config: SwingShadowConfig,
) -> CandidateResult:
    candidate = SwingCandidate.UPTREND_FIRST_PULLBACK
    price_bars = tuple(
        PriceBar(
            timestamp=bar_timestamp(bar.session_date),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in bars
    )
    # 기존 evaluator의 confirmed는 feature_enabled와 무관하게 계산된다(관찰 저장
    # 목록만 feature_enabled로 갈린다). 평가 시각은 신호 세션 정규장 종료 시각이다.
    result = evaluate_shadow_setups(
        price_bars,
        symbol=symbol,
        market="KRX",
        as_of=evaluation_as_of,
        completed_through=evaluation_as_of,
        config=config.shadow_setup_config,
    )
    first = result.first_pullback
    if (
        result.status is not ShadowStatus.VALID
        or first.status is not ShadowStatus.VALID
    ):
        code = result.evidence[0].code if result.evidence else "invalid"
        return _no_signal(candidate, f"pullback_evaluator_{code}")
    if result.source_timestamps[-1] != bar_timestamp(bars[-1].session_date):
        return _no_signal(candidate, "pullback_evaluator_cutoff_mismatch")
    if not first.confirmed:
        return _no_signal(candidate, "pullback_not_confirmed")
    if first.contact_label != "first":
        return _no_signal(candidate, "not_first_contact")
    cluster = next(
        (item for item in first.evidence if item.code == "contact_label"), None
    )
    if cluster is None or not cluster.source_timestamps:
        return _no_signal(candidate, "pullback_cluster_unavailable")
    # 접촉 묶음이 기존 evaluator의 접촉 lookback 시작에서 gap 이내로 시작하면 그 앞의
    # 접촉이 잘렸을 수 있다. 그러면 묶음 시작일(anchor)이 날마다 뒤로 밀려 같은
    # 사이클을 새 신호로 다시 세게 되므로, 묶음 전체가 보이는 경우만 신호로 남긴다.
    setup_config = config.shadow_setup_config
    contact_lookback_start = max(
        setup_config.ema_period - 1, len(bars) - setup_config.contact_lookback_bars
    )
    cluster_start = cluster.source_timestamps[0].astimezone(UTC).date()
    cluster_start_index = next(
        index for index, bar in enumerate(bars) if bar.session_date == cluster_start
    )
    if (
        cluster_start_index - contact_lookback_start
        <= setup_config.contact_cluster_gap_bars
    ):
        return _no_signal(candidate, "pullback_cluster_truncated")
    closes = tuple(bar.close for bar in bars)
    end = len(closes) - 1
    fast = _sma(closes, config.pullback_fast_sma, end)
    slow = _sma(closes, config.pullback_slow_sma, end)
    slow_before = _sma(
        closes, config.pullback_slow_sma, end - config.pullback_slope_sessions
    )
    if not fast > slow:
        return _no_signal(candidate, "fast_sma_not_above_slow")
    if not slow > slow_before:
        return _no_signal(candidate, "slow_sma_not_rising")
    if not closes[-1] > slow:
        return _no_signal(candidate, "close_not_above_slow_sma")
    assert first.trigger_price is not None
    anchor = cluster_start
    latest = bars[-1]
    evidence: dict[str, object] = {
        "shadowSetupConfigFingerprint": result.config_fingerprint,
        "contactLabel": first.contact_label,
        "contactSessions": [
            item.astimezone(UTC).date().isoformat()
            for item in cluster.source_timestamps
        ],
        "pullbackPivot": decimal_text(first.pullback_pivot)
        if first.pullback_pivot is not None
        else None,
        "pullbackPivotSession": first.pullback_pivot_at.astimezone(UTC)
        .date()
        .isoformat()
        if first.pullback_pivot_at is not None
        else None,
        "resumptionPivot": decimal_text(first.resumption_pivot)
        if first.resumption_pivot is not None
        else None,
        "pullbackDepth": decimal_text(first.pullback_depth)
        if first.pullback_depth is not None
        else None,
        "orderliness": decimal_text(first.orderliness)
        if first.orderliness is not None
        else None,
        "fastSma": decimal_text(fast),
        "slowSma": decimal_text(slow),
        "slowSmaBefore": decimal_text(slow_before),
    }
    return CandidateResult(
        candidate,
        CandidateStatus.SIGNAL,
        None,
        SwingSignal(
            candidate=candidate,
            symbol=symbol,
            signal_session=latest.session_date,
            anchor_session=anchor,
            trigger_price=first.trigger_price,
            stop_reference=first.pullback_pivot,
            signal_bar=latest,
            evidence=evidence,
        ),
    )


def _box_breakout_retest(
    bars: Sequence[SwingBar], *, symbol: str, config: SwingShadowConfig
) -> CandidateResult:
    candidate = SwingCandidate.BOX_BREAKOUT_RETEST
    signal_index = len(bars) - 1
    breakout: tuple[int, Decimal, Decimal, Decimal, Decimal] | None = None
    for age in range(config.box_breakout_min_age, config.box_breakout_max_age + 1):
        index = signal_index - age
        start = index - config.box_lookback_sessions
        if start < 0 or index - config.box_volume_sessions < 0:
            break
        box = bars[start:index]
        box_high = max(bar.high for bar in box)
        box_low = min(bar.low for bar in box)
        width = box_high / box_low - _ONE
        if width > config.box_max_width:
            continue
        if not bars[index].close > box_high:
            continue
        average_volume = _mean(
            tuple(
                bar.volume for bar in bars[index - config.box_volume_sessions : index]
            )
        )
        ratio = bars[index].volume / average_volume if average_volume > _ZERO else _ZERO
        if ratio < config.box_volume_ratio:
            continue
        breakout = (index, box_high, box_low, width, ratio)
        break
    if breakout is None:
        return _no_signal(candidate, "no_recent_box_breakout")
    index, box_high, box_low, width, ratio = breakout
    after = bars[index + 1 :]
    retest_limit = box_high * (_ONE + config.box_retest_tolerance)
    hold_floor = box_high * (_ONE - config.box_hold_tolerance)
    if not any(bar.low <= retest_limit for bar in after):
        return _no_signal(candidate, "no_retest_of_box_top")
    if any(bar.close < hold_floor for bar in after):
        return _no_signal(candidate, "closed_back_inside_box")
    latest = bars[-1]
    previous = bars[-2]
    if not latest.close > box_high:
        return _no_signal(candidate, "close_not_above_box_top")
    if not latest.close > latest.open:
        return _no_signal(candidate, "signal_bar_not_bullish")
    if not latest.close > previous.close:
        return _no_signal(candidate, "close_not_above_previous_close")
    retest_low = min(bar.low for bar in after)
    evidence: dict[str, object] = {
        "breakoutSession": bars[index].session_date.isoformat(),
        "breakoutClose": decimal_text(bars[index].close),
        "breakoutVolumeRatio": decimal_text(ratio),
        "boxStart": bars[index - config.box_lookback_sessions].session_date.isoformat(),
        "boxEnd": bars[index - 1].session_date.isoformat(),
        "boxHigh": decimal_text(box_high),
        "boxLow": decimal_text(box_low),
        "boxWidth": decimal_text(width),
        "retestLow": decimal_text(retest_low),
        "retestLimit": decimal_text(retest_limit),
        "holdFloor": decimal_text(hold_floor),
    }
    return CandidateResult(
        candidate,
        CandidateStatus.SIGNAL,
        None,
        SwingSignal(
            candidate=candidate,
            symbol=symbol,
            signal_session=latest.session_date,
            anchor_session=bars[index].session_date,
            trigger_price=_quantize(box_high),
            stop_reference=_quantize(retest_low),
            signal_bar=latest,
            evidence=evidence,
        ),
    )


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    horizon: int
    status: OutcomeStatus
    entry_session: date | None = None
    exit_session: date | None = None
    entry_open: Decimal | None = None
    exit_close: Decimal | None = None
    gross_return: Decimal | None = None
    net_return: Decimal | None = None
    mfe: Decimal | None = None
    mae: Decimal | None = None
    zero_volume_holding_sessions: int = 0


def evaluate_outcome(
    *,
    horizon: int,
    signal_close: Decimal,
    forward_sessions: Sequence[date],
    last_final_session: date | None,
    bars_by_date: Mapping[date, SwingBar],
    config: SwingShadowConfig = DEFAULT_SWING_SHADOW_CONFIG,
) -> HorizonOutcome:
    """다음 세션 시가 가상 진입 → ``horizon``번째 세션(진입일=1) 종가 가상 청산.

    ``forward_sessions``는 신호 세션 뒤의 calendar 세션 오름차순 목록이다. 아직
    완료되지 않은 세션이 필요한 표본은 ``pending``이고, 완료됐는데 봉이 없으면
    ``entry_missing``/``bar_missing``으로 분리한다. 실제 주문·체결이 아니다.
    """

    if len(forward_sessions) < horizon:
        return HorizonOutcome(horizon, OutcomeStatus.CALENDAR_UNAVAILABLE)
    window = tuple(forward_sessions[:horizon])
    entry_session, exit_session = window[0], window[-1]
    if last_final_session is None or exit_session > last_final_session:
        return HorizonOutcome(
            horizon,
            OutcomeStatus.PENDING,
            entry_session=entry_session,
            exit_session=exit_session,
        )
    if entry_session not in bars_by_date:
        return HorizonOutcome(
            horizon,
            OutcomeStatus.ENTRY_MISSING,
            entry_session=entry_session,
            exit_session=exit_session,
        )
    if any(day not in bars_by_date for day in window):
        return HorizonOutcome(
            horizon,
            OutcomeStatus.BAR_MISSING,
            entry_session=entry_session,
            exit_session=exit_session,
        )
    path = tuple(bars_by_date[day] for day in window)

    def blocked(status: OutcomeStatus) -> HorizonOutcome:
        return HorizonOutcome(
            horizon, status, entry_session=entry_session, exit_session=exit_session
        )

    # 나눗셈·MFE/MAE 전에 원시 봉을 검사한다. 거래량 0인 진입·청산 세션은 그 가격에
    # 체결될 수 없으므로 실현 가능한 성과에서 뺀다. 중간 보유일의 거래량 0은
    # 보유 평가(mark-to-market)로 남기고 개수만 따로 센다.
    if not all(_bar_is_valid(bar) for bar in path):
        return blocked(OutcomeStatus.INVALID_BAR)
    if path[0].volume == _ZERO:
        return blocked(OutcomeStatus.ENTRY_UNTRADABLE)
    if path[-1].volume == _ZERO:
        return blocked(OutcomeStatus.EXIT_UNTRADABLE)
    previous_close = signal_close
    for bar in path:
        for price in (bar.open, bar.close):
            if abs(price / previous_close - _ONE) > config.max_abs_session_move:
                return blocked(OutcomeStatus.PRICE_DISCONTINUITY)
        previous_close = bar.close
    entry_open = path[0].open
    exit_close = path[-1].close
    gross = exit_close / entry_open - _ONE
    entry_cost = entry_open * (_ONE + config.buy_fee_rate + config.slippage_rate)
    exit_proceeds = exit_close * (
        _ONE - config.sell_fee_rate - config.sell_tax_rate - config.slippage_rate
    )
    net = exit_proceeds / entry_cost - _ONE
    mfe = max(bar.high for bar in path) / entry_open - _ONE
    mae = min(bar.low for bar in path) / entry_open - _ONE
    return HorizonOutcome(
        horizon,
        OutcomeStatus.MATURE,
        entry_session=entry_session,
        exit_session=exit_session,
        entry_open=entry_open,
        exit_close=exit_close,
        gross_return=_quantize(gross),
        net_return=_quantize(net),
        mfe=_quantize(mfe),
        mae=_quantize(mae),
        zero_volume_holding_sessions=sum(1 for bar in path if bar.volume == _ZERO),
    )


def summarize_horizon(
    outcomes: Sequence[HorizonOutcome], *, min_mature_sample: int
) -> dict[str, object]:
    """한 후보·한 horizon 표본 집계. 표본 부족 표기는 연구 advisory일 뿐 gate가 아니다."""

    counts = {status.value: 0 for status in OutcomeStatus}
    for outcome in outcomes:
        counts[outcome.status.value] += 1
    mature = [item for item in outcomes if item.status is OutcomeStatus.MATURE]
    summary: dict[str, object] = {
        "signals": len(outcomes),
        "statusCounts": counts,
        "matureCount": len(mature),
        "matureWithZeroVolumeHolding": sum(
            1 for item in mature if item.zero_volume_holding_sessions > 0
        ),
        "sampleAdvisory": (
            "insufficient_sample"
            if len(mature) < min_mature_sample
            else "sample_size_reached"
        ),
        "minMatureSampleAdvisory": min_mature_sample,
    }
    if not mature:
        summary["stats"] = None
        return summary
    nets = [item.net_return for item in mature if item.net_return is not None]
    grosses = [item.gross_return for item in mature if item.gross_return is not None]
    mfes = [item.mfe for item in mature if item.mfe is not None]
    maes = [item.mae for item in mature if item.mae is not None]
    wins = sum(1 for value in nets if value > _ZERO)
    summary["stats"] = {
        "meanNetReturn": decimal_text(_mean(nets)),
        "medianNetReturn": decimal_text(Decimal(median(nets))),
        "meanGrossReturn": decimal_text(_mean(grosses)),
        "netWinRate": decimal_text(Decimal(wins) / Decimal(len(nets))),
        "meanMfe": decimal_text(_mean(mfes)),
        "maxMfe": decimal_text(max(mfes)),
        "meanMae": decimal_text(_mean(maes)),
        "worstMae": decimal_text(min(maes)),
    }
    return summary


__all__ = [
    "DEFAULT_SWING_SHADOW_CONFIG",
    "SWING_CANDIDATES",
    "SWING_SHADOW_CONFIG_SCHEMA_VERSION",
    "SWING_SHADOW_MARKET",
    "SWING_SHADOW_REPORT_SCHEMA_VERSION",
    "SWING_SHADOW_SCHEMA_VERSION",
    "CandidateResult",
    "CandidateStatus",
    "HorizonOutcome",
    "OutcomeStatus",
    "SwingBar",
    "SwingCandidate",
    "SwingShadowConfig",
    "SwingSignal",
    "SymbolEvaluation",
    "bar_timestamp",
    "decimal_text",
    "evaluate_outcome",
    "evaluate_swing_symbol",
    "summarize_horizon",
]
