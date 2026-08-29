"""Pure strategy promotion policy for globally approved PAPER versions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

_ZERO = Decimal("0")
_ONE = Decimal("1")
DEFAULT_PAPER_STRATEGY_KEY = "qullamaggie_breakout_portfolio"
DEFAULT_PAPER_STRATEGY_VERSION = "1.0.0"


class PromotionState(StrEnum):
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    PAPER_APPROVED = "PAPER_APPROVED"
    PAPER_SUSPENDED = "PAPER_SUSPENDED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class PromotionMetrics:
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    expectancy: Decimal
    excess_return: Decimal
    trade_count: int
    walk_forward_folds: int
    walk_forward_passed_folds: int
    data_quality_evidence: bool
    survivorship_evidence: bool
    deterministic: bool
    backtest_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "total_return",
            "max_drawdown",
            "win_rate",
            "expectancy",
            "excess_return",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, field_name, value)
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if not _ZERO <= self.max_drawdown <= _ONE:
            raise ValueError("max_drawdown must be in [0, 1]")
        if not _ZERO <= self.win_rate <= _ONE:
            raise ValueError("win_rate must be in [0, 1]")
        for field_name in (
            "trade_count",
            "walk_forward_folds",
            "walk_forward_passed_folds",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.walk_forward_passed_folds > self.walk_forward_folds:
            raise ValueError("passed walk-forward folds cannot exceed all folds")
        for field_name in (
            "data_quality_evidence",
            "survivorship_evidence",
            "deterministic",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        hashes = tuple(dict.fromkeys(value.strip() for value in self.backtest_hashes))
        if not hashes or any(not value for value in hashes):
            raise ValueError("at least one non-empty backtest hash is required")
        object.__setattr__(self, "backtest_hashes", hashes)

    @property
    def walk_forward_pass_rate(self) -> Decimal:
        if self.walk_forward_folds == 0:
            return _ZERO
        return Decimal(self.walk_forward_passed_folds) / Decimal(
            self.walk_forward_folds
        )

    def as_snapshot(self) -> dict[str, object]:
        return {
            "totalReturn": _decimal_text(self.total_return),
            "maxDrawdown": _decimal_text(self.max_drawdown),
            "winRate": _decimal_text(self.win_rate),
            "expectancy": _decimal_text(self.expectancy),
            "excessReturn": _decimal_text(self.excess_return),
            "tradeCount": self.trade_count,
            "walkForwardFolds": self.walk_forward_folds,
            "walkForwardPassedFolds": self.walk_forward_passed_folds,
            "walkForwardPassRate": _decimal_text(self.walk_forward_pass_rate),
            "dataQualityEvidence": self.data_quality_evidence,
            "survivorshipEvidence": self.survivorship_evidence,
            "deterministic": self.deterministic,
            "backtestHashes": list(self.backtest_hashes),
        }


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    min_total_return: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0.20")
    min_win_rate: Decimal = Decimal("0.40")
    min_expectancy: Decimal = Decimal("0")
    min_excess_return: Decimal = Decimal("0")
    min_trade_count: int = 30
    min_walk_forward_folds: int = 3
    min_walk_forward_pass_rate: Decimal = Decimal("0.67")
    require_data_quality_evidence: bool = True
    require_survivorship_evidence: bool = True
    require_deterministic: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "min_total_return",
            "max_drawdown",
            "min_win_rate",
            "min_expectancy",
            "min_excess_return",
            "min_walk_forward_pass_rate",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, field_name, value)
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if not _ZERO <= self.max_drawdown <= _ONE:
            raise ValueError("max_drawdown threshold must be in [0, 1]")
        if not _ZERO <= self.min_win_rate <= _ONE:
            raise ValueError("min_win_rate must be in [0, 1]")
        if not _ZERO <= self.min_walk_forward_pass_rate <= _ONE:
            raise ValueError("min_walk_forward_pass_rate must be in [0, 1]")
        for field_name in ("min_trade_count", "min_walk_forward_folds"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        for field_name in (
            "require_data_quality_evidence",
            "require_survivorship_evidence",
            "require_deterministic",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")


DEFAULT_PROMOTION_THRESHOLDS = PromotionThresholds()


@dataclass(frozen=True, slots=True)
class ThresholdCheck:
    metric: str
    observed: str
    comparator: str
    required: str
    passed: bool

    def as_evidence(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "observed": self.observed,
            "comparator": self.comparator,
            "required": self.required,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ThresholdEvaluation:
    passed: bool
    metrics_hash: str
    checks: tuple[ThresholdCheck, ...]

    @property
    def failed_metrics(self) -> tuple[str, ...]:
        return tuple(check.metric for check in self.checks if not check.passed)

    def as_evidence(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "metricsHash": self.metrics_hash,
            "failedMetrics": list(self.failed_metrics),
            "checks": [check.as_evidence() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    code: str
    detail: str
    reference: str

    def __post_init__(self) -> None:
        for field_name in ("code", "detail", "reference"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"promotion evidence {field_name} is required")
            object.__setattr__(self, field_name, value)

    def as_evidence(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class StrategyPromotion:
    strategy_key: str
    version: str
    state: PromotionState
    metrics: PromotionMetrics | None
    metrics_hash: str | None
    threshold_evaluation: ThresholdEvaluation | None
    evidence: tuple[PromotionEvidence, ...]
    promotion_candidate_id: int | None
    strategy_artifact_fingerprint: str | None
    source_commit: str | None
    evidence_schema_version: str | None
    approved_at: datetime | None
    suspended_at: datetime | None
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        strategy_key = self.strategy_key.strip()
        version = self.version.strip()
        if not strategy_key or not version:
            raise ValueError("strategy_key and version are required")
        object.__setattr__(self, "strategy_key", strategy_key)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "state", PromotionState(self.state))
        trust_bundle = (
            self.promotion_candidate_id,
            self.strategy_artifact_fingerprint,
            self.source_commit,
            self.evidence_schema_version,
        )
        if any(value is not None for value in trust_bundle) and not all(
            value is not None for value in trust_bundle
        ):
            raise ValueError("promotion candidate trust bundle must be complete")
        if self.promotion_candidate_id is not None:
            if (
                type(self.promotion_candidate_id) is not int
                or self.promotion_candidate_id < 1
            ):
                raise ValueError("promotion_candidate_id must be a positive integer")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(self.strategy_artifact_fingerprint)
            ):
                raise ValueError(
                    "strategy_artifact_fingerprint must be lowercase 64-hex"
                )
            if not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(self.source_commit)
            ):
                raise ValueError("source_commit must be a full lowercase Git object id")
            evidence_schema = str(self.evidence_schema_version).strip()
            if not evidence_schema:
                raise ValueError("evidence_schema_version is required")
            object.__setattr__(self, "evidence_schema_version", evidence_schema)
        for field_name in (
            "approved_at",
            "suspended_at",
            "retired_at",
            "created_at",
            "updated_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _utc(value, field_name))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.metrics is None:
            if self.metrics_hash is not None:
                raise ValueError("metrics_hash requires metrics")
        else:
            expected_hash = hash_metrics_snapshot(self.metrics)
            if self.metrics_hash != expected_hash:
                raise ValueError("metrics_hash does not match the metrics snapshot")
        _validate_state_invariants(self)

    def metrics_snapshot(self) -> dict[str, object]:
        return self.metrics.as_snapshot() if self.metrics is not None else {}

    def as_evidence(self) -> dict[str, object]:
        return {
            "strategyKey": self.strategy_key,
            "version": self.version,
            "state": self.state.value,
            "metrics": self.metrics_snapshot(),
            "metricsHash": self.metrics_hash,
            "thresholdEvaluation": (
                self.threshold_evaluation.as_evidence()
                if self.threshold_evaluation is not None
                else None
            ),
            "promotionCandidateId": self.promotion_candidate_id,
            "strategyArtifactFingerprint": self.strategy_artifact_fingerprint,
            "sourceCommit": self.source_commit,
            "evidenceSchemaVersion": self.evidence_schema_version,
            "evidence": [item.as_evidence() for item in self.evidence],
            "approvedAt": _timestamp_text(self.approved_at),
            "suspendedAt": _timestamp_text(self.suspended_at),
            "retiredAt": _timestamp_text(self.retired_at),
            "createdAt": _timestamp_text(self.created_at),
            "updatedAt": _timestamp_text(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class PaperApprovalDecision:
    approved: bool
    strategy_key: str
    version: str
    state: PromotionState | None
    metrics_hash: str | None
    reason: str
    promotion_fingerprint: str | None = None
    recommendation_fingerprint: str | None = None
    runtime_fingerprint: str | None = None


class IllegalPromotionTransition(ValueError):
    pass


class PromotionIdentityMismatch(ValueError):
    pass


class PromotionThresholdNotMet(ValueError):
    def __init__(self, evaluation: ThresholdEvaluation) -> None:
        self.evaluation = evaluation
        failed = ",".join(evaluation.failed_metrics)
        super().__init__(f"promotion thresholds not met: {failed}")


_ALLOWED_TRANSITIONS: dict[PromotionState, frozenset[PromotionState]] = {
    PromotionState.DRAFT: frozenset(
        {PromotionState.BACKTESTED, PromotionState.RETIRED}
    ),
    PromotionState.BACKTESTED: frozenset(
        {PromotionState.PAPER_APPROVED, PromotionState.RETIRED}
    ),
    PromotionState.PAPER_APPROVED: frozenset(
        {PromotionState.PAPER_SUSPENDED, PromotionState.RETIRED}
    ),
    PromotionState.PAPER_SUSPENDED: frozenset({PromotionState.RETIRED}),
    PromotionState.RETIRED: frozenset(),
}


def create_draft(
    strategy_key: str,
    version: str,
    *,
    at: datetime,
    evidence: Sequence[PromotionEvidence] = (),
    promotion_candidate_id: int | None = None,
    strategy_artifact_fingerprint: str | None = None,
    source_commit: str | None = None,
    evidence_schema_version: str | None = None,
) -> StrategyPromotion:
    timestamp = _utc(at, "at")
    return StrategyPromotion(
        strategy_key=strategy_key,
        version=version,
        state=PromotionState.DRAFT,
        metrics=None,
        metrics_hash=None,
        threshold_evaluation=None,
        evidence=tuple(evidence),
        promotion_candidate_id=promotion_candidate_id,
        strategy_artifact_fingerprint=strategy_artifact_fingerprint,
        source_commit=source_commit,
        evidence_schema_version=evidence_schema_version,
        approved_at=None,
        suspended_at=None,
        retired_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def evaluate_thresholds(
    metrics: PromotionMetrics,
    thresholds: PromotionThresholds = DEFAULT_PROMOTION_THRESHOLDS,
) -> ThresholdEvaluation:
    checks = (
        _numeric_check(
            "total_return",
            metrics.total_return,
            ">=",
            thresholds.min_total_return,
        ),
        _numeric_check(
            "max_drawdown",
            metrics.max_drawdown,
            "<=",
            thresholds.max_drawdown,
        ),
        _numeric_check("win_rate", metrics.win_rate, ">=", thresholds.min_win_rate),
        _numeric_check(
            "expectancy", metrics.expectancy, ">=", thresholds.min_expectancy
        ),
        _numeric_check(
            "excess_return",
            metrics.excess_return,
            ">=",
            thresholds.min_excess_return,
        ),
        _integer_check("trade_count", metrics.trade_count, thresholds.min_trade_count),
        _integer_check(
            "walk_forward_folds",
            metrics.walk_forward_folds,
            thresholds.min_walk_forward_folds,
        ),
        _numeric_check(
            "walk_forward_pass_rate",
            metrics.walk_forward_pass_rate,
            ">=",
            thresholds.min_walk_forward_pass_rate,
        ),
        _boolean_check(
            "data_quality_evidence",
            metrics.data_quality_evidence,
            thresholds.require_data_quality_evidence,
        ),
        _boolean_check(
            "survivorship_evidence",
            metrics.survivorship_evidence,
            thresholds.require_survivorship_evidence,
        ),
        _boolean_check(
            "deterministic",
            metrics.deterministic,
            thresholds.require_deterministic,
        ),
    )
    return ThresholdEvaluation(
        passed=all(check.passed for check in checks),
        metrics_hash=hash_metrics_snapshot(metrics),
        checks=checks,
    )


def transition_promotion(
    current: StrategyPromotion,
    target: PromotionState | str,
    *,
    strategy_key: str,
    version: str,
    at: datetime,
    metrics: PromotionMetrics | None = None,
    thresholds: PromotionThresholds = DEFAULT_PROMOTION_THRESHOLDS,
    evidence: Sequence[PromotionEvidence] = (),
) -> StrategyPromotion:
    """Apply one monotonic transition to the exact strategy/version identity."""

    if (
        strategy_key.strip() != current.strategy_key
        or version.strip() != current.version
    ):
        raise PromotionIdentityMismatch(
            "a promotion transition cannot change strategy_key or version"
        )
    target_state = PromotionState(target)
    if target_state not in _ALLOWED_TRANSITIONS[current.state]:
        raise IllegalPromotionTransition(
            f"transition {current.state.value}->{target_state.value} is not allowed"
        )
    timestamp = _utc(at, "at")
    if timestamp < current.updated_at:
        raise ValueError("transition time cannot precede updated_at")
    supplied_evidence = tuple(evidence)

    if target_state == PromotionState.BACKTESTED:
        if metrics is None:
            raise ValueError("BACKTESTED requires a metrics snapshot")
        if not supplied_evidence:
            raise ValueError("BACKTESTED requires evidence")
        metrics_hash = hash_metrics_snapshot(metrics)
        return replace(
            current,
            state=target_state,
            metrics=metrics,
            metrics_hash=metrics_hash,
            evidence=current.evidence + supplied_evidence,
            updated_at=timestamp,
        )

    if target_state == PromotionState.PAPER_APPROVED:
        candidate_metrics = metrics or current.metrics
        if candidate_metrics is None:
            raise ValueError("PAPER_APPROVED requires BACKTESTED metrics")
        metrics_hash = hash_metrics_snapshot(candidate_metrics)
        if metrics_hash != current.metrics_hash:
            raise ValueError(
                "approval metrics must match the BACKTESTED snapshot exactly"
            )
        evaluation = evaluate_thresholds(candidate_metrics, thresholds)
        if not evaluation.passed:
            raise PromotionThresholdNotMet(evaluation)
        generated = (
            PromotionEvidence(
                code="METRICS_SNAPSHOT_HASH",
                detail="Approved metrics snapshot SHA-256.",
                reference=evaluation.metrics_hash,
            ),
            PromotionEvidence(
                code="THRESHOLD_EVALUATION",
                detail="Every configured PAPER promotion threshold passed.",
                reference=evaluation.metrics_hash,
            ),
        )
        return replace(
            current,
            state=target_state,
            metrics=candidate_metrics,
            metrics_hash=metrics_hash,
            threshold_evaluation=evaluation,
            evidence=current.evidence + supplied_evidence + generated,
            approved_at=timestamp,
            updated_at=timestamp,
        )

    if not supplied_evidence:
        raise ValueError(f"{target_state.value} requires evidence")
    if target_state == PromotionState.PAPER_SUSPENDED:
        return replace(
            current,
            state=target_state,
            evidence=current.evidence + supplied_evidence,
            suspended_at=timestamp,
            updated_at=timestamp,
        )
    return replace(
        current,
        state=PromotionState.RETIRED,
        evidence=current.evidence + supplied_evidence,
        retired_at=timestamp,
        updated_at=timestamp,
    )


def paper_approval_for(
    promotions: Sequence[StrategyPromotion],
    *,
    strategy_key: str,
    version: str,
) -> PaperApprovalDecision:
    """Resolve the owner-independent global gate for one exact version."""

    normalized_key = strategy_key.strip()
    normalized_version = version.strip()
    if not normalized_key or not normalized_version:
        raise ValueError("strategy_key and version are required")
    matching = [
        promotion
        for promotion in promotions
        if promotion.strategy_key == normalized_key
        and promotion.version == normalized_version
    ]
    if len(matching) > 1:
        raise ValueError("duplicate strategy/version promotion records")
    if not matching:
        return PaperApprovalDecision(
            approved=False,
            strategy_key=normalized_key,
            version=normalized_version,
            state=None,
            metrics_hash=None,
            reason="strategy_version_not_registered",
        )
    promotion = matching[0]
    approved = promotion.state == PromotionState.PAPER_APPROVED
    return PaperApprovalDecision(
        approved=approved,
        strategy_key=normalized_key,
        version=normalized_version,
        state=promotion.state,
        metrics_hash=promotion.metrics_hash,
        promotion_fingerprint=promotion.strategy_artifact_fingerprint,
        reason="paper_approved"
        if approved
        else f"state_{promotion.state.value.lower()}",
    )


def hash_metrics_snapshot(metrics: PromotionMetrics) -> str:
    payload = json.dumps(
        metrics.as_snapshot(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_state_invariants(promotion: StrategyPromotion) -> None:
    if promotion.state == PromotionState.DRAFT:
        if promotion.metrics is not None or promotion.metrics_hash is not None:
            raise ValueError("DRAFT cannot contain backtest metrics")
        if any(
            value is not None
            for value in (
                promotion.approved_at,
                promotion.suspended_at,
                promotion.retired_at,
                promotion.threshold_evaluation,
            )
        ):
            raise ValueError("DRAFT cannot contain lifecycle timestamps")
        return

    if promotion.state in {
        PromotionState.BACKTESTED,
        PromotionState.PAPER_APPROVED,
        PromotionState.PAPER_SUSPENDED,
    } and (promotion.metrics is None or promotion.metrics_hash is None):
        raise ValueError(f"{promotion.state.value} requires metrics and metrics_hash")

    if promotion.threshold_evaluation is not None:
        if promotion.threshold_evaluation.metrics_hash != promotion.metrics_hash:
            raise ValueError("threshold evaluation does not match the metrics snapshot")
        if (
            promotion.state
            in {PromotionState.PAPER_APPROVED, PromotionState.PAPER_SUSPENDED}
            and not promotion.threshold_evaluation.passed
        ):
            raise ValueError("approved promotion requires passed thresholds")

    if promotion.state == PromotionState.BACKTESTED:
        if any(
            value is not None
            for value in (
                promotion.approved_at,
                promotion.suspended_at,
                promotion.retired_at,
                promotion.threshold_evaluation,
            )
        ):
            raise ValueError("BACKTESTED cannot contain approval lifecycle fields")
    elif promotion.state == PromotionState.PAPER_APPROVED:
        if promotion.approved_at is None or promotion.threshold_evaluation is None:
            raise ValueError("PAPER_APPROVED requires approval evidence")
        if promotion.suspended_at is not None or promotion.retired_at is not None:
            raise ValueError("PAPER_APPROVED cannot be suspended or retired")
    elif promotion.state == PromotionState.PAPER_SUSPENDED:
        if (
            promotion.approved_at is None
            or promotion.suspended_at is None
            or promotion.threshold_evaluation is None
        ):
            raise ValueError("PAPER_SUSPENDED requires prior approval and suspension")
        if promotion.retired_at is not None:
            raise ValueError("PAPER_SUSPENDED cannot be retired")

    if promotion.retired_at is not None and promotion.state != PromotionState.RETIRED:
        raise ValueError("retired_at is valid only for RETIRED")
    if promotion.state == PromotionState.RETIRED and promotion.retired_at is None:
        raise ValueError("RETIRED requires retired_at")
    if promotion.suspended_at is not None and promotion.approved_at is None:
        raise ValueError("suspension requires prior approval")
    if (
        promotion.approved_at is not None
        and promotion.suspended_at is not None
        and promotion.suspended_at < promotion.approved_at
    ):
        raise ValueError("suspended_at cannot precede approved_at")
    latest_lifecycle = next(
        (
            value
            for value in (
                promotion.retired_at,
                promotion.suspended_at,
                promotion.approved_at,
            )
            if value is not None
        ),
        promotion.created_at,
    )
    if (
        latest_lifecycle < promotion.created_at
        or promotion.updated_at < latest_lifecycle
    ):
        raise ValueError("lifecycle timestamps must be monotonic")


def _numeric_check(
    metric: str, observed: Decimal, comparator: str, required: Decimal
) -> ThresholdCheck:
    passed = observed <= required if comparator == "<=" else observed >= required
    return ThresholdCheck(
        metric=metric,
        observed=_decimal_text(observed),
        comparator=comparator,
        required=_decimal_text(required),
        passed=passed,
    )


def _integer_check(metric: str, observed: int, required: int) -> ThresholdCheck:
    return ThresholdCheck(
        metric=metric,
        observed=str(observed),
        comparator=">=",
        required=str(required),
        passed=observed >= required,
    )


def _boolean_check(metric: str, observed: bool, required: bool) -> ThresholdCheck:
    return ThresholdCheck(
        metric=metric,
        observed=str(observed).lower(),
        comparator="required" if required else "not_required",
        required=str(required).lower(),
        passed=observed or not required,
    )


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == _ZERO else format(normalized, "f")


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_PAPER_STRATEGY_KEY",
    "DEFAULT_PAPER_STRATEGY_VERSION",
    "DEFAULT_PROMOTION_THRESHOLDS",
    "IllegalPromotionTransition",
    "PaperApprovalDecision",
    "PromotionEvidence",
    "PromotionIdentityMismatch",
    "PromotionMetrics",
    "PromotionState",
    "PromotionThresholdNotMet",
    "PromotionThresholds",
    "StrategyPromotion",
    "ThresholdCheck",
    "ThresholdEvaluation",
    "create_draft",
    "evaluate_thresholds",
    "hash_metrics_snapshot",
    "paper_approval_for",
    "transition_promotion",
]
