"""PAPER Hard Risk에서 AI 검토가 SHADOW로만 기록되는 계약.

AI와 뉴스·공시는 검증되지 않은 입력을 보므로 실제 주문 veto를 갖지 않는다.
Hard Risk 차단은 kill switch, 손실·포지션·주문 한도, 현금, 거래시간 같은
결정론적 안전장치만 담당한다.
"""

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
    kill_switch: bool = False,
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
        kill_switch=kill_switch,
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


def _ai_shadow_check(result: HardRiskResult):
    return next(check for check in result.checks if check.rule == "AI_SHADOW")


def _rules(result: HardRiskResult) -> set[str]:
    return {check.rule for check in result.checks}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "confidence"),
    [
        ("agrees", "0.72"),
        ("disagrees", "0.72"),
        ("agrees", "0.40"),
        ("disagrees", "0.05"),
        ("agrees", "not-a-decimal"),
        ("agrees", "NaN"),
        ("agrees", "Infinity"),
    ],
)
async def test_ai_review_never_blocks_a_paper_order(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    confidence: str,
) -> None:
    """동의·반대·저확신·파싱불가 모두 주문을 차단하지 않는다."""
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status=status, confidence=confidence)],
        recommendation_confidence="0.99",
    )
    shadow = _ai_shadow_check(result)
    assert shadow.passed is True
    assert f"aiStatus={status}" in shadow.detail
    assert "shadow" in shadow.detail
    assert result.passed is True


@pytest.mark.asyncio
async def test_missing_ai_review_evidence_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[],
        recommendation_confidence="0.99",
    )
    assert _ai_shadow_check(result).passed is True
    assert result.passed is True


@pytest.mark.asyncio
async def test_no_hard_risk_rule_is_named_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI가 관문 이름으로 남아 주문을 거절하는 경로가 없어야 한다."""
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="disagrees", confidence="0.90")],
        recommendation_confidence="0.99",
    )
    assert "AI" not in _rules(result)
    assert "AI_SHADOW" in _rules(result)


@pytest.mark.asyncio
async def test_kill_switch_still_blocks_regardless_of_ai_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _evaluate(
        monkeypatch,
        evidence=[_ai_review(status="agrees", confidence="0.99")],
        recommendation_confidence="0.99",
        kill_switch=True,
    )
    assert result.passed is False
    assert result.blocked_reason == "kill switch가 켜져 있습니다."


@pytest.mark.asyncio
async def test_deterministic_position_exit_records_deterministic_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    shadow = _ai_shadow_check(result)
    assert shadow.passed is True
    assert "aiStatus=deterministic_exit" in shadow.detail


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
