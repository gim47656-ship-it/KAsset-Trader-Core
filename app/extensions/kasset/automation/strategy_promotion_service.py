"""Persisted, research-registry-backed PAPER strategy promotion gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import DecimalException
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.promotion_evidence import (
    PromotionEvidenceBuildError,
    derive_metrics_from_stored_payload,
)
from app.extensions.kasset.automation.strategy_artifact import (
    PROMOTION_EVIDENCE_SCHEMA_VERSION,
    current_strategy_artifact,
)
from app.extensions.kasset.automation.strategy_promotion import (
    PaperApprovalDecision,
    PromotionEvidence,
    PromotionMetrics,
    PromotionState,
    PromotionThresholds,
    PromotionTrack,
    StrategyPromotion,
    ThresholdCheck,
    ThresholdEvaluation,
    create_draft,
    evaluate_thresholds,
    paper_approval_for,
    promotion_thresholds_for_track,
    transition_promotion,
)
from app.extensions.kasset.models import KAssetStrategyPromotion
from app.models.ai_recommendations import AIRecommendation
from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchPromotionCandidate,
    ResearchStrategyExperiment,
)
from app.services.research_canonical_hash import (
    IDENTITY_COMPONENTS,
    canonical_sha256,
    compute_identity_hashes_from_ast,
    derive_experiment_id,
)


class PromotionCandidateTrustError(ValueError):
    """A candidate/run/experiment chain is missing, malformed, or divergent."""


@dataclass(frozen=True, slots=True)
class RecommendationStrategyIdentity:
    strategy_key: str
    version: str
    artifact_fingerprint: str

    def __post_init__(self) -> None:
        strategy_key = self.strategy_key.strip()
        version = self.version.strip()
        fingerprint = self.artifact_fingerprint.strip()
        if not strategy_key or not version:
            raise ValueError("strategy_key and version are required")
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("artifact_fingerprint must be lowercase 64-hex")
        object.__setattr__(self, "strategy_key", strategy_key)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "artifact_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class _CandidateTrust:
    candidate: ResearchPromotionCandidate
    run: ResearchBacktestRun
    experiment: ResearchStrategyExperiment
    metrics: PromotionMetrics
    artifact_fingerprint: str
    source_commit: str
    evidence_schema_version: str
    #: 근거 payload가 선언한 트랙과 그 트랙에만 허용된 임계 프로필.
    track: PromotionTrack
    thresholds: PromotionThresholds


def recommendation_strategy_identity(
    recommendation: AIRecommendation,
) -> RecommendationStrategyIdentity | None:
    matches: list[RecommendationStrategyIdentity] = []
    for item in recommendation.evidence or []:
        if not isinstance(item, Mapping) or item.get("kind") != "strategy_promotion":
            continue
        strategy_key = item.get("strategyKey")
        version = item.get("version")
        fingerprint = item.get("artifactFingerprint")
        if not all(
            isinstance(value, str) for value in (strategy_key, version, fingerprint)
        ):
            return None
        try:
            matches.append(
                RecommendationStrategyIdentity(
                    strategy_key=cast(str, strategy_key),
                    version=cast(str, version),
                    artifact_fingerprint=cast(str, fingerprint),
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
        total_return=str(raw["totalReturn"]),
        max_drawdown=str(raw["maxDrawdown"]),
        win_rate=str(raw["winRate"]),
        expectancy=str(raw["expectancy"]),
        excess_return=str(raw["excessReturn"]),
        gross_profit=str(raw["grossProfit"]),
        gross_loss=str(raw["grossLoss"]),
        cost_stressed_total_return=str(raw["costStressedTotalReturn"]),
        total_costs=str(raw["totalCosts"]),
        trade_count=raw["tradeCount"],  # type: ignore[arg-type]
        walk_forward_folds=raw["walkForwardFolds"],  # type: ignore[arg-type]
        walk_forward_passed_folds=raw["walkForwardPassedFolds"],  # type: ignore[arg-type]
        data_quality_evidence=raw["dataQualityEvidence"],  # type: ignore[arg-type]
        survivorship_evidence=raw["survivorshipEvidence"],  # type: ignore[arg-type]
        deterministic=raw["deterministic"],  # type: ignore[arg-type]
        backtest_hashes=tuple(str(value) for value in hashes),
    )
    stored_pass_rate = raw.get("walkForwardPassRate")
    if stored_pass_rate is not None and str(metrics.walk_forward_pass_rate) != str(
        stored_pass_rate
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
                passed=cast(bool, item["passed"]),
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
        promotion_candidate_id=row.promotion_candidate_id,
        strategy_artifact_fingerprint=row.strategy_artifact_fingerprint,
        source_commit=row.source_commit,
        evidence_schema_version=row.evidence_schema_version,
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
    """Persist transitions and resolve AUTO_PAPER from immutable registry evidence."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(
        self,
        strategy_key: str,
        version: str,
        *,
        for_update: bool = False,
    ) -> StrategyPromotion | None:
        row = await self._promotion_row(
            strategy_key=strategy_key,
            version=version,
            for_update=for_update,
        )
        return _promotion_from_row(row) if row is not None else None

    async def list_status(self) -> tuple[StrategyPromotion, ...]:
        rows = (
            await self._db.scalars(
                select(KAssetStrategyPromotion).order_by(
                    KAssetStrategyPromotion.updated_at.desc(),
                    KAssetStrategyPromotion.id.desc(),
                )
            )
        ).all()
        return tuple(_promotion_from_row(row) for row in rows)

    async def create_draft(
        self,
        candidate_id: int,
        *,
        at: datetime,
        operator_reason: str,
    ) -> StrategyPromotion:
        reason = _operator_reason(operator_reason)
        preview = await self._candidate_trust(candidate_id, for_update=False)
        if (
            await self._promotion_row(
                strategy_key=preview.experiment.strategy_key,
                version=preview.experiment.strategy_version,
                for_update=True,
            )
            is not None
        ):
            raise ValueError("strategy/version promotion already exists")
        trust = await self._candidate_trust(candidate_id)
        if (
            trust.experiment.strategy_key != preview.experiment.strategy_key
            or trust.experiment.strategy_version != preview.experiment.strategy_version
        ):
            raise PromotionCandidateTrustError("candidate_identity_changed")
        evidence = (
            _candidate_evidence(trust),
            PromotionEvidence(
                code="OPERATOR_DRAFT_REASON",
                detail=reason,
                reference=f"candidate:{candidate_id}",
            ),
        )
        promotion = create_draft(
            trust.experiment.strategy_key,
            trust.experiment.strategy_version,
            at=at,
            evidence=evidence,
            promotion_candidate_id=trust.candidate.id,
            strategy_artifact_fingerprint=trust.artifact_fingerprint,
            source_commit=trust.source_commit,
            evidence_schema_version=trust.evidence_schema_version,
        )
        row = KAssetStrategyPromotion(
            strategy_key=promotion.strategy_key,
            version=promotion.version,
            state=promotion.state.value,
            metrics={},
            metrics_hash=None,
            promotion_candidate_id=trust.candidate.id,
            strategy_artifact_fingerprint=trust.artifact_fingerprint,
            source_commit=trust.source_commit,
            evidence_schema_version=trust.evidence_schema_version,
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

    async def approve_candidate(
        self,
        candidate_id: int,
        *,
        at: datetime,
        operator_reason: str,
        # 임계 프로필은 후보가 실제로 평가받은 트랙에서만 나온다. 호출자가
        # 느슨한 프로필을 넘겨 gate를 우회할 여지를 남기지 않는다.
    ) -> StrategyPromotion:
        reason = _operator_reason(operator_reason)
        row = await self._db.scalar(
            select(KAssetStrategyPromotion)
            .where(KAssetStrategyPromotion.promotion_candidate_id == candidate_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError("promotion draft for candidate is not registered")
        trust = await self._candidate_trust(candidate_id)
        current = _promotion_from_row(row)
        _verify_promotion_trust(current, trust)
        if current.state == PromotionState.DRAFT:
            current = transition_promotion(
                current,
                PromotionState.BACKTESTED,
                strategy_key=current.strategy_key,
                version=current.version,
                at=at,
                metrics=trust.metrics,
                evidence=(
                    PromotionEvidence(
                        code="PERSISTED_BACKTEST_CANDIDATE",
                        detail="Metrics derived from the locked research candidate chain.",
                        reference=f"candidate:{candidate_id}",
                    ),
                ),
            )
        approval = transition_promotion(
            current,
            PromotionState.PAPER_APPROVED,
            strategy_key=current.strategy_key,
            version=current.version,
            at=at,
            thresholds=trust.thresholds,
            evidence=(
                PromotionEvidence(
                    code="OPERATOR_APPROVAL_REASON",
                    detail=reason,
                    reference=f"candidate:{candidate_id}",
                ),
            ),
        )
        _apply_promotion(row, approval)
        await self._db.commit()
        return approval

    async def transition(
        self,
        strategy_key: str,
        version: str,
        target: PromotionState | str,
        *,
        at: datetime,
        operator_reason: str,
    ) -> StrategyPromotion:
        """Apply suspend/retire only; metrics can never enter through this API."""

        target_state = PromotionState(target)
        if target_state not in {
            PromotionState.PAPER_SUSPENDED,
            PromotionState.RETIRED,
        }:
            raise ValueError("BACKTESTED/PAPER_APPROVED require a persisted candidate")
        row = await self._promotion_row(
            strategy_key=strategy_key,
            version=version,
            for_update=True,
        )
        if row is None:
            raise ValueError("strategy/version promotion is not registered")
        current = _promotion_from_row(row)
        if current.promotion_candidate_id is None:
            raise PromotionCandidateTrustError("legacy_promotion_evidence_missing")
        trust = await self._candidate_trust(current.promotion_candidate_id)
        _verify_promotion_trust(current, trust)
        promotion = transition_promotion(
            current,
            target_state,
            strategy_key=strategy_key,
            version=version,
            at=at,
            evidence=(
                PromotionEvidence(
                    code=f"OPERATOR_{target_state.value}_REASON",
                    detail=_operator_reason(operator_reason),
                    reference=f"candidate:{trust.candidate.id}",
                ),
            ),
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
            row = await self._promotion_row(
                strategy_key=identity.strategy_key,
                version=identity.version,
                for_update=for_update,
            )
            if row is None:
                return _decision(identity, reason="strategy_version_not_registered")
            promotion = _promotion_from_row(row)
            if promotion.promotion_candidate_id is None:
                return _decision(
                    identity,
                    promotion=promotion,
                    reason="legacy_promotion_evidence_missing",
                )
            trust = await self._candidate_trust(promotion.promotion_candidate_id)
            _verify_promotion_trust(promotion, trust)
        except (
            DecimalException,
            KeyError,
            PromotionCandidateTrustError,
            PromotionEvidenceBuildError,
            TypeError,
            ValueError,
        ):
            return _decision(identity, reason="strategy_promotion_record_invalid")

        state_decision = paper_approval_for(
            (promotion,),
            strategy_key=identity.strategy_key,
            version=identity.version,
        )
        if not state_decision.approved:
            return _decision(
                identity,
                promotion=promotion,
                reason=state_decision.reason,
            )
        try:
            runtime_fingerprint = current_strategy_artifact().fingerprint
        except (OSError, TypeError, ValueError):
            return _decision(
                identity,
                promotion=promotion,
                reason="runtime_strategy_artifact_unavailable",
            )
        if identity.artifact_fingerprint != promotion.strategy_artifact_fingerprint:
            return _decision(
                identity,
                promotion=promotion,
                runtime_fingerprint=runtime_fingerprint,
                reason="recommendation_promotion_fingerprint_mismatch",
            )
        if promotion.strategy_artifact_fingerprint != runtime_fingerprint:
            return _decision(
                identity,
                promotion=promotion,
                runtime_fingerprint=runtime_fingerprint,
                reason="promotion_runtime_fingerprint_mismatch",
            )
        return PaperApprovalDecision(
            approved=True,
            strategy_key=identity.strategy_key,
            version=identity.version,
            state=promotion.state,
            metrics_hash=promotion.metrics_hash,
            reason="paper_approved",
            promotion_fingerprint=promotion.strategy_artifact_fingerprint,
            recommendation_fingerprint=identity.artifact_fingerprint,
            runtime_fingerprint=runtime_fingerprint,
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

    async def _promotion_row(
        self,
        *,
        strategy_key: str,
        version: str,
        for_update: bool,
    ) -> KAssetStrategyPromotion | None:
        statement = select(KAssetStrategyPromotion).where(
            KAssetStrategyPromotion.strategy_key == strategy_key.strip(),
            KAssetStrategyPromotion.version == version.strip(),
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._db.scalar(statement)

    async def _candidate_trust(
        self,
        candidate_id: int,
        *,
        for_update: bool = True,
    ) -> _CandidateTrust:
        if type(candidate_id) is not int or candidate_id < 1:
            raise PromotionCandidateTrustError("candidate_id_invalid")
        statement = (
            select(
                ResearchPromotionCandidate,
                ResearchBacktestRun,
                ResearchStrategyExperiment,
            )
            .join(
                ResearchBacktestRun,
                ResearchBacktestRun.id == ResearchPromotionCandidate.backtest_run_id,
            )
            .join(
                ResearchStrategyExperiment,
                ResearchStrategyExperiment.id
                == ResearchBacktestRun.strategy_experiment_id,
            )
            .where(ResearchPromotionCandidate.id == candidate_id)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._db.execute(statement)
        joined = result.one_or_none()
        if joined is None:
            raise PromotionCandidateTrustError("promotion_candidate_missing")
        candidate, run, experiment = joined
        _verify_experiment(experiment)
        raw = run.raw_payload
        if type(raw) is not dict:
            raise PromotionCandidateTrustError("backtest_raw_payload_missing")
        payload_hash = canonical_sha256(raw)
        if (
            run.trial_status != "completed"
            or run.strategy_experiment_id != experiment.id
            or run.artifact_hash != payload_hash
        ):
            raise PromotionCandidateTrustError("backtest_run_hash_mismatch")
        metrics = derive_metrics_from_stored_payload(raw)
        strategy = cast(Mapping[str, object], raw["strategy"])
        fingerprint = str(strategy["artifactFingerprint"])
        source_commit = str(strategy["sourceCommit"])
        schema_version = str(raw["schemaVersion"])
        if (
            run.gate_artifact_hash != fingerprint
            or experiment.strategy_key != strategy["key"]
            or experiment.strategy_version != strategy["version"]
            or candidate.backtest_run_id != run.id
            or candidate.experiment_id != experiment.experiment_id
            or candidate.run_config_hash != experiment.frozen_config_hash
            or candidate.run_data_hash != experiment.dataset_manifest_hash
            or canonical_sha256(candidate.metrics)
            != canonical_sha256(metrics.as_snapshot())
            or canonical_sha256(candidate.thresholds)
            != canonical_sha256(raw.get("promotionThresholds"))
        ):
            raise PromotionCandidateTrustError("candidate_run_experiment_hash_mismatch")
        # ``derive_metrics_from_stored_payload`` already pinned the stored
        # threshold snapshot to this track, so the profile below is the only
        # one this candidate could ever have been evaluated against.
        track = cast(PromotionTrack, str(raw["promotionTrack"]))
        thresholds = promotion_thresholds_for_track(track)
        evaluation = evaluate_thresholds(metrics, thresholds)
        expected_status = "eligible" if evaluation.passed else "non_promotable"
        expected_reason = (
            "thresholds_passed"
            if evaluation.passed
            else f"threshold_failed:{evaluation.failed_metrics[0]}"
        )
        if (
            candidate.status != expected_status
            or candidate.reason_code != expected_reason
        ):
            raise PromotionCandidateTrustError("candidate_evaluation_mismatch")
        if schema_version != PROMOTION_EVIDENCE_SCHEMA_VERSION:
            raise PromotionCandidateTrustError("evidence_schema_version_mismatch")
        return _CandidateTrust(
            candidate=candidate,
            run=run,
            experiment=experiment,
            metrics=metrics,
            artifact_fingerprint=fingerprint,
            source_commit=source_commit,
            evidence_schema_version=schema_version,
            track=track,
            thresholds=thresholds,
        )


def _verify_experiment(experiment: ResearchStrategyExperiment) -> None:
    if type(experiment.manifest) is not dict:
        raise PromotionCandidateTrustError("experiment_manifest_missing")
    try:
        component_hashes = compute_identity_hashes_from_ast(experiment.manifest)
    except (TypeError, ValueError) as exc:
        raise PromotionCandidateTrustError("experiment_manifest_invalid") from exc
    if any(
        getattr(experiment, f"{component}_hash")
        != component_hashes[f"{component}_hash"]
        for component in IDENTITY_COMPONENTS
    ):
        raise PromotionCandidateTrustError("experiment_component_hash_mismatch")
    expected_id = derive_experiment_id(
        experiment.strategy_key,
        experiment.strategy_version,
        component_hashes,
    )
    if expected_id != experiment.experiment_id:
        raise PromotionCandidateTrustError("experiment_id_hash_mismatch")


def _verify_promotion_trust(
    promotion: StrategyPromotion,
    trust: _CandidateTrust,
) -> None:
    if (
        promotion.promotion_candidate_id != trust.candidate.id
        or promotion.strategy_key != trust.experiment.strategy_key
        or promotion.version != trust.experiment.strategy_version
        or promotion.strategy_artifact_fingerprint != trust.artifact_fingerprint
        or promotion.source_commit != trust.source_commit
        or promotion.evidence_schema_version != trust.evidence_schema_version
    ):
        raise PromotionCandidateTrustError("promotion_candidate_identity_mismatch")
    if promotion.metrics is not None and canonical_sha256(
        promotion.metrics.as_snapshot()
    ) != canonical_sha256(trust.metrics.as_snapshot()):
        raise PromotionCandidateTrustError("promotion_metrics_mismatch")


def _candidate_evidence(trust: _CandidateTrust) -> PromotionEvidence:
    return PromotionEvidence(
        code="RESEARCH_PROMOTION_CANDIDATE",
        detail="Locked candidate, run, experiment, and canonical hashes verified.",
        reference=f"candidate:{trust.candidate.id};run:{trust.run.id}",
    )


def _operator_reason(value: str) -> str:
    reason = value.strip()
    if not reason:
        raise ValueError("operator_reason is required")
    return reason


def _decision(
    identity: RecommendationStrategyIdentity,
    *,
    reason: str,
    promotion: StrategyPromotion | None = None,
    runtime_fingerprint: str | None = None,
) -> PaperApprovalDecision:
    return PaperApprovalDecision(
        approved=False,
        strategy_key=identity.strategy_key,
        version=identity.version,
        state=promotion.state if promotion is not None else None,
        metrics_hash=promotion.metrics_hash if promotion is not None else None,
        reason=reason,
        promotion_fingerprint=(
            promotion.strategy_artifact_fingerprint if promotion is not None else None
        ),
        recommendation_fingerprint=identity.artifact_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
    )


__all__ = [
    "PromotionCandidateTrustError",
    "RecommendationStrategyIdentity",
    "StrategyPromotionService",
    "recommendation_strategy_identity",
]
