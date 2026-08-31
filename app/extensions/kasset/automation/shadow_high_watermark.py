"""계좌 일중 고점 정책을 실제 주문과 분리해 평가·저장하는 SHADOW 계약."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.models import KAssetShadowDailyHighWatermark

SHADOW_HIGH_WATERMARK_SCHEMA_VERSION = "kasset.shadow-high-watermark.v1"
SHADOW_HIGH_WATERMARK_CONFIG_SCHEMA_VERSION = (
    "kasset.shadow-high-watermark-config.v1"
)
SHADOW_MODE = "SHADOW"

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MARKET_TIMEZONES = {
    "KRX": ZoneInfo("Asia/Seoul"),
    "US": ZoneInfo("America/New_York"),
}


class ShadowEvidenceStatus(StrEnum):
    VALID = "valid"
    INSUFFICIENT = "insufficient"
    FAIL_CLOSED = "fail-closed"


class ShadowBuyState(StrEnum):
    NORMAL = "NORMAL"
    STAGED_REDUCTION = "STAGED_REDUCTION"
    EXIT_ONLY = "EXIT_ONLY"


class ShadowReasonCode(StrEnum):
    VALUATION_APPLIED = "VALUATION_APPLIED"
    TRADING_DAY_ROLLOVER = "TRADING_DAY_ROLLOVER"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    MISSING_VALUATION_SOURCE = "MISSING_VALUATION_SOURCE"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    INVALID_SCOPE = "INVALID_SCOPE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    FUTURE_VALUATION = "FUTURE_VALUATION"
    STALE_VALUATION = "STALE_VALUATION"
    INVALID_EQUITY = "INVALID_EQUITY"
    INVALID_REFERENCE_EQUITY = "INVALID_REFERENCE_EQUITY"
    OUT_OF_ORDER_VALUATION = "OUT_OF_ORDER_VALUATION"
    VALUATION_TIMESTAMP_COLLISION = "VALUATION_TIMESTAMP_COLLISION"
    MAXIMUM_LOSS_REACHED = "MAXIMUM_LOSS_REACHED"


@dataclass(frozen=True, slots=True)
class ShadowReductionStage:
    """한 단계의 명시적 SHADOW 축소 기준."""

    name: str
    trigger_ratio: Decimal
    buy_multiplier: Decimal

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("stage name must be nonempty")
        if not _is_finite_decimal(self.trigger_ratio) or self.trigger_ratio <= _ZERO:
            raise ValueError("trigger_ratio must be a positive finite Decimal")
        if (
            not _is_finite_decimal(self.buy_multiplier)
            or self.buy_multiplier <= _ZERO
            or self.buy_multiplier >= _ONE
        ):
            raise ValueError("buy_multiplier must be between 0 and 1")

    def as_serializable(self) -> dict[str, str]:
        return {
            "name": self.name,
            "triggerRatio": str(self.trigger_ratio),
            "buyMultiplier": str(self.buy_multiplier),
        }


@dataclass(frozen=True, slots=True)
class ShadowHighWatermarkThresholds:
    """활성 전략 설정과 독립적으로 직렬화·지문 생성되는 불변 SHADOW 기준."""

    profit_target_stages: tuple[ShadowReductionStage, ...]
    peak_drawdown_stages: tuple[ShadowReductionStage, ...]
    maximum_loss_ratio: Decimal
    max_valuation_age: timedelta

    def __post_init__(self) -> None:
        if (
            not _is_finite_decimal(self.maximum_loss_ratio)
            or self.maximum_loss_ratio <= _ZERO
            or self.maximum_loss_ratio > _ONE
        ):
            raise ValueError("maximum_loss_ratio must be in (0, 1]")
        if self.max_valuation_age <= timedelta(0):
            raise ValueError("max_valuation_age must be positive")
        _validate_stages(self.profit_target_stages, "profit_target_stages")
        _validate_stages(self.peak_drawdown_stages, "peak_drawdown_stages")

    def as_serializable(self) -> dict[str, object]:
        return {
            "configSchemaVersion": SHADOW_HIGH_WATERMARK_CONFIG_SCHEMA_VERSION,
            "profitTargetStages": [
                stage.as_serializable() for stage in self.profit_target_stages
            ],
            "peakDrawdownStages": [
                stage.as_serializable() for stage in self.peak_drawdown_stages
            ],
            "maximumLossRatio": str(self.maximum_loss_ratio),
            "maxValuationAgeMicroseconds": (
                (
                    self.max_valuation_age.days * 86_400
                    + self.max_valuation_age.seconds
                )
                * 1_000_000
                + self.max_valuation_age.microseconds
            ),
        }

    @property
    def fingerprint(self) -> str:
        return fingerprint_shadow_high_watermark_thresholds(self)


@dataclass(frozen=True, slots=True)
class ShadowEquityValuation:
    owner_user_id: int
    account_key: str
    market: Literal["KRX", "US"] | str
    equity: Decimal
    valuation_at: datetime
    evaluated_at: datetime
    valuation_source: str
    reference_equity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ShadowHighWatermarkState:
    owner_user_id: int
    account_key: str
    market: Literal["KRX", "US"]
    trading_date: date
    session_opening_equity: Decimal
    reference_equity: Decimal
    peak_equity: Decimal
    current_equity: Decimal
    valuation_at: datetime
    valuation_source: str
    state_version: int

    def __post_init__(self) -> None:
        if self.owner_user_id <= 0 or not self.account_key.strip():
            raise ValueError("state scope is invalid")
        if self.market not in _MARKET_TIMEZONES:
            raise ValueError("state market is unsupported")
        for field_name in (
            "session_opening_equity",
            "reference_equity",
            "peak_equity",
            "current_equity",
        ):
            value = getattr(self, field_name)
            if not _is_finite_decimal(value) or value <= _ZERO:
                raise ValueError(f"{field_name} must be a positive finite Decimal")
        if self.peak_equity < self.session_opening_equity:
            raise ValueError("peak_equity must cover session_opening_equity")
        if self.peak_equity < self.current_equity:
            raise ValueError("peak_equity must cover current_equity")
        if _utc_or_none(self.valuation_at) is None:
            raise ValueError("valuation_at must be timezone-aware")
        if not self.valuation_source.strip():
            raise ValueError("valuation_source must be nonempty")
        if self.state_version <= 0:
            raise ValueError("state_version must be positive")

    def as_evidence(self) -> dict[str, object]:
        return {
            "ownerUserId": self.owner_user_id,
            "accountKey": self.account_key,
            "market": self.market,
            "tradingDate": self.trading_date.isoformat(),
            "sessionOpeningEquity": str(self.session_opening_equity),
            "referenceEquity": str(self.reference_equity),
            "peakEquity": str(self.peak_equity),
            "currentEquity": str(self.current_equity),
            "valuationAt": _timestamp_text(self.valuation_at),
            "valuationSource": self.valuation_source,
            "stateVersion": self.state_version,
        }


@dataclass(frozen=True, slots=True)
class ShadowTriggeredStage:
    kind: Literal["PROFIT_TARGET", "PEAK_DRAWDOWN"]
    name: str
    trigger_ratio: Decimal
    observed_ratio: Decimal
    buy_multiplier: Decimal

    def as_evidence(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "triggerRatio": str(self.trigger_ratio),
            "observedRatio": str(self.observed_ratio),
            "buyMultiplier": str(self.buy_multiplier),
        }


@dataclass(frozen=True, slots=True)
class ShadowReason:
    code: ShadowReasonCode
    detail: str

    def as_evidence(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ShadowHighWatermarkEvaluation:
    status: ShadowEvidenceStatus
    trading_date: date | None
    buy_state: ShadowBuyState
    buy_multiplier: Decimal
    sell_risk_reduction_allowed: bool
    state: ShadowHighWatermarkState | None
    expected_state_version: int | None
    threshold_fingerprint: str
    thresholds: ShadowHighWatermarkThresholds
    valuation: ShadowEquityValuation
    profit_ratio: Decimal | None
    peak_drawdown_ratio: Decimal | None
    session_loss_ratio: Decimal | None
    reference_loss_ratio: Decimal | None
    triggered_stages: tuple[ShadowTriggeredStage, ...]
    reasons: tuple[ShadowReason, ...]

    @property
    def persistence_required(self) -> bool:
        if self.status != ShadowEvidenceStatus.VALID or self.state is None:
            return False
        if self.expected_state_version is None:
            return True
        return self.state.state_version > self.expected_state_version

    def as_evidence(self) -> dict[str, object]:
        state_valuation_at = (
            _timestamp_text(self.state.valuation_at) if self.state is not None else None
        )
        return {
            "schemaVersion": SHADOW_HIGH_WATERMARK_SCHEMA_VERSION,
            "mode": SHADOW_MODE,
            "status": self.status.value,
            "scope": {
                "ownerUserId": self.valuation.owner_user_id,
                "accountKey": self.valuation.account_key,
                "market": str(self.valuation.market).upper(),
                "tradingDate": (
                    self.trading_date.isoformat()
                    if self.trading_date is not None
                    else None
                ),
            },
            "sourceTimestamps": {
                "valuationAt": _timestamp_text(self.valuation.valuation_at),
                "evaluatedAt": _timestamp_text(self.valuation.evaluated_at),
                "stateValuationAt": state_valuation_at,
            },
            "valuationSource": self.valuation.valuation_source,
            "thresholdConfig": {
                "fingerprint": self.threshold_fingerprint,
                "config": self.thresholds.as_serializable(),
            },
            "hypothetical": {
                "buyState": self.buy_state.value,
                "buyMultiplier": str(self.buy_multiplier),
                "buyActionable": (
                    self.status == ShadowEvidenceStatus.VALID
                    and self.buy_state != ShadowBuyState.EXIT_ONLY
                ),
                "sellRiskReductionAllowed": self.sell_risk_reduction_allowed,
            },
            "metrics": {
                "profitRatio": _decimal_text(self.profit_ratio),
                "peakDrawdownRatio": _decimal_text(self.peak_drawdown_ratio),
                "sessionLossRatio": _decimal_text(self.session_loss_ratio),
                "referenceLossRatio": _decimal_text(self.reference_loss_ratio),
            },
            "triggeredStages": [
                stage.as_evidence() for stage in self.triggered_stages
            ],
            "reasons": [reason.as_evidence() for reason in self.reasons],
            "state": self.state.as_evidence() if self.state is not None else None,
        }


class ConcurrentShadowHighWatermarkUpdate(RuntimeError):
    """낙관적 버전과 다른 상태가 이미 저장된 경우."""


def fingerprint_shadow_high_watermark_thresholds(
    thresholds: ShadowHighWatermarkThresholds,
) -> str:
    canonical = json.dumps(
        thresholds.as_serializable(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def market_trading_date(market: str, at: datetime) -> date:
    normalized_market = market.upper()
    timezone = _MARKET_TIMEZONES.get(normalized_market)
    utc_at = _utc_or_none(at)
    if timezone is None:
        raise ValueError("unsupported market")
    if utc_at is None:
        raise ValueError("at must be timezone-aware")
    return utc_at.astimezone(timezone).date()


def evaluate_shadow_high_watermark(
    valuation: ShadowEquityValuation,
    *,
    thresholds: ShadowHighWatermarkThresholds,
    previous: ShadowHighWatermarkState | None = None,
) -> ShadowHighWatermarkEvaluation:
    """외부 효과 없이 일별 고점 상태와 가상 BUY 축소 배수를 계산한다."""

    market = str(valuation.market).upper()
    fingerprint = thresholds.fingerprint
    evaluated_at = _utc_or_none(valuation.evaluated_at)
    valuation_at = _utc_or_none(valuation.valuation_at)
    trading_date: date | None = None

    if (
        isinstance(valuation.owner_user_id, bool)
        or valuation.owner_user_id <= 0
        or not valuation.account_key.strip()
    ):
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=None,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.INVALID_SCOPE,
            detail="소유자 또는 계좌 범위가 유효하지 않습니다.",
        )
    if market not in _MARKET_TIMEZONES:
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=None,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.UNSUPPORTED_MARKET,
            detail="지원하지 않는 시장입니다.",
        )
    if evaluated_at is None or valuation_at is None:
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=None,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.INVALID_TIMESTAMP,
            detail="평가 및 가치평가 시각은 timezone-aware 값이어야 합니다.",
        )
    trading_date = market_trading_date(market, valuation_at)
    if not valuation.valuation_source.strip():
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=trading_date,
            status=ShadowEvidenceStatus.INSUFFICIENT,
            code=ShadowReasonCode.MISSING_VALUATION_SOURCE,
            detail="가치평가 출처가 없습니다.",
        )
    if valuation_at > evaluated_at:
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=trading_date,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.FUTURE_VALUATION,
            detail="평가 시각보다 미래의 가치평가입니다.",
        )
    if evaluated_at - valuation_at > thresholds.max_valuation_age:
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=trading_date,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.STALE_VALUATION,
            detail="허용된 최신성 범위를 지난 가치평가입니다.",
        )
    if not _is_finite_decimal(valuation.equity) or valuation.equity <= _ZERO:
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=trading_date,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.INVALID_EQUITY,
            detail="계좌 자산은 양의 유한 Decimal이어야 합니다.",
        )
    if valuation.reference_equity is not None and (
        not _is_finite_decimal(valuation.reference_equity)
        or valuation.reference_equity <= _ZERO
    ):
        return _non_actionable(
            valuation,
            thresholds,
            fingerprint,
            previous=previous,
            trading_date=trading_date,
            status=ShadowEvidenceStatus.FAIL_CLOSED,
            code=ShadowReasonCode.INVALID_REFERENCE_EQUITY,
            detail="기준 자산은 양의 유한 Decimal이어야 합니다.",
        )

    if previous is not None:
        if (
            previous.owner_user_id != valuation.owner_user_id
            or previous.account_key != valuation.account_key
            or previous.market != market
        ):
            return _non_actionable(
                valuation,
                thresholds,
                fingerprint,
                previous=previous,
                trading_date=trading_date,
                status=ShadowEvidenceStatus.FAIL_CLOSED,
                code=ShadowReasonCode.SCOPE_MISMATCH,
                detail="이전 상태의 소유자·계좌·시장 범위가 다릅니다.",
            )
        if trading_date < previous.trading_date or valuation_at < _as_utc(
            previous.valuation_at
        ):
            return _non_actionable(
                valuation,
                thresholds,
                fingerprint,
                previous=previous,
                trading_date=trading_date,
                status=ShadowEvidenceStatus.INSUFFICIENT,
                code=ShadowReasonCode.OUT_OF_ORDER_VALUATION,
                detail="저장 상태보다 오래된 가치평가라 상태를 변경하지 않습니다.",
            )
        if trading_date == previous.trading_date and valuation_at == _as_utc(
            previous.valuation_at
        ):
            if (
                valuation.equity == previous.current_equity
                and valuation.valuation_source == previous.valuation_source
            ):
                return _valid_evaluation(
                    valuation,
                    thresholds,
                    fingerprint,
                    previous,
                    expected_state_version=previous.state_version,
                    reason=ShadowReason(
                        ShadowReasonCode.IDEMPOTENT_REPLAY,
                        "동일 가치평가를 재처리해 저장 상태를 그대로 복구했습니다.",
                    ),
                )
            return _non_actionable(
                valuation,
                thresholds,
                fingerprint,
                previous=previous,
                trading_date=trading_date,
                status=ShadowEvidenceStatus.FAIL_CLOSED,
                code=ShadowReasonCode.VALUATION_TIMESTAMP_COLLISION,
                detail="동일 시각에 서로 다른 가치평가가 제공되었습니다.",
            )

    rollover = previous is not None and trading_date > previous.trading_date
    if previous is None or rollover:
        reference_equity = valuation.reference_equity or valuation.equity
        state = ShadowHighWatermarkState(
            owner_user_id=valuation.owner_user_id,
            account_key=valuation.account_key,
            market=market,  # type: ignore[arg-type]
            trading_date=trading_date,
            session_opening_equity=valuation.equity,
            reference_equity=reference_equity,
            peak_equity=valuation.equity,
            current_equity=valuation.equity,
            valuation_at=valuation_at,
            valuation_source=valuation.valuation_source,
            state_version=1,
        )
        reason = ShadowReason(
            (
                ShadowReasonCode.TRADING_DAY_ROLLOVER
                if rollover
                else ShadowReasonCode.VALUATION_APPLIED
            ),
            (
                "시장 현지 거래일 변경으로 새 일별 상태를 시작했습니다."
                if rollover
                else "첫 가치평가로 일별 상태를 시작했습니다."
            ),
        )
        return _valid_evaluation(
            valuation,
            thresholds,
            fingerprint,
            state,
            expected_state_version=None,
            reason=reason,
        )

    state = ShadowHighWatermarkState(
        owner_user_id=previous.owner_user_id,
        account_key=previous.account_key,
        market=previous.market,
        trading_date=previous.trading_date,
        session_opening_equity=previous.session_opening_equity,
        reference_equity=previous.reference_equity,
        peak_equity=max(previous.peak_equity, valuation.equity),
        current_equity=valuation.equity,
        valuation_at=valuation_at,
        valuation_source=valuation.valuation_source,
        state_version=previous.state_version + 1,
    )
    return _valid_evaluation(
        valuation,
        thresholds,
        fingerprint,
        state,
        expected_state_version=previous.state_version,
        reason=ShadowReason(
            ShadowReasonCode.VALUATION_APPLIED,
            "최신 가치평가로 일별 상태를 갱신했습니다.",
        ),
    )


async def load_shadow_high_watermark(
    db: AsyncSession,
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    trading_date: date,
) -> ShadowHighWatermarkState | None:
    """재시작 후에도 동일 복합 키의 마지막 커밋 상태를 읽는다."""

    row = await db.scalar(
        select(KAssetShadowDailyHighWatermark).where(
            KAssetShadowDailyHighWatermark.owner_user_id == owner_user_id,
            KAssetShadowDailyHighWatermark.account_key == account_key,
            KAssetShadowDailyHighWatermark.market == market.upper(),
            KAssetShadowDailyHighWatermark.trading_date == trading_date,
        )
    )
    return _row_to_state(row) if row is not None else None


async def persist_shadow_high_watermark(
    db: AsyncSession,
    evaluation: ShadowHighWatermarkEvaluation,
) -> ShadowHighWatermarkState:
    """복합 키와 state_version으로 낙관적·멱등 SHADOW 상태를 저장한다.

    트랜잭션 commit 경계는 호출자가 소유한다. 충돌 시 기존 행을 덮어쓰지 않는다.
    """

    if evaluation.status != ShadowEvidenceStatus.VALID or evaluation.state is None:
        raise ValueError("only valid SHADOW evaluations may be persisted")
    state = evaluation.state
    evidence = evaluation.as_evidence()
    values = _persistence_values(state, evidence)
    expected = evaluation.expected_state_version

    if expected is None:
        statement = (
            insert(KAssetShadowDailyHighWatermark)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    "owner_user_id",
                    "account_key",
                    "market",
                    "trading_date",
                ]
            )
            .returning(KAssetShadowDailyHighWatermark)
        )
        inserted = await db.scalar(statement)
        if inserted is not None:
            return _row_to_state(inserted)
    elif state.state_version == expected:
        existing = await _load_exact_state(db, state)
        if existing is not None and _same_state(existing, state):
            return existing
        raise ConcurrentShadowHighWatermarkUpdate(
            "idempotent replay does not match persisted SHADOW state"
        )
    elif state.state_version == expected + 1:
        mutable_values = dict(values)
        for key in ("owner_user_id", "account_key", "market", "trading_date"):
            mutable_values.pop(key)
        statement = (
            update(KAssetShadowDailyHighWatermark)
            .where(
                KAssetShadowDailyHighWatermark.owner_user_id == state.owner_user_id,
                KAssetShadowDailyHighWatermark.account_key == state.account_key,
                KAssetShadowDailyHighWatermark.market == state.market,
                KAssetShadowDailyHighWatermark.trading_date == state.trading_date,
                KAssetShadowDailyHighWatermark.state_version == expected,
            )
            .values(**mutable_values)
            .returning(KAssetShadowDailyHighWatermark)
        )
        updated = await db.scalar(statement)
        if updated is not None:
            return _row_to_state(updated)
    else:
        raise ValueError("evaluation state_version is not an optimistic successor")

    existing = await _load_exact_state(db, state)
    if existing is not None and _same_state(existing, state):
        return existing
    raise ConcurrentShadowHighWatermarkUpdate(
        "persisted SHADOW state changed since evaluation"
    )


async def evaluate_and_persist_shadow_high_watermark(
    db: AsyncSession,
    valuation: ShadowEquityValuation,
    *,
    thresholds: ShadowHighWatermarkThresholds,
) -> ShadowHighWatermarkEvaluation:
    """현재 시장 거래일 상태를 읽고 같은 순수 평가기를 거쳐 필요할 때만 저장한다."""

    try:
        trading_date = market_trading_date(
            str(valuation.market),
            valuation.valuation_at,
        )
    except ValueError:
        return evaluate_shadow_high_watermark(valuation, thresholds=thresholds)
    previous = await load_shadow_high_watermark(
        db,
        owner_user_id=valuation.owner_user_id,
        account_key=valuation.account_key,
        market=str(valuation.market),
        trading_date=trading_date,
    )
    evaluation = evaluate_shadow_high_watermark(
        valuation,
        thresholds=thresholds,
        previous=previous,
    )
    if evaluation.persistence_required:
        await persist_shadow_high_watermark(db, evaluation)
    return evaluation


def _valid_evaluation(
    valuation: ShadowEquityValuation,
    thresholds: ShadowHighWatermarkThresholds,
    fingerprint: str,
    state: ShadowHighWatermarkState,
    *,
    expected_state_version: int | None,
    reason: ShadowReason,
) -> ShadowHighWatermarkEvaluation:
    profit_ratio = (
        state.current_equity - state.reference_equity
    ) / state.reference_equity
    peak_drawdown_ratio = max(
        _ZERO,
        (state.peak_equity - state.current_equity) / state.peak_equity,
    )
    session_loss_ratio = max(
        _ZERO,
        (state.session_opening_equity - state.current_equity)
        / state.session_opening_equity,
    )
    reference_loss_ratio = max(_ZERO, -profit_ratio)
    triggered: list[ShadowTriggeredStage] = []
    for stage in thresholds.profit_target_stages:
        if profit_ratio >= stage.trigger_ratio:
            triggered.append(
                ShadowTriggeredStage(
                    kind="PROFIT_TARGET",
                    name=stage.name,
                    trigger_ratio=stage.trigger_ratio,
                    observed_ratio=profit_ratio,
                    buy_multiplier=stage.buy_multiplier,
                )
            )
    for stage in thresholds.peak_drawdown_stages:
        if peak_drawdown_ratio >= stage.trigger_ratio:
            triggered.append(
                ShadowTriggeredStage(
                    kind="PEAK_DRAWDOWN",
                    name=stage.name,
                    trigger_ratio=stage.trigger_ratio,
                    observed_ratio=peak_drawdown_ratio,
                    buy_multiplier=stage.buy_multiplier,
                )
            )

    maximum_loss_reached = (
        max(session_loss_ratio, reference_loss_ratio)
        >= thresholds.maximum_loss_ratio
    )
    reasons = (reason,)
    if maximum_loss_reached:
        buy_state = ShadowBuyState.EXIT_ONLY
        buy_multiplier = _ZERO
        reasons = reasons + (
            ShadowReason(
                ShadowReasonCode.MAXIMUM_LOSS_REACHED,
                "세션 또는 기준 자산 대비 최대 손실 한계에 도달했습니다.",
            ),
        )
    elif triggered:
        buy_state = ShadowBuyState.STAGED_REDUCTION
        buy_multiplier = min(item.buy_multiplier for item in triggered)
    else:
        buy_state = ShadowBuyState.NORMAL
        buy_multiplier = _ONE

    return ShadowHighWatermarkEvaluation(
        status=ShadowEvidenceStatus.VALID,
        trading_date=state.trading_date,
        buy_state=buy_state,
        buy_multiplier=buy_multiplier,
        sell_risk_reduction_allowed=True,
        state=state,
        expected_state_version=expected_state_version,
        threshold_fingerprint=fingerprint,
        thresholds=thresholds,
        valuation=valuation,
        profit_ratio=profit_ratio,
        peak_drawdown_ratio=peak_drawdown_ratio,
        session_loss_ratio=session_loss_ratio,
        reference_loss_ratio=reference_loss_ratio,
        triggered_stages=tuple(triggered),
        reasons=reasons,
    )


def _non_actionable(
    valuation: ShadowEquityValuation,
    thresholds: ShadowHighWatermarkThresholds,
    fingerprint: str,
    *,
    previous: ShadowHighWatermarkState | None,
    trading_date: date | None,
    status: ShadowEvidenceStatus,
    code: ShadowReasonCode,
    detail: str,
) -> ShadowHighWatermarkEvaluation:
    return ShadowHighWatermarkEvaluation(
        status=status,
        trading_date=trading_date,
        buy_state=ShadowBuyState.EXIT_ONLY,
        buy_multiplier=_ZERO,
        sell_risk_reduction_allowed=True,
        state=previous,
        expected_state_version=(
            previous.state_version if previous is not None else None
        ),
        threshold_fingerprint=fingerprint,
        thresholds=thresholds,
        valuation=valuation,
        profit_ratio=None,
        peak_drawdown_ratio=None,
        session_loss_ratio=None,
        reference_loss_ratio=None,
        triggered_stages=(),
        reasons=(ShadowReason(code, detail),),
    )


def _validate_stages(
    stages: tuple[ShadowReductionStage, ...],
    field_name: str,
) -> None:
    if not isinstance(stages, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if len({stage.name for stage in stages}) != len(stages):
        raise ValueError(f"{field_name} names must be unique")
    for previous, current in zip(stages, stages[1:], strict=False):
        if current.trigger_ratio <= previous.trigger_ratio:
            raise ValueError(f"{field_name} triggers must be strictly increasing")
        if current.buy_multiplier > previous.buy_multiplier:
            raise ValueError(f"{field_name} multipliers must not increase")


def _is_finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _utc_or_none(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    normalized = _utc_or_none(value)
    if normalized is None:
        raise ValueError("timestamp must be timezone-aware")
    return normalized


def _timestamp_text(value: object) -> str | None:
    normalized = _utc_or_none(value)
    if normalized is not None:
        return normalized.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _persistence_values(
    state: ShadowHighWatermarkState,
    evidence: dict[str, object],
) -> dict[str, object]:
    return {
        "owner_user_id": state.owner_user_id,
        "account_key": state.account_key,
        "market": state.market,
        "trading_date": state.trading_date,
        "session_opening_equity": state.session_opening_equity,
        "reference_equity": state.reference_equity,
        "peak_equity": state.peak_equity,
        "current_equity": state.current_equity,
        "valuation_at": _as_utc(state.valuation_at),
        "valuation_source": state.valuation_source,
        "state_version": state.state_version,
        "evidence_schema_version": SHADOW_HIGH_WATERMARK_SCHEMA_VERSION,
        "mode": SHADOW_MODE,
        "evidence": evidence,
    }


async def _load_exact_state(
    db: AsyncSession,
    state: ShadowHighWatermarkState,
) -> ShadowHighWatermarkState | None:
    return await load_shadow_high_watermark(
        db,
        owner_user_id=state.owner_user_id,
        account_key=state.account_key,
        market=state.market,
        trading_date=state.trading_date,
    )


def _row_to_state(row: KAssetShadowDailyHighWatermark) -> ShadowHighWatermarkState:
    return ShadowHighWatermarkState(
        owner_user_id=int(row.owner_user_id),
        account_key=str(row.account_key),
        market=str(row.market),  # type: ignore[arg-type]
        trading_date=row.trading_date,
        session_opening_equity=Decimal(row.session_opening_equity),
        reference_equity=Decimal(row.reference_equity),
        peak_equity=Decimal(row.peak_equity),
        current_equity=Decimal(row.current_equity),
        valuation_at=_as_utc(row.valuation_at),
        valuation_source=str(row.valuation_source),
        state_version=int(row.state_version),
    )


def _same_state(
    left: ShadowHighWatermarkState,
    right: ShadowHighWatermarkState,
) -> bool:
    return left == right


__all__ = [
    "ConcurrentShadowHighWatermarkUpdate",
    "SHADOW_HIGH_WATERMARK_CONFIG_SCHEMA_VERSION",
    "SHADOW_HIGH_WATERMARK_SCHEMA_VERSION",
    "SHADOW_MODE",
    "ShadowBuyState",
    "ShadowEquityValuation",
    "ShadowEvidenceStatus",
    "ShadowHighWatermarkEvaluation",
    "ShadowHighWatermarkState",
    "ShadowHighWatermarkThresholds",
    "ShadowReasonCode",
    "ShadowReductionStage",
    "evaluate_and_persist_shadow_high_watermark",
    "evaluate_shadow_high_watermark",
    "fingerprint_shadow_high_watermark_thresholds",
    "load_shadow_high_watermark",
    "market_trading_date",
    "persist_shadow_high_watermark",
]
