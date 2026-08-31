"""활성 전략과 분리된 First Pullback 및 NR7/Inside Day SHADOW 관찰기."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
from typing import Literal

from app.extensions.kasset.automation.contracts import PriceBar

SHADOW_SETUPS_SCHEMA_VERSION = "kasset.shadow-setups.v1"
SHADOW_SETUPS_CONFIG_SCHEMA_VERSION = "kasset.shadow-setups-config.v1"
SHADOW_MODE: Literal["SHADOW"] = "SHADOW"
_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class ShadowStatus(StrEnum):
    """외부 소비자가 추론 없이 처리할 수 있는 닫힌 상태 집합."""

    VALID = "valid"
    INSUFFICIENT = "insufficient"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class ShadowSetupConfig:
    """SHADOW 전용 불변 설정이며 활성 전략 설정과 지문을 공유하지 않는다."""

    feature_enabled: bool = False
    ema_period: int = 10
    volume_lookback: int = 20
    contact_lookback_bars: int = 40
    contact_cluster_gap_bars: int = 1
    pullback_reference_bars: int = 10
    maximum_pullback_depth: Decimal = Decimal("0.15")
    minimum_orderliness: Decimal = Decimal("0.60")
    minimum_supportive_volume_ratio: Decimal = Decimal("1")
    nr_window: int = 7
    validity: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        if self.ema_period != 10:
            raise ValueError("ema_period must remain 10 for First Pullback")
        if self.volume_lookback < 1:
            raise ValueError("volume_lookback must be positive")
        if self.contact_lookback_bars < self.ema_period:
            raise ValueError("contact_lookback_bars must cover ema_period")
        if self.contact_cluster_gap_bars < 0:
            raise ValueError("contact_cluster_gap_bars cannot be negative")
        if self.pullback_reference_bars < 1:
            raise ValueError("pullback_reference_bars must be positive")
        if self.nr_window != 7:
            raise ValueError("nr_window must remain 7 for NR7")
        if self.validity <= timedelta(0):
            raise ValueError("validity must be positive")
        for name in (
            "maximum_pullback_depth",
            "minimum_orderliness",
            "minimum_supportive_volume_ratio",
        ):
            value = _decimal(getattr(self, name))
            if not value.is_finite() or value < _ZERO:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.maximum_pullback_depth > _ONE or self.minimum_orderliness > _ONE:
            raise ValueError("ratio thresholds cannot exceed one")

    def fingerprint_payload(self) -> dict[str, object]:
        """활성 전략 artifact와 합치지 않는 정규 SHADOW 설정 payload."""

        return {
            "schemaVersion": SHADOW_SETUPS_CONFIG_SCHEMA_VERSION,
            "featureEnabled": self.feature_enabled,
            "emaPeriod": self.ema_period,
            "volumeLookback": self.volume_lookback,
            "contactLookbackBars": self.contact_lookback_bars,
            "contactClusterGapBars": self.contact_cluster_gap_bars,
            "pullbackReferenceBars": self.pullback_reference_bars,
            "maximumPullbackDepth": _text(self.maximum_pullback_depth),
            "minimumOrderliness": _text(self.minimum_orderliness),
            "minimumSupportiveVolumeRatio": _text(
                self.minimum_supportive_volume_ratio
            ),
            "nrWindow": self.nr_window,
            "validitySeconds": int(self.validity.total_seconds()),
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


DEFAULT_SHADOW_SETUP_CONFIG = ShadowSetupConfig()


@dataclass(frozen=True, slots=True)
class ShadowComponentEvidence:
    """계산 요소 하나의 닫힌 SHADOW 근거."""

    schema_version: str
    mode: Literal["SHADOW"]
    status: ShadowStatus
    code: str
    value: str
    threshold: str | None
    passed: bool | None
    source_timestamps: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class FirstPullbackResult:
    schema_version: str
    mode: Literal["SHADOW"]
    status: ShadowStatus
    source_timestamps: tuple[datetime, ...]
    contact_label: Literal["none", "first", "second", "later"]
    distinct_contact_count: int
    pullback_pivot: Decimal | None
    pullback_pivot_at: datetime | None
    resumption_pivot: Decimal | None
    resumption_pivot_at: datetime | None
    trigger_price: Decimal | None
    pullback_depth: Decimal | None
    orderliness: Decimal | None
    ema_recovered: bool
    supportive_volume: bool
    confirmed: bool
    valid_until: datetime | None
    evidence: tuple[ShadowComponentEvidence, ...]


@dataclass(frozen=True, slots=True)
class Nr7InsideDayResult:
    schema_version: str
    mode: Literal["SHADOW"]
    status: ShadowStatus
    source_timestamps: tuple[datetime, ...]
    subtype: Literal["none", "nr7", "inside_day", "nr7_inside_day"]
    nr7: bool
    inside_day: bool
    volume_contracted: bool
    pivot: Decimal | None
    pivot_at: datetime | None
    trigger_price: Decimal | None
    valid_until: datetime | None
    evidence: tuple[ShadowComponentEvidence, ...]


@dataclass(frozen=True, slots=True)
class ShadowSetupObservation:
    schema_version: str
    mode: Literal["SHADOW"]
    status: ShadowStatus
    setup: Literal["first_pullback", "nr7_inside_day"]
    subtype: str
    symbol: str
    market: Literal["KRX", "US"]
    observed_at: datetime
    valid_until: datetime
    trigger_price: Decimal
    pivot_at: datetime
    source_timestamps: tuple[datetime, ...]
    evidence: tuple[ShadowComponentEvidence, ...]


@dataclass(frozen=True, slots=True)
class ShadowSetupsResult:
    schema_version: str
    mode: Literal["SHADOW"]
    status: ShadowStatus
    symbol: str
    market: Literal["KRX", "US"]
    evaluated_at: datetime
    source_timestamps: tuple[datetime, ...]
    config_fingerprint: str
    first_pullback: FirstPullbackResult
    nr7_inside_day: Nr7InsideDayResult
    observations: tuple[ShadowSetupObservation, ...]
    evidence: tuple[ShadowComponentEvidence, ...]


@dataclass(frozen=True, slots=True)
class _ContactCluster:
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PreparedBars:
    bars: tuple[PriceBar, ...]
    timestamps: tuple[datetime, ...]


def evaluate_shadow_setups(
    bars: Sequence[PriceBar],
    *,
    symbol: str,
    market: Literal["KRX", "US"],
    as_of: datetime,
    completed_through: datetime | None = None,
    config: ShadowSetupConfig = DEFAULT_SHADOW_SETUP_CONFIG,
) -> ShadowSetupsResult:
    """완료된 과거 bar만으로 두 SHADOW setup을 결정론적으로 평가한다.

    ``completed_through``는 공급자가 완료를 보증한 마지막 시각이다. 미래 또는 그
    시각 뒤의 bar는 계산 전에 제외하므로 동일 과거 prefix의 결과를 바꾸지 않는다.
    """

    evaluated_at = _utc(as_of, "as_of")
    cutoff = _utc(completed_through, "completed_through") if completed_through else evaluated_at
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        return _failed_batch(
            symbol="UNKNOWN",
            market=market,
            evaluated_at=evaluated_at,
            config=config,
            code="invalid_symbol",
            timestamps=(),
        )
    if market not in {"KRX", "US"}:
        return _failed_batch(
            symbol=normalized_symbol,
            market=market,
            evaluated_at=evaluated_at,
            config=config,
            code="invalid_market",
            timestamps=(),
        )
    if cutoff > evaluated_at:
        return _failed_batch(
            symbol=normalized_symbol,
            market=market,
            evaluated_at=evaluated_at,
            config=config,
            code="future_completion_cutoff",
            timestamps=(),
        )

    prepared, failure = _prepare_bars(bars, cutoff=cutoff, as_of=evaluated_at)
    if prepared is None:
        timestamps = failure[1] if failure is not None else ()
        return _failed_batch(
            symbol=normalized_symbol,
            market=market,
            evaluated_at=evaluated_at,
            config=config,
            code=failure[0] if failure is not None else "invalid_bars",
            timestamps=timestamps,
        )

    minimum = max(config.ema_period, config.volume_lookback + 1, config.nr_window)
    if len(prepared.bars) < minimum:
        first = _insufficient_first(prepared.timestamps, minimum)
        compression = _insufficient_compression(prepared.timestamps, minimum)
        evidence = (
            _component(
                ShadowStatus.INSUFFICIENT,
                "history_bars",
                str(len(prepared.bars)),
                str(minimum),
                False,
                prepared.timestamps,
            ),
        )
        return ShadowSetupsResult(
            schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
            mode=SHADOW_MODE,
            status=ShadowStatus.INSUFFICIENT,
            symbol=normalized_symbol,
            market=market,
            evaluated_at=evaluated_at,
            source_timestamps=prepared.timestamps,
            config_fingerprint=config.fingerprint,
            first_pullback=first,
            nr7_inside_day=compression,
            observations=(),
            evidence=evidence,
        )

    data_as_of = prepared.timestamps[-1]
    valid_until = data_as_of + config.validity
    if valid_until <= evaluated_at:
        return _failed_batch(
            symbol=normalized_symbol,
            market=market,
            evaluated_at=evaluated_at,
            config=config,
            code="stale_completed_bar",
            timestamps=prepared.timestamps,
        )

    first = _evaluate_first_pullback(prepared.bars, config=config)
    compression = _evaluate_nr7_inside_day(prepared.bars, config=config)
    observations: list[ShadowSetupObservation] = []
    if config.feature_enabled and first.confirmed:
        assert first.trigger_price is not None
        assert first.resumption_pivot_at is not None
        assert first.valid_until is not None
        observations.append(
            ShadowSetupObservation(
                schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
                mode=SHADOW_MODE,
                status=ShadowStatus.VALID,
                setup="first_pullback",
                subtype=first.contact_label,
                symbol=normalized_symbol,
                market=market,
                observed_at=data_as_of,
                valid_until=first.valid_until,
                trigger_price=first.trigger_price,
                pivot_at=first.resumption_pivot_at,
                source_timestamps=first.source_timestamps,
                evidence=first.evidence,
            )
        )
    if config.feature_enabled and compression.subtype != "none":
        assert compression.trigger_price is not None
        assert compression.pivot_at is not None
        assert compression.valid_until is not None
        observations.append(
            ShadowSetupObservation(
                schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
                mode=SHADOW_MODE,
                status=ShadowStatus.VALID,
                setup="nr7_inside_day",
                subtype=compression.subtype,
                symbol=normalized_symbol,
                market=market,
                observed_at=data_as_of,
                valid_until=compression.valid_until,
                trigger_price=compression.trigger_price,
                pivot_at=compression.pivot_at,
                source_timestamps=compression.source_timestamps,
                evidence=compression.evidence,
            )
        )
    observations.sort(key=lambda item: (item.observed_at, item.setup, item.subtype))
    batch_evidence = (
        _component(
            ShadowStatus.VALID,
            "history_bars",
            str(len(prepared.bars)),
            str(minimum),
            True,
            prepared.timestamps,
        ),
        _component(
            ShadowStatus.VALID,
            "feature_enabled",
            str(config.feature_enabled).lower(),
            None,
            config.feature_enabled,
            (data_as_of,),
        ),
    )
    return ShadowSetupsResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.VALID,
        symbol=normalized_symbol,
        market=market,
        evaluated_at=evaluated_at,
        source_timestamps=prepared.timestamps,
        config_fingerprint=config.fingerprint,
        first_pullback=first,
        nr7_inside_day=compression,
        observations=tuple(observations),
        evidence=batch_evidence,
    )


def _prepare_bars(
    bars: Sequence[PriceBar], *, cutoff: datetime, as_of: datetime
) -> tuple[_PreparedBars | None, tuple[str, tuple[datetime, ...]] | None]:
    retained: list[PriceBar] = []
    for bar in bars:
        try:
            timestamp = _utc(bar.timestamp, "bar.timestamp")
        except (AttributeError, TypeError, ValueError):
            return None, ("invalid_bar_timestamp", ())
        if timestamp > as_of or timestamp > cutoff:
            continue
        try:
            values = tuple(_decimal(getattr(bar, name)) for name in ("open", "high", "low", "close", "volume"))
        except (AttributeError, InvalidOperation, ValueError):
            return None, ("invalid_ohlcv", tuple(item.timestamp for item in retained))
        open_, high, low, close, volume = values
        if (
            any(not value.is_finite() for value in values)
            or any(value <= _ZERO for value in (open_, high, low, close))
            or volume < _ZERO
            or high < max(open_, close)
            or low > min(open_, close)
            or high < low
        ):
            return None, ("invalid_ohlcv", tuple(item.timestamp for item in retained))
        retained.append(
            PriceBar(
                timestamp=timestamp,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    retained.sort(key=lambda item: item.timestamp)
    timestamps = tuple(item.timestamp for item in retained)
    if len(set(timestamps)) != len(timestamps):
        return None, ("duplicate_bar_timestamp", timestamps)
    return _PreparedBars(tuple(retained), timestamps), None


def _evaluate_first_pullback(
    bars: tuple[PriceBar, ...], *, config: ShadowSetupConfig
) -> FirstPullbackResult:
    timestamps = tuple(bar.timestamp for bar in bars)
    emas = _ema(tuple(bar.close for bar in bars), config.ema_period)
    usable_start = config.ema_period - 1
    lookback_start = max(usable_start, len(bars) - config.contact_lookback_bars)
    contact_indices = tuple(
        index
        for index in range(lookback_start, len(bars) - 1)
        if bars[index].low <= emas[index] <= bars[index].high
    )
    clusters = _cluster_contacts(contact_indices, config.contact_cluster_gap_bars)
    if not clusters:
        evidence = (
            _component(
                ShadowStatus.VALID,
                "distinct_contacts",
                "0",
                None,
                False,
                timestamps[lookback_start:],
            ),
        )
        return FirstPullbackResult(
            schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
            mode=SHADOW_MODE,
            status=ShadowStatus.VALID,
            source_timestamps=timestamps,
            contact_label="none",
            distinct_contact_count=0,
            pullback_pivot=None,
            pullback_pivot_at=None,
            resumption_pivot=None,
            resumption_pivot_at=None,
            trigger_price=None,
            pullback_depth=None,
            orderliness=None,
            ema_recovered=False,
            supportive_volume=False,
            confirmed=False,
            valid_until=bars[-1].timestamp + config.validity,
            evidence=evidence,
        )

    cluster = clusters[-1]
    label: Literal["first", "second", "later"]
    if len(clusters) == 1:
        label = "first"
    elif len(clusters) == 2:
        label = "second"
    else:
        label = "later"
    pullback_index = min(cluster.indices, key=lambda index: (bars[index].low, bars[index].timestamp))
    pullback_pivot = bars[pullback_index].low
    reference_start = max(lookback_start, cluster.indices[0] - config.pullback_reference_bars)
    reference_indices = tuple(range(reference_start, cluster.indices[0]))
    if not reference_indices:
        reference_indices = (cluster.indices[0],)
    swing_index = max(reference_indices, key=lambda index: (bars[index].high, -index))
    swing_high = bars[swing_index].high
    pullback_depth = max(_ZERO, (swing_high - pullback_pivot) / swing_high)
    orderly_indices = tuple(range(swing_index, pullback_index + 1))
    orderliness = _orderliness(bars, orderly_indices)

    prior_to_current = tuple(range(cluster.indices[0], len(bars) - 1))
    resumption_index = max(
        prior_to_current,
        key=lambda index: (bars[index].high, -index),
    )
    resumption_pivot = bars[resumption_index].high
    trigger_price = resumption_pivot
    latest = bars[-1]
    ema_recovered = latest.close > emas[-1]
    trigger_cleared = latest.close > trigger_price
    prior_volumes = tuple(bar.volume for bar in bars[-(config.volume_lookback + 1) : -1])
    average_volume = _mean(prior_volumes)
    volume_ratio = latest.volume / average_volume if average_volume > _ZERO else _ZERO
    supportive_volume = (
        average_volume > _ZERO
        and volume_ratio >= config.minimum_supportive_volume_ratio
    )
    depth_passed = pullback_depth <= config.maximum_pullback_depth
    orderly = orderliness >= config.minimum_orderliness
    confirmed = (
        ema_recovered
        and trigger_cleared
        and supportive_volume
        and depth_passed
        and orderly
    )
    valid_until = latest.timestamp + config.validity
    evidence = (
        _component(
            ShadowStatus.VALID,
            "distinct_contacts",
            str(len(clusters)),
            None,
            True,
            tuple(bars[index].timestamp for index in contact_indices),
        ),
        _component(
            ShadowStatus.VALID,
            "contact_label",
            label,
            None,
            True,
            tuple(bars[index].timestamp for index in cluster.indices),
        ),
        _component(
            ShadowStatus.VALID,
            "pullback_pivot",
            _text(pullback_pivot),
            None,
            True,
            (bars[pullback_index].timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "resumption_pivot",
            _text(resumption_pivot),
            None,
            True,
            (bars[resumption_index].timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "trigger_price",
            _text(trigger_price),
            "close > trigger_price",
            trigger_cleared,
            (bars[resumption_index].timestamp, latest.timestamp),
        ),
        _component(
            ShadowStatus.VALID,
            "pullback_depth",
            _text(pullback_depth),
            _text(config.maximum_pullback_depth),
            depth_passed,
            (bars[swing_index].timestamp, bars[pullback_index].timestamp),
        ),
        _component(
            ShadowStatus.VALID,
            "orderliness",
            _text(orderliness),
            _text(config.minimum_orderliness),
            orderly,
            tuple(bars[index].timestamp for index in orderly_indices),
        ),
        _component(
            ShadowStatus.VALID,
            "current_close",
            _text(latest.close),
            None,
            None,
            (latest.timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "ema10_recovery",
            _text(emas[-1]),
            "close > EMA10",
            ema_recovered,
            (latest.timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "current_volume",
            _text(latest.volume),
            None,
            None,
            (latest.timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "prior_volume_average",
            _text(average_volume),
            f"{config.volume_lookback} completed bars excluding current",
            average_volume > _ZERO,
            tuple(bar.timestamp for bar in bars[-(config.volume_lookback + 1) : -1]),
        ),
        _component(
            ShadowStatus.VALID,
            "supportive_volume_ratio",
            _text(volume_ratio),
            _text(config.minimum_supportive_volume_ratio),
            supportive_volume,
            tuple(bar.timestamp for bar in bars[-(config.volume_lookback + 1) :]),
        ),
        _component(
            ShadowStatus.VALID,
            "confirmed",
            str(confirmed).lower(),
            "all first-pullback components",
            confirmed,
            timestamps,
        ),
    )
    return FirstPullbackResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.VALID,
        source_timestamps=timestamps,
        contact_label=label,
        distinct_contact_count=len(clusters),
        pullback_pivot=_quantize(pullback_pivot),
        pullback_pivot_at=bars[pullback_index].timestamp,
        resumption_pivot=_quantize(resumption_pivot),
        resumption_pivot_at=bars[resumption_index].timestamp,
        trigger_price=_quantize(trigger_price),
        pullback_depth=_quantize(pullback_depth),
        orderliness=_quantize(orderliness),
        ema_recovered=ema_recovered,
        supportive_volume=supportive_volume,
        confirmed=confirmed,
        valid_until=valid_until,
        evidence=evidence,
    )


def _evaluate_nr7_inside_day(
    bars: tuple[PriceBar, ...], *, config: ShadowSetupConfig
) -> Nr7InsideDayResult:
    timestamps = tuple(bar.timestamp for bar in bars)
    current = bars[-1]
    previous = bars[-2]
    window = bars[-config.nr_window :]
    ranges = tuple(bar.high - bar.low for bar in window)
    current_range = ranges[-1]
    nr7 = current_range <= min(ranges)
    inside_day = current.high < previous.high and current.low > previous.low
    if nr7 and inside_day:
        subtype: Literal["none", "nr7", "inside_day", "nr7_inside_day"] = "nr7_inside_day"
    elif nr7:
        subtype = "nr7"
    elif inside_day:
        subtype = "inside_day"
    else:
        subtype = "none"
    prior_volumes = tuple(bar.volume for bar in bars[-(config.volume_lookback + 1) : -1])
    average_volume = _mean(prior_volumes)
    volume_ratio = current.volume / average_volume if average_volume > _ZERO else _ZERO
    volume_contracted = average_volume > _ZERO and volume_ratio < _ONE
    pivot = current.high if subtype != "none" else None
    valid_until = current.timestamp + config.validity if pivot is not None else None
    evidence = (
        _component(
            ShadowStatus.VALID,
            "nr7_range",
            _text(current_range),
            f"minimum of {config.nr_window}; ties accepted",
            nr7,
            tuple(bar.timestamp for bar in window),
        ),
        _component(
            ShadowStatus.VALID,
            "strict_inside_day",
            f"{_text(current.low)}<{_text(current.high)}",
            f"{_text(previous.low)} < current < {_text(previous.high)}",
            inside_day,
            (previous.timestamp, current.timestamp),
        ),
        _component(
            ShadowStatus.VALID,
            "subtype",
            subtype,
            None,
            subtype != "none",
            (current.timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "current_volume",
            _text(current.volume),
            None,
            None,
            (current.timestamp,),
        ),
        _component(
            ShadowStatus.VALID,
            "prior_volume_average",
            _text(average_volume),
            f"{config.volume_lookback} completed bars excluding current",
            average_volume > _ZERO,
            tuple(bar.timestamp for bar in bars[-(config.volume_lookback + 1) : -1]),
        ),
        _component(
            ShadowStatus.VALID,
            "volume_contraction_ratio",
            _text(volume_ratio),
            "< 1",
            volume_contracted,
            tuple(bar.timestamp for bar in bars[-(config.volume_lookback + 1) :]),
        ),
        _component(
            ShadowStatus.VALID,
            "trigger_price",
            _text(pivot) if pivot is not None else "",
            "completed setup high",
            pivot is not None,
            (current.timestamp,),
        ),
    )
    return Nr7InsideDayResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.VALID,
        source_timestamps=timestamps,
        subtype=subtype,
        nr7=nr7,
        inside_day=inside_day,
        volume_contracted=volume_contracted,
        pivot=_quantize(pivot) if pivot is not None else None,
        pivot_at=current.timestamp if pivot is not None else None,
        trigger_price=_quantize(pivot) if pivot is not None else None,
        valid_until=valid_until,
        evidence=evidence,
    )


def _cluster_contacts(indices: tuple[int, ...], gap_bars: int) -> tuple[_ContactCluster, ...]:
    if not indices:
        return ()
    clusters: list[list[int]] = [[indices[0]]]
    for index in indices[1:]:
        untouched_bars = index - clusters[-1][-1] - 1
        if untouched_bars <= gap_bars:
            clusters[-1].append(index)
        else:
            clusters.append([index])
    return tuple(_ContactCluster(tuple(cluster)) for cluster in clusters)


def _ema(values: tuple[Decimal, ...], period: int) -> tuple[Decimal, ...]:
    multiplier = Decimal(2) / Decimal(period + 1)
    seed = _mean(values[:period])
    result = [seed] * period
    current = seed
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return tuple(result)


def _orderliness(bars: tuple[PriceBar, ...], indices: tuple[int, ...]) -> Decimal:
    if len(indices) < 2:
        return _ONE
    comparisons = 0
    orderly = 0
    for previous_index, current_index in zip(indices, indices[1:]):
        comparisons += 2
        if bars[current_index].high <= bars[previous_index].high:
            orderly += 1
        if bars[current_index].low <= bars[previous_index].low:
            orderly += 1
    return Decimal(orderly) / Decimal(comparisons)


def _insufficient_first(
    timestamps: tuple[datetime, ...], minimum: int
) -> FirstPullbackResult:
    evidence = (
        _component(
            ShadowStatus.INSUFFICIENT,
            "history_bars",
            str(len(timestamps)),
            str(minimum),
            False,
            timestamps,
        ),
    )
    return FirstPullbackResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.INSUFFICIENT,
        source_timestamps=timestamps,
        contact_label="none",
        distinct_contact_count=0,
        pullback_pivot=None,
        pullback_pivot_at=None,
        resumption_pivot=None,
        resumption_pivot_at=None,
        trigger_price=None,
        pullback_depth=None,
        orderliness=None,
        ema_recovered=False,
        supportive_volume=False,
        confirmed=False,
        valid_until=None,
        evidence=evidence,
    )


def _insufficient_compression(
    timestamps: tuple[datetime, ...], minimum: int
) -> Nr7InsideDayResult:
    evidence = (
        _component(
            ShadowStatus.INSUFFICIENT,
            "history_bars",
            str(len(timestamps)),
            str(minimum),
            False,
            timestamps,
        ),
    )
    return Nr7InsideDayResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.INSUFFICIENT,
        source_timestamps=timestamps,
        subtype="none",
        nr7=False,
        inside_day=False,
        volume_contracted=False,
        pivot=None,
        pivot_at=None,
        trigger_price=None,
        valid_until=None,
        evidence=evidence,
    )


def _failed_batch(
    *,
    symbol: str,
    market: Literal["KRX", "US"],
    evaluated_at: datetime,
    config: ShadowSetupConfig,
    code: str,
    timestamps: tuple[datetime, ...],
) -> ShadowSetupsResult:
    evidence = (
        _component(
            ShadowStatus.FAIL_CLOSED,
            code,
            "",
            None,
            False,
            timestamps,
        ),
    )
    first = FirstPullbackResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.FAIL_CLOSED,
        source_timestamps=timestamps,
        contact_label="none",
        distinct_contact_count=0,
        pullback_pivot=None,
        pullback_pivot_at=None,
        resumption_pivot=None,
        resumption_pivot_at=None,
        trigger_price=None,
        pullback_depth=None,
        orderliness=None,
        ema_recovered=False,
        supportive_volume=False,
        confirmed=False,
        valid_until=None,
        evidence=evidence,
    )
    compression = Nr7InsideDayResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.FAIL_CLOSED,
        source_timestamps=timestamps,
        subtype="none",
        nr7=False,
        inside_day=False,
        volume_contracted=False,
        pivot=None,
        pivot_at=None,
        trigger_price=None,
        valid_until=None,
        evidence=evidence,
    )
    return ShadowSetupsResult(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=ShadowStatus.FAIL_CLOSED,
        symbol=symbol,
        market=market,
        evaluated_at=evaluated_at,
        source_timestamps=timestamps,
        config_fingerprint=config.fingerprint,
        first_pullback=first,
        nr7_inside_day=compression,
        observations=(),
        evidence=evidence,
    )


def _component(
    status: ShadowStatus,
    code: str,
    value: str,
    threshold: str | None,
    passed: bool | None,
    timestamps: tuple[datetime, ...],
) -> ShadowComponentEvidence:
    return ShadowComponentEvidence(
        schema_version=SHADOW_SETUPS_SCHEMA_VERSION,
        mode=SHADOW_MODE,
        status=status,
        code=code,
        value=value,
        threshold=threshold,
        passed=passed,
        source_timestamps=timestamps,
    )


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)




def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, start=_ZERO) / Decimal(len(values))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _text(value: Decimal) -> str:
    return format(_quantize(value), "f")
def evaluate_ranked_shadow_setups(
    candidate_keys: Sequence[tuple[str, str]],
    histories: Mapping[tuple[str, str], Sequence[PriceBar]],
    *,
    as_of: datetime,
    limit: int = 5,
    config: ShadowSetupConfig = DEFAULT_SHADOW_SETUP_CONFIG,
) -> tuple[ShadowSetupsResult, ...]:
    """상위 후보를 runtime/backtest 공용 detector로 SHADOW 관찰한다."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if not config.feature_enabled:
        return ()
    results: list[ShadowSetupsResult] = []
    seen: set[tuple[str, str]] = set()
    for raw_market, raw_symbol in candidate_keys:
        market = str(raw_market).strip().upper()
        symbol = str(raw_symbol).strip().upper()
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        bars = histories.get(key)
        if not bars:
            continue
        setup_market: Literal["KRX", "US"]
        if market == "KR":
            setup_market = "KRX"
        elif market == "US":
            setup_market = "US"
        else:
            continue
        results.append(
            evaluate_shadow_setups(
                bars,
                symbol=symbol,
                market=setup_market,
                as_of=as_of,
                config=config,
            )
        )
        if len(results) >= limit:
            break
    return tuple(results)


def shadow_setups_evidence(result: ShadowSetupsResult) -> dict[str, object]:
    """Return JSON-safe evidence without exposing it to order decisions."""

    return {
        "schemaVersion": result.schema_version,
        "mode": result.mode,
        "status": result.status.value,
        "symbol": result.symbol,
        "market": result.market,
        "evaluatedAt": _timestamp_text(result.evaluated_at),
        "configFingerprint": result.config_fingerprint,
        "firstPullback": {
            "status": result.first_pullback.status.value,
            "contactLabel": result.first_pullback.contact_label,
            "distinctContactCount": result.first_pullback.distinct_contact_count,
            "confirmed": result.first_pullback.confirmed,
            "triggerPrice": _optional_decimal_text(
                result.first_pullback.trigger_price
            ),
            "validUntil": _optional_timestamp_text(
                result.first_pullback.valid_until
            ),
        },
        "nr7InsideDay": {
            "status": result.nr7_inside_day.status.value,
            "subtype": result.nr7_inside_day.subtype,
            "nr7": result.nr7_inside_day.nr7,
            "insideDay": result.nr7_inside_day.inside_day,
            "volumeContracted": result.nr7_inside_day.volume_contracted,
            "triggerPrice": _optional_decimal_text(
                result.nr7_inside_day.trigger_price
            ),
            "validUntil": _optional_timestamp_text(
                result.nr7_inside_day.valid_until
            ),
        },
        "observations": [
            {
                "setup": observation.setup,
                "subtype": observation.subtype,
                "triggerPrice": _text(observation.trigger_price),
                "pivotAt": _timestamp_text(observation.pivot_at),
                "validUntil": _timestamp_text(observation.valid_until),
            }
            for observation in result.observations
        ],
    }


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp_text(value: datetime | None) -> str | None:
    return _timestamp_text(value) if value is not None else None


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return _text(value) if value is not None else None




__all__ = [
    "DEFAULT_SHADOW_SETUP_CONFIG",
    "SHADOW_SETUPS_CONFIG_SCHEMA_VERSION",
    "SHADOW_SETUPS_SCHEMA_VERSION",
    "FirstPullbackResult",
    "Nr7InsideDayResult",
    "ShadowComponentEvidence",
    "ShadowSetupConfig",
    "ShadowSetupObservation",
    "ShadowSetupsResult",
    "evaluate_ranked_shadow_setups",
    "shadow_setups_evidence",
    "ShadowStatus",
    "evaluate_shadow_setups",
]
