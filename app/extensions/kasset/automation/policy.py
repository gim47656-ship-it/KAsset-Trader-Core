"""Persisted owner limits, portfolio sizing, usage, and deterministic hard risk."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    AndroidPaperOrder,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperPosition, PaperTrade
from app.models.user_settings import UserSetting

_SETTING_KEY = "kasset.ai_trading"
_MIN_AI_CONFIDENCE = Decimal("0.50")


class OperatingMode(StrEnum):
    APPROVAL = "APPROVAL"
    AUTO_PAPER = "AUTO_PAPER"


@dataclass(frozen=True, slots=True)
class AITradingLimits:
    operating_budget: Decimal = Decimal("10000000")
    conservative_daily_goal: Decimal = Decimal("100000")
    daily_max_loss: Decimal = Decimal("200000")
    max_buys_per_day: int = 3
    max_orders_per_day: int = 5
    max_symbol_allocation: Decimal = Decimal("0.20")
    max_concurrent_holdings: int = 5
    same_symbol_reentry_limit: int = 1
    kill_switch: bool = False
    currency: Literal["KRW", "USD"] = "KRW"

    def to_storage(self) -> dict[str, object]:
        return {
            "operating_budget": str(self.operating_budget),
            "conservative_daily_goal": str(self.conservative_daily_goal),
            "daily_max_loss": str(self.daily_max_loss),
            "max_buys_per_day": self.max_buys_per_day,
            "max_orders_per_day": self.max_orders_per_day,
            "max_symbol_allocation": str(self.max_symbol_allocation),
            "max_concurrent_holdings": self.max_concurrent_holdings,
            "same_symbol_reentry_limit": self.same_symbol_reentry_limit,
            "kill_switch": self.kill_switch,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class AITradingUsage:
    realized_pnl_today: Decimal = Decimal("0")
    realized_loss_today: Decimal = Decimal("0")
    buys_today: int = 0
    orders_today: int = 0
    concurrent_holdings: int = 0
    budget_used: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PaperExecutionView:
    id: str
    recommendation_id: str | None
    market: str
    symbol: str
    name: str | None
    side: str
    quantity: Decimal
    price: Decimal | None
    currency: str
    status: str
    at: datetime
    reject_reason: str | None


@dataclass(frozen=True, slots=True)
class AITradingSnapshot:
    mode: OperatingMode
    limits: AITradingLimits
    usage: AITradingUsage
    kill_switch: bool
    updated_at: datetime
    executions: tuple[PaperExecutionView, ...] = ()


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    target_weight: Decimal
    target_quantity: Decimal
    cash_after: Decimal
    note: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "targetWeight": str(self.target_weight),
            "targetQuantity": str(self.target_quantity),
            "cashAfter": str(self.cash_after),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class HardRiskCheck:
    rule: str
    passed: bool
    detail: str

    def as_evidence(self) -> dict[str, object]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class HardRiskResult:
    passed: bool
    checks: tuple[HardRiskCheck, ...]
    blocked_reason: str | None

    def as_evidence(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [check.as_evidence() for check in self.checks],
            "blockedReason": self.blocked_reason,
        }


class AITradingPolicyService:
    """The only KAsset owner-policy writer and hard-risk evaluator."""

    async def get_snapshot(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        now: datetime,
        execution_limit: int = 20,
    ) -> AITradingSnapshot:
        current = _aware_utc(now)
        row = await self._setting_row(db, owner_user_id)
        mode, limits = _decode_setting(row.value if row is not None else None)
        state = await runtime_state.get(db, owner_user_id)
        global_state = await runtime_state.get_global(db)
        usage = await self.usage(db, owner_user_id, limits=limits, now=current)
        executions = await self._executions(
            db, owner_user_id, limit=max(0, min(execution_limit, 100))
        )
        updated_at = row.updated_at if row is not None else state.updated_at
        return AITradingSnapshot(
            mode=mode,
            limits=limits,
            usage=usage,
            kill_switch=(
                limits.kill_switch
                or bool(state.kill_switch_enabled)
                or bool(global_state.kill_switch_enabled)
            ),
            updated_at=_aware_utc(updated_at),
            executions=tuple(executions),
        )

    async def put_snapshot(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        mode: OperatingMode,
        limits: AITradingLimits,
        now: datetime,
    ) -> AITradingSnapshot:
        current = _aware_utc(now)
        state = await runtime_state.get(db, owner_user_id, for_update=True)
        row = await self._setting_row(db, owner_user_id, for_update=True)
        payload = {"mode": OperatingMode(mode).value, "settings": limits.to_storage()}
        if row is None:
            row = UserSetting(user_id=owner_user_id, key=_SETTING_KEY, value=payload)
            db.add(row)
        else:
            row.value = payload
        state.trading_mode = "PAPER"
        state.kill_switch_enabled = limits.kill_switch
        state.max_symbol_ratio = limits.max_symbol_allocation
        await db.commit()
        await db.refresh(row)
        return await self.get_snapshot(db, owner_user_id, now=current)

    async def usage(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        limits: AITradingLimits,
        now: datetime,
    ) -> AITradingUsage:
        account_id = await db.scalar(
            select(AndroidPaperAccount.paper_account_id)
            .where(AndroidPaperAccount.owner_user_id == owner_user_id)
            .order_by(AndroidPaperAccount.paper_account_id)
            .limit(1)
        )
        if account_id is None:
            return AITradingUsage()
        start = _trading_day_start(now, limits.currency)
        orders_today = int(
            await db.scalar(
                select(func.count())
                .select_from(AndroidPaperOrder)
                .where(
                    AndroidPaperOrder.owner_user_id == owner_user_id,
                    AndroidPaperOrder.created_at >= start,
                )
            )
            or 0
        )
        buys_today = int(
            await db.scalar(
                select(func.count())
                .select_from(AndroidPaperOrder)
                .where(
                    AndroidPaperOrder.owner_user_id == owner_user_id,
                    AndroidPaperOrder.side == "BUY",
                    AndroidPaperOrder.created_at >= start,
                )
            )
            or 0
        )
        concurrent_holdings = int(
            await db.scalar(
                select(func.count())
                .select_from(PaperPosition)
                .where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.quantity > 0,
                )
            )
            or 0
        )
        budget_used = _decimal_or_zero(
            await db.scalar(
                select(func.coalesce(func.sum(PaperPosition.total_invested), 0)).where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.quantity > 0,
                )
            )
        )
        realized_pnl = _decimal_or_zero(
            await db.scalar(
                select(func.coalesce(func.sum(PaperTrade.realized_pnl), 0)).where(
                    PaperTrade.account_id == account_id,
                    PaperTrade.executed_at >= start,
                )
            )
        )
        return AITradingUsage(
            realized_pnl_today=realized_pnl,
            realized_loss_today=max(Decimal("0"), -realized_pnl),
            buys_today=buys_today,
            orders_today=orders_today,
            concurrent_holdings=concurrent_holdings,
            budget_used=budget_used,
        )

    async def portfolio_plan(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        action: str,
        market: str,
        symbol: str,
        reference_price: Decimal,
        limits: AITradingLimits,
        usage: AITradingUsage,
    ) -> PortfolioPlan:
        account_id = await db.scalar(
            select(AndroidPaperAccount.paper_account_id)
            .where(AndroidPaperAccount.owner_user_id == owner_user_id)
            .order_by(AndroidPaperAccount.paper_account_id)
            .limit(1)
        )
        position = None
        if account_id is not None:
            position = await db.scalar(
                select(PaperPosition).where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.symbol == symbol,
                    PaperPosition.quantity > 0,
                )
            )
        current_quantity = _decimal_or_zero(
            position.quantity if position is not None else None
        )
        current_invested = _decimal_or_zero(
            position.total_invested if position is not None else None
        )
        target_weight = limits.max_symbol_allocation
        if action == "SELL":
            quantity = current_quantity
            cash_after = min(
                limits.operating_budget,
                max(Decimal("0"), limits.operating_budget - usage.budget_used)
                + quantity * reference_price,
            )
            note = "현재 PAPER 보유수량 안에서만 매도 수량을 산정했습니다."
        else:
            target_notional = limits.operating_budget * target_weight
            additional_notional = max(Decimal("0"), target_notional - current_invested)
            additional_notional = min(
                additional_notional,
                max(Decimal("0"), limits.operating_budget - usage.budget_used),
            )
            raw_quantity = additional_notional / reference_price
            quantum = Decimal("1") if market == "KRX" else Decimal("0.0001")
            quantity = raw_quantity.quantize(quantum, rounding=ROUND_DOWN)
            cash_after = max(
                Decimal("0"),
                limits.operating_budget
                - usage.budget_used
                - quantity * reference_price,
            )
            note = (
                "운영예산과 종목별 배분 상한 안에서 추가 PAPER 매수수량을 산정했습니다."
            )
        return PortfolioPlan(
            target_weight=target_weight,
            target_quantity=quantity,
            cash_after=cash_after,
            note=note,
        )

    async def evaluate_hard_risk(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        action: str,
        market: str,
        symbol: str,
        quantity: Decimal,
        reference_price: Decimal,
        ai_confidence: Decimal,
        now: datetime,
        base_risk_reasons: Sequence[Any] = (),
    ) -> HardRiskResult:
        snapshot = await self.get_snapshot(
            db, owner_user_id, now=now, execution_limit=0
        )
        limits = snapshot.limits
        usage = snapshot.usage
        order_notional = quantity * reference_price
        account_id = await db.scalar(
            select(AndroidPaperAccount.paper_account_id)
            .where(AndroidPaperAccount.owner_user_id == owner_user_id)
            .order_by(AndroidPaperAccount.paper_account_id)
            .limit(1)
        )
        position = None
        if account_id is not None:
            position = await db.scalar(
                select(PaperPosition).where(
                    PaperPosition.account_id == account_id,
                    PaperPosition.symbol == symbol,
                    PaperPosition.quantity > 0,
                )
            )
        current_invested = _decimal_or_zero(
            position.total_invested if position is not None else None
        )
        current_quantity = _decimal_or_zero(
            position.quantity if position is not None else None
        )
        start = _trading_day_start(now, limits.currency)
        same_symbol_buys = int(
            await db.scalar(
                select(func.count())
                .select_from(AndroidPaperOrder)
                .where(
                    AndroidPaperOrder.owner_user_id == owner_user_id,
                    AndroidPaperOrder.symbol == symbol,
                    AndroidPaperOrder.side == "BUY",
                    AndroidPaperOrder.created_at >= start,
                )
            )
            or 0
        )

        expected_market = "KRX" if limits.currency == "KRW" else "US"
        is_buy = action == "BUY"
        valid_shape = (
            action in {"BUY", "SELL"}
            and market == expected_market
            and quantity.is_finite()
            and quantity > 0
            and reference_price.is_finite()
            and reference_price > 0
        )
        base_risk_details = [
            str(getattr(reason, "message", reason)) for reason in base_risk_reasons
        ]
        budget_passed = (not is_buy) or (
            usage.budget_used + order_notional <= limits.operating_budget
            and current_invested + order_notional
            <= limits.operating_budget * limits.max_symbol_allocation
        )
        position_passed = (
            valid_shape
            and not base_risk_details
            and (
                (
                    is_buy
                    and (
                        position is not None
                        or usage.concurrent_holdings < limits.max_concurrent_holdings
                    )
                )
                or (action == "SELL" and current_quantity >= quantity)
            )
        )
        order_count_passed = usage.orders_today < limits.max_orders_per_day and (
            (not is_buy)
            or (
                usage.buys_today < limits.max_buys_per_day
                and same_symbol_buys < limits.same_symbol_reentry_limit
            )
        )
        checks = [
            HardRiskCheck(
                "DAILY_MAX_LOSS",
                usage.realized_loss_today < limits.daily_max_loss,
                f"realizedLossToday={usage.realized_loss_today}; limit={limits.daily_max_loss}",
            ),
            HardRiskCheck(
                "BUDGET",
                budget_passed,
                (
                    f"budgetUsed={usage.budget_used}; order={order_notional}; "
                    f"operatingBudget={limits.operating_budget}; "
                    f"symbolCurrent={current_invested}; "
                    f"symbolRatio={limits.max_symbol_allocation}"
                ),
            ),
            HardRiskCheck(
                "POSITION",
                position_passed,
                (
                    f"action={action}; market={market}; expectedMarket={expected_market}; "
                    f"held={current_quantity}; holdings={usage.concurrent_holdings}; "
                    f"limit={limits.max_concurrent_holdings}; "
                    f"paperRisk={'; '.join(base_risk_details) or 'clear'}"
                ),
            ),
            HardRiskCheck(
                "ORDER_COUNT",
                order_count_passed,
                (
                    f"ordersToday={usage.orders_today}/{limits.max_orders_per_day}; "
                    f"buysToday={usage.buys_today}/{limits.max_buys_per_day}; "
                    f"sameSymbolBuys={same_symbol_buys}/{limits.same_symbol_reentry_limit}"
                ),
            ),
            HardRiskCheck(
                "AI",
                ai_confidence.is_finite() and ai_confidence >= _MIN_AI_CONFIDENCE,
                f"confidence={ai_confidence}; floor={_MIN_AI_CONFIDENCE}",
            ),
            HardRiskCheck(
                "DAILY_GOAL",
                True,
                (
                    f"realizedPnlToday={usage.realized_pnl_today}; "
                    f"referenceGoal={limits.conservative_daily_goal}; "
                    "목표수익은 참고값이며 주문 강제 조건이 아닙니다."
                ),
            ),
        ]
        first_failed = next((check for check in checks if not check.passed), None)
        kill_blocked = snapshot.kill_switch
        return HardRiskResult(
            passed=not kill_blocked and first_failed is None,
            checks=tuple(checks),
            blocked_reason=(
                "kill switch가 켜져 있습니다."
                if kill_blocked
                else first_failed.detail
                if first_failed is not None
                else None
            ),
        )

    async def _setting_row(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        for_update: bool = False,
    ) -> UserSetting | None:
        statement = select(UserSetting).where(
            UserSetting.user_id == owner_user_id,
            UserSetting.key == _SETTING_KEY,
        )
        if for_update:
            statement = statement.with_for_update()
        return await db.scalar(statement)

    async def _executions(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        limit: int,
    ) -> list[PaperExecutionView]:
        if limit <= 0:
            return []
        orders = list(
            (
                await db.scalars(
                    select(AndroidPaperOrder)
                    .where(AndroidPaperOrder.owner_user_id == owner_user_id)
                    .order_by(AndroidPaperOrder.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        if not orders:
            return []
        recommendation_rows = (
            await db.execute(
                select(AIRecommendation.paper_order_id, AIRecommendation.id).where(
                    AIRecommendation.owner_user_id == owner_user_id,
                    AIRecommendation.paper_order_id.in_([order.id for order in orders]),
                )
            )
        ).all()
        recommendation_by_order = {
            str(order_id): str(recommendation_id)
            for order_id, recommendation_id in recommendation_rows
            if order_id is not None
        }
        return [
            PaperExecutionView(
                id=order.id,
                recommendation_id=recommendation_by_order.get(order.id),
                market=order.market,
                symbol=order.symbol,
                name=order.name,
                side=order.side,
                quantity=_decimal_or_zero(order.quantity),
                price=(
                    _decimal_or_zero(order.average_fill_price)
                    if order.average_fill_price is not None
                    else (
                        _decimal_or_zero(order.limit_price)
                        if order.limit_price is not None
                        else None
                    )
                ),
                currency=order.currency,
                status=order.status,
                at=_aware_utc(order.created_at),
                reject_reason=order.reject_reason,
            )
            for order in orders
        ]


def _decode_setting(value: object) -> tuple[OperatingMode, AITradingLimits]:
    if value is None:
        return OperatingMode.APPROVAL, AITradingLimits()
    if not isinstance(value, dict):
        raise ValueError("stored AI trading setting must be an object")
    mode = OperatingMode(str(value.get("mode", OperatingMode.APPROVAL.value)))
    raw = value.get("settings", {})
    if not isinstance(raw, dict):
        raise ValueError("stored AI trading limits must be an object")
    defaults = AITradingLimits()
    currency = str(raw.get("currency", defaults.currency)).upper()
    if currency not in {"KRW", "USD"}:
        raise ValueError("stored AI trading currency is invalid")
    limits = AITradingLimits(
        operating_budget=_positive_decimal(
            raw.get("operating_budget", defaults.operating_budget),
            "operating_budget",
        ),
        conservative_daily_goal=_nonnegative_decimal(
            raw.get("conservative_daily_goal", defaults.conservative_daily_goal),
            "conservative_daily_goal",
        ),
        daily_max_loss=_nonnegative_decimal(
            raw.get("daily_max_loss", defaults.daily_max_loss), "daily_max_loss"
        ),
        max_buys_per_day=_nonnegative_int(
            raw.get("max_buys_per_day", defaults.max_buys_per_day),
            "max_buys_per_day",
        ),
        max_orders_per_day=_nonnegative_int(
            raw.get("max_orders_per_day", defaults.max_orders_per_day),
            "max_orders_per_day",
        ),
        max_symbol_allocation=_ratio(
            raw.get("max_symbol_allocation", defaults.max_symbol_allocation),
            "max_symbol_allocation",
        ),
        max_concurrent_holdings=_nonnegative_int(
            raw.get("max_concurrent_holdings", defaults.max_concurrent_holdings),
            "max_concurrent_holdings",
        ),
        same_symbol_reentry_limit=_nonnegative_int(
            raw.get("same_symbol_reentry_limit", defaults.same_symbol_reentry_limit),
            "same_symbol_reentry_limit",
        ),
        kill_switch=_strict_bool(
            raw.get("kill_switch", defaults.kill_switch), "kill_switch"
        ),
        currency=currency,  # type: ignore[arg-type]
    )
    return mode, limits


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC).replace(microsecond=0)


def _trading_day_start(value: datetime, currency: str) -> datetime:
    current = _aware_utc(value)
    zone = ZoneInfo("Asia/Seoul" if currency == "KRW" else "America/New_York")
    local = current.astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _decimal_or_zero(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    return result if result.is_finite() else Decimal("0")


def _positive_decimal(value: object, field: str) -> Decimal:
    result = _decimal_or_zero(value)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    result = _decimal_or_zero(value)
    if result < 0:
        raise ValueError(f"{field} must not be negative")
    return result


def _ratio(value: object, field: str) -> Decimal:
    result = _positive_decimal(value, field)
    if result > 1:
        raise ValueError(f"{field} must not exceed one")
    return result


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


__all__ = [
    "AITradingLimits",
    "AITradingPolicyService",
    "AITradingSnapshot",
    "AITradingUsage",
    "HardRiskCheck",
    "HardRiskResult",
    "OperatingMode",
    "PaperExecutionView",
    "PortfolioPlan",
]
