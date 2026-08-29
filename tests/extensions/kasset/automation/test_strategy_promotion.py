from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation.strategy_promotion import (
    IllegalPromotionTransition,
    PromotionEvidence,
    PromotionIdentityMismatch,
    PromotionMetrics,
    PromotionState,
    PromotionThresholdNotMet,
    PromotionThresholds,
    StrategyPromotion,
    create_draft,
    evaluate_thresholds,
    hash_metrics_snapshot,
    paper_approval_for,
    transition_promotion,
)
from app.extensions.kasset.automation.strategy_promotion_service import (
    StrategyPromotionService,
)
from app.models.ai_recommendations import AIRecommendation

_NOW = datetime(2026, 8, 29, 9, tzinfo=UTC)


def _evidence(code: str = "BACKTEST") -> PromotionEvidence:
    return PromotionEvidence(
        code=code,
        detail=f"{code} deterministic evidence.",
        reference=f"evidence:{code.lower()}",
    )


def _passing_metrics() -> PromotionMetrics:
    return PromotionMetrics(
        total_return=Decimal("0.18"),
        max_drawdown=Decimal("0.12"),
        win_rate=Decimal("0.52"),
        expectancy=Decimal("125.50"),
        excess_return=Decimal("0.08"),
        trade_count=48,
        walk_forward_folds=5,
        walk_forward_passed_folds=4,
        data_quality_evidence=True,
        survivorship_evidence=True,
        deterministic=True,
        backtest_hashes=("a" * 64, "b" * 64),
    )


def _backtested(version: str = "1.0.0"):
    draft = create_draft(
        "qullamaggie_breakout_portfolio",
        version,
        at=_NOW,
    )
    return transition_promotion(
        draft,
        PromotionState.BACKTESTED,
        strategy_key=draft.strategy_key,
        version=draft.version,
        at=_NOW + timedelta(minutes=1),
        metrics=_passing_metrics(),
        evidence=(_evidence(),),
    )


def _approved(version: str = "1.0.0"):
    backtested = _backtested(version)
    return transition_promotion(
        backtested,
        PromotionState.PAPER_APPROVED,
        strategy_key=backtested.strategy_key,
        version=backtested.version,
        at=_NOW + timedelta(minutes=2),
    )


def test_threshold_evaluation_returns_snapshot_hash_and_all_checks() -> None:
    metrics = _passing_metrics()

    evaluation = evaluate_thresholds(metrics)

    assert evaluation.passed is True
    assert evaluation.failed_metrics == ()
    assert evaluation.metrics_hash == hash_metrics_snapshot(metrics)
    assert {check.metric for check in evaluation.checks} == {
        "total_return",
        "max_drawdown",
        "win_rate",
        "expectancy",
        "excess_return",
        "trade_count",
        "walk_forward_folds",
        "walk_forward_pass_rate",
        "data_quality_evidence",
        "survivorship_evidence",
        "deterministic",
    }


def test_each_failed_threshold_is_returned_as_stable_evidence() -> None:
    metrics = replace(
        _passing_metrics(),
        max_drawdown=Decimal("0.30"),
        trade_count=2,
        walk_forward_passed_folds=1,
        deterministic=False,
    )

    evaluation = evaluate_thresholds(metrics)

    assert evaluation.passed is False
    assert set(evaluation.failed_metrics) == {
        "max_drawdown",
        "trade_count",
        "walk_forward_pass_rate",
        "deterministic",
    }


def test_paper_approval_requires_every_threshold_to_pass() -> None:
    metrics = replace(_passing_metrics(), excess_return=Decimal("-0.01"))
    draft = create_draft("strategy", "2.0.0", at=_NOW)
    backtested = transition_promotion(
        draft,
        PromotionState.BACKTESTED,
        strategy_key="strategy",
        version="2.0.0",
        at=_NOW + timedelta(minutes=1),
        metrics=metrics,
        evidence=(_evidence(),),
    )

    with pytest.raises(PromotionThresholdNotMet) as captured:
        transition_promotion(
            backtested,
            PromotionState.PAPER_APPROVED,
            strategy_key="strategy",
            version="2.0.0",
            at=_NOW + timedelta(minutes=2),
        )

    assert captured.value.evaluation.failed_metrics == ("excess_return",)
    assert backtested.state == PromotionState.BACKTESTED


def test_approved_record_preserves_exact_metrics_hash_and_evidence() -> None:
    approved = _approved()

    assert approved.state == PromotionState.PAPER_APPROVED
    assert approved.approved_at == _NOW + timedelta(minutes=2)
    assert approved.threshold_evaluation is not None
    assert approved.threshold_evaluation.passed is True
    assert approved.metrics_hash == hash_metrics_snapshot(_passing_metrics())
    assert approved.metrics_snapshot() == _passing_metrics().as_snapshot()
    assert {item.code for item in approved.evidence} >= {
        "BACKTEST",
        "METRICS_SNAPSHOT_HASH",
        "THRESHOLD_EVALUATION",
    }


def test_illegal_or_backward_transitions_are_rejected() -> None:
    draft = create_draft("strategy", "1", at=_NOW)
    approved = _approved()

    with pytest.raises(IllegalPromotionTransition):
        transition_promotion(
            draft,
            PromotionState.PAPER_APPROVED,
            strategy_key="strategy",
            version="1",
            at=_NOW + timedelta(minutes=1),
        )

    suspended = transition_promotion(
        approved,
        PromotionState.PAPER_SUSPENDED,
        strategy_key=approved.strategy_key,
        version=approved.version,
        at=_NOW + timedelta(minutes=3),
        evidence=(_evidence("SUSPEND"),),
    )
    with pytest.raises(IllegalPromotionTransition):
        transition_promotion(
            suspended,
            PromotionState.PAPER_APPROVED,
            strategy_key=suspended.strategy_key,
            version=suspended.version,
            at=_NOW + timedelta(minutes=4),
        )


def test_transition_cannot_change_strategy_or_version() -> None:
    draft = create_draft("strategy", "1.0.0", at=_NOW)

    with pytest.raises(PromotionIdentityMismatch):
        transition_promotion(
            draft,
            PromotionState.BACKTESTED,
            strategy_key="strategy",
            version="1.0.1",
            at=_NOW + timedelta(minutes=1),
            metrics=_passing_metrics(),
            evidence=(_evidence(),),
        )


def test_global_paper_gate_isolated_by_exact_version_and_suspension() -> None:
    approved_v1 = _approved("1.0.0")
    backtested_v2 = _backtested("2.0.0")

    v1 = paper_approval_for(
        (approved_v1, backtested_v2),
        strategy_key=approved_v1.strategy_key,
        version="1.0.0",
    )
    v2 = paper_approval_for(
        (approved_v1, backtested_v2),
        strategy_key=approved_v1.strategy_key,
        version="2.0.0",
    )
    missing = paper_approval_for(
        (approved_v1, backtested_v2),
        strategy_key=approved_v1.strategy_key,
        version="3.0.0",
    )

    assert v1.approved is True
    assert v1.reason == "paper_approved"
    assert v2.approved is False
    assert v2.state == PromotionState.BACKTESTED
    assert missing.approved is False
    assert missing.reason == "strategy_version_not_registered"

    suspended = transition_promotion(
        approved_v1,
        PromotionState.PAPER_SUSPENDED,
        strategy_key=approved_v1.strategy_key,
        version=approved_v1.version,
        at=_NOW + timedelta(minutes=3),
        evidence=(_evidence("SUSPEND"),),
    )
    assert paper_approval_for(
        (suspended,),
        strategy_key=suspended.strategy_key,
        version=suspended.version,
    ).approved is False


def test_optional_boolean_threshold_does_not_reject_stronger_evidence() -> None:
    thresholds = PromotionThresholds(
        require_data_quality_evidence=False,
        require_survivorship_evidence=False,
        require_deterministic=False,
    )

    evaluation = evaluate_thresholds(_passing_metrics(), thresholds)

    assert evaluation.passed is True


class _PromotionDb:
    def __init__(self, row: object | None) -> None:
        self.row = row

    async def scalar(self, _statement: object) -> object | None:
        return self.row


def _promotion_row(promotion: StrategyPromotion) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_key=promotion.strategy_key,
        version=promotion.version,
        state=promotion.state.value,
        metrics=promotion.metrics_snapshot(),
        metrics_hash=promotion.metrics_hash,
        threshold_evaluation=(
            promotion.threshold_evaluation.as_evidence()
            if promotion.threshold_evaluation is not None
            else None
        ),
        evidence=[item.as_evidence() for item in promotion.evidence],
        approved_at=promotion.approved_at,
        suspended_at=promotion.suspended_at,
        retired_at=promotion.retired_at,
        created_at=promotion.created_at,
        updated_at=promotion.updated_at,
    )

def _recommendation(
    owner_user_id: int,
    *,
    version: str = "1.0.0",
    duplicate_identity: bool = False,
) -> AIRecommendation:
    identity = {
        "kind": "strategy_promotion",
        "strategyKey": "qullamaggie_breakout_portfolio",
        "version": version,
    }
    evidence = [identity, dict(identity)] if duplicate_identity else [identity]
    return AIRecommendation(
        owner_user_id=owner_user_id,
        action="BUY",
        decision="PENDING",
        market="KRX",
        symbol="005930",
        rationale=[],
        risks=[],
        evidence=evidence,
        source="kasset-automation",
        created_at=_NOW,
        valid_until=_NOW + timedelta(hours=1),
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_persisted_gate_is_owner_independent_but_version_exact() -> None:
    approved = _approved("1.0.0")
    service = StrategyPromotionService(  # type: ignore[arg-type]
        _PromotionDb(_promotion_row(approved))
    )

    owner_a = await service.approval_for_recommendation(_recommendation(11))
    owner_b = await service.approval_for_recommendation(_recommendation(22))
    wrong_version = await service.approval_for_recommendation(
        _recommendation(11, version="2.0.0")
    )

    assert owner_a.approved is True
    assert owner_b.approved is True
    assert wrong_version.approved is False


@pytest.mark.asyncio
async def test_persisted_gate_rejects_duplicate_or_tampered_identity() -> None:
    approved = _approved("1.0.0")
    service = StrategyPromotionService(  # type: ignore[arg-type]
        _PromotionDb(_promotion_row(approved))
    )

    duplicate = await service.approval_for_recommendation(
        _recommendation(11, duplicate_identity=True)
    )
    tampered_row = _promotion_row(approved)
    assert isinstance(tampered_row.threshold_evaluation, dict)
    tampered_row.threshold_evaluation["passed"] = False
    malformed_identity = _recommendation(11)
    malformed_identity.evidence[0]["version"] = 1
    malformed = await service.approval_for_recommendation(malformed_identity)
    tampered = await StrategyPromotionService(  # type: ignore[arg-type]
        _PromotionDb(tampered_row)
    ).approval_for_recommendation(_recommendation(11))

    assert duplicate.approved is False
    assert duplicate.reason == "recommendation_strategy_identity_invalid"
    assert tampered.approved is False
    assert malformed.approved is False
    assert malformed.reason == "recommendation_strategy_identity_invalid"
    assert tampered.reason == "strategy_promotion_record_invalid"
