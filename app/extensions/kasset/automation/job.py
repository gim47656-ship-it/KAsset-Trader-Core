"""Production wiring for owner-scoped PAPER recommendation automation.

The pure consumer (``PaperAutomationConsumer``) speaks the string-owner
protocol from ``contracts``; Core persistence speaks integer ``users.id``.
This module owns that translation plus the scheduler-facing entrypoint so a
TaskIQ (or any other) scheduler can run one bounded automation sweep.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import cast

from sqlalchemy import and_, or_, select, text
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
from app.extensions.kasset.automation.account_state_gate import (
    AccountStateEvaluation,
    AccountStateGate,
)
from app.extensions.kasset.automation.consumer import PaperAutomationConsumer
from app.extensions.kasset.automation.contracts import (
    PROMOTION_BYPASSED_BY_OWNER,
    OwnerExecutionPolicy,
    PaperExecutionClaim,
    PaperExecutionOutcome,
)
from app.extensions.kasset.automation.decision_evidence import (
    is_deterministic_position_exit,
    latest_ai_review_from_evidence,
)
from app.extensions.kasset.automation.policy import (
    AITradingPolicyService,
    HardRiskResult,
    OperatingMode,
)
from app.extensions.kasset.automation.position_sizing import PositionSizingConfig
from app.extensions.kasset.automation.realtime_tape import requires_realtime_entry
from app.extensions.kasset.automation.strategy_promotion_service import (
    StrategyPromotionService,
)
from app.extensions.kasset.fcm_push_service import dispatch_order_execution_pushes
from app.extensions.kasset.nhplug.tape_store import RealtimeEntryGate, RedisTapeStore
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
#: 두 sweep(5분 전체 시장, 1분 KRX 전용)이 같은 owner를 동시에 집행하지 않게
#: 잡는 세션 advisory lock namespace. 키는 owner id다.
_OWNER_EXECUTION_LOCK_NAMESPACE = 1_263_498_068
#: 제출 직전 실시간 재확인 실패 코드.
REALTIME_ENTRY_NOT_CONFIRMED = "REALTIME_ENTRY_NOT_CONFIRMED"


@asynccontextmanager
async def realtime_entry_gate() -> AsyncIterator[RealtimeEntryGate]:
    """실행 경로용 실시간 관문. Redis 연결은 첫 조회 때만 열린다."""

    store = RedisTapeStore.from_settings()
    try:
        yield RealtimeEntryGate(store)
    finally:
        with contextlib.suppress(Exception):
            await store.aclose()


@asynccontextmanager
async def _owner_execution_lock(owner_user_id: int) -> AsyncIterator[bool]:
    """owner 하나의 집행을 프로세스·sweep 사이에서 직렬화한다."""

    async with _session() as session:
        acquired = bool(
            await session.scalar(
                text("SELECT pg_try_advisory_lock(:namespace, :key)"),
                {"namespace": _OWNER_EXECUTION_LOCK_NAMESPACE, "key": owner_user_id},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await session.scalar(
                        text("SELECT pg_advisory_unlock(:namespace, :key)"),
                        {
                            "namespace": _OWNER_EXECUTION_LOCK_NAMESPACE,
                            "key": owner_user_id,
                        },
                    )
                except Exception:
                    # 연결이 닫히면 세션 advisory lock도 풀린다.
                    logger.exception(
                        "kasset owner execution unlock failed: owner_user_id=%s",
                        owner_user_id,
                    )


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


async def _dispatch_auto_paper_execution_push(
    *,
    owner_user_id: int,
    outcome: PaperExecutionOutcome,
    now: datetime,
) -> None:
    """성공 집행과 원장 기록이 끝난 뒤에만 소유자 기기로 알린다."""

    if outcome.status != "SUBMITTED" or outcome.recommendation_id is None:
        return
    try:
        async with _session() as db:
            order_id = await db.scalar(
                select(AIRecommendation.paper_order_id).where(
                    AIRecommendation.id == outcome.recommendation_id,
                    AIRecommendation.owner_user_id == owner_user_id,
                )
            )
            if order_id is None:
                logger.warning(
                    "kasset auto PAPER execution push skipped without order id: "
                    "owner_user_id=%s recommendation_id=%s",
                    owner_user_id,
                    outcome.recommendation_id,
                )
                return
            await dispatch_order_execution_pushes(
                db,
                owner_user_id=owner_user_id,
                order_id=order_id,
                now=now,
            )
    except Exception:
        logger.warning(
            "kasset auto PAPER execution push failed: owner_user_id=%s "
            "recommendation_id=%s",
            owner_user_id,
            outcome.recommendation_id,
            exc_info=True,
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

# 무인 sweep은 해당 시장의 정규장 안에서만 주문을 만든다. PAPER 체결 시뮬레이터는
# 마지막 시세로 즉시 채워 주므로, 장이 닫힌 시각에 집행하면 실제 시장에서 성립할 수
# 없는 체결이 원장에 남고 전략 성과가 왜곡된다. 사람이 화면을 보고 결정하는 수동
# 경로(`POST /orders`, ``run_approved_recommendation_once``)는 이 관문을 쓰지 않는다.
OUT_OF_SESSION_BLOCK_REASON = "out_of_regular_session"
OUT_OF_SESSION_UNKNOWN_MARKET_REASON = "unsupported_session_market"

# 추천 와이어 시장 → 공용 거래소 캘린더 시장 키. 자동 주문은
# ``PaperAutomationConsumer``가 KRX/US로만 만든다.
_CALENDAR_MARKET: dict[str, str] = {"KRX": "kr", "KR": "kr", "US": "us"}


def _market_out_of_session_reason(market: object, *, now: datetime) -> str | None:
    """해당 시장의 정규장이 닫혀 있으면 무인 집행 차단 사유를 돌려준다."""
    calendar_market = _CALENDAR_MARKET.get(str(market).strip().upper())
    if calendar_market is None:
        # 시장을 캘린더로 증명할 수 없으면 집행하지 않는다.
        return OUT_OF_SESSION_UNKNOWN_MARKET_REASON
    if is_market_open(calendar_market, now=now):
        return None
    return OUT_OF_SESSION_BLOCK_REASON


async def _out_of_session_block_reason(
    db: AsyncSession,
    recommendation_id: str,
    *,
    now: datetime,
) -> str | None:
    """정규장 밖이면 무인 집행 차단 사유를 돌려준다."""
    recommendation = await db.get(AIRecommendation, recommendation_id)
    if recommendation is None:
        return None
    return _market_out_of_session_reason(recommendation.market, now=now)


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
        markets: Collection[str] | None = None,
        realtime_gate: RealtimeEntryGate | None = None,
    ) -> None:
        self._db = db
        self._service = AIRecommendationService(db)
        self._recommendation_id = recommendation_id
        self._require_promotion = require_promotion
        self._markets = (
            None
            if markets is None
            else frozenset(market.strip().upper() for market in markets)
        )
        # 없으면 실시간 관문을 요구하는 BUY는 준비되지 않은 것으로 본다.
        self._realtime_gate = realtime_gate

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
        if self._markets is not None:
            base = base.where(AIRecommendation.market.in_(sorted(self._markets)))
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
        # 소유자당 한 tick에 한 건만 집행하므로 "무엇을 먼저 보는가"가 곧
        # 보호 청산의 도달 가능성이다. 후보를 걸러내지 않고 순서만 정한다.
        # 관문(정규장/시세 신선도/Hard Risk)은 그대로 최종 판정에 남는다.
        #
        # 1. 지금 정규장이 열린 시장을 먼저 본다. KR 장중에 US BUY가 그 tick의
        #    슬롯을 차지하면 어차피 정규장 관문에서 막히면서 KR 손절만 굶는다.
        # 2. 유효기간이 끝난 CLAIMED 재수습을 그다음에 본다. claim/lease를
        #    흘리지 않는 기존 복구 우선순위를 유지한다.
        # 3. Position Manager 보호 청산을 새 진입보다 먼저 본다. 시간순으로만
        #    고르면 더 오래된 BUY가 계속 앞을 막아 손절이 다음 tick으로 밀린다.
        #
        # 안정 정렬이므로 같은 등급 안의 기존 순서(승인 우선, 그다음 시간순)는
        # 그대로다. 후보가 하나뿐이면 순서도 결과도 이전과 동일하다.
        candidates = sorted(
            (*approved_rows, *pending_rows),
            key=lambda item: (
                _market_out_of_session_reason(item.market, now=now) is not None,
                not _is_reclaimable_execution_claim(item, now),
                not is_deterministic_position_exit(item.evidence),
            ),
        )
        for row in candidates:
            if _is_reclaimable_execution_claim(row, now):
                self._recommendation_id = row.id
                return row.id
            if promotion_service is not None:
                approval = await promotion_service.approval_for_recommendation(row)
                if not approval.approved:
                    continue
            if requires_realtime_entry(
                source=row.source, market=row.market, action=row.action
            ):
                # 관찰이 준비되지 않은 실시간 BUY가 이 tick의 슬롯을 차지하지
                # 않게 건너뛴다. 추천은 그대로 남아 다음 tick에 다시 본다.
                check = (
                    await self._realtime_gate.check(str(row.symbol))
                    if self._realtime_gate is not None
                    else None
                )
                if check is None or not check.ready:
                    logger.info(
                        "kasset realtime entry not ready; skipping BUY: "
                        "owner_user_id=%s recommendation_id=%s symbol=%s reasons=%s",
                        owner_id,
                        row.id,
                        row.symbol,
                        list(check.reasons) if check is not None else ["gate_absent"],
                    )
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
        realtime_gate: RealtimeEntryGate | None = None,
    ) -> None:
        self._now = (now or datetime.now(UTC)).replace(microsecond=0)
        self._require_promotion = require_promotion
        self._realtime_gate = realtime_gate

    async def preview(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> RiskAssessment:
        request, base, hard_risk = await self._assess(db, owner_user_id, request)
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
        request, base, hard_risk = await self._assess(db, owner_user_id, request)
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
        await self._confirm_realtime_entry(db, owner_user_id, request)
        return await paper_orders.submit(db, int(owner_user_id), request)

    async def _assess(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> tuple[OrderRequest, RiskAssessment, HardRiskResult]:
        """Hard Risk를 적용하고, BUY가 BUDGET만 넘으면 한도 안 수량으로 줄인다.

        추천 수량은 추천 시점 가격으로 종목 비중을 거의 채운다. 제출 시점 시세가
        조금 오르면 BUDGET만 넘으므로 그 순간 가격으로 들어가는 최대 lot으로
        줄여 다시 판정한다. 다른 관문이 실패하거나 1 lot도 안 들어가면 원래
        수량의 판정을 그대로 돌려준다. 수량을 늘리지는 않는다.
        """

        base = await paper_orders.preview(db, int(owner_user_id), request)
        hard_risk = await self._hard_risk(
            db,
            owner_user_id,
            request,
            reference_price=base.reference_price,
            base_reasons=base.reasons,
        )
        fitted = _budget_fitted_quantity(request, base, hard_risk)
        if fitted is None:
            return request, base, hard_risk
        fitted_request = request.model_copy(update={"quantity": fitted})
        fitted_base = await paper_orders.preview(db, int(owner_user_id), fitted_request)
        fitted_risk = await self._hard_risk(
            db,
            owner_user_id,
            fitted_request,
            reference_price=fitted_base.reference_price,
            base_reasons=fitted_base.reasons,
        )
        if not fitted_risk.passed:
            return request, base, hard_risk
        logger.info(
            "kasset BUY quantity fitted to budget: owner_user_id=%s "
            "client_order_id=%s symbol=%s requested=%s fitted=%s price=%s",
            owner_user_id,
            request.client_order_id,
            request.symbol,
            request.quantity,
            fitted,
            fitted_base.reference_price,
        )
        return fitted_request, fitted_base, fitted_risk

    async def _confirm_realtime_entry(
        self,
        db: AsyncSession,
        owner_user_id: str,
        request: OrderRequest,
    ) -> None:
        """실시간 관문을 요구한 BUY는 제출 바로 앞에서 관찰을 다시 판정한다."""

        if request.side != "BUY":
            return
        recommendation = await AIRecommendationService(db).get_recommendation(
            int(owner_user_id),
            _recommendation_id_from_client_order(request.client_order_id),
        )
        if not requires_realtime_entry(
            source=recommendation.source,
            market=recommendation.market,
            action=recommendation.action,
        ):
            return
        check = (
            await self._realtime_gate.check(request.symbol)
            if self._realtime_gate is not None
            else None
        )
        if check is not None and check.ready:
            return
        raise MobileApiError(
            409,
            REALTIME_ENTRY_NOT_CONFIRMED,
            "제출 직전 실시간 체결·호가 재확인에서 PAPER 매수가 차단되었습니다.",
            {
                "reasons": (
                    list(check.reasons)
                    if check is not None
                    else ["realtime_gate_unavailable"]
                ),
                "realtime": dict(check.evidence) if check is not None else None,
            },
        )

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
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("recommendation numeric evidence is invalid") from exc
        evidence = recommendation.evidence
        if is_deterministic_position_exit(evidence):
            # Position Manager 청산은 AI 검토 없이 생성 시점에 ai_confidence=1로
            # Hard Risk를 통과한 결정론 경로다. 집행에서도 같은 값을 재현한다.
            ai_review_status: str | None = "deterministic_exit"
            confidence = Decimal("1")
        else:
            # AI는 주문을 차단하지 않는다. SHADOW 기록에는 마지막 검토의 실제
            # confidence를 넘기고, 부재·파싱 실패·비유한 값만 0으로 남긴다.
            ai_review = latest_ai_review_from_evidence(evidence)
            ai_review_confidence: Decimal | None = None
            if ai_review is None:
                ai_review_status = None
            else:
                ai_review_status, _, ai_review_confidence = ai_review
            confidence = (
                ai_review_confidence
                if ai_review_confidence is not None and ai_review_confidence.is_finite()
                else Decimal("0")
            )
        policy = AITradingPolicyService()
        account_state: AccountStateEvaluation | None = None
        risk_snapshot = None
        try:
            risk_snapshot = await policy.get_snapshot(
                db,
                int(owner_user_id),
                now=self._now,
                execution_limit=0,
            )
            account_state_snapshot = await AccountStateGate().evaluate_owner(
                db,
                int(owner_user_id),
                markets=(request.market,),
                daily_target_rate_pct=risk_snapshot.limits.daily_target_rate_pct,
                max_daily_loss_rate_pct=risk_snapshot.limits.max_daily_loss_rate_pct,
                now=self._now,
            )
            account_state = account_state_snapshot.for_market(request.market)
        except Exception:  # noqa: BLE001 - 신규 집행 관문 계산 불가는 fail-open
            logger.warning(
                "kasset execution ACCOUNT_STATE unavailable; gate passes: "
                "owner_user_id=%s market=%s symbol=%s",
                owner_user_id,
                request.market,
                request.symbol,
                exc_info=True,
            )
        return await policy.evaluate_hard_risk(
            db,
            int(owner_user_id),
            action=request.side,
            market=request.market,
            symbol=request.symbol,
            quantity=request.quantity,
            reference_price=price,
            ai_confidence=confidence,
            ai_review_status=ai_review_status,
            now=self._now,
            base_risk_reasons=base_reasons,
            account_state=account_state,
            snapshot=risk_snapshot,
        )


def _budget_fitted_quantity(
    request: OrderRequest,
    base: RiskAssessment,
    hard_risk: HardRiskResult,
) -> Decimal | None:
    """BUDGET 하나만 실패한 BUY의 한도 안 최대 수량. 줄일 수 없으면 ``None``."""

    if hard_risk.passed or request.side != "BUY":
        return None
    if request.market not in {"KRX", "US"}:
        return None
    failed = {check.rule for check in hard_risk.checks if not check.passed}
    if failed != {"BUDGET"} or hard_risk.max_buy_notional is None:
        return None
    try:
        price = Decimal(str(base.reference_price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    sizing = PositionSizingConfig()
    lot = sizing.krx_lot_size if request.market == "KRX" else sizing.us_lot_size
    units = (hard_risk.max_buy_notional / price / lot).to_integral_value(
        rounding=ROUND_DOWN
    )
    fitted = units * lot
    if fitted <= 0 or fitted >= request.quantity:
        return None
    return fitted


async def _claimable_owner_ids(
    db: AsyncSession,
    now: datetime,
    markets: Collection[str] | None = None,
) -> list[int]:
    market_filter = (
        ()
        if markets is None
        else (AIRecommendation.market.in_(sorted({m.upper() for m in markets})),)
    )
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
            *market_filter,
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
    markets: Collection[str] | None = None,
    realtime_gate: RealtimeEntryGate | None = None,
) -> dict[str, object]:
    """Run one bounded automation sweep: at most one execution per owner.

    Fail-closed by default: with ``AI_PAPER_AUTO_EXECUTION_ENABLED`` false the
    sweep reports itself disabled without touching the database. One owner's
    failure never aborts the other owners' sweeps.  During a regular session a
    degraded reference quote blocks the owner before any order is built; see
    ``_stale_quote_block_reason``.

    ``markets``를 주면 그 시장의 추천만 본다(1분 KRX sweep은 ``{"KRX"}``).
    같은 owner를 두 sweep이 동시에 집행하지 않도록 owner advisory lock을 잡고,
    잡지 못하면 그 owner는 이번 sweep에서 건너뛴다.
    """

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    if not settings.AI_PAPER_AUTO_EXECUTION_ENABLED:
        return {"enabled": False, "owners": 0, "outcomes": []}

    async with _session() as db:
        owner_ids = await _claimable_owner_ids(db, current, markets)

    outcomes: list[dict[str, object]] = []
    async with contextlib.AsyncExitStack() as stack:
        gate = (
            realtime_gate
            if realtime_gate is not None
            else await stack.enter_async_context(realtime_entry_gate())
        )
        for owner_id in owner_ids:
            try:
                async with _owner_execution_lock(owner_id) as locked:
                    outcome = (
                        await _run_owner_sweep(
                            owner_id,
                            current=current,
                            markets=markets,
                            realtime_gate=gate,
                        )
                        if locked
                        else PaperExecutionOutcome(
                            status="BLOCKED",
                            reason="owner_execution_in_progress",
                        )
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
            await _dispatch_auto_paper_execution_push(
                owner_user_id=owner_id,
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
        "kasset paper automation sweep done: owners=%d outcomes=%s markets=%s",
        len(owner_ids),
        outcomes,
        sorted(markets) if markets is not None else "ALL",
    )
    return result


async def _run_owner_sweep(
    owner_id: int,
    *,
    current: datetime,
    markets: Collection[str] | None,
    realtime_gate: RealtimeEntryGate,
) -> PaperExecutionOutcome:
    async with _session() as db:
        snapshot = await AITradingPolicyService().get_snapshot(
            db,
            owner_id,
            now=current,
            execution_limit=0,
        )
        if snapshot.mode != OperatingMode.AUTO_PAPER:
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason="auto_paper_mode_required",
            )
        if snapshot.kill_switch:
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason="global_kill_switch_enabled",
            )
        # 여기까지 왔으면 AUTO_PAPER이고 kill switch는 꺼져 있다. override는
        # 승격 근거 요구 하나만 면제하며, PAPER 판정과 kill switch는
        # snapshot.promotion_bypass 계산에서 이미 반영됐다.
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
            markets=markets,
            realtime_gate=realtime_gate,
        )
        recommendation_id = (
            await recommendation_service.authorize_next_for_auto_execution(
                str(owner_id),
                current,
            )
        )
        if recommendation_id is None:
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason=(
                    "no_eligible_recommendation"
                    if promotion_bypassed
                    else "strategy_promotion_required"
                ),
            )
        # 주문을 만들기 전에 정규장 여부와 기준 시세 신선도를 검사한다.
        out_of_session_reason = await _out_of_session_block_reason(
            db,
            recommendation_id,
            now=current,
        )
        if out_of_session_reason is not None:
            logger.info(
                "kasset paper automation blocked outside the regular "
                "session: owner_user_id=%s recommendation_id=%s "
                "reason=%s",
                owner_id,
                recommendation_id,
                out_of_session_reason,
            )
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason=out_of_session_reason,
                recommendation_id=recommendation_id,
            )
        stale_quote_reason = await _stale_quote_block_reason(
            db,
            recommendation_id,
            now=current,
        )
        if stale_quote_reason is not None:
            logger.warning(
                "kasset paper automation blocked on a stale reference "
                "quote: owner_user_id=%s recommendation_id=%s reason=%s",
                owner_id,
                recommendation_id,
                stale_quote_reason,
            )
            return PaperExecutionOutcome(
                status="BLOCKED",
                reason=stale_quote_reason,
                recommendation_id=recommendation_id,
            )
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
                realtime_gate=realtime_gate,
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
        return outcome


async def run_approved_recommendation_once(
    owner_user_id: int,
    recommendation_id: str,
    *,
    now: datetime | None = None,
) -> PaperExecutionOutcome:
    """Synchronously execute one explicit APPROVAL decision in PAPER only."""

    current = (now or datetime.now(UTC)).replace(microsecond=0)
    async with _session() as db, realtime_entry_gate() as gate:
        consumer = PaperAutomationConsumer(
            owner_user_id=str(owner_user_id),
            safety_gate=RuntimeStateSafetyGate(db, automatic=False),
            recommendation_service=OwnerScopedRecommendationService(
                db,
                recommendation_id=recommendation_id,
            ),
            paper_orders=OwnerScopedPaperOrders(now=current, realtime_gate=gate),
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
    "REALTIME_ENTRY_NOT_CONFIRMED",
    "OwnerScopedPaperOrders",
    "OwnerScopedRecommendationService",
    "RuntimeStateSafetyGate",
    "run_paper_automation_once",
    "run_approved_recommendation_once",
]
