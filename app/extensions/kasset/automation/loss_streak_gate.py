"""닫힌 PAPER 손절 연속을 BUY 전용 Hard Risk 관문으로 적용한다."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.market_session import current_regular_session
from app.extensions.kasset.automation.position_manager import ExitKind
from app.extensions.kasset.automation.shadow_loss_streak import (
    ShadowLossLockObservation,
    ShadowLossReason,
    ShadowLossStatus,
    ShadowLossStreakConfig,
    ShadowLossStreakResult,
    ShadowPaperTradeFact,
    evaluate_shadow_loss_streak,
    persist_shadow_loss_locks,
)
from app.extensions.kasset.models import AndroidPaperAccount, AndroidPaperOrder
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperTrade

logger = logging.getLogger(__name__)

LOSS_STREAK_GATE_SCHEMA_VERSION: Final = "kasset.loss-streak-gate.v1"
LOSS_STREAK_CODE: Final[Literal["LOSS_STREAK"]] = "LOSS_STREAK"
STOP_LOSS_REASONS: Final[tuple[str, ...]] = tuple(
    kind.value
    for kind in (
        ExitKind.STOP,
        ExitKind.STOP_GAP,
        ExitKind.TRAILING_STOP,
        ExitKind.TRAILING_STOP_GAP,
    )
)


@dataclass(frozen=True, slots=True)
class LossStreakConfig:
    """운영 risk preset level 2의 손실 연속 기본값."""

    global_loss_limit: int = 3
    global_lookback: timedelta = timedelta(minutes=90)
    global_lock_duration: timedelta = timedelta(minutes=60)
    symbol_loss_limit: int = 2

    def __post_init__(self) -> None:
        if self.global_loss_limit < 1 or self.symbol_loss_limit < 1:
            raise ValueError("loss limits must be positive")
        if self.global_lookback <= timedelta(0):
            raise ValueError("global lookback must be positive")
        if self.global_lock_duration <= timedelta(0):
            raise ValueError("global lock duration must be positive")


DEFAULT_LOSS_STREAK_CONFIG: Final = LossStreakConfig()


@dataclass(frozen=True, slots=True)
class LossStreakGateResult:
    code: Literal["LOSS_STREAK"]
    passed: bool
    reason: Literal["global_lock", "symbol_lock", "sell_bypass", "unavailable"] | None
    detail: str
    evidence: dict[str, object]


class LossStreakGate:
    """PAPER 체결 사실을 계산하고 기존 SHADOW lock 테이블에 관측값을 남긴다."""

    def __init__(self, config: LossStreakConfig = DEFAULT_LOSS_STREAK_CONFIG) -> None:
        self._config = config

    async def evaluate(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        market: str,
        symbol: str,
        side: str,
        now: datetime,
    ) -> LossStreakGateResult:
        normalized_side = side.strip().upper()
        if normalized_side != "BUY":
            return _result(
                passed=True,
                reason="sell_bypass",
                detail=f"side={normalized_side or 'UNKNOWN'}; BUY 전용 관문",
                global_lock=None,
                symbol_lock=None,
                streak_global=0,
                streak_symbol=0,
            )

        try:
            current = _aware_utc(now)
            normalized_market = market.strip().upper()
            normalized_symbol = symbol.strip().upper()
            session = current_regular_session(normalized_market, current)
            if session is None:
                raise ValueError("regular_session_unavailable")
            earliest = min(current - self._config.global_lookback, session.opens_at)
            account_key, facts = await _load_paper_trade_facts(
                db,
                owner_user_id=owner_user_id,
                market=normalized_market,
                earliest=earliest,
            )
            combined = _evaluate_active_streaks(
                facts,
                owner_user_id=owner_user_id,
                account_key=account_key,
                market=normalized_market,
                symbol=normalized_symbol,
                now=current,
                session_opens_at=session.opens_at,
                session_closes_at=session.closes_at,
                config=self._config,
            )
        except Exception as exc:  # noqa: BLE001 - 관문 계산 불가는 명시적으로 fail-open
            unavailable = f"{type(exc).__name__}:{exc}"
            logger.warning(
                "kasset LOSS_STREAK gate unavailable: owner_user_id=%s market=%s "
                "symbol=%s reason=%s",
                owner_user_id,
                market,
                symbol,
                unavailable,
                exc_info=True,
            )
            return _result(
                passed=True,
                reason="unavailable",
                detail=f"unavailable={unavailable}",
                global_lock=None,
                symbol_lock=None,
                streak_global=0,
                streak_symbol=0,
                unavailable=unavailable,
            )

        persist_failed: str | None = None
        try:
            async with db.begin_nested():
                await persist_shadow_loss_locks(db, combined)
        except Exception as exc:  # noqa: BLE001 - 관측 저장 실패는 확정 판정을 바꾸지 않음
            persist_failed = f"{type(exc).__name__}:{exc}"
            logger.warning(
                "kasset LOSS_STREAK persistence failed: owner_user_id=%s market=%s "
                "symbol=%s reason=%s",
                owner_user_id,
                market,
                symbol,
                persist_failed,
                exc_info=True,
            )

        global_lock = combined.global_lock
        symbol_lock = combined.symbol_lock
        if global_lock.buy_locked:
            reason = "global_lock"
            passed = False
        elif symbol_lock.buy_locked:
            reason = "symbol_lock"
            passed = False
        else:
            reason = None
            passed = True
        return _result(
            passed=passed,
            reason=reason,
            detail=(
                f"reason={reason or 'clear'}; "
                f"streakGlobal={global_lock.streak_count}/{global_lock.loss_limit}; "
                f"streakSymbol={symbol_lock.streak_count}/{symbol_lock.loss_limit}; "
                f"persistFailed={persist_failed}"
            ),
            global_lock=global_lock if global_lock.buy_locked else None,
            symbol_lock=symbol_lock if symbol_lock.buy_locked else None,
            streak_global=global_lock.streak_count,
            streak_symbol=symbol_lock.streak_count,
            persist_failed=persist_failed,
        )


def _evaluate_active_streaks(
    facts: Sequence[ShadowPaperTradeFact],
    *,
    owner_user_id: int,
    account_key: str,
    market: str,
    symbol: str,
    now: datetime,
    session_opens_at: datetime,
    session_closes_at: datetime,
    config: LossStreakConfig,
) -> ShadowLossStreakResult:
    global_config = ShadowLossStreakConfig(
        stop_loss_reasons=STOP_LOSS_REASONS,
        loss_limit=config.global_loss_limit,
        lookback=config.global_lookback,
        lock_duration=config.global_lock_duration,
    )
    global_result = _valid_result(
        evaluate_shadow_loss_streak(
            facts,
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=market,
            symbol=symbol,
            evaluated_at=now,
            config=global_config,
        )
    )

    session_facts = tuple(
        fact for fact in facts if session_opens_at <= fact.executed_at <= now
    )
    session_duration = session_closes_at - session_opens_at
    preliminary_config = ShadowLossStreakConfig(
        stop_loss_reasons=STOP_LOSS_REASONS,
        loss_limit=config.symbol_loss_limit,
        lookback=session_duration,
        lock_duration=session_duration,
    )
    preliminary = _valid_result(
        evaluate_shadow_loss_streak(
            session_facts,
            owner_user_id=owner_user_id,
            account_key=account_key,
            market=market,
            symbol=symbol,
            evaluated_at=now,
            config=preliminary_config,
        )
    )
    newest_loss_at = preliminary.symbol_lock.newest_loss_at
    if newest_loss_at is None:
        symbol_lock = preliminary.symbol_lock
    elif newest_loss_at >= session_closes_at:
        symbol_lock = replace(
            preliminary.symbol_lock,
            streak_count=0,
            newest_loss_id=None,
            newest_loss_transaction_id=None,
            newest_loss_trade_id=None,
            newest_loss_at=None,
            expires_at=None,
            buy_locked=False,
            reason=ShadowLossReason.EXPIRED,
        )
    else:
        symbol_config = ShadowLossStreakConfig(
            stop_loss_reasons=STOP_LOSS_REASONS,
            loss_limit=config.symbol_loss_limit,
            lookback=session_duration,
            lock_duration=session_closes_at - newest_loss_at,
        )
        symbol_result = _valid_result(
            evaluate_shadow_loss_streak(
                session_facts,
                owner_user_id=owner_user_id,
                account_key=account_key,
                market=market,
                symbol=symbol,
                evaluated_at=now,
                config=symbol_config,
            )
        )
        symbol_lock = symbol_result.symbol_lock

    return replace(global_result, symbol_lock=symbol_lock)


def _valid_result(result: ShadowLossStreakResult) -> ShadowLossStreakResult:
    if result.status is ShadowLossStatus.VALID:
        return result
    if result.status is not ShadowLossStatus.INSUFFICIENT:
        raise ValueError(
            f"shadow_evaluation_{result.status.value}:{result.global_lock.reason.value}"
        )

    def unlocked(observation: ShadowLossLockObservation) -> ShadowLossLockObservation:
        return replace(
            observation,
            status=ShadowLossStatus.VALID,
            streak_count=0,
            newest_loss_id=None,
            newest_loss_transaction_id=None,
            newest_loss_trade_id=None,
            newest_loss_at=None,
            expires_at=None,
            buy_locked=False,
            reason=ShadowLossReason.BELOW_LIMIT,
        )

    return replace(
        result,
        status=ShadowLossStatus.VALID,
        global_lock=unlocked(result.global_lock),
        symbol_lock=unlocked(result.symbol_lock),
    )


async def _load_paper_trade_facts(
    db: AsyncSession,
    *,
    owner_user_id: int,
    market: str,
    earliest: datetime,
) -> tuple[str, tuple[ShadowPaperTradeFact, ...]]:
    account_id = await db.scalar(
        select(AndroidPaperAccount.paper_account_id)
        .where(AndroidPaperAccount.owner_user_id == owner_user_id)
        .order_by(AndroidPaperAccount.paper_account_id)
        .limit(1)
    )
    if account_id is None:
        raise LookupError("paper_account_unavailable")

    rows = (
        await db.execute(
            select(AndroidPaperOrder, PaperTrade)
            .join(PaperTrade, PaperTrade.id == AndroidPaperOrder.paper_trade_id)
            .where(
                AndroidPaperOrder.owner_user_id == owner_user_id,
                AndroidPaperOrder.paper_account_id == account_id,
                AndroidPaperOrder.market == market,
                AndroidPaperOrder.side == "SELL",
                PaperTrade.executed_at >= earliest,
            )
            .order_by(PaperTrade.executed_at, PaperTrade.id)
        )
    ).all()
    recommendation_ids = tuple(
        dict.fromkeys(
            recommendation_id
            for order, _trade in rows
            if (recommendation_id := _recommendation_id(order.client_order_id))
            is not None
        )
    )
    recommendations: dict[str, AIRecommendation] = {}
    if recommendation_ids:
        loaded = (
            await db.scalars(
                select(AIRecommendation).where(
                    AIRecommendation.owner_user_id == owner_user_id,
                    AIRecommendation.id.in_(recommendation_ids),
                )
            )
        ).all()
        recommendations = {str(item.id): item for item in loaded}

    facts = tuple(
        ShadowPaperTradeFact(
            id=int(trade.id),
            transaction_id=str(order.id),
            trade_id=str(trade.id),
            owner_user_id=owner_user_id,
            account_key=f"paper:{account_id}",
            market=market,
            symbol=str(trade.symbol),
            side="SELL",
            lifecycle_status="CLOSED",
            realized_pnl=(
                Decimal(trade.realized_pnl) if trade.realized_pnl is not None else None
            ),
            exit_reason=_exit_reason(
                recommendations.get(_recommendation_id(order.client_order_id) or ""),
                fallback=trade.reason,
            ),
            executed_at=_aware_utc(trade.executed_at),
            account_mode="PAPER",
        )
        for order, trade in rows
    )
    return f"paper:{account_id}", facts


def _recommendation_id(client_order_id: object) -> str | None:
    value = str(client_order_id)
    prefix = "ai-rec:"
    if not value.startswith(prefix):
        return None
    recommendation_id = value[len(prefix) :].strip()
    return recommendation_id or None


def _exit_reason(
    recommendation: AIRecommendation | None,
    *,
    fallback: object,
) -> str | None:
    evidence: object = getattr(recommendation, "evidence", ())
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        for item in reversed(evidence):
            if (
                isinstance(item, Mapping)
                and item.get("source") == "position_manager"
                and item.get("kind") == "position_exit"
            ):
                exit_kind = item.get("exitKind")
                if isinstance(exit_kind, str) and exit_kind.strip():
                    return exit_kind.strip()
    return str(fallback).strip() if fallback is not None else None


def _lock_evidence(
    observation: ShadowLossLockObservation | None,
) -> dict[str, object] | None:
    if observation is None:
        return None
    return {
        "scope": observation.scope.value,
        "symbol": observation.symbol,
        "streakCount": observation.streak_count,
        "lossLimit": observation.loss_limit,
        "newestLossAt": _timestamp_text(observation.newest_loss_at),
        "expiresAt": _timestamp_text(observation.expires_at),
        "reason": observation.reason.value,
    }


def _result(
    *,
    passed: bool,
    reason: Literal["global_lock", "symbol_lock", "sell_bypass", "unavailable"] | None,
    detail: str,
    global_lock: ShadowLossLockObservation | None,
    symbol_lock: ShadowLossLockObservation | None,
    streak_global: int,
    streak_symbol: int,
    unavailable: str | None = None,
    persist_failed: str | None = None,
) -> LossStreakGateResult:
    evidence: dict[str, object] = {
        "schemaVersion": LOSS_STREAK_GATE_SCHEMA_VERSION,
        "globalLock": _lock_evidence(global_lock),
        "symbolLock": _lock_evidence(symbol_lock),
        "streakGlobal": streak_global,
        "streakSymbol": streak_symbol,
        "unavailable": unavailable,
        "persistFailed": persist_failed,
    }
    return LossStreakGateResult(
        code=LOSS_STREAK_CODE,
        passed=passed,
        reason=reason,
        detail=detail,
        evidence=evidence,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


loss_streak_gate = LossStreakGate()

__all__ = [
    "DEFAULT_LOSS_STREAK_CONFIG",
    "LOSS_STREAK_CODE",
    "LOSS_STREAK_GATE_SCHEMA_VERSION",
    "STOP_LOSS_REASONS",
    "LossStreakConfig",
    "LossStreakGate",
    "LossStreakGateResult",
    "loss_streak_gate",
]
