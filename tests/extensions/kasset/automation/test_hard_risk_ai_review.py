"""PAPER Hard Risk가 최종 AI 검토 근거를 사용하는 계약."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.api.paper_schemas import OrderRequest
from app.extensions.kasset.automation import job as job_module
from app.extensions.kasset.automation.decision_evidence import (
    latest_ai_review_from_evidence,
)
from app.extensions.kasset.automation.job import OwnerScopedPaperOrders
from app.extensions.kasset.automation.policy import (
    AITradingLimits,
    AITradingPolicyService,
    AITradingSnapshot,
    AITradingUsage,
    HardRiskResult,
    OperatingMode,
)

_NOW = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)


class _EmptyRiskDb:
    async def scalar(self, _statement: object) -> None:
        return None


def _ai_review(*, status: str, confidence: str) -> dict[str, object]:
    return {
        "source": "kasset_ai_review",
        "status": status,
        "action": "BUY",
        "confidence": confidence,
    }


async def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    evidence: list[dict[str, object]],
    recommendation_confidence: str,
):
    recommendation = SimpleNamespace(
        reference_price="70000",
        confidence=recommendation_confidence,
        evidence=evidence,
    )
    recommendation_service = SimpleNamespace(
        get_recommendation=AsyncMock(return_value=recommendation)
    )
    monkeypatch.setattr(
        job_module,
        "AIRecommendationService",
        lambda _db: recommendation_service,
    )

    policy = AITradingPolicyService()
    snapshot = AITradingSnapshot(
        mode=OperatingMode.AUTO_PAPER,
        limits=AITradingLimits(),
        usage=AITradingUsage(),
        kill_switch=False,
        updated_at=_NOW,
    )
    monkeypatch.setattr(
        policy,
        "get_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(job_module, "AITradingPolicyService", lambda: policy)

    request = OrderRequest(
        client_order_id="ai-rec:rec-ai-review",
        broker="PAPER",
        market="KRX",
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("1"),
    )
    return await OwnerScopedPaperOrders(now=_NOW)._hard_risk(
        _EmptyRiskDb(),  # type: ignore[arg-type]
        "101",
        request,
        reference_price="70000",
        base_reasons=(),
    )


def _ai_check(result: HardRiskResult):
    return next(check for check in result.checks if check.rule == "AI")


@pytest.mark.asyncio
async def test_agreeing_ai_review_above_floor_passes_ai_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="agrees", confidence="0.72")],
        recommendation_confidence="0.99",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is True
    assert "confidence=0.72" in ai_check.detail
    assert "aiStatus=agrees" in ai_check.detail


@pytest.mark.asyncio
async def test_disagreeing_ai_review_blocks_even_with_high_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="disagrees", confidence="0.72")],
        recommendation_confidence="0.99",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is False
    assert "aiStatus=disagrees" in ai_check.detail


@pytest.mark.asyncio
async def test_agreeing_ai_review_below_floor_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="agrees", confidence="0.40")],
        recommendation_confidence="0.99",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is False
    assert "confidence=0.40" in ai_check.detail


@pytest.mark.asyncio
async def test_missing_ai_review_evidence_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[],
        recommendation_confidence="0.99",
    )

    assert _ai_check(result).passed is False


@pytest.mark.asyncio
async def test_deterministic_position_exit_passes_ai_rule_without_ai_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Position Manager 청산은 AI evidence가 없어도 생성 시점과 같은 값(1)으로 통과한다."""

    result = await _evaluate(
        monkeypatch,
        evidence=[
            {
                "title": "Deterministic PAPER position exit",
                "source": "position_manager",
                "kind": "position_exit",
            }
        ],
        recommendation_confidence="1",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is True
    assert "confidence=1" in ai_check.detail
    assert "aiStatus=deterministic_exit" in ai_check.detail


@pytest.mark.asyncio
async def test_exit_marker_from_other_source_does_not_bypass_ai_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[{"source": "kasset_ai_review", "kind": "position_exit"}],
        recommendation_confidence="1",
    )

    assert _ai_check(result).passed is False


@pytest.mark.asyncio
async def test_recommendation_confidence_does_not_replace_ai_review_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="agrees", confidence="0.72")],
        recommendation_confidence="0.28",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is True
    assert "confidence=0.72" in ai_check.detail


def test_latest_ai_review_uses_the_last_matching_entry() -> None:
    evidence = [
        _ai_review(status="agrees", confidence="0.91"),
        {"source": "other", "confidence": "1"},
        _ai_review(status="disagrees", confidence="0.72"),
    ]
    evidence[-1]["action"] = "HOLD"

    assert latest_ai_review_from_evidence(evidence) == (
        "disagrees",
        "HOLD",
        Decimal("0.72"),
    )


def test_latest_ai_review_keeps_invalid_confidence_fail_closed() -> None:
    review = latest_ai_review_from_evidence(
        [_ai_review(status="agrees", confidence="not-a-decimal")]
    )

    assert review == ("agrees", "BUY", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", ["not-a-decimal", "NaN", "Infinity"])
async def test_invalid_or_non_finite_ai_confidence_blocks(
    monkeypatch: pytest.MonkeyPatch,
    confidence: str,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="agrees", confidence=confidence)],
        recommendation_confidence="0.99",
    )

    ai_check = _ai_check(result)
    assert ai_check.passed is False
    assert "confidence=0" in ai_check.detail
    assert "aiStatus=agrees" in ai_check.detail
