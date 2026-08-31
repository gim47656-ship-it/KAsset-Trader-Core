"""활성 주문 경로와 분리된 결정론적 SHADOW 목표 비중 배분기."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
from typing import Literal

from app.extensions.kasset.automation.candidate_ranker import (
    CandidateKey,
    CandidateRankResult,
    MarketKey,
)

SHADOW_SELECTION_SCHEMA_VERSION = "kasset.shadow-selection.v1"
SHADOW_SELECTION_EVIDENCE_VERSION = "kasset.shadow-selection-evidence.v1"
SHADOW_SELECTION_CONFIG_SCHEMA_VERSION = "kasset.shadow-selection-config.v1"
SHADOW_MODE: Literal["SHADOW"] = "SHADOW"
UNKNOWN_SECTOR = "UNKNOWN"

_ZERO = Decimal("0")
_ONE = Decimal("1")
_QUANTUM = Decimal("0.00000001")


class ShadowSelectionStatus(StrEnum):
    """외부 소비자가 추론 없이 처리할 수 있는 닫힌 상태 집합."""

    VALID = "valid"
    INSUFFICIENT = "insufficient"
    FAIL_CLOSED = "fail_closed"


class ShadowAdjustmentKind(StrEnum):
    REDUCE_NON_TOP_K = "reduce_non_top_k"
    REDUCE_OVERWEIGHT = "reduce_overweight"
    INCREASE_UNDERWEIGHT = "increase_underweight"
    NO_TRADE_BAND = "no_trade_band"
    SECTOR_CAP = "sector_cap"
    ATR_CAP = "atr_cap"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class ShadowSelectionConfig:
    """활성 전략 설정과 독립적인 불변 SHADOW 배분 설정."""

    top_k: int = 5
    target_investment_weight: Decimal = Decimal("0.90")
    maximum_rebalance_delta: Decimal = Decimal("0.10")
    no_trade_band: Decimal = Decimal("0.01")
    sector_weight_cap: Decimal = Decimal("0.30")
    emit_evidence: bool = True

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be positive")
        for name in (
            "target_investment_weight",
            "maximum_rebalance_delta",
            "no_trade_band",
            "sector_weight_cap",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if not (_ZERO < self.target_investment_weight <= _ONE):
            raise ValueError("target_investment_weight must be in (0, 1]")
        if not (_ZERO < self.maximum_rebalance_delta <= _ONE):
            raise ValueError("maximum_rebalance_delta must be in (0, 1]")
        if not (_ZERO <= self.no_trade_band <= _ONE):
            raise ValueError("no_trade_band must be in [0, 1]")
        if not (_ZERO < self.sector_weight_cap <= _ONE):
            raise ValueError("sector_weight_cap must be in (0, 1]")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "configSchemaVersion": SHADOW_SELECTION_CONFIG_SCHEMA_VERSION,
            "topK": self.top_k,
            "targetInvestmentWeight": _text(self.target_investment_weight),
            "maximumRebalanceDelta": _text(self.maximum_rebalance_delta),
            "noTradeBand": _text(self.no_trade_band),
            "sectorWeightCap": _text(self.sector_weight_cap),
            "emitEvidence": self.emit_evidence,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.fingerprint_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return sha256(encoded).hexdigest()


DEFAULT_SHADOW_SELECTION_CONFIG = ShadowSelectionConfig()


@dataclass(frozen=True, slots=True)
class ShadowSelectionPosition:
    """현재 비중과 후보의 안정적 섹터 키를 함께 전달하는 입력."""

    market: MarketKey
    symbol: str
    current_weight: Decimal
    sector_key: str | None
    source_timestamp: datetime

    @property
    def key(self) -> CandidateKey:
        return self.market, self.symbol


@dataclass(frozen=True, slots=True)
class ShadowSectorExposure:
    """배분 직전 이미 예상되는 섹터 비중 입력."""

    sector_key: str
    projected_weight: Decimal
    source_timestamp: datetime


@dataclass(frozen=True, slots=True)
class ShadowAtrCeiling:
    """기존 ATR 산출물을 대체하지 않고 상한으로만 소비하는 입력."""

    market: MarketKey
    symbol: str
    status: ShadowSelectionStatus
    maximum_allocation_weight: Decimal | None
    maximum_quantity: Decimal | None
    source_timestamp: datetime

    @property
    def key(self) -> CandidateKey:
        return self.market, self.symbol


@dataclass(frozen=True, slots=True)
class ShadowSectorCoverage:
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowSelectionStatus
    source_timestamps: tuple[datetime, ...]
    known_count: int
    unknown_count: int
    unknown_keys: tuple[CandidateKey, ...]

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "knownCount": self.known_count,
            "unknownCount": self.unknown_count,
            "unknownKeys": [
                {"market": market, "symbol": symbol}
                for market, symbol in self.unknown_keys
            ],
        }


@dataclass(frozen=True, slots=True)
class ShadowSelectionEvidence:
    """한 번의 가상 조정에 대한 before/after/comparison 근거."""

    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowSelectionStatus
    source_timestamps: tuple[datetime, ...]
    kind: ShadowAdjustmentKind
    market: MarketKey | None
    symbol: str | None
    sector_key: str | None
    before_weight: Decimal | None
    after_weight: Decimal | None
    comparison_delta: Decimal | None
    sector_before_weight: Decimal | None
    sector_after_weight: Decimal | None
    detail: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "kind": self.kind.value,
            "key": (
                {"market": self.market, "symbol": self.symbol}
                if self.market is not None and self.symbol is not None
                else None
            ),
            "sectorKey": self.sector_key,
            "before": {
                "weight": _optional_text(self.before_weight),
                "sectorWeight": _optional_text(self.sector_before_weight),
            },
            "after": {
                "weight": _optional_text(self.after_weight),
                "sectorWeight": _optional_text(self.sector_after_weight),
            },
            "comparison": {"delta": _optional_text(self.comparison_delta)},
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ShadowTargetAllocation:
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowSelectionStatus
    source_timestamps: tuple[datetime, ...]
    market: MarketKey
    symbol: str
    sector_key: str
    selected: bool
    rank_score: Decimal | None
    current_weight: Decimal
    unconstrained_target_weight: Decimal
    target_weight: Decimal
    comparison_delta: Decimal
    atr_allocation_ceiling: Decimal | None
    atr_quantity_ceiling: Decimal | None
    sell_risk_reduction_allowed: bool

    @property
    def key(self) -> CandidateKey:
        return self.market, self.symbol

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "key": {"market": self.market, "symbol": self.symbol},
            "sectorKey": self.sector_key,
            "selected": self.selected,
            "rankScore": _optional_text(self.rank_score),
            "before": {"weight": _text(self.current_weight)},
            "after": {
                "unconstrainedTargetWeight": _text(self.unconstrained_target_weight),
                "targetWeight": _text(self.target_weight),
                "atrAllocationCeiling": _optional_text(self.atr_allocation_ceiling),
                "atrQuantityCeiling": _optional_text(self.atr_quantity_ceiling),
            },
            "comparison": {"delta": _text(self.comparison_delta)},
            "sellRiskReductionAllowed": self.sell_risk_reduction_allowed,
        }


@dataclass(frozen=True, slots=True)
class ShadowSelectionResult:
    schema_version: str
    evidence_version: str
    mode: Literal["SHADOW"]
    status: ShadowSelectionStatus
    evaluated_at: datetime
    source_timestamps: tuple[datetime, ...]
    config_fingerprint: str
    selected_keys: tuple[CandidateKey, ...]
    released_weight: Decimal
    preexisting_investment_headroom: Decimal
    buy_budget: Decimal
    allocations: tuple[ShadowTargetAllocation, ...]
    projected_sector_exposures: tuple[tuple[str, Decimal], ...]
    sector_coverage: ShadowSectorCoverage
    sell_risk_reduction_allowed: bool
    evidence: tuple[ShadowSelectionEvidence, ...]

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "evidenceVersion": self.evidence_version,
            "mode": self.mode,
            "status": self.status.value,
            "evaluatedAt": _timestamp_text(self.evaluated_at),
            "sourceTimestamps": [
                _timestamp_text(item) for item in self.source_timestamps
            ],
            "shadowConfigFingerprint": self.config_fingerprint,
            "selectedKeys": [
                {"market": market, "symbol": symbol}
                for market, symbol in self.selected_keys
            ],
            "budget": {
                "releasedWeight": _text(self.released_weight),
                "preexistingInvestmentHeadroom": _text(
                    self.preexisting_investment_headroom
                ),
                "buyBudget": _text(self.buy_budget),
            },
            "allocations": [item.as_evidence() for item in self.allocations],
            "projectedSectorExposures": [
                {"sectorKey": sector, "weight": _text(weight)}
                for sector, weight in self.projected_sector_exposures
            ],
            "sectorCoverage": self.sector_coverage.as_evidence(),
            "sellRiskReductionAllowed": self.sell_risk_reduction_allowed,
            "evidence": [item.as_evidence() for item in self.evidence],
        }


class _CalculationFailure(ValueError):
    pass


def allocate_shadow_targets(
    rankings: Sequence[CandidateRankResult],
    positions: Sequence[ShadowSelectionPosition],
    sector_exposures: Sequence[ShadowSectorExposure],
    *,
    evaluated_at: datetime,
    atr_ceilings: Sequence[ShadowAtrCeiling] = (),
    config: ShadowSelectionConfig = DEFAULT_SHADOW_SELECTION_CONFIG,
) -> ShadowSelectionResult:
    """점수와 현재 비중만으로 주문 없는 SHADOW 목표 비중을 계산한다.

    감축을 먼저 반영하고 그때 해제된 비중과 기존 투자 여유만 매수 예산으로
    사용한다. ATR 입력이 실패 상태이거나 계산 입력이 유효하지 않으면 어떤 종목도
    늘리지 않는 닫힌 결과를 반환한다.
    """

    normalized_evaluated_at = _aware_utc(evaluated_at, "evaluated_at")
    raw_timestamps = _collect_valid_timestamps(
        rankings, positions, sector_exposures, atr_ceilings, normalized_evaluated_at
    )
    try:
        prepared = _prepare_inputs(
            rankings, positions, sector_exposures, atr_ceilings, normalized_evaluated_at
        )
    except (InvalidOperation, TypeError, _CalculationFailure) as exc:
        return _closed_result(
            rankings,
            positions,
            evaluated_at=normalized_evaluated_at,
            source_timestamps=raw_timestamps,
            config=config,
            status=ShadowSelectionStatus.FAIL_CLOSED,
            detail=str(exc),
        )

    rank_by_key, position_by_key, exposure_by_sector, atr_by_key, timestamps = prepared
    ranked = sorted(
        (item for item in rank_by_key.values() if item.included),
        key=lambda item: (-item.total_score, item.symbol, item.market),
    )
    if not ranked:
        return _closed_result(
            rankings,
            positions,
            evaluated_at=normalized_evaluated_at,
            source_timestamps=timestamps,
            config=config,
            status=ShadowSelectionStatus.INSUFFICIENT,
            detail="포함 가능한 CandidateRankResult가 없습니다.",
        )

    failed_atr_keys = tuple(
        sorted(
            (
                key
                for key, ceiling in atr_by_key.items()
                if ceiling.status != ShadowSelectionStatus.VALID
            ),
            key=lambda key: (key[1], key[0]),
        )
    )
    if failed_atr_keys:
        return _closed_result(
            rankings,
            positions,
            evaluated_at=normalized_evaluated_at,
            source_timestamps=timestamps,
            config=config,
            status=ShadowSelectionStatus.FAIL_CLOSED,
            detail="ATR 상한 상태가 valid가 아니므로 비중 증가를 닫았습니다.",
        )

    selected = tuple(ranked[: config.top_k])
    selected_keys = tuple(item.key for item in selected)
    selected_set = frozenset(selected_keys)
    all_keys = set(position_by_key) | set(rank_by_key)
    current = {
        key: position_by_key[key].current_weight if key in position_by_key else _ZERO
        for key in all_keys
    }
    sectors = {
        key: (
            _sector(position_by_key[key].sector_key)
            if key in position_by_key
            else UNKNOWN_SECTOR
        )
        for key in all_keys
    }
    ideal = _q(config.target_investment_weight / Decimal(len(selected)))
    targets = dict(current)
    unconstrained = dict(current)
    exposures = dict(exposure_by_sector)
    for sector in sectors.values():
        exposures.setdefault(sector, _ZERO)

    evidence: list[ShadowSelectionEvidence] = []
    original_total = sum(current.values(), _ZERO)
    preexisting_headroom = max(_ZERO, config.target_investment_weight - original_total)

    non_selected_order = sorted(
        all_keys - selected_set, key=lambda key: (key[1], key[0])
    )
    for key in non_selected_order:
        _apply_reduction(
            key,
            desired=_ZERO,
            kind=ShadowAdjustmentKind.REDUCE_NON_TOP_K,
            current=current,
            targets=targets,
            unconstrained=unconstrained,
            sectors=sectors,
            exposures=exposures,
            timestamps=timestamps,
            config=config,
            evidence=evidence,
        )

    for item in selected:
        ceiling = atr_by_key.get(item.key)
        desired = ideal
        kind = ShadowAdjustmentKind.REDUCE_OVERWEIGHT
        if ceiling is not None and ceiling.maximum_allocation_weight is not None:
            desired = min(desired, ceiling.maximum_allocation_weight)
            if desired < ideal:
                kind = ShadowAdjustmentKind.ATR_CAP
        _apply_reduction(
            item.key,
            desired=desired,
            kind=kind,
            current=current,
            targets=targets,
            unconstrained=unconstrained,
            sectors=sectors,
            exposures=exposures,
            timestamps=timestamps,
            config=config,
            evidence=evidence,
        )

    # 기존 ATR 상한은 일반 리밸런싱 속도 제한보다 바깥의 안전 상한이다.
    # 따라서 가상 목표도 상한을 넘긴 채 남겨 두지 않는다.
    for item in selected:
        ceiling = atr_by_key.get(item.key)
        if ceiling is None or ceiling.maximum_allocation_weight is None:
            continue
        key = item.key
        if targets[key] <= ceiling.maximum_allocation_weight:
            continue
        sector = sectors[key]
        before = targets[key]
        sector_before = exposures[sector]
        after = ceiling.maximum_allocation_weight
        sector_after = _q(sector_before - (before - after))
        targets[key] = after
        exposures[sector] = sector_after
        _record_evidence(
            evidence,
            config=config,
            timestamps=timestamps,
            status=ShadowSelectionStatus.VALID,
            kind=ShadowAdjustmentKind.ATR_CAP,
            key=key,
            sector=sector,
            before=before,
            after=after,
            sector_before=sector_before,
            sector_after=sector_after,
            detail="일반 조정 뒤에도 기존 ATR 배분 상한을 넘지 않도록 min을 적용했습니다.",
        )

    released = _q(sum((current[key] - targets[key] for key in all_keys), _ZERO))
    after_reductions_total = sum(targets.values(), _ZERO)
    investment_headroom = max(
        _ZERO, config.target_investment_weight - after_reductions_total
    )
    buy_budget = _q(min(investment_headroom, released + preexisting_headroom))

    shortfalls: dict[CandidateKey, Decimal] = {}
    for item in selected:
        desired = ideal
        shortfall = max(_ZERO, desired - targets[item.key])
        if shortfall <= config.no_trade_band:
            if shortfall > _ZERO:
                _record_evidence(
                    evidence,
                    config=config,
                    timestamps=timestamps,
                    status=ShadowSelectionStatus.VALID,
                    kind=ShadowAdjustmentKind.NO_TRADE_BAND,
                    key=item.key,
                    sector=sectors[item.key],
                    before=targets[item.key],
                    after=targets[item.key],
                    sector_before=exposures[sectors[item.key]],
                    sector_after=exposures[sectors[item.key]],
                    detail="목표와 현재 비중 차이가 no-trade band 이내입니다.",
                )
            continue
        shortfalls[item.key] = shortfall

    total_shortfall = sum(shortfalls.values(), _ZERO)
    if buy_budget > _ZERO and total_shortfall > _ZERO:
        unconstrained_deltas: dict[CandidateKey, Decimal] = {}
        atr_deltas: dict[CandidateKey, Decimal] = {}
        sector_delta_totals: dict[str, Decimal] = {}
        for item in selected:
            key = item.key
            shortfall = shortfalls.get(key)
            if shortfall is None:
                continue
            proportional = buy_budget * shortfall / total_shortfall
            delta_room = max(
                _ZERO,
                current[key] + config.maximum_rebalance_delta - targets[key],
            )
            unconstrained_delta = min(shortfall, proportional, delta_room)
            unconstrained_deltas[key] = unconstrained_delta
            unconstrained[key] = _q(targets[key] + unconstrained_delta)

            ceiling = atr_by_key.get(key)
            atr_room = unconstrained_delta
            if ceiling is not None and ceiling.maximum_allocation_weight is not None:
                atr_room = max(
                    _ZERO, ceiling.maximum_allocation_weight - targets[key]
                )
            atr_delta = min(unconstrained_delta, atr_room)
            atr_deltas[key] = atr_delta
            sector = sectors[key]
            sector_delta_totals[sector] = (
                sector_delta_totals.get(sector, _ZERO) + atr_delta
            )

        sector_scales: dict[str, Decimal] = {}
        for sector, proposed_delta in sector_delta_totals.items():
            sector_room = max(
                _ZERO, config.sector_weight_cap - exposures[sector]
            )
            sector_scales[sector] = (
                min(_ONE, sector_room / proposed_delta)
                if proposed_delta > _ZERO
                else _ZERO
            )

        remaining_buy_budget = buy_budget
        for item in selected:
            key = item.key
            if key not in unconstrained_deltas:
                continue
            sector = sectors[key]
            unconstrained_delta = unconstrained_deltas[key]
            atr_delta = atr_deltas[key]
            sector_room = max(
                _ZERO, config.sector_weight_cap - exposures[sector]
            )
            applied_delta = _q(
                min(
                    atr_delta * sector_scales[sector],
                    sector_room,
                    remaining_buy_budget,
                )
            )
            before = targets[key]
            sector_before = exposures[sector]
            after = _q(before + applied_delta)
            sector_after = _q(sector_before + applied_delta)
            targets[key] = after
            exposures[sector] = sector_after
            remaining_buy_budget = _q(remaining_buy_budget - applied_delta)

            kind = ShadowAdjustmentKind.INCREASE_UNDERWEIGHT
            detail = "부족 비중에 비례해 SHADOW 매수 예산을 배분했습니다."
            if atr_delta < unconstrained_delta:
                kind = ShadowAdjustmentKind.ATR_CAP
                detail = "기존 ATR 배분 상한과 min으로 결합했습니다."
            elif sector_scales[sector] < _ONE:
                kind = ShadowAdjustmentKind.SECTOR_CAP
                detail = "SHADOW 목표만 섹터 상한에 맞춰 비례 축소했습니다."
            _record_evidence(
                evidence,
                config=config,
                timestamps=timestamps,
                status=ShadowSelectionStatus.VALID,
                kind=kind,
                key=key,
                sector=sector,
                before=before,
                after=after,
                sector_before=sector_before,
                sector_after=sector_after,
                detail=detail,
            )

    allocation_order = selected_keys + tuple(
        key for key in non_selected_order if key not in selected_set
    )
    allocations = tuple(
        _allocation(
            key,
            status=ShadowSelectionStatus.VALID,
            timestamps=timestamps,
            selected=key in selected_set,
            rank=rank_by_key.get(key),
            sector=sectors[key],
            current=current[key],
            unconstrained=unconstrained[key],
            target=targets[key],
            atr=atr_by_key.get(key),
        )
        for key in allocation_order
    )
    coverage = _coverage(sectors, timestamps, ShadowSelectionStatus.VALID)
    return ShadowSelectionResult(
        schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
        evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=ShadowSelectionStatus.VALID,
        evaluated_at=normalized_evaluated_at,
        source_timestamps=timestamps,
        config_fingerprint=config.fingerprint,
        selected_keys=selected_keys,
        released_weight=released,
        preexisting_investment_headroom=_q(preexisting_headroom),
        buy_budget=buy_budget,
        allocations=allocations,
        projected_sector_exposures=tuple(
            (sector, _q(weight)) for sector, weight in sorted(exposures.items())
        ),
        sector_coverage=coverage,
        sell_risk_reduction_allowed=True,
        evidence=tuple(evidence) if config.emit_evidence else (),
    )


def _prepare_inputs(
    rankings: Sequence[CandidateRankResult],
    positions: Sequence[ShadowSelectionPosition],
    sector_exposures: Sequence[ShadowSectorExposure],
    atr_ceilings: Sequence[ShadowAtrCeiling],
    evaluated_at: datetime,
) -> tuple[
    dict[CandidateKey, CandidateRankResult],
    dict[CandidateKey, ShadowSelectionPosition],
    dict[str, Decimal],
    dict[CandidateKey, ShadowAtrCeiling],
    tuple[datetime, ...],
]:
    rank_by_key: dict[CandidateKey, CandidateRankResult] = {}
    for item in rankings:
        _validate_key(item.market, item.symbol)
        if item.key in rank_by_key:
            raise _CalculationFailure("duplicate ranking key")
        _finite_ratio(item.total_score, "total_score", allow_above_one=True)
        if item.data_as_of is not None:
            _source_time(item.data_as_of, evaluated_at, "ranking data_as_of")
        rank_by_key[item.key] = item

    position_by_key: dict[CandidateKey, ShadowSelectionPosition] = {}
    for item in positions:
        _validate_key(item.market, item.symbol)
        if item.key in position_by_key:
            raise _CalculationFailure("duplicate position key")
        _finite_ratio(item.current_weight, "current_weight")
        _source_time(item.source_timestamp, evaluated_at, "position source_timestamp")
        position_by_key[item.key] = item
    if sum((item.current_weight for item in positions), _ZERO) > _ONE:
        raise _CalculationFailure("current weights exceed one")

    exposure_by_sector: dict[str, Decimal] = {}
    for item in sector_exposures:
        sector = _sector(item.sector_key)
        if sector in exposure_by_sector:
            raise _CalculationFailure("duplicate sector exposure key")
        _finite_ratio(item.projected_weight, "projected sector weight")
        _source_time(item.source_timestamp, evaluated_at, "sector source_timestamp")
        exposure_by_sector[sector] = item.projected_weight

    tracked_by_sector: dict[str, Decimal] = {}
    for item in positions:
        sector = _sector(item.sector_key)
        tracked_by_sector[sector] = (
            tracked_by_sector.get(sector, _ZERO) + item.current_weight
        )
    for sector, tracked_weight in tracked_by_sector.items():
        if exposure_by_sector.get(sector, _ZERO) < tracked_weight:
            raise _CalculationFailure(
                "projected sector exposure is below tracked current weight"
            )

    atr_by_key: dict[CandidateKey, ShadowAtrCeiling] = {}
    for item in atr_ceilings:
        _validate_key(item.market, item.symbol)
        if item.key in atr_by_key:
            raise _CalculationFailure("duplicate ATR ceiling key")
        _source_time(item.source_timestamp, evaluated_at, "ATR source_timestamp")
        if item.maximum_allocation_weight is not None:
            _finite_ratio(
                item.maximum_allocation_weight, "maximum_allocation_weight"
            )
        if item.maximum_quantity is not None:
            _finite_nonnegative(item.maximum_quantity, "maximum_quantity")
        if (
            item.status == ShadowSelectionStatus.VALID
            and item.maximum_allocation_weight is None
            and item.maximum_quantity is None
        ):
            raise _CalculationFailure("valid ATR ceiling has no ceiling value")
        atr_by_key[item.key] = item

    timestamps = _collect_valid_timestamps(
        rankings, positions, sector_exposures, atr_ceilings, evaluated_at
    )
    return rank_by_key, position_by_key, exposure_by_sector, atr_by_key, timestamps


def _apply_reduction(
    key: CandidateKey,
    *,
    desired: Decimal,
    kind: ShadowAdjustmentKind,
    current: dict[CandidateKey, Decimal],
    targets: dict[CandidateKey, Decimal],
    unconstrained: dict[CandidateKey, Decimal],
    sectors: dict[CandidateKey, str],
    exposures: dict[str, Decimal],
    timestamps: tuple[datetime, ...],
    config: ShadowSelectionConfig,
    evidence: list[ShadowSelectionEvidence],
) -> None:
    before = targets[key]
    difference = before - desired
    if difference <= _ZERO:
        return
    sector = sectors[key]
    sector_before = exposures[sector]
    if difference <= config.no_trade_band:
        _record_evidence(
            evidence,
            config=config,
            timestamps=timestamps,
            status=ShadowSelectionStatus.VALID,
            kind=ShadowAdjustmentKind.NO_TRADE_BAND,
            key=key,
            sector=sector,
            before=before,
            after=before,
            sector_before=sector_before,
            sector_after=sector_before,
            detail="목표와 현재 비중 차이가 no-trade band 이내입니다.",
        )
        return
    delta = min(difference, config.maximum_rebalance_delta)
    after = _q(before - delta)
    sector_after = _q(max(_ZERO, sector_before - delta))
    targets[key] = after
    unconstrained[key] = after
    exposures[sector] = sector_after
    _record_evidence(
        evidence,
        config=config,
        timestamps=timestamps,
        status=ShadowSelectionStatus.VALID,
        kind=kind,
        key=key,
        sector=sector,
        before=before,
        after=after,
        sector_before=sector_before,
        sector_after=sector_after,
        detail="실제 주문 없이 증가 계산보다 먼저 목표 비중을 줄였습니다.",
    )


def _allocation(
    key: CandidateKey,
    *,
    status: ShadowSelectionStatus,
    timestamps: tuple[datetime, ...],
    selected: bool,
    rank: CandidateRankResult | None,
    sector: str,
    current: Decimal,
    unconstrained: Decimal,
    target: Decimal,
    atr: ShadowAtrCeiling | None,
) -> ShadowTargetAllocation:
    return ShadowTargetAllocation(
        schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
        evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=status,
        source_timestamps=timestamps,
        market=key[0],
        symbol=key[1],
        sector_key=sector,
        selected=selected,
        rank_score=rank.total_score if rank is not None else None,
        current_weight=_q(current),
        unconstrained_target_weight=_q(unconstrained),
        target_weight=_q(target),
        comparison_delta=_q(target - current),
        atr_allocation_ceiling=(
            atr.maximum_allocation_weight if atr is not None else None
        ),
        atr_quantity_ceiling=atr.maximum_quantity if atr is not None else None,
        sell_risk_reduction_allowed=True,
    )


def _closed_result(
    rankings: Sequence[CandidateRankResult],
    positions: Sequence[ShadowSelectionPosition],
    *,
    evaluated_at: datetime,
    source_timestamps: tuple[datetime, ...],
    config: ShadowSelectionConfig,
    status: ShadowSelectionStatus,
    detail: str,
) -> ShadowSelectionResult:
    rank_by_key = {item.key: item for item in rankings}
    position_by_key = {item.key: item for item in positions}
    keys = tuple(
        sorted(
            set(rank_by_key) | set(position_by_key),
            key=lambda key: (key[1], key[0]),
        )
    )
    sectors = {
        key: (
            _sector(position_by_key[key].sector_key)
            if key in position_by_key
            else UNKNOWN_SECTOR
        )
        for key in keys
    }
    allocations = tuple(
        _allocation(
            key,
            status=status,
            timestamps=source_timestamps,
            selected=False,
            rank=rank_by_key.get(key),
            sector=sectors[key],
            current=(
                position_by_key[key].current_weight
                if key in position_by_key
                and isinstance(position_by_key[key].current_weight, Decimal)
                and position_by_key[key].current_weight.is_finite()
                else _ZERO
            ),
            unconstrained=(
                position_by_key[key].current_weight
                if key in position_by_key
                and isinstance(position_by_key[key].current_weight, Decimal)
                and position_by_key[key].current_weight.is_finite()
                else _ZERO
            ),
            target=(
                position_by_key[key].current_weight
                if key in position_by_key
                and isinstance(position_by_key[key].current_weight, Decimal)
                and position_by_key[key].current_weight.is_finite()
                else _ZERO
            ),
            atr=None,
        )
        for key in keys
    )
    evidence_items: tuple[ShadowSelectionEvidence, ...] = ()
    if config.emit_evidence:
        evidence_items = (
            ShadowSelectionEvidence(
                schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
                evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
                mode=SHADOW_MODE,
                status=status,
                source_timestamps=source_timestamps,
                kind=ShadowAdjustmentKind.FAIL_CLOSED,
                market=None,
                symbol=None,
                sector_key=None,
                before_weight=None,
                after_weight=None,
                comparison_delta=None,
                sector_before_weight=None,
                sector_after_weight=None,
                detail=detail,
            ),
        )
    coverage = _coverage(sectors, source_timestamps, status)
    return ShadowSelectionResult(
        schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
        evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=status,
        evaluated_at=evaluated_at,
        source_timestamps=source_timestamps,
        config_fingerprint=config.fingerprint,
        selected_keys=(),
        released_weight=_ZERO,
        preexisting_investment_headroom=_ZERO,
        buy_budget=_ZERO,
        allocations=allocations,
        projected_sector_exposures=(),
        sector_coverage=coverage,
        sell_risk_reduction_allowed=True,
        evidence=evidence_items,
    )


def _coverage(
    sectors: dict[CandidateKey, str],
    timestamps: tuple[datetime, ...],
    status: ShadowSelectionStatus,
) -> ShadowSectorCoverage:
    unknown = tuple(
        sorted(
            (key for key, sector in sectors.items() if sector == UNKNOWN_SECTOR),
            key=lambda key: (key[1], key[0]),
        )
    )
    return ShadowSectorCoverage(
        schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
        evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
        mode=SHADOW_MODE,
        status=status,
        source_timestamps=timestamps,
        known_count=len(sectors) - len(unknown),
        unknown_count=len(unknown),
        unknown_keys=unknown,
    )


def _record_evidence(
    evidence: list[ShadowSelectionEvidence],
    *,
    config: ShadowSelectionConfig,
    timestamps: tuple[datetime, ...],
    status: ShadowSelectionStatus,
    kind: ShadowAdjustmentKind,
    key: CandidateKey,
    sector: str,
    before: Decimal,
    after: Decimal,
    sector_before: Decimal,
    sector_after: Decimal,
    detail: str,
) -> None:
    if not config.emit_evidence:
        return
    evidence.append(
        ShadowSelectionEvidence(
            schema_version=SHADOW_SELECTION_SCHEMA_VERSION,
            evidence_version=SHADOW_SELECTION_EVIDENCE_VERSION,
            mode=SHADOW_MODE,
            status=status,
            source_timestamps=timestamps,
            kind=kind,
            market=key[0],
            symbol=key[1],
            sector_key=sector,
            before_weight=_q(before),
            after_weight=_q(after),
            comparison_delta=_q(after - before),
            sector_before_weight=_q(sector_before),
            sector_after_weight=_q(sector_after),
            detail=detail,
        )
    )


def _collect_valid_timestamps(
    rankings: Sequence[CandidateRankResult],
    positions: Sequence[ShadowSelectionPosition],
    sector_exposures: Sequence[ShadowSectorExposure],
    atr_ceilings: Sequence[ShadowAtrCeiling],
    evaluated_at: datetime,
) -> tuple[datetime, ...]:
    values: list[datetime] = [evaluated_at]
    values.extend(item.data_as_of for item in rankings if item.data_as_of is not None)
    values.extend(item.source_timestamp for item in positions)
    values.extend(item.source_timestamp for item in sector_exposures)
    values.extend(item.source_timestamp for item in atr_ceilings)
    normalized = {
        item.astimezone(UTC)
        for item in values
        if isinstance(item, datetime) and item.tzinfo is not None
    }
    return tuple(sorted(normalized))


def _validate_key(market: str, symbol: str) -> None:
    if market not in {"KR", "US"}:
        raise _CalculationFailure("unsupported market")
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        raise _CalculationFailure("symbol must be a stable nonempty key")


def _finite_ratio(value: Decimal, name: str, *, allow_above_one: bool = False) -> None:
    _finite_nonnegative(value, name)
    if not allow_above_one and value > _ONE:
        raise _CalculationFailure(f"{name} exceeds one")


def _finite_nonnegative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
        raise _CalculationFailure(f"{name} must be a nonnegative finite Decimal")


def _source_time(value: datetime, evaluated_at: datetime, name: str) -> None:
    try:
        normalized = _aware_utc(value, name)
    except ValueError as exc:
        raise _CalculationFailure(str(exc)) from exc
    if normalized > evaluated_at:
        raise _CalculationFailure(f"{name} cannot be in the future")


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sector(value: str | None) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        return UNKNOWN_SECTOR
    return value.strip().upper()


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _optional_text(value: Decimal | None) -> str | None:
    return _text(value) if value is not None else None


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_SHADOW_SELECTION_CONFIG",
    "SHADOW_MODE",
    "SHADOW_SELECTION_CONFIG_SCHEMA_VERSION",
    "SHADOW_SELECTION_EVIDENCE_VERSION",
    "SHADOW_SELECTION_SCHEMA_VERSION",
    "UNKNOWN_SECTOR",
    "ShadowAdjustmentKind",
    "ShadowAtrCeiling",
    "ShadowSectorCoverage",
    "ShadowSectorExposure",
    "ShadowSelectionConfig",
    "ShadowSelectionEvidence",
    "ShadowSelectionPosition",
    "ShadowSelectionResult",
    "ShadowSelectionStatus",
    "ShadowTargetAllocation",
    "allocate_shadow_targets",
]
