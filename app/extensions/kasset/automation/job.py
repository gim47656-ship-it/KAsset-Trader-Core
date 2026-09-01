"""Production wiring for owner-scoped PAPER recommendation automation.

The pure consumer (``PaperAutomationConsumer``) speaks the string-owner
protocol from ``contracts``; Core persistence speaks integer ``users.id``.
This module owns that translation plus the scheduler-facing entrypoint so a
TaskIQ (or any other) scheduler can run one bounded automation sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.paper_schemas import (
    OrderRequest,
    RiskAssessment,
    RiskReason,
)
from app.extensions.kasset.automation.consumer import PaperAutomationConsumer
from app.extensions.kasset.automation.contracts import (
    PROMOTION_BYPASSED_BY_OWNER,
    OwnerExecutionPolicy,
    PaperExecutionClaim,
    PaperExecutionOutcome,
)
from app.extensions.kasset.automation.policy import (
    AITradingPolicyService,
    OperatingMode,
)
from app.extensions.kasset.automation.strategy_promotion_service import (
    StrategyPromotionService,
)
from app.jobs.watch_market_data import is_market_open
from app.models.ai_recommendations import (
    AIRecommendation,
    RecommendationDecision,
    RecommendationExecutionStatus,
)
from app.services.ai_recommendations.service import AIRecommendationService
from app.services.kasset_automation_audit import record_paper_execution_event

logger = logging.getLogger(__name__)


#: 실행 원장에 남기는 출처. 무인 sweep과 사람이 누른 승인 실행을 구분한다.
AUTO_PAPER_EXECUTION_ORIGIN = "AUTO_PAPER"
APPROVAL_EXECUTION_ORIGIN = "APPROVAL"


async def _record_execution_event(
    *,
    owner_user_id: int,
    origin: str,
    outcome: PaperExecutionOutcome,
    now: datetime,
) -> None:
    """실행 시도 하나를 원장에 남긴다. 원장 실패는 주문 결과를 바꾸지 않는다.

    추천을 특정하지 못한 결과(운용 모드 차단, 후보 없음)는 남기지 않는다. 그런
    결과는 sweep마다 반복되므로 원장을 무한히 키우고, 특정 추천의 실행 이력을
    설명해 주지도 않는다.
    """

    recommendation_id = outcome.recommendation_id
    if recommendation_id is None:
        return
    try:
        await record_paper_execution_event(
            owner_user_id=owner_user_id,
            origin=origin,
            status=outcome.status,
            reason=outcome.reason,
            recommendation_id=recommendation_id,
            observed_at=now,
            replayed=outcome.replayed,
            promotion_bypass_reason=outcome.promotion_bypass_reason,
        )
    except Exception:
        logger.exception(
            "kasset paper execution audit write failed: owner_user_id=%s "
            "recommendation_id=%s origin=%s",
            owner_user_id,
            recommendation_id,
            origin,
        )


def _is_reclaimable_execution_claim(
    recommendation: AIRecommendation,
    now: datetime,
) -> bool:
    lease_expires_at = recommendation.paper_execution_lease_expires_at
    return bool(
        recommendation.paper_execution_status == RecommendationExecutionStatus.CLAIMED
        and lease_expires_at is not None
        and lease_expires_at <= now
    )


# 무인 sweep 전용 기준 시세 신선도 게이트.
#
# 정규장 중 실시간 공급자 토스가 실패하면 ``krx_quotes.quote_for_market()``은
# 저장 일봉으로 강등되고 그 종가는 전 거래일 값이다.
# 주문 경로는 ``price``만 읽고 ``source``/``asOf``를 검증하지 않으므로,
# 사람이 보지 않는 sweep은 추천 판단과 무관한 가격으로 원장에 체결을 남긴다.
# 장 마감 후에는 같은 종가가 정상 최신값이라 정규장이 열려 있을 때만 차단한다.
# 수동 경로(`POST /orders`, ``run_approved_recommendation_once``)는 사람이 화면
# 에서 값을 보고 결정하므로 지금처럼 강등된 시세를 그대로 허용한다.
STALE_QUOTE_BLOCK_REASON = "stale_quote_fallback"
STALE_QUOTE_UNRESOLVED_REASON = "stale_quote_unresolved"

# 추천 와이어 시장 → 공용 거래소 캘린더 시장 키. 자동 주문은
# ``PaperAutomationConsumer``가 KRX/US로만 만든다.
_CALENDAR_MARKET: dict[str, str] = {"KRX": "kr", "KR": "kr", "US": "us"}


async def _stale_quote_block_reason(
    db: AsyncSession,
    recommendation_id: str,
    *,
    now: datetime,
) -> str | None:
    """정규장 중 기준 시세가 저장 일봉으로 강등됐으면 차단 사유를 돌려준다."""
    recommendation = await db.get(AIRecommendation, recommendation_id)
    if recommendation is None:
        return None
    calendar_market = _CALENDAR_MARKET.get(str(recommendation.market).strip().upper())
    if calendar_market is None or not is_market_open(calendar_market, now=now):
        return None
    try:
        quote = await krx_quotes.quote_for_market(
            db,
            market=recommendation.market,
            symbol=recommendation.symbol,
        )
    except Exception as exc:
        # 기준 시세를 증명할 수 없으면 실행하지 않는다. 주문 경로도 같은 진입점을
        # 쓰므로 막지 않아도 체결은 생기지 않지만, 사유를 남겨 원인이 preview
        # 예외로 뭉개지지 않게 한다.
        return f"{STALE_QUOTE_UNRESOLVED_REASON}:{type(exc).__name__}"
    if quote.source == krx_quotes.CANDLE_QUOTE_SOURCE:
        return STALE_QUOTE_BLOCK_REASON
    return None


class RuntimeStateSafetyGate:
    """Resolve the persisted operating mode again at every execution boundary."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        automatic: bool = True,
        recommendation_id: str | None = None,
    ) -> None:
        self._db = db
        self._automatic = automatic
        self._recommendation_id = (
            recommendation_id.strip()
            if recommendation_id is not None and recommendation_id.strip()
            else None
        )

    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy:
        snapshot = await AITradingPolicyService().get_snapshot(
            self._db,
            int(owner_user_id),
            now=now,
            execution_limit=0,
        )
        required_mode = (
            OperatingMode.AUTO_PAPER if self._automatic else OperatingMode.APPROVAL
        )
        enabled = snapshot.mode == required_mode
        # override는 자동 경로에서만, 그리고 승격 근거 요구에만 적용된다. 소유자
        # 일치·kill switch·PAPER 판정은 아래에서 그대로 유지된다.
        promotion_bypassed = self._automatic and snapshot.promotion_bypass
        if self._automatic:
            enabled = enabled and settings.AI_PAPER_AUTO_EXECUTION_ENABLED
            if enabled and self._recommendation_id is not None:
                recommendation = await self._db.get(
                    AIRecommendation,
                    self._recommendation_id,
                )
                if recommendation is None or recommendation.owner_user_id != int(
                    owner_user_id
                ):
                    enabled = False
                elif not promotion_bypassed and not _is_reclaimable_execution_claim(
                    recommendation, now
                ):
                    enabled = (
                        await StrategyPromotionService(
                            self._db
                        ).approval_for_recommendation(recommendation)
                    ).approved
        return OwnerExecutionPolicy(
            owner_user_id=owner_user_id,
            paper_automation_enabled=enabled,
            global_kill_switch_enabled=snapshot.kill_switch,
            trading_mode="PAPER",
            promotion_bypassed=promotion_bypassed,
        )


class OwnerScopedRecommendationService:
    """String-owner facade over the integer-owner recommendation service."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        recommendation_id: str | None = None,
        require_promotion: bool = False,
    ) -> None:
        self._db = db
        self._service = AIRecommendationService(db)
        self._recommendation_id = recommendation_id
        self._require_promotion = require_promotion

    async def authorize_next_for_auto_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> str | None:
        owner_id = int(owner_user_id)
        base = select(AIRecommendation).where(
            AIRecommendation.owner_user_id == owner_id,
            AIRecommendation.action.in_(("BUY", "SELL")),
            AIRecommendation.source == "kasset-automation",
        )
        approved_rows = list(
            (
                await self._db.scalars(
                    base.where(
                        AIRecommendation.decision == RecommendationDecision.APPROVED,
                        or_(
                            and_(
                                AIRecommendation.paper_execution_status.is_(None),
                                AIRecommendation.valid_until > now,
                            ),
                            and_(
                                AIRecommendation.paper_execution_status
                                == RecommendationExecutionStatus.CLAIMED,
                                AIRecommendation.paper_execution_lease_expires_at
                                <= now,
                            ),
                        ),
                    )
                    .order_by(
                        AIRecommendation.decided_at,
                        AIRecommendation.created_at,
                        AIRecommendation.id,
                    )
                    .limit(100)
                )
            ).all()
        )
        pending_rows = list(
            (
                await self._db.scalars(
                    base.where(
                        AIRecommendation.decision == RecommendationDecision.PENDING,
                        AIRecommendation.paper_execution_status.is_(None),
                        AIRecommendation.valid_until > now,
                    )
                    .order_by(
                        AIRecommendation.created_at,
                        AIRecommendation.id,
                    )
                    .limit(100)
                )
            ).all()
        )
        # ``require_promotion``이 False면 소유자 override가 승격 근거 요구를 면제한
        # 상태다. 그때만 승격 없는 후보도 자동실행 대상으로 잡는다.
        promotion_service = (
            StrategyPromotionService(self._db) if self._require_promotion else None
        )
        for row in (*approved_rows, *pending_rows):
            if _is_reclaimable_execution_claim(row, now):
                self._recommendation_id = row.id
                return row.id
            if promotion_service is not None:
                approval = await promotion_service.approval_for_recommendation(row)
                if not approval.approved:
                    continue
            if row.decision == RecommendationDecision.PENDING:
                row = await self._service.decide(
                    owner_id,
                    recommendation_id=row.id,
                    decision=RecommendationDecision.APPROVED,
                )
            self._recommendation_id = row.id
            return row.id
        return None

    async def claim_for_paper_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> PaperExecutionClaim | None:
        if self._require_promotion:
            if self._recommendation_id is None:
                return None
            candidate = await self._db.get(
                AIRecommendation,
                self._recommendation_id,
            )
            if candidate is None or candidate.owner_user_id != int(owner_user_id):
                return None
            if (
                not _is_reclaimable_execution_claim(candidate, now)
                and not (
                    await StrategyPromotionService(
                        self._db
                    ).approval_for_recommendation(candidate)
                ).approved
            ):
                return None
        row = await self._service.claim_for_paper_execution(
            int(owner_user_id),
            now,
            recommendation_id=self._recommendation_id,
            automation_only=True,
        )
        if row is None:
            return None
        if (
            not row.paper_execution_token
            or row.paper_execution_claimed_at is None
            or row.paper_execution_lease_expires_at is None
            or row.paper_execution_attempt_count < 1
            or row.valid_until is None
        ):
            raise RuntimeError("claimed recommendation is missing lease metadata")
        return PaperExecutionClaim(
            id=row.id,
            owner_user_id=str(row.owner_user_id),
            paper_execution_token=row.paper_execution_token,
            paper_execution_claimed_at=row.paper_execution_claimed_at,
            paper_execution_lease_expires_at=row.paper_execution_lease_expires_at,
            paper_execution_attempt_count=row.paper_execution_attempt_count,
            decision=row.decision,
            action=row.action,
            market=row.market,
            symbol=row.symbol,
            suggested_quantity=row.suggested_quantity,
            valid_until=row.valid_until,
        )

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> None:
        await self._service.complete_paper_execution(
            int(owner_user_id),
            recommendation_id,
            claim_token,
            paper_order_id,
            now,
        )

    async def reconcile_paper_execution_completion(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> bool:
        return await self._service.reconcile_paper_execution_completion(
            int(owner_user_id),
            recommendation_id,
            claim_token,
            paper_order_id,
            now,
        )

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None:
        await self._service.fail_paper_execution(
            int(owner_user_id),
            recommendation_id,
            claim_token,
            error,
            now,
        )


class OwnerScopedPaperOrders:
    """Apply KAsset Hard Risk, then delegate only to the shared PAPER facade."""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        require_promotion: bool = False,
    ) -> None:
        self._now = (now or datetime.now(UTC)).replace(microsecond=0)
        self._require_promotion = require_promotion

    async def preview(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> RiskAssessment:
        base = await paper_orders.preview(db, int(owner_user_id), request)
        hard_risk = await self._hard_risk(
            db,
            owner_user_id,
            request,
            reference_price=base.reference_price,
            base_reasons=base.reasons,
        )
        failed = [
            RiskReason(code=check.rule, message=check.detail)
            for check in hard_risk.checks
            if not check.passed
        ]
        if not hard_risk.passed and not failed:
            failed.append(
                RiskReason(
                    code="KILL_SWITCH",
                    message=hard_risk.blocked_reason or "Hard Risk 차단",
                )
            )
        return RiskAssessment(
            decision="APPROVED" if hard_risk.passed else "REJECTED",
            reasons=failed,
            estimated_amount=base.estimated_amount,
            estimated_fee=base.estimated_fee,
            reference_price=base.reference_price,
            currency=base.currency,
        )

    async def get_by_client_order_id(
        self,
        db: AsyncSession,
        owner_user_id: str,
        client_order_id: str,
    ) -> object | None:
        return await paper_orders.get_by_client_order_id(
            db,
            int(owner_user_id),
            client_order_id,
        )

    async def reconcile(
        self,
        db: AsyncSession,
        owner_user_id: str,
        order: object,
    ) -> object:
        return await paper_orders.reconcile(
            db,
            int(owner_user_id),
            order,
        )

    async def submit(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> tuple[object, bool]:
        base = await paper_orders.preview(db, int(owner_user_id), request)
        hard_risk = await self._hard_risk(
            db,
            owner_user_id,
            request,
            reference_price=base.reference_price,
            base_reasons=base.reasons,
        )
        if not hard_risk.passed:
            raise MobileApiError(
                409,
                "HARD_RISK_REJECTED",
                "Hard Risk 재검증에서 PAPER 주문이 차단되었습니다.",
                {
                    "blockedReason": hard_risk.blocked_reason,
                    "checks": [check.as_evidence() for check in hard_risk.checks],
                },
            )
        return await paper_orders.submit(db, int(owner_user_id), request)

    async def _hard_risk(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
        *,
        reference_price: str | None,
        base_reasons: Sequence[RiskReason],
    ):
        recommendation_id = _recommendation_id_from_client_order(
            request.client_order_id
        )
        recommendation = await AIRecommendationService(db).get_recommendation(
            int(owner_user_id),
            recommendation_id,
        )
        if self._require_promotion:
            promotion = await StrategyPromotionService(db).approval_for_recommendation(
                recommendation, for_update=True
            )
            if not promotion.approved:
                raise MobileApiError(
                    409,
                    "STRATEGY_PROMOTION_REQUIRED",
                    "승인된 전략 버전의 PAPER 추천만 자동 주문할 수 있습니다.",
                    {
                        "strategyKey": promotion.strategy_key,
                        "version": promotion.version,
                        "state": (
                            promotion.state.value
                            if promotion.state is not None
                            else None
                        ),
                        "reason": promotion.reason,
                    },
                )
        try:
            price = Decimal(
                reference_price
                if reference_price is not None
                else str(recommendation.reference_price)
            )
            confidence = Decimal(str(recommendation.confidence))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("recommendation numeric evidence is invalid") from exc
        return await AITradingPolicyService().evaluate_hard_risk(
            db,
            int(owner_user_id),
            action=request.side,
            market=request.market,
            symbol=request.symbol,
            quantity=request.quantity,
            reference_price=price,
            ai_confidence=confidence,
            now=self._now,
            base_risk_reasons=base_reasons,
        )


async def _claimable_owner_ids(db: AsyncSession, now: datetime) -> list[int]:
    rows = await db.execute(
        select(AIRecommendation.owner_user_id)
        .distinct()
        .where(
            AIRecommendation.decision.in_(
                (
                    RecommendationDecision.PENDING,
                    RecommendationDecision.APPROVED,
                )
            ),
            AIRecommendation.action.in_(("BUY", "SELL")),
            or_(
                and_(
                    AIRecommendation.paper_execution_status.is_(None),
                    AIRecommendation.valid_until > now,
                ),
                and_(
                    AIRecommendation.decision == RecommendationDecision.APPROVED,
                    AIRecommendation.paper_execution_status
                    == RecommendationExecutionStatus.CLAIMED,
                    AIRecommendation.paper_execution_lease_expires_at <= now,
                ),
            ),
            AIRecommendation.source == "kasset-automation",
        )
        .order_by(AIRecommendation.owner_user_id)
    )
    return [int(owner_id) for (owner_id,) in rows.all()]


def _recommendation_id_from_client_order(client_order_id: str | None) -> str:
    value = str(client_order_id or "")
    prefix = "ai-rec:"
    if not value.startswith(prefix) or not value[len(prefix) :].strip():
        raise ValueError("AI PAPER order requires a recommendation clientOrderId")
    return value[len(prefix) :]


def _session() -> AbstractAsyncContextManager[AsyncSession]:
    return cast(
        AbstractAsyncContextManager[AsyncSession],
        cast(object, AsyncSessionLocal()),
    )


async def run_paper_automation_once(
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run one bounded automation sweep: at most one execution per owner.

    Fail-closed by default: with ``AI_PAPER_AUTO_EXECUTION_ENABLED`` false the
    sweep reports itself disabled without touching the database. One owner's
    failure never aborts the other owners' sweeps.  During a regular session a
    degraded reference quote blocks the owner before any order is built; see
    ``_stale_quote_block_reason``.
    """

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    if not settings.AI_PAPER_AUTO_EXECUTION_ENABLED:
        return {"enabled": False, "owners": 0, "outcomes": []}

    async with _session() as db:
        owner_ids = await _claimable_owner_ids(db, current)

    outcomes: list[dict[str, object]] = []
    for owner_id in owner_ids:
        try:
            async with _session() as db:
                snapshot = await AITradingPolicyService().get_snapshot(
                    db,
                    owner_id,
                    now=current,
                    execution_limit=0,
                )
                if snapshot.mode != OperatingMode.AUTO_PAPER:
                    outcome = PaperExecutionOutcome(
                        status="BLOCKED",
                        reason="auto_paper_mode_required",
                    )
                elif snapshot.kill_switch:
                    outcome = PaperExecutionOutcome(
                        status="BLOCKED",
                        reason="global_kill_switch_enabled",
                    )
                else:
                    # 여기까지 왔으면 AUTO_PAPER이고 kill switch는 꺼져 있다.
                    # override는 승격 근거 요구 하나만 면제하며, PAPER 판정과
                    # kill switch는 snapshot.promotion_bypass 계산에서 이미
                    # 반영됐다.
                    promotion_bypassed = snapshot.promotion_bypass
                    if promotion_bypassed:
                        logger.warning(
                            "kasset paper automation runs without promotion "
                            "evidence: owner_user_id=%s reason=%s",
                            owner_id,
                            PROMOTION_BYPASSED_BY_OWNER,
                        )
                    recommendation_service = OwnerScopedRecommendationService(
                        db,
                        require_promotion=not promotion_bypassed,
                    )
                    recommendation_id = (
                        await recommendation_service.authorize_next_for_auto_execution(
                            str(owner_id),
                            current,
                        )
                    )
                    # 주문을 만들기 전에 기준 시세 신선도를 검사한다. 후보가
                    # 없으면 검사할 종목도 없다.
                    stale_quote_reason = (
                        None
                        if recommendation_id is None
                        else await _stale_quote_block_reason(
                            db,
                            recommendation_id,
                            now=current,
                        )
                    )
                    if recommendation_id is None:
                        outcome = PaperExecutionOutcome(
                            status="BLOCKED",
                            reason=(
                                "no_eligible_recommendation"
                                if promotion_bypassed
                                else "strategy_promotion_required"
                            ),
                        )
                    elif stale_quote_reason is not None:
                        logger.warning(
                            "kasset paper automation blocked on a stale reference "
                            "quote: owner_user_id=%s recommendation_id=%s reason=%s",
                            owner_id,
                            recommendation_id,
                            stale_quote_reason,
                        )
                        outcome = PaperExecutionOutcome(
                            status="BLOCKED",
                            reason=stale_quote_reason,
                            recommendation_id=recommendation_id,
                        )
                    else:
                        consumer = PaperAutomationConsumer(
                            owner_user_id=str(owner_id),
                            safety_gate=RuntimeStateSafetyGate(
                                db,
                                automatic=True,
                                recommendation_id=recommendation_id,
                            ),
                            recommendation_service=recommendation_service,
                            paper_orders=OwnerScopedPaperOrders(
                                now=current,
                                require_promotion=not promotion_bypassed,
                            ),
                            db=db,
                        )
                        outcome = await consumer.run_once(now=current)
                        if promotion_bypassed:
                            # 승격 근거 없이 나간 실행임을 결과에 남긴다.
                            outcome = replace(
                                outcome,
                                promotion_bypass_reason=PROMOTION_BYPASSED_BY_OWNER,
                            )
        except Exception as exc:  # one owner's failure must not stop the sweep
            outcome = PaperExecutionOutcome(
                status="FAILED",
                reason=f"owner_sweep_failed:{type(exc).__name__}",
            )
        await _record_execution_event(
            owner_user_id=owner_id,
            origin=AUTO_PAPER_EXECUTION_ORIGIN,
            outcome=outcome,
            now=current,
        )
        outcomes.append(
            {
                "owner_user_id": owner_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "recommendation_id": outcome.recommendation_id,
                "replayed": outcome.replayed,
                "promotion_bypass_reason": outcome.promotion_bypass_reason,
            }
        )
    result = {"enabled": True, "owners": len(owner_ids), "outcomes": outcomes}
    logger.info(
        "kasset paper automation sweep done: owners=%d outcomes=%s",
        len(owner_ids),
        outcomes,
    )
    return result


async def run_approved_recommendation_once(
    owner_user_id: int,
    recommendation_id: str,
    *,
    now: datetime | None = None,
) -> PaperExecutionOutcome:
    """Synchronously execute one explicit APPROVAL decision in PAPER only."""

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    async with _session() as db:
        consumer = PaperAutomationConsumer(
            owner_user_id=str(owner_user_id),
            safety_gate=RuntimeStateSafetyGate(db, automatic=False),
            recommendation_service=OwnerScopedRecommendationService(
                db,
                recommendation_id=recommendation_id,
            ),
            paper_orders=OwnerScopedPaperOrders(now=current),
            db=db,
        )
        outcome = await consumer.run_once(now=current)
    # 실행 트랜잭션이 끝난 뒤 별도 세션으로 원장을 남긴다. 그래야 원장이 이
    # 실행의 확정 결과(시도 횟수·주문 id)를 읽고, 원장 실패가 이 반환값을
    # 바꾸지 못한다.
    await _record_execution_event(
        owner_user_id=owner_user_id,
        origin=APPROVAL_EXECUTION_ORIGIN,
        outcome=outcome,
        now=current,
    )
    return outcome


__all__ = [
    "STALE_QUOTE_BLOCK_REASON",
    "STALE_QUOTE_UNRESOLVED_REASON",
    "APPROVAL_EXECUTION_ORIGIN",
    "AUTO_PAPER_EXECUTION_ORIGIN",
    "OwnerScopedPaperOrders",
    "OwnerScopedRecommendationService",
    "RuntimeStateSafetyGate",
    "run_paper_automation_once",
    "run_approved_recommendation_once",
]
