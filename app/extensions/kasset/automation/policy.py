"""Persisted owner limits, portfolio sizing, usage, and deterministic hard risk."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.automation.position_sizing import (
    DEFAULT_POSITION_SIZING_CONFIG,
    PositionSizingConfig,
    PositionSizingInput,
    PositionSizingResult,
    calculate_position_size,
)
from app.extensions.kasset.automation.regime import MarketRegime
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    AndroidPaperOrder,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperPosition, PaperTrade
from app.models.user_settings import UserSetting

logger = logging.getLogger(__name__)

_SETTING_KEY = "kasset.ai_trading"
_MIN_AI_CONFIDENCE = Decimal("0.50")
_DEFAULT_OPERATING_BUDGET = Decimal("10000000")


@dataclass(frozen=True, slots=True)
class _RiskPreset:
    daily_target_rate_pct: Decimal
    max_daily_loss_rate_pct: Decimal
    risk_per_trade_rate: Decimal
    max_symbol_allocation: Decimal
    max_concurrent_holdings: int
    max_buys_per_day: int
    max_sells_per_day: int
    same_symbol_reentry_limit: int


_RISK_PRESETS = {
    1: _RiskPreset(
        Decimal("0.3"),
        Decimal("0.5"),
        Decimal("0.0025"),
        Decimal("0.10"),
        3,
        1,
        1,
        1,
    ),
    2: _RiskPreset(
        Decimal("0.5"),
        Decimal("1.0"),
        Decimal("0.005"),
        Decimal("0.15"),
        4,
        2,
        1,
        1,
    ),
    3: _RiskPreset(
        Decimal("0.8"),
        Decimal("1.5"),
        Decimal("0.0075"),
        Decimal("0.20"),
        5,
        3,
        2,
        1,
    ),
    4: _RiskPreset(
        Decimal("1.2"),
        Decimal("2.5"),
        Decimal("0.01"),
        Decimal("0.25"),
        5,
        5,
        3,
        1,
    ),
    5: _RiskPreset(
        Decimal("2.0"),
        Decimal("4.0"),
        Decimal("0.015"),
        Decimal("0.30"),
        6,
        8,
        4,
        2,
    ),
}
_LEGACY_PRESET_DAILY_LIMITS = {
    1: {"max_buys_per_day": 1, "max_orders_per_day": 2},
    2: {"max_buys_per_day": 2, "max_orders_per_day": 3},
    3: {"max_buys_per_day": 3, "max_orders_per_day": 5},
    4: {"max_buys_per_day": 5, "max_orders_per_day": 8},
    5: {"max_buys_per_day": 8, "max_orders_per_day": 12},
}
_DEFAULT_RISK_LEVEL = 2


class OperatingMode(StrEnum):
    APPROVAL = "APPROVAL"
    AUTO_PAPER = "AUTO_PAPER"


@dataclass(frozen=True, slots=True)
class AITradingLimits:
    risk_level: int = _DEFAULT_RISK_LEVEL
    operating_budget: Decimal = _DEFAULT_OPERATING_BUDGET
    daily_target_rate_pct: Decimal = _RISK_PRESETS[
        _DEFAULT_RISK_LEVEL
    ].daily_target_rate_pct
    max_daily_loss_rate_pct: Decimal = _RISK_PRESETS[
        _DEFAULT_RISK_LEVEL
    ].max_daily_loss_rate_pct
    risk_per_trade_rate: Decimal = field(init=False)
    custom_max_buys_per_day: int | None = None
    custom_max_sells_per_day: int | None = None
    max_buys_per_day: int = field(init=False)
    max_sells_per_day: int = field(init=False)
    max_orders_per_day: int = field(init=False)
    max_custom_buys_per_day: int = field(init=False)
    max_custom_sells_per_day: int = field(init=False)
    max_custom_orders_per_day: int = field(init=False)
    max_symbol_allocation: Decimal = field(init=False)
    max_concurrent_holdings: int = field(init=False)
    same_symbol_reentry_limit: int = field(init=False)
    kill_switch: bool = False
    currency: Literal["KRW", "USD"] = "KRW"

    def __init__(
        self,
        *,
        risk_level: int = _DEFAULT_RISK_LEVEL,
        operating_budget: Decimal = _DEFAULT_OPERATING_BUDGET,
        daily_target_rate_pct: Decimal | None = None,
        max_daily_loss_rate_pct: Decimal | None = None,
        custom_max_buys_per_day: int | None = None,
        custom_max_sells_per_day: int | None = None,
        kill_switch: bool = False,
        currency: Literal["KRW", "USD"] = "KRW",
    ) -> None:
        level = _risk_level(risk_level, "risk_level")
        preset = _RISK_PRESETS[level]
        normalized_currency = str(currency).upper()
        if normalized_currency not in {"KRW", "USD"}:
            raise ValueError("currency must be KRW or USD")
        object.__setattr__(self, "risk_level", level)
        object.__setattr__(
            self,
            "operating_budget",
            _positive_decimal(operating_budget, "operating_budget"),
        )
        object.__setattr__(
            self,
            "daily_target_rate_pct",
            preset.daily_target_rate_pct
            if daily_target_rate_pct is None
            else _percentage(
                daily_target_rate_pct,
                "daily_target_rate_pct",
                minimum=Decimal("0"),
                maximum=Decimal("10"),
            ),
        )
        object.__setattr__(
            self,
            "max_daily_loss_rate_pct",
            preset.max_daily_loss_rate_pct
            if max_daily_loss_rate_pct is None
            else _percentage(
                max_daily_loss_rate_pct,
                "max_daily_loss_rate_pct",
                minimum=Decimal("0"),
                maximum=Decimal("20"),
            ),
        )
        object.__setattr__(self, "risk_per_trade_rate", preset.risk_per_trade_rate)
        object.__setattr__(self, "max_symbol_allocation", preset.max_symbol_allocation)
        object.__setattr__(
            self, "max_concurrent_holdings", preset.max_concurrent_holdings
        )
        object.__setattr__(
            self,
            "same_symbol_reentry_limit",
            preset.same_symbol_reentry_limit,
        )
        max_custom_buys = preset.max_concurrent_holdings * max(
            2, preset.same_symbol_reentry_limit * 2
        )
        max_custom_sells = preset.max_concurrent_holdings * (
            preset.same_symbol_reentry_limit + 3
        )
        effective_buys = _optional_daily_limit(
            custom_max_buys_per_day,
            "custom_max_buys_per_day",
            maximum=max_custom_buys,
            default=preset.max_buys_per_day,
        )
        effective_sells = _optional_daily_limit(
            custom_max_sells_per_day,
            "custom_max_sells_per_day",
            maximum=max_custom_sells,
            default=preset.max_sells_per_day,
        )
        object.__setattr__(self, "custom_max_buys_per_day", custom_max_buys_per_day)
        object.__setattr__(self, "custom_max_sells_per_day", custom_max_sells_per_day)
        object.__setattr__(self, "max_buys_per_day", effective_buys)
        object.__setattr__(self, "max_sells_per_day", effective_sells)
        object.__setattr__(self, "max_orders_per_day", effective_buys + effective_sells)
        object.__setattr__(self, "max_custom_buys_per_day", max_custom_buys)
        object.__setattr__(self, "max_custom_sells_per_day", max_custom_sells)
        object.__setattr__(
            self,
            "max_custom_orders_per_day",
            max_custom_buys + max_custom_sells,
        )
        object.__setattr__(
            self, "kill_switch", _strict_bool(kill_switch, "kill_switch")
        )
        object.__setattr__(self, "currency", normalized_currency)

    @property
    def daily_target_amount(self) -> Decimal:
        return self.operating_budget * self.daily_target_rate_pct / Decimal("100")

    @property
    def max_daily_loss_amount(self) -> Decimal:
        return self.operating_budget * self.max_daily_loss_rate_pct / Decimal("100")

    @property
    def max_symbol_allocation_pct(self) -> Decimal:
        return self.max_symbol_allocation * Decimal("100")

    @property
    def min_ai_confidence(self) -> Decimal:
        return _MIN_AI_CONFIDENCE

    def to_storage(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "risk_level": self.risk_level,
            "operating_budget": str(self.operating_budget),
            "daily_target_rate_pct": str(self.daily_target_rate_pct),
            "max_daily_loss_rate_pct": str(self.max_daily_loss_rate_pct),
            "kill_switch": self.kill_switch,
            "currency": self.currency,
        }
        if self.custom_max_buys_per_day is not None:
            payload["custom_max_buys_per_day"] = self.custom_max_buys_per_day
        if self.custom_max_sells_per_day is not None:
            payload["custom_max_sells_per_day"] = self.custom_max_sells_per_day
        return payload


@dataclass(frozen=True, slots=True)
class AITradingUsage:
    realized_pnl_today: Decimal = Decimal("0")
    realized_loss_today: Decimal = Decimal("0")
    buys_today: int = 0
    sells_today: int = 0
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
    # 소유자가 명시적으로 켠 "승격 근거 없이 PAPER 자동실행 허용". kill switch가
    # 켜져 있거나 trading_mode가 PAPER가 아니면 여기서 이미 False로 접힌다.
    promotion_bypass: bool = False


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    target_weight: Decimal
    target_quantity: Decimal
    cash_after: Decimal
    note: str
    position_sizing: PositionSizingResult | None = None

    def as_evidence(self) -> dict[str, object]:
        evidence: dict[str, object] = {
            "targetWeight": str(self.target_weight),
            "targetQuantity": str(self.target_quantity),
            "cashAfter": str(self.cash_after),
            "note": self.note,
        }
        if self.position_sizing is not None:
            evidence["positionSizing"] = self.position_sizing.as_evidence()
        return evidence


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
        kill_switch = (
            limits.kill_switch
            or bool(state.kill_switch_enabled)
            or bool(global_state.kill_switch_enabled)
        )
        return AITradingSnapshot(
            mode=mode,
            limits=limits,
            usage=usage,
            kill_switch=kill_switch,
            updated_at=_aware_utc(updated_at),
            executions=tuple(executions),
            promotion_bypass=(
                bool(state.promotion_bypass_enabled)
                and not kill_switch
                and str(state.trading_mode).strip().upper() == "PAPER"
            ),
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
        await db.commit()
        await db.refresh(row)
        return await self.get_snapshot(db, owner_user_id, now=current)

    async def set_promotion_bypass(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        enabled: bool,
        reason: str,
        now: datetime,
    ) -> AITradingSnapshot:
        """승격 근거 없이 PAPER 자동실행을 허용할지 소유자별로 저장한다.

        기본값은 False이고, 켜도 kill switch와 PAPER 판정은 그대로 남는다.
        승격 근거 요구 하나만 면제하므로 전환 사실과 사유를 감사 로그에 남긴다.
        """

        current = _aware_utc(now)
        state = await runtime_state.get(db, owner_user_id, for_update=True)
        state.promotion_bypass_enabled = _strict_bool(enabled, "enabled")
        await db.commit()
        logger.warning(
            "kasset promotion bypass %s: owner_user_id=%s reason=%s",
            "enabled" if enabled else "disabled",
            owner_user_id,
            reason,
        )
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
        sells_today = int(
            await db.scalar(
                select(func.count())
                .select_from(AndroidPaperOrder)
                .where(
                    AndroidPaperOrder.owner_user_id == owner_user_id,
                    AndroidPaperOrder.side == "SELL",
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
            sells_today=sells_today,
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
        strategy_stop: Decimal | None = None,
        strategy_atr: Decimal | None = None,
        price_as_of: datetime | None = None,
        evaluated_at: datetime | None = None,
        regime: MarketRegime | str = MarketRegime.SIDEWAYS,
        average_volume: Decimal | None = None,
        average_turnover: Decimal | None = None,
        strategy_quantity: Decimal | None = None,
        sizing_config: PositionSizingConfig = DEFAULT_POSITION_SIZING_CONFIG,
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
        current_quantity = position.quantity if position is not None else Decimal("0")
        current_invested = (
            position.total_invested if position is not None else Decimal("0")
        )
        normalized_action = str(action).upper()
        target_weight = limits.max_symbol_allocation
        sizing = calculate_position_size(
            PositionSizingInput(
                action=normalized_action,
                market=market,
                entry_price=reference_price,
                price_as_of=price_as_of,
                evaluated_at=evaluated_at,
                operating_budget=limits.operating_budget,
                budget_used=usage.budget_used,
                max_symbol_allocation=target_weight,
                current_symbol_invested=current_invested,
                current_holding_quantity=current_quantity,
                risk_per_trade_rate=limits.risk_per_trade_rate,
                regime=regime,
                strategy_stop=strategy_stop,
                strategy_atr=strategy_atr,
                strategy_quantity=strategy_quantity,
                average_volume=average_volume,
                average_turnover=average_turnover,
            ),
            config=sizing_config,
        )
        quantity = sizing.quantity
        if normalized_action == "SELL":
            cash_after = min(
                limits.operating_budget,
                max(Decimal("0"), limits.operating_budget - usage.budget_used)
                + quantity * reference_price,
            )
        else:
            cash_after = max(
                Decimal("0"),
                limits.operating_budget
                - usage.budget_used
                - quantity * reference_price,
            )
        if sizing.actionable:
            caps = ",".join(cap.value for cap in sizing.limiting_caps)
            note = f"Deterministic ATR risk sizing; limitingCaps={caps}."
        else:
            reasons = ",".join(reason.code.value for reason in sizing.zero_reasons)
            note = f"Deterministic position sizing returned zero; reasons={reasons}."
        return PortfolioPlan(
            target_weight=target_weight,
            target_quantity=quantity,
            cash_after=cash_after,
            note=note,
            position_sizing=sizing,
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
        ai_review_status: str | None = None,
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
        daily_loss_limit = limits.max_daily_loss_amount
        daily_target_amount = limits.daily_target_amount
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
        side_count_passed = (
            is_buy
            and usage.buys_today < limits.max_buys_per_day
            and same_symbol_buys < limits.same_symbol_reentry_limit
        ) or (action == "SELL" and usage.sells_today < limits.max_sells_per_day)
        order_count_passed = (
            usage.orders_today < limits.max_orders_per_day and side_count_passed
        )
        checks = [
            HardRiskCheck(
                "DAILY_MAX_LOSS",
                (not is_buy) or usage.realized_loss_today < daily_loss_limit,
                (
                    f"realizedLossToday={usage.realized_loss_today}; "
                    f"limit={daily_loss_limit}"
                ),
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
                    f"sellsToday={usage.sells_today}/{limits.max_sells_per_day}; "
                    f"hardMaxBuys={limits.max_custom_buys_per_day}; "
                    f"hardMaxSells={limits.max_custom_sells_per_day}; "
                    f"hardMaxOrders={limits.max_custom_orders_per_day}; "
                    f"sameSymbolBuys={same_symbol_buys}/{limits.same_symbol_reentry_limit}"
                ),
            ),
            HardRiskCheck(
                # AI 검토는 SHADOW다. 뉴스·공시 등 검증되지 않은 입력을 보는
                # 판단에 실제 주문 veto를 주지 않는다. 값은 근거로만 남기고
                # 이 관문은 결정론적 안전장치가 아니므로 차단하지 않는다.
                "AI_SHADOW",
                True,
                f"shadow; confidence={ai_confidence}; "
                f"shadowFloor={_MIN_AI_CONFIDENCE}"
                + (
                    f"; aiStatus={ai_review_status}"
                    if ai_review_status is not None
                    else ""
                )
                + "; AI는 주문을 차단하지 않습니다.",
            ),
            HardRiskCheck(
                "DAILY_GOAL",
                True,
                (
                    f"realizedPnlToday={usage.realized_pnl_today}; "
                    f"referenceGoal={daily_target_amount}; "
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

    budget = _positive_decimal(
        raw.get("operating_budget", _DEFAULT_OPERATING_BUDGET),
        "operating_budget",
    )
    currency = str(raw.get("currency", "KRW")).upper()
    if currency not in {"KRW", "USD"}:
        raise ValueError("stored AI trading currency is invalid")

    if "risk_level" in raw:
        risk_level = _risk_level(raw["risk_level"], "risk_level")
    else:
        risk_level = _nearest_risk_level(raw, budget)
    preset = _RISK_PRESETS[risk_level]

    target_rate = _stored_percentage(
        raw,
        canonical_key="daily_target_rate_pct",
        legacy_amount_key="conservative_daily_goal",
        budget=budget,
        default=preset.daily_target_rate_pct,
        maximum=Decimal("10"),
    )
    loss_rate = _stored_percentage(
        raw,
        canonical_key="max_daily_loss_rate_pct",
        legacy_amount_key="daily_max_loss",
        budget=budget,
        default=preset.max_daily_loss_rate_pct,
        maximum=Decimal("20"),
    )
    limits = AITradingLimits(
        risk_level=risk_level,
        operating_budget=budget,
        daily_target_rate_pct=target_rate,
        max_daily_loss_rate_pct=loss_rate,
        custom_max_buys_per_day=raw.get("custom_max_buys_per_day"),
        custom_max_sells_per_day=raw.get("custom_max_sells_per_day"),
        kill_switch=_strict_bool(raw.get("kill_switch", False), "kill_switch"),
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


def _risk_level(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value not in _RISK_PRESETS:
        raise ValueError(f"{field} must be between 1 and 5")
    return value


def _percentage(
    value: object,
    field: str,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if result < minimum or result > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _stored_percentage(
    raw: dict[object, object],
    *,
    canonical_key: str,
    legacy_amount_key: str,
    budget: Decimal,
    default: Decimal,
    maximum: Decimal,
) -> Decimal:
    if canonical_key in raw:
        return _percentage(
            raw[canonical_key],
            canonical_key,
            minimum=Decimal("0"),
            maximum=maximum,
        )
    if legacy_amount_key not in raw:
        return default
    amount = _percentage(
        raw[legacy_amount_key],
        legacy_amount_key,
        minimum=Decimal("0"),
        maximum=Decimal("Infinity"),
    )
    return min(maximum, amount * Decimal("100") / budget)


def _nearest_risk_level(raw: dict[object, object], budget: Decimal) -> int:
    components: list[tuple[Decimal, str, Decimal]] = []
    if "max_symbol_allocation" in raw:
        allocation = _percentage(
            raw["max_symbol_allocation"],
            "max_symbol_allocation",
            minimum=Decimal("0.0000001"),
            maximum=Decimal("1"),
        )
        components.append((allocation, "max_symbol_allocation", Decimal("0.05")))
    for key in (
        "max_concurrent_holdings",
        "max_buys_per_day",
        "max_orders_per_day",
        "same_symbol_reentry_limit",
    ):
        if key in raw:
            components.append(
                (Decimal(_nonnegative_int(raw[key], key)), key, Decimal("1"))
            )

    if not components:
        if "conservative_daily_goal" in raw:
            target_rate = (
                _percentage(
                    raw["conservative_daily_goal"],
                    "conservative_daily_goal",
                    minimum=Decimal("0"),
                    maximum=Decimal("Infinity"),
                )
                * Decimal("100")
                / budget
            )
            components.append((target_rate, "daily_target_rate_pct", Decimal("0.1")))
        if "daily_max_loss" in raw:
            loss_rate = (
                _percentage(
                    raw["daily_max_loss"],
                    "daily_max_loss",
                    minimum=Decimal("0"),
                    maximum=Decimal("Infinity"),
                )
                * Decimal("100")
                / budget
            )
            components.append((loss_rate, "max_daily_loss_rate_pct", Decimal("0.5")))

    if not components:
        return _DEFAULT_RISK_LEVEL

    def score(level: int) -> Decimal:
        preset = _RISK_PRESETS[level]
        legacy_limits = _LEGACY_PRESET_DAILY_LIMITS[level]

        def expected(field_name: str) -> object:
            if field_name in legacy_limits:
                return legacy_limits[field_name]
            return getattr(preset, field_name)

        return sum(
            (
                abs(actual - Decimal(str(expected(field_name)))) / scale
                for actual, field_name, scale in components
            ),
            start=Decimal("0"),
        )

    return min(_RISK_PRESETS, key=lambda level: (score(level), level))


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def _optional_daily_limit(
    value: object,
    field: str,
    *,
    maximum: int,
    default: int,
) -> int:
    if value is None:
        if default > maximum:
            raise ValueError(f"default {field} exceeds hard maximum {maximum}")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
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
