from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PROMOTION_THRESHOLDS,
    PromotionMetrics,
    PromotionThresholds,
    evaluate_thresholds,
)


def _metrics(**overrides: object) -> PromotionMetrics:
    payload: dict[str, object] = {
        "total_return": Decimal("0.30"),
        "max_drawdown": Decimal("0.10"),
        "win_rate": Decimal("0.50"),
        "expectancy": Decimal("1"),
        "excess_return": Decimal("0.10"),
        "gross_profit": Decimal("300"),
        "gross_loss": Decimal("100"),
        "cost_stressed_total_return": Decimal("0.05"),
        "total_costs": Decimal("12"),
        "trade_count": 40,
        "walk_forward_folds": 3,
        "walk_forward_passed_folds": 3,
        "data_quality_evidence": True,
        "survivorship_evidence": True,
        "deterministic": True,
        "backtest_hashes": ("a" * 64,),
    }
    payload.update(overrides)
    return PromotionMetrics(**payload)  # type: ignore[arg-type]


def test_promotion_checks_mdd_profit_factor_and_net_of_cost_return() -> None:
    evaluation = evaluate_thresholds(_metrics())

    metrics = [check.metric for check in evaluation.checks]
    assert "max_drawdown" in metrics
    assert "profit_factor" in metrics
    assert "cost_stressed_total_return" in metrics
    assert evaluation.passed is True


def test_existing_minimum_trade_and_fold_gates_are_preserved() -> None:
    assert DEFAULT_PROMOTION_THRESHOLDS.min_trade_count == 30
    assert DEFAULT_PROMOTION_THRESHOLDS.min_walk_forward_folds == 3
    assert DEFAULT_PROMOTION_THRESHOLDS.min_walk_forward_pass_rate == Decimal("0.67")

    evaluation = evaluate_thresholds(
        _metrics(trade_count=29, walk_forward_folds=2, walk_forward_passed_folds=2)
    )

    assert evaluation.passed is False
    assert {"trade_count", "walk_forward_folds"} <= set(evaluation.failed_metrics)


def test_weak_profit_factor_blocks_promotion() -> None:
    evaluation = evaluate_thresholds(
        _metrics(gross_profit=Decimal("100"), gross_loss=Decimal("100"))
    )

    assert evaluation.passed is False
    assert "profit_factor" in evaluation.failed_metrics
    check = next(item for item in evaluation.checks if item.metric == "profit_factor")
    assert check.observed == "1"
    assert check.required == "1.2"


def test_lossless_history_passes_without_inventing_a_ratio() -> None:
    evaluation = evaluate_thresholds(_metrics(gross_loss=Decimal("0")))

    check = next(item for item in evaluation.checks if item.metric == "profit_factor")
    assert check.passed is True
    assert check.observed == "lossless"


def test_no_profit_and_no_loss_cannot_pass_profit_factor() -> None:
    evaluation = evaluate_thresholds(
        _metrics(gross_profit=Decimal("0"), gross_loss=Decimal("0"))
    )

    check = next(item for item in evaluation.checks if item.metric == "profit_factor")
    assert check.passed is False
    assert check.observed == "undefined"


def test_negative_return_under_cost_stress_blocks_promotion() -> None:
    evaluation = evaluate_thresholds(
        _metrics(cost_stressed_total_return=Decimal("-0.02"))
    )

    assert evaluation.passed is False
    assert "cost_stressed_total_return" in evaluation.failed_metrics


def test_excess_drawdown_still_blocks_promotion() -> None:
    evaluation = evaluate_thresholds(_metrics(max_drawdown=Decimal("0.35")))

    assert evaluation.passed is False
    assert "max_drawdown" in evaluation.failed_metrics


def test_snapshot_exposes_the_new_cost_metrics() -> None:
    snapshot = _metrics().as_snapshot()

    assert snapshot["profitFactor"] == "3"
    assert snapshot["grossProfit"] == "300"
    assert snapshot["grossLoss"] == "100"
    assert snapshot["costStressedTotalReturn"] == "0.05"
    assert snapshot["totalCosts"] == "12"


def test_negative_cost_components_are_rejected() -> None:
    with pytest.raises(ValueError, match="gross_loss must be non-negative"):
        _metrics(gross_loss=Decimal("-1"))


def test_thresholds_reject_a_negative_profit_factor_floor() -> None:
    with pytest.raises(ValueError, match="min_profit_factor"):
        PromotionThresholds(min_profit_factor=Decimal("-1"))
