"""기술 추천 판정의 보조 근거: AI 검토, 뉴스/공시 shadow, 비교 코호트.

이 모듈의 값은 기술 BUY/SELL 추천 판정을 바꾸지 않는다. 다만 PAPER 집행
직전 Hard Risk는 저장된 최종 AI 검토의 상태와 확신도를 fail-closed 관문으로
재사용한다. 뉴스와 비교 코호트는 계속 관측·사후분석 용도로만 기록한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from app.extensions.kasset.automation.contracts import Action

DECISION_COHORT_SCHEMA_VERSION: Final = "kasset.decision-cohorts.v1"
NEWS_SHADOW_SCHEMA_VERSION: Final = "kasset.news-shadow.v1"
AI_REVIEW_SCHEMA_VERSION: Final = "kasset.ai-review.v1"

#: 비교 코호트 이름. 오프라인 분석이 이 문자열로 조인한다.
COHORT_TECHNICAL_ONLY: Final = "technical_only"
COHORT_TECHNICAL_AI: Final = "technical_ai"
COHORT_TECHNICAL_AI_NEWS: Final = "technical_ai_news"

#: 실제로 주문 후보를 결정한 코호트.
LIVE_COHORT: Final = COHORT_TECHNICAL_ONLY

#: technical+AI 코호트가 "AI도 관문이었다면"을 재현할 때 쓰는 신뢰도 하한.
#: 예전 구현이 실제로 썼던 값이라 코호트 비교가 과거와 이어진다.
AI_COHORT_CONFIDENCE_FLOOR: Final = Decimal("0.50")


class AiReviewStatus(StrEnum):
    """AI 검토 관측 상태. 어떤 값도 기술 판정을 바꾸지 않는다."""

    AGREES = "agrees"
    DISAGREES = "disagrees"
    LOW_CONFIDENCE = "low_confidence"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"


class NewsShadowStatus(StrEnum):
    """뉴스/공시 shadow 상태.

    ``UNKNOWN``은 "없었다"가 아니라 "없었다고 말할 근거가 없다"는 뜻이다.
    수집 경로 건강이 입증되지 않으면 항상 이 값이다.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AiReviewEvidence:
    """AI 검토 한 건의 관측 사실."""

    status: AiReviewStatus
    action: str | None
    confidence: str | None
    provider: str | None
    tier: str | None
    model_id: str | None
    rationale_tags: tuple[str, ...]
    observed_at: str | None
    failure_reason: str | None
    detail: str

    @property
    def agrees(self) -> bool:
        return self.status is AiReviewStatus.AGREES

    def as_evidence(self) -> dict[str, object]:
        return {
            "title": "AI candidate review (advisory for admission; gates PAPER execution)",
            "source": "kasset_ai_review",
            "kind": "ai_review",
            "schemaVersion": AI_REVIEW_SCHEMA_VERSION,
            "gating": False,
            "status": self.status.value,
            "action": self.action,
            "confidence": self.confidence,
            "provider": self.provider,
            "tier": self.tier,
            "modelId": self.model_id,
            "rationaleTags": list(self.rationale_tags),
            "observedAt": self.observed_at,
            "failureReason": self.failure_reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class NewsShadowEvidence:
    """한 종목의 뉴스/공시 shadow 관측."""

    status: NewsShadowStatus
    source_health_proven: bool
    news_count: int
    disclosure_count: int
    items: tuple[Mapping[str, object], ...]
    observed_at: datetime
    detail: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "title": "News and disclosure shadow observation",
            "source": "kasset_news_shadow",
            "kind": "news_shadow",
            "schemaVersion": NEWS_SHADOW_SCHEMA_VERSION,
            "gating": False,
            "status": self.status.value,
            "sourceHealthProven": self.source_health_proven,
            "newsCount": self.news_count,
            "disclosureCount": self.disclosure_count,
            "observedAt": _timestamp_text(self.observed_at),
            "detail": self.detail,
            "items": [dict(item) for item in self.items],
        }


def build_news_shadow(
    *,
    items: Sequence[Mapping[str, object]],
    news_count: int,
    disclosure_count: int,
    source_health_proven: bool,
    observed_at: datetime,
    detail: str,
) -> NewsShadowEvidence:
    """수집 결과와 경로 건강만으로 shadow 상태를 정한다.

    건강이 입증되지 않았는데 항목이 없다고 ``NOT_FOUND``라고 말하지 않는다.
    """

    if news_count + disclosure_count > 0:
        status = NewsShadowStatus.FOUND
    elif source_health_proven:
        status = NewsShadowStatus.NOT_FOUND
    else:
        status = NewsShadowStatus.UNKNOWN
    return NewsShadowEvidence(
        status=status,
        source_health_proven=source_health_proven,
        news_count=news_count,
        disclosure_count=disclosure_count,
        items=tuple(items),
        observed_at=_aware_utc(observed_at, "observed_at"),
        detail=detail,
    )


def unknown_news_shadow(
    *,
    observed_at: datetime,
    detail: str,
) -> NewsShadowEvidence:
    """수집 자체가 실패했을 때의 shadow. 항상 ``UNKNOWN``이다."""

    return NewsShadowEvidence(
        status=NewsShadowStatus.UNKNOWN,
        source_health_proven=False,
        news_count=0,
        disclosure_count=0,
        items=(),
        observed_at=_aware_utc(observed_at, "observed_at"),
        detail=detail,
    )


def build_decision_cohorts(
    *,
    action: Action,
    technical_admitted: bool,
    technical_reason: str | None,
    ai_review: AiReviewEvidence,
    news_shadow: NewsShadowEvidence,
) -> dict[str, object]:
    """같은 결정에 세 코호트를 나란히 기록한다.

    ``technical_only``만 실제 결정이다. 나머지 둘은 "그 관문을 썼다면"의
    반사실이며 어떤 것도 지금 결정을 바꾸지 않는다.
    """

    ai_blocked = None if ai_review.agrees else f"ai_{ai_review.status.value}"
    news_blocked = (
        None
        if news_shadow.status is not NewsShadowStatus.UNKNOWN
        else "news_source_health_unproven"
    )
    technical_ai_admitted = technical_admitted and ai_blocked is None
    cohorts = [
        {
            "name": COHORT_TECHNICAL_ONLY,
            "live": True,
            "admitted": technical_admitted,
            "action": action.value,
            "blockedReason": technical_reason,
            "gates": ["daily_setup", "intraday_triggers", "hard_risk"],
        },
        {
            "name": COHORT_TECHNICAL_AI,
            "live": False,
            "admitted": technical_ai_admitted,
            "action": action.value,
            "blockedReason": technical_reason or ai_blocked,
            "gates": [
                "daily_setup",
                "intraday_triggers",
                "hard_risk",
                "ai_agreement",
            ],
            "aiConfidenceFloor": str(AI_COHORT_CONFIDENCE_FLOOR),
        },
        {
            "name": COHORT_TECHNICAL_AI_NEWS,
            "live": False,
            "admitted": technical_ai_admitted and news_blocked is None,
            "action": action.value,
            "blockedReason": technical_reason or ai_blocked or news_blocked,
            "gates": [
                "daily_setup",
                "intraday_triggers",
                "hard_risk",
                "ai_agreement",
                "news_source_health",
            ],
        },
    ]
    return {
        "title": "Decision cohort comparison",
        "source": "kasset_decision_cohorts",
        "kind": "decision_cohorts",
        "schemaVersion": DECISION_COHORT_SCHEMA_VERSION,
        "gating": False,
        "liveCohort": LIVE_COHORT,
        "aiStatus": ai_review.status.value,
        "newsStatus": news_shadow.status.value,
        "cohorts": cohorts,
    }


def ai_review_from_observation(
    *,
    status: AiReviewStatus,
    observation: object | None = None,
    failure_reason: str | None = None,
    detail: str = "",
) -> AiReviewEvidence:
    """``AiShadowObservation``이 있으면 그 사실만 옮겨 담는다."""

    return AiReviewEvidence(
        status=status,
        action=_optional_text(getattr(observation, "action", None)),
        confidence=_optional_text(getattr(observation, "confidence", None)),
        provider=_optional_text(getattr(observation, "provider", None)),
        tier=_optional_text(getattr(observation, "tier", None)),
        model_id=_optional_text(getattr(observation, "model_id", None)),
        rationale_tags=tuple(
            str(item) for item in (getattr(observation, "rationale_tags", None) or ())
        ),
        observed_at=_optional_text(getattr(observation, "observed_at", None)),
        failure_reason=failure_reason,
        detail=detail,
    )


def latest_ai_review_from_evidence(
    evidence: object,
) -> tuple[str | None, str | None, Decimal | None] | None:
    """추천 evidence에서 마지막 AI 검토의 상태·행동·확신도를 읽는다.

    마지막 항목이 최종 tier 결과다. 그 항목의 확신도를 해석할 수 없으면 이전
    항목으로 되돌아가지 않고 확신도 자리에 ``None``을 반환한다.
    """

    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return None
    review = next(
        (
            item
            for item in reversed(evidence)
            if isinstance(item, Mapping) and item.get("source") == "kasset_ai_review"
        ),
        None,
    )
    if review is None:
        return None

    confidence = None
    confidence_text = _optional_text(review.get("confidence"))
    if confidence_text is not None:
        try:
            confidence = Decimal(confidence_text)
        except (InvalidOperation, ValueError):
            pass
    return (
        _optional_text(review.get("status")),
        _optional_text(review.get("action")),
        confidence,
    )


def is_deterministic_position_exit(evidence: object) -> bool:
    """Position Manager가 만든 결정론 청산 추천인지 판별한다.

    청산 추천은 AI 검토 없이 ``position_manager``/``position_exit`` evidence만
    갖고 생성 시점 Hard Risk를 ``ai_confidence=1``로 통과했다. 집행 시 AI 규칙을
    같은 값으로 재현해야 청산이 AI evidence 부재로 막히지 않는다.
    """

    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("source") == "position_manager"
        and item.get("kind") == "position_exit"
        for item in evidence
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "AI_COHORT_CONFIDENCE_FLOOR",
    "AI_REVIEW_SCHEMA_VERSION",
    "COHORT_TECHNICAL_AI",
    "COHORT_TECHNICAL_AI_NEWS",
    "COHORT_TECHNICAL_ONLY",
    "DECISION_COHORT_SCHEMA_VERSION",
    "LIVE_COHORT",
    "NEWS_SHADOW_SCHEMA_VERSION",
    "AiReviewEvidence",
    "AiReviewStatus",
    "NewsShadowEvidence",
    "NewsShadowStatus",
    "ai_review_from_observation",
    "latest_ai_review_from_evidence",
    "is_deterministic_position_exit",
    "build_decision_cohorts",
    "build_news_shadow",
    "unknown_news_shadow",
]
