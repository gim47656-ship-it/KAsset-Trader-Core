"""Persisted, owner-independent PAPER strategy promotion gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PROMOTION_THRESHOLDS,
    PaperApprovalDecision,
    PromotionEvidence,
    PromotionMetrics,
    PromotionState,
    PromotionThresholds,
    StrategyPromotion,
    ThresholdCheck,
    ThresholdEvaluation,
    create_draft,
    paper_approval_for,
    transition_promotion,
)
from app.extensions.kasset.models import KAssetStrategyPromotion
from app.models.ai_recommendations import AIRecommendation


@dataclass(frozen=True, slots=True)
class RecommendationStrategyIdentity:
    strategy_key: str
    version: str

    def __post_init__(self) -> None:
        strategy_key = self.strategy_key.strip()
        version = self.version.strip()
        if not strategy_key or not version:
            raise ValueError("strategy_key and version are required")
        object.__setattr__(self, "strategy_key", strategy_key)
        object.__setattr__(self, "version", version)


def recommendation_strategy_identity(
    recommendation: AIRecommendation,
) -> RecommendationStrategyIdentity | None:
    matches: list[RecommendationStrategyIdentity] = []
    for item in recommendation.evidence or []:
        if not isinstance(item, Mapping) or item.get("kind") != "strategy_promotion":
            continue
        strategy_key = item.get("strategyKey")
        version = item.get("version")
        if not isinstance(strategy_key, str) or not isinstance(version, str):
            return None
        try:
            matches.append(
                RecommendationStrategyIdentity(
                    strategy_key=strategy_key,
                    version=version,
                )
            )
        except ValueError:
            return None
    if len(matches) != 1:
        return None
    return matches[0]


def _metrics_from_snapshot(raw: object) -> PromotionMetrics | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    hashes = raw.get("backtestHashes")
    if not isinstance(hashes, Sequence) or isinstance(hashes, (str, bytes)):
        raise ValueError("promotion backtestHashes must be an array")
    metrics = PromotionMetrics(
        total_return=Decimal(str(raw["totalReturn"])),
        max_drawdown=Decimal(str(raw["maxDrawdown"])),
        win_rate=Decimal(str(raw["winRate"])),
        expectancy=Decimal(str(raw["expectancy"])),
        excess_return=Decimal(str(raw["excessReturn"])),
        trade_count=raw["tradeCount"],  # type: ignore[arg-type]
        walk_forward_folds=raw["walkForwardFolds"],  # type: ignore[arg-type]
        walk_forward_passed_folds=raw["walkForwardPassedFolds"],  # type: ignore[arg-type]
        data_quality_evidence=raw["dataQualityEvidence"],  # type: ignore[arg-type]
        survivorship_evidence=raw["survivorshipEvidence"],  # type: ignore[arg-type]
        deterministic=raw["deterministic"],  # type: ignore[arg-type]
        backtest_hashes=tuple(str(value) for value in hashes),
    )
    stored_pass_rate = raw.get("walkForwardPassRate")
    if (
        stored_pass_rate is not None
        and Decimal(str(stored_pass_rate)) != metrics.walk_forward_pass_rate
    ):
        raise ValueError("stored walk-forward pass rate is inconsistent")
    return metrics


def _threshold_from_snapshot(raw: object) -> ThresholdEvaluation | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("promotion threshold evaluation must be an object")
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, Sequence) or isinstance(checks_raw, (str, bytes)):
        raise ValueError("promotion threshold checks must be an array")
    checks: list[ThresholdCheck] = []
    for item in checks_raw:
        if not isinstance(item, Mapping) or type(item.get("passed")) is not bool:
            raise ValueError("promotion threshold check is invalid")
        values = tuple(
            str(item.get(field_name, "")).strip()
            for field_name in ("metric", "observed", "comparator", "required")
        )
        if not all(values):
            raise ValueError("promotion threshold check fields are required")
        checks.append(
            ThresholdCheck(
                metric=values[0],
                observed=values[1],
                comparator=values[2],
                required=values[3],
                passed=item["passed"],  # type: ignore[arg-type]
            )
        )
    metrics_hash = str(raw.get("metricsHash", ""))
    passed = raw.get("passed")
    failed_metrics = tuple(check.metric for check in checks if not check.passed)
    stored_failed = raw.get("failedMetrics")
    if (
        type(passed) is not bool
        or not metrics_hash
        or not checks
        or passed != all(check.passed for check in checks)
        or not isinstance(stored_failed, Sequence)
        or isinstance(stored_failed, (str, bytes))
        or tuple(str(value) for value in stored_failed) != failed_metrics
    ):
        raise ValueError("promotion threshold summary is inconsistent")
    return ThresholdEvaluation(
        passed=passed,
        metrics_hash=metrics_hash,
        checks=tuple(checks),
    )


def _promotion_evidence(raw: object) -> tuple[PromotionEvidence, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("promotion evidence must be an array")
    output: list[PromotionEvidence] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("promotion evidence item must be an object")
        output.append(
            PromotionEvidence(
                code=str(item.get("code", "")),
                detail=str(item.get("detail", "")),
                reference=str(item.get("reference", "")),
            )
        )
    return tuple(output)


def _promotion_from_row(row: KAssetStrategyPromotion) -> StrategyPromotion:
    return StrategyPromotion(
        strategy_key=row.strategy_key,
        version=row.version,
        state=PromotionState(row.state),
        metrics=_metrics_from_snapshot(row.metrics),
        metrics_hash=row.metrics_hash,
        threshold_evaluation=_threshold_from_snapshot(row.threshold_evaluation),
        evidence=_promotion_evidence(row.evidence),
        approved_at=row.approved_at,
        suspended_at=row.suspended_at,
        retired_at=row.retired_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply_promotion(
    row: KAssetStrategyPromotion,
    promotion: StrategyPromotion,
) -> None:
    row.state = promotion.state.value
    row.metrics = promotion.metrics_snapshot()
    row.metrics_hash = promotion.metrics_hash
    row.threshold_evaluation = (
        promotion.threshold_evaluation.as_evidence()
        if promotion.threshold_evaluation is not None
        else None
    )
    row.evidence = [item.as_evidence() for item in promotion.evidence]
    row.approved_at = promotion.approved_at
    row.suspended_at = promotion.suspended_at
    row.retired_at = promotion.retired_at
    row.updated_at = promotion.updated_at


class StrategyPromotionService:
    """Persist promotion transitions and resolve AUTO_PAPER eligibility."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(
        self,
        strategy_key: str,
        version: str,
        *,
        for_update: bool = False,
    ) -> StrategyPromotion | None:
        statement = select(KAssetStrategyPromotion).where(
            KAssetStrategyPromotion.strategy_key == strategy_key.strip(),
            KAssetStrategyPromotion.version == version.strip(),
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._db.scalar(statement)
        return _promotion_from_row(row) if row is not None else None

    async def create_draft(
        self,
        strategy_key: str,
        version: str,
        *,
        at: datetime,
        evidence: Sequence[PromotionEvidence] = (),
    ) -> StrategyPromotion:
        if await self.get(strategy_key, version, for_update=True) is not None:
            raise ValueError("strategy/version promotion already exists")
        promotion = create_draft(
            strategy_key,
            version,
            at=at,
            evidence=evidence,
        )
        row = KAssetStrategyPromotion(
            strategy_key=promotion.strategy_key,
            version=promotion.version,
            state=promotion.state.value,
            metrics={},
            metrics_hash=None,
            threshold_evaluation=None,
            evidence=[item.as_evidence() for item in promotion.evidence],
            approved_at=None,
            suspended_at=None,
            retired_at=None,
            created_at=promotion.created_at,
            updated_at=promotion.updated_at,
        )
        self._db.add(row)
        await self._db.commit()
        return promotion

    async def transition(
        self,
        strategy_key: str,
        version: str,
        target: PromotionState | str,
        *,
        at: datetime,
        metrics: PromotionMetrics | None = None,
        thresholds: PromotionThresholds = DEFAULT_PROMOTION_THRESHOLDS,
        evidence: Sequence[PromotionEvidence] = (),
    ) -> StrategyPromotion:
        statement = (
            select(KAssetStrategyPromotion)
            .where(
                KAssetStrategyPromotion.strategy_key == strategy_key.strip(),
                KAssetStrategyPromotion.version == version.strip(),
            )
            .with_for_update()
        )
        row = await self._db.scalar(statement)
        if row is None:
            raise ValueError("strategy/version promotion is not registered")
        promotion = transition_promotion(
            _promotion_from_row(row),
            target,
            strategy_key=strategy_key,
            version=version,
            at=at,
            metrics=metrics,
            thresholds=thresholds,
            evidence=evidence,
        )
        _apply_promotion(row, promotion)
        await self._db.commit()
        return promotion

    async def approval_for_identity(
        self,
        identity: RecommendationStrategyIdentity,
        *,
        for_update: bool = False,
    ) -> PaperApprovalDecision:
        try:
            promotion = await self.get(
                identity.strategy_key,
                identity.version,
                for_update=for_update,
            )
        except (DecimalException, KeyError, TypeError, ValueError):
            return PaperApprovalDecision(
                approved=False,
                strategy_key=identity.strategy_key,
                version=identity.version,
                state=None,
                metrics_hash=None,
                reason="strategy_promotion_record_invalid",
            )
        return paper_approval_for(
            (promotion,) if promotion is not None else (),
            strategy_key=identity.strategy_key,
            version=identity.version,
        )

    async def approval_for_recommendation(
        self,
        recommendation: AIRecommendation,
        *,
        for_update: bool = False,
    ) -> PaperApprovalDecision:
        identity = recommendation_strategy_identity(recommendation)
        if identity is None:
            return PaperApprovalDecision(
                approved=False,
                strategy_key="",
                version="",
                state=None,
                metrics_hash=None,
                reason="recommendation_strategy_identity_invalid",
            )
        return await self.approval_for_identity(identity, for_update=for_update)

__all__ = [
    "RecommendationStrategyIdentity",
    "StrategyPromotionService",
    "recommendation_strategy_identity",
]
