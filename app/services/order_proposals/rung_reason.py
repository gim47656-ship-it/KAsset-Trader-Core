"""Observation-only classification for rung ``void_reason`` text.

The classifier never changes a rung state and never authorizes a second send.  It
only maps known reason shapes to the closed vocabulary used by the additive
``void_reason_group`` column; anything else remains ``unclassified``.
"""

from __future__ import annotations

from app.models.rung_reason_vocabulary import (
    RUNG_VOID_REASON_BROKER_REJECTION,
    RUNG_VOID_REASON_CANCELLED_OR_EXPIRED,
    RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT,
    RUNG_VOID_REASON_POLICY_GUARD,
    RUNG_VOID_REASON_PROVIDER_THROTTLE,
    UNCLASSIFIED_VOID_REASON_GROUP,
    project_rung_void_reason_group,
)

# 삭제된 ``app.services.brokers.kis.order_throttle``의 순수 판정 로직만 그대로
# 옮겨왔다. 값과 의미는 원본과 동일하며(EGW00201/EGW00215 게이트웨이 초당 한도,
# 그리고 "초당"+"초과" 문구 fallback), 여기서는 이미 저장된 과거 ``void_reason``
# 텍스트를 사후 분류하는 데에만 쓰인다. 재전송을 승인하지 않는다.
_THROTTLE_MSG_CODES: frozenset[str] = frozenset({"EGW00201", "EGW00215"})

_DUPLICATE_PENDING_INTENT_MARKERS: tuple[str, ...] = (
    "duplicate order intent",
    "duplicate mock mirror intent",
    "conflicting order intent already reserved",
    "order intent already reserved",
)
_DUPLICATE_INTENT_NEGATION_WORDS: tuple[str, ...] = ("no", "not", "without")

_BROKER_REJECTION_EXACT: tuple[str, ...] = (
    "broker rejected",
    "broker_rejected",
    "cancel_rejected",
    "provider rejected",
    "provider_rejected",
    "submit rejected",
    "submit_rejected",
)

_POLICY_GUARD_EXACT: tuple[str, ...] = (
    "insufficient balance",
    "insufficient_balance",
    "loss guard violation",
    "operator_denied",
    "telegram_deny",
    "loss_cut_preconditions_failed",
    "toss_auto_submission_frozen",
)

_POLICY_GUARD_MARKERS: tuple[str, ...] = (
    "authority=server_loss_guard_invalid",
    "guard_blocked:",
    "policy_guard:",
    "target_evidence_invalid:",
    "target_evidence_missing",
    "target_snapshot_mismatch:",
    "주문가능금액을 초과",
    "주문가능수량을 초과",
)

_CANCELLED_OR_EXPIRED_EXACT: tuple[str, ...] = (
    "cancelled",
    "canceled",
    "expired",
    "expiry",
    "order_expired",
)

_CANCELLED_OR_EXPIRED_PREFIXES: tuple[str, ...] = (
    "cancelled_",
    "canceled_",
    "expired_",
    "expiry_",
)


def _normalized_reason(reason: object) -> str:
    return " ".join(str(reason or "").strip().lower().split())


def _is_provider_throttle(reason: str) -> bool:
    """게이트웨이 초당 한도 거절 문구인지 판정한다.

    문서화된 코드(``EGW00201``/``EGW00215``)가 문자열에 포함되면 우선 인정하고,
    없으면 원본과 동일하게 "초당"과 "초과"가 함께 있는 메시지만 throttle로 본다
    (주문가능금액 초과 같은 무관한 "초과"를 오분류하지 않기 위함).
    """
    upper_reason = reason.upper()
    if any(code in upper_reason for code in _THROTTLE_MSG_CODES):
        return True
    return "초당" in reason and "초과" in reason


def _has_unnegated_duplicate_intent_marker(reason: str) -> bool:
    for marker in _DUPLICATE_PENDING_INTENT_MARKERS:
        offset = 0
        while (start := reason.find(marker, offset)) >= 0:
            prefix = reason[:start].rstrip()
            if not any(
                prefix == negation or prefix.endswith(f" {negation}")
                for negation in _DUPLICATE_INTENT_NEGATION_WORDS
            ):
                return True
            offset = start + len(marker)
    return False


def classify_rung_void_reason(reason: object) -> str:
    """Classify known rung reason text without guessing at unknown text."""
    normalized = _normalized_reason(reason)

    # 과거 행에 남은 게이트웨이 throttle 문구를 사후 분류할 뿐, 재전송 판단이
    # 아니다.
    if _is_provider_throttle(normalized):
        return RUNG_VOID_REASON_PROVIDER_THROTTLE

    if _has_unnegated_duplicate_intent_marker(normalized):
        return RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT

    if (
        normalized in _CANCELLED_OR_EXPIRED_EXACT
        or normalized.startswith(_CANCELLED_OR_EXPIRED_PREFIXES)
        or "authority=server_expired" in normalized
    ):
        return RUNG_VOID_REASON_CANCELLED_OR_EXPIRED

    if normalized in _POLICY_GUARD_EXACT or any(
        marker in normalized for marker in _POLICY_GUARD_MARKERS
    ):
        return RUNG_VOID_REASON_POLICY_GUARD

    if normalized in _BROKER_REJECTION_EXACT or normalized.startswith(
        ("broker_rejected:", "provider_rejected:", "submit_rejected:")
    ):
        return RUNG_VOID_REASON_BROKER_REJECTION

    return UNCLASSIFIED_VOID_REASON_GROUP


__all__ = [
    "classify_rung_void_reason",
    "project_rung_void_reason_group",
]
