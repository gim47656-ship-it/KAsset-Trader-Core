"""PAPER 계좌의 통화별 일중 고점 상태를 BUY 관문으로 활성화한다."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.shadow_high_watermark import (
    ShadowBuyState,
    ShadowEquityValuation,
    ShadowEvidenceStatus,
    ShadowHighWatermarkEvaluation,
    ShadowHighWatermarkThresholds,
    ShadowReductionStage,
    evaluate_shadow_high_watermark,
    load_shadow_high_watermark,
    market_trading_date,
    persist_shadow_high_watermark,
)
from app.extensions.kasset.models import AndroidPaperAccount
from app.models.paper_trading import PaperAccount
from app.services.paper_trading_service import PaperTradingService

logger = logging.getLogger(__name__)

ACCOUNT_STATE_SCHEMA_VERSION = "kasset.account-state.v1"
ACCOUNT_STATE_GATE_CODE = "ACCOUNT_STATE"
STAGED_REDUCTION_MULTIPLIER = Decimal("0.75")
DEFAULT_MAX_VALUATION_AGE = timedelta(minutes=5)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_MARKET_BOOKS: Mapping[str, tuple[Literal["KRX", "US"], str, str]] = {
    "KR": ("KRX", "KRW", "equity_kr"),
    "KRX": ("KRX", "KRW", "equity_kr"),
    "US": ("US", "USD", "equity_us"),
}


class AccountState(StrEnum):
    NORMAL = "NORMAL"
    STAGED_REDUCTION = "STAGED_REDUCTION"
    EXIT_ONLY = "EXIT_ONLY"


@dataclass(frozen=True, slots=True)
class AccountStateThresholds:
    """운영 risk preset의 percentage-point 값을 비율 기준으로 고정한다."""

    staged_profit_ratio: Decimal
    exit_only_profit_ratio: Decimal
    staged_peak_drawdown_ratio: Decimal
    staged_reduction_multiplier: Decimal = STAGED_REDUCTION_MULTIPLIER
    max_valuation_age: timedelta = DEFAULT_MAX_VALUATION_AGE

    def __post_init__(self) -> None:
        for field_name in (
            "staged_profit_ratio",
            "exit_only_profit_ratio",
            "staged_peak_drawdown_ratio",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
                raise ValueError(f"{field_name} must be a nonnegative finite Decimal")
        multiplier = self.staged_reduction_multiplier
        if (
            not isinstance(multiplier, Decimal)
            or not multiplier.is_finite()
            or multiplier <= _ZERO
            or multiplier > _ONE
        ):
            raise ValueError("staged_reduction_multiplier must be a Decimal in (0, 1]")
        if self.max_valuation_age <= timedelta(0):
            raise ValueError("max_valuation_age must be positive")

    @classmethod
    def from_risk_rates(
        cls,
        *,
        daily_target_rate_pct: Decimal,
        max_daily_loss_rate_pct: Decimal,
    ) -> AccountStateThresholds:
        goal_ratio = _rate_ratio(daily_target_rate_pct, "daily_target_rate_pct")
        max_loss_ratio = _rate_ratio(
            max_daily_loss_rate_pct,
            "max_daily_loss_rate_pct",
        )
        return cls(
            staged_profit_ratio=goal_ratio * Decimal("0.5"),
            exit_only_profit_ratio=goal_ratio,
            staged_peak_drawdown_ratio=max_loss_ratio * Decimal("0.5"),
        )

    def as_evidence(self) -> dict[str, str]:
        return {
            "stagedProfitRatio": str(self.staged_profit_ratio),
            "exitOnlyProfitRatio": str(self.exit_only_profit_ratio),
            "stagedPeakDrawdownRatio": str(self.staged_peak_drawdown_ratio),
            "stagedReductionMultiplier": str(self.staged_reduction_multiplier),
        }

    def as_shadow_thresholds(self) -> ShadowHighWatermarkThresholds:
        profit_stages = (
            (
                ShadowReductionStage(
                    "account-state-profit-reduction",
                    self.staged_profit_ratio,
                    self.staged_reduction_multiplier,
                ),
            )
            if self.staged_profit_ratio > _ZERO
            else ()
        )
        drawdown_stages = (
            (
                ShadowReductionStage(
                    "account-state-drawdown-reduction",
                    self.staged_peak_drawdown_ratio,
                    self.staged_reduction_multiplier,
                ),
            )
            if self.staged_peak_drawdown_ratio > _ZERO
            else ()
        )
        return ShadowHighWatermarkThresholds(
            profit_target_stages=profit_stages,
            peak_drawdown_stages=drawdown_stages,
            # 활성 EXIT_ONLY는 목표수익 조건만 사용한다. SHADOW 계산기의 별도
            # 최대손실 EXIT_ONLY는 100% 손실에서만 닿도록 비활성화한다.
            maximum_loss_ratio=_ONE,
            max_valuation_age=self.max_valuation_age,
        )


@dataclass(frozen=True, slots=True)
class AccountStateEvaluation:
    market: Literal["KRX", "US"]
    state: AccountState
    profit_ratio: Decimal | None
    peak_drawdown_ratio: Decimal | None
    multiplier: Decimal
    thresholds: AccountStateThresholds
    unavailable: str | None = None
    persist_failed: str | None = None

    def as_evidence(self) -> dict[str, object]:
        return {
            "schemaVersion": ACCOUNT_STATE_SCHEMA_VERSION,
            "state": self.state.value,
            "profitRatio": (
                str(self.profit_ratio) if self.profit_ratio is not None else None
            ),
            "peakDrawdownRatio": (
                str(self.peak_drawdown_ratio)
                if self.peak_drawdown_ratio is not None
                else None
            ),
            "multiplier": str(self.multiplier),
            "thresholds": self.thresholds.as_evidence(),
            "unavailable": self.unavailable,
            "persistFailed": self.persist_failed,
        }


@dataclass(frozen=True, slots=True)
class AccountStateGateResult:
    code: str
    passed: bool
    reason: str | None
    detail: str
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AccountStateSnapshot:
    thresholds: AccountStateThresholds
    evaluations: tuple[AccountStateEvaluation, ...]

    def for_market(self, market: str) -> AccountStateEvaluation:
        normalized = _normalize_market(market)
        for evaluation in self.evaluations:
            if evaluation.market == normalized:
                return evaluation
        return unavailable_account_state(
            market=normalized,
            thresholds=self.thresholds,
            reason="market_not_evaluated",
        )

    def as_evidence(self) -> dict[str, object]:
        if not self.evaluations:
            evidence = unavailable_account_state(
                market="KRX",
                thresholds=self.thresholds,
                reason="market_scope_unavailable",
            ).as_evidence()
            evidence["representativeMarket"] = None
            return evidence
        priority = {
            AccountState.NORMAL: 0,
            AccountState.STAGED_REDUCTION: 1,
            AccountState.EXIT_ONLY: 2,
        }
        representative = max(
            self.evaluations,
            key=lambda item: (priority[item.state], item.market),
        )
        evidence = representative.as_evidence()
        evidence["representativeMarket"] = representative.market
        if len(self.evaluations) > 1:
            evidence["books"] = {
                item.market: item.as_evidence() for item in self.evaluations
            }
        return evidence


class AccountStateGate:
    """기존 PAPER 평가 소스와 SHADOW HWM 상태를 통화 장부별로 재사용한다."""

    async def evaluate_owner(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        markets: Sequence[str],
        daily_target_rate_pct: Decimal,
        max_daily_loss_rate_pct: Decimal,
        now: datetime,
    ) -> AccountStateSnapshot:
        thresholds = AccountStateThresholds.from_risk_rates(
            daily_target_rate_pct=daily_target_rate_pct,
            max_daily_loss_rate_pct=max_daily_loss_rate_pct,
        )
        normalized_markets = tuple(
            sorted({_normalize_market(item) for item in markets})
        )
        if not normalized_markets:
            return AccountStateSnapshot(thresholds=thresholds, evaluations=())

        try:
            account = await db.scalar(
                select(PaperAccount)
                .join(
                    AndroidPaperAccount,
                    AndroidPaperAccount.paper_account_id == PaperAccount.id,
                )
                .where(AndroidPaperAccount.owner_user_id == owner_user_id)
                .order_by(AndroidPaperAccount.created_at, PaperAccount.id)
                .limit(1)
            )
            if account is None:
                raise LookupError("paper_account_unavailable")
            account_id = int(account.id)
            positions = await PaperTradingService(db).get_positions(account_id)
            book_cash = {
                "KRX": Decimal(account.cash_krw),
                "US": Decimal(account.cash_usd),
            }
        except Exception as exc:  # noqa: BLE001 - 신규 관문은 계산 불가 시 PASS
            reason = _unavailable_reason(exc)
            logger.warning(
                "kasset account state valuation unavailable: owner_user_id=%s reason=%s",
                owner_user_id,
                reason,
                exc_info=True,
            )
            return AccountStateSnapshot(
                thresholds=thresholds,
                evaluations=tuple(
                    unavailable_account_state(
                        market=market,
                        thresholds=thresholds,
                        reason=reason,
                    )
                    for market in normalized_markets
                ),
            )

        evaluations: list[AccountStateEvaluation] = []
        for market in normalized_markets:
            try:
                valuation = _book_valuation(
                    account_id=account_id,
                    cash=book_cash[market],
                    positions=positions,
                    owner_user_id=owner_user_id,
                    market=market,
                    now=now,
                )
                previous = await load_shadow_high_watermark(
                    db,
                    owner_user_id=owner_user_id,
                    account_key=valuation.account_key,
                    market=market,
                    trading_date=market_trading_date(market, valuation.valuation_at),
                )
                shadow = evaluate_shadow_high_watermark(
                    valuation,
                    thresholds=thresholds.as_shadow_thresholds(),
                    previous=previous,
                )
                evaluation = account_state_from_shadow(shadow, thresholds=thresholds)
            except Exception as exc:  # noqa: BLE001 - 신규 관문은 계산 불가 시 PASS
                reason = _unavailable_reason(exc)
                logger.warning(
                    "kasset account state calculation unavailable: "
                    "owner_user_id=%s market=%s reason=%s",
                    owner_user_id,
                    market,
                    reason,
                    exc_info=True,
                )
                evaluations.append(
                    unavailable_account_state(
                        market=market,
                        thresholds=thresholds,
                        reason=reason,
                    )
                )
                continue

            if evaluation.unavailable is not None:
                logger.warning(
                    "kasset account state calculation unavailable: "
                    "owner_user_id=%s market=%s reason=%s",
                    owner_user_id,
                    market,
                    evaluation.unavailable,
                )
            persist_failed: str | None = None
            if shadow.persistence_required:
                try:
                    async with db.begin_nested():
                        await persist_shadow_high_watermark(db, shadow)
                except Exception as exc:  # noqa: BLE001 - 관측 저장 실패는 판정을 바꾸지 않음
                    persist_failed = _unavailable_reason(exc)
                    logger.warning(
                        "kasset account state persistence failed: "
                        "owner_user_id=%s market=%s reason=%s",
                        owner_user_id,
                        market,
                        persist_failed,
                        exc_info=True,
                    )
            evaluations.append(replace(evaluation, persist_failed=persist_failed))
        return AccountStateSnapshot(
            thresholds=thresholds,
            evaluations=tuple(evaluations),
        )


def account_state_from_shadow(
    shadow: ShadowHighWatermarkEvaluation,
    *,
    thresholds: AccountStateThresholds,
) -> AccountStateEvaluation:
    """SHADOW 상태 계산을 운영 BUY 상태로 투영하되 계산 실패는 NORMAL로 연다."""

    market = _normalize_market(str(shadow.valuation.market))
    if (
        shadow.status is not ShadowEvidenceStatus.VALID
        or shadow.profit_ratio is None
        or shadow.peak_drawdown_ratio is None
    ):
        reason = (
            shadow.reasons[0].code.value
            if shadow.reasons
            else "high_watermark_unavailable"
        )
        return unavailable_account_state(
            market=market,
            thresholds=thresholds,
            reason=reason,
        )

    if shadow.profit_ratio >= thresholds.exit_only_profit_ratio:
        state = AccountState.EXIT_ONLY
        multiplier = _ZERO
    elif (
        shadow.profit_ratio >= thresholds.staged_profit_ratio
        or shadow.peak_drawdown_ratio >= thresholds.staged_peak_drawdown_ratio
        or shadow.buy_state is ShadowBuyState.STAGED_REDUCTION
    ):
        state = AccountState.STAGED_REDUCTION
        multiplier = thresholds.staged_reduction_multiplier
    else:
        state = AccountState.NORMAL
        multiplier = _ONE
    return AccountStateEvaluation(
        market=market,
        state=state,
        profit_ratio=shadow.profit_ratio,
        peak_drawdown_ratio=shadow.peak_drawdown_ratio,
        multiplier=multiplier,
        thresholds=thresholds,
    )


def evaluate_account_state_gate(
    action: str,
    evaluation: AccountStateEvaluation | None,
) -> AccountStateGateResult:
    normalized_action = str(action).upper()
    if evaluation is None:
        detail = "state=NORMAL; unavailable=account_state_not_supplied"
        return AccountStateGateResult(
            code=ACCOUNT_STATE_GATE_CODE,
            passed=True,
            reason="unavailable",
            detail=detail,
            evidence={
                "schemaVersion": ACCOUNT_STATE_SCHEMA_VERSION,
                "state": AccountState.NORMAL.value,
                "profitRatio": None,
                "peakDrawdownRatio": None,
                "multiplier": str(_ONE),
                "thresholds": {},
                "unavailable": "account_state_not_supplied",
                "persistFailed": None,
            },
        )
    if normalized_action == "SELL":
        passed = True
        reason = None
    elif evaluation.unavailable is not None:
        passed = True
        reason = "unavailable"
    elif evaluation.state is AccountState.EXIT_ONLY:
        passed = False
        reason = "exit_only"
    else:
        passed = True
        reason = None
    detail = (
        f"state={evaluation.state.value}; profitRatio={evaluation.profit_ratio}; "
        f"peakDrawdownRatio={evaluation.peak_drawdown_ratio}; "
        f"multiplier={evaluation.multiplier}; "
        f"reason={reason or 'clear'}; unavailable={evaluation.unavailable}; "
        f"persistFailed={evaluation.persist_failed}"
    )
    return AccountStateGateResult(
        code=ACCOUNT_STATE_GATE_CODE,
        passed=passed,
        reason=reason,
        detail=detail,
        evidence=evaluation.as_evidence(),
    )


def unavailable_account_state(
    *,
    market: Literal["KRX", "US"],
    thresholds: AccountStateThresholds,
    reason: str,
) -> AccountStateEvaluation:
    return AccountStateEvaluation(
        market=market,
        state=AccountState.NORMAL,
        profit_ratio=None,
        peak_drawdown_ratio=None,
        multiplier=_ONE,
        thresholds=thresholds,
        unavailable=reason,
    )


def _book_valuation(
    *,
    account_id: int,
    cash: Decimal,
    positions: Sequence[Mapping[str, object]],
    owner_user_id: int,
    market: Literal["KRX", "US"],
    now: datetime,
) -> ShadowEquityValuation:
    _, currency, instrument_type = _MARKET_BOOKS[market]
    if not cash.is_finite() or cash < _ZERO:
        raise ValueError(f"{currency.lower()}_cash_unavailable")
    equity = cash
    for position in positions:
        if str(position.get("instrument_type")) != instrument_type:
            continue
        evaluation_amount = position.get("evaluation_amount")
        if evaluation_amount is None:
            raise ValueError(
                f"position_valuation_unavailable:{position.get('symbol', 'unknown')}"
            )
        if position.get("quote_is_stale") is True:
            raise ValueError(
                f"position_quote_stale:{position.get('symbol', 'unknown')}"
            )
        amount = Decimal(str(evaluation_amount))
        if not amount.is_finite() or amount < _ZERO:
            raise ValueError(
                f"position_valuation_invalid:{position.get('symbol', 'unknown')}"
            )
        equity += amount
    if not equity.is_finite() or equity <= _ZERO:
        raise ValueError("book_equity_unavailable")
    if account_id < 1:
        raise ValueError("paper_account_unavailable")
    return ShadowEquityValuation(
        owner_user_id=owner_user_id,
        account_key=f"PAPER-{account_id}",
        market=market,
        equity=equity,
        valuation_at=now,
        evaluated_at=now,
        valuation_source="paper-trading-service",
    )


def _normalize_market(value: str) -> Literal["KRX", "US"]:
    normalized = str(value).strip().upper()
    book = _MARKET_BOOKS.get(normalized)
    if book is None:
        raise ValueError(f"unsupported account state market: {value}")
    return book[0]


def _rate_ratio(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
        raise ValueError(f"{field_name} must be a nonnegative finite Decimal")
    return value / _HUNDRED


def _unavailable_reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text and len(text) <= 128 else type(exc).__name__


__all__ = [
    "ACCOUNT_STATE_GATE_CODE",
    "ACCOUNT_STATE_SCHEMA_VERSION",
    "DEFAULT_MAX_VALUATION_AGE",
    "STAGED_REDUCTION_MULTIPLIER",
    "AccountState",
    "AccountStateEvaluation",
    "AccountStateGate",
    "AccountStateGateResult",
    "AccountStateSnapshot",
    "AccountStateThresholds",
    "account_state_from_shadow",
    "evaluate_account_state_gate",
    "unavailable_account_state",
]
