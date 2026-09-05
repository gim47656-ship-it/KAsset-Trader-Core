"""Deterministic ATR-risk position sizing for KAsset PAPER automation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, DecimalException
from enum import StrEnum
from typing import Literal

from app.extensions.kasset.automation.regime import MarketRegime

_ZERO = Decimal("0")
_ONE = Decimal("1")


class PositionSizingZeroCode(StrEnum):
    """Stable fail-closed reason codes for a zero-quantity result."""

    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_PRICE_TIMESTAMP = "INVALID_PRICE_TIMESTAMP"
    FUTURE_PRICE = "FUTURE_PRICE"
    STALE_PRICE = "STALE_PRICE"
    MISSING_STOP = "MISSING_STOP"
    INVALID_STOP = "INVALID_STOP"
    INVERTED_STOP = "INVERTED_STOP"
    MISSING_ATR = "MISSING_ATR"
    NONPOSITIVE_ATR = "NONPOSITIVE_ATR"
    INVALID_REGIME = "INVALID_REGIME"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    INVALID_RISK_POLICY = "INVALID_RISK_POLICY"
    ZERO_RISK_BUDGET = "ZERO_RISK_BUDGET"
    INVALID_BUDGET = "INVALID_BUDGET"
    ZERO_BUDGET = "ZERO_BUDGET"
    INVALID_PORTFOLIO_STATE = "INVALID_PORTFOLIO_STATE"
    ZERO_SYMBOL_ALLOCATION = "ZERO_SYMBOL_ALLOCATION"
    MISSING_LIQUIDITY = "MISSING_LIQUIDITY"
    INVALID_LIQUIDITY = "INVALID_LIQUIDITY"
    NONFINITE_QUANTITY = "NONFINITE_QUANTITY"
    INVALID_SELL_QUANTITY = "INVALID_SELL_QUANTITY"
    ZERO_HOLDING = "ZERO_HOLDING"
    BELOW_MARKET_LOT = "BELOW_MARKET_LOT"


class PositionSizeCapCode(StrEnum):
    RISK_BUDGET = "RISK_BUDGET"
    SYMBOL_ALLOCATION = "SYMBOL_ALLOCATION"
    OWNER_BUDGET = "OWNER_BUDGET"
    AVERAGE_VOLUME = "AVERAGE_VOLUME"
    AVERAGE_TURNOVER = "AVERAGE_TURNOVER"
    PAPER_HOLDING = "PAPER_HOLDING"
    STRATEGY_SELL = "STRATEGY_SELL"


@dataclass(frozen=True, slots=True)
class PositionSizingConfig:
    """Central policy parameters that are not supplied by an AI response."""

    max_price_age: timedelta = timedelta(days=4)
    krx_lot_size: Decimal = Decimal("1")
    us_lot_size: Decimal = Decimal("0.0001")
    max_average_volume_participation: Decimal = Decimal("0.01")
    max_average_turnover_participation: Decimal = Decimal("0.01")
    bull_risk_multiplier: Decimal = Decimal("1")
    bear_risk_multiplier: Decimal = Decimal("0")
    sideways_risk_multiplier: Decimal = Decimal("0.75")
    volatile_risk_multiplier: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        if self.max_price_age <= timedelta(0):
            raise ValueError("max_price_age must be positive")
        for field_name in (
            "krx_lot_size",
            "us_lot_size",
            "max_average_volume_participation",
            "max_average_turnover_participation",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be a positive finite Decimal")
        for field_name in (
            "bull_risk_multiplier",
            "bear_risk_multiplier",
            "sideways_risk_multiplier",
            "volatile_risk_multiplier",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative finite Decimal")
        for field_name in (
            "max_average_volume_participation",
            "max_average_turnover_participation",
        ):
            if getattr(self, field_name) > _ONE:
                raise ValueError(f"{field_name} must not exceed 1")

    def lot_size(self, market: Literal["KRX", "US"]) -> Decimal:
        return self.krx_lot_size if market == "KRX" else self.us_lot_size

    def regime_multiplier(self, regime: MarketRegime) -> Decimal:
        if regime == MarketRegime.BULL:
            return self.bull_risk_multiplier
        if regime == MarketRegime.BEAR:
            return self.bear_risk_multiplier
        if regime == MarketRegime.VOLATILE:
            return self.volatile_risk_multiplier
        return self.sideways_risk_multiplier


DEFAULT_POSITION_SIZING_CONFIG = PositionSizingConfig()


@dataclass(frozen=True, slots=True)
class PositionSizingInput:
    """Trusted strategy, market, and owner state consumed by the pure sizer.

    The contract deliberately has no AI quantity, stop, ATR, or risk fields. BUY
    stop and ATR values must come from the deterministic strategy path.
    """

    action: Literal["BUY", "SELL"] | str
    market: Literal["KRX", "US"] | str
    entry_price: Decimal
    price_as_of: datetime | None
    evaluated_at: datetime | None
    operating_budget: Decimal = _ZERO
    budget_used: Decimal = _ZERO
    max_symbol_allocation: Decimal = _ZERO
    current_symbol_invested: Decimal = _ZERO
    current_holding_quantity: Decimal = _ZERO
    risk_per_trade_rate: Decimal = _ZERO
    regime: MarketRegime | str = MarketRegime.SIDEWAYS
    strategy_stop: Decimal | None = None
    strategy_atr: Decimal | None = None
    strategy_quantity: Decimal | None = None
    average_volume: Decimal | None = None
    average_turnover: Decimal | None = None
    account_state_multiplier: Decimal = _ONE

    def __post_init__(self) -> None:
        multiplier = self.account_state_multiplier
        if (
            not isinstance(multiplier, Decimal)
            or not multiplier.is_finite()
            or multiplier < _ZERO
            or multiplier > _ONE
        ):
            raise ValueError(
                "account_state_multiplier must be a Decimal between 0 and 1"
            )


@dataclass(frozen=True, slots=True)
class PositionSizingReason:
    code: PositionSizingZeroCode
    field: str
    detail: str

    def as_evidence(self) -> dict[str, str]:
        return {"code": self.code.value, "field": self.field, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PositionSizeCap:
    code: PositionSizeCapCode
    quantity: Decimal

    def as_evidence(self) -> dict[str, str]:
        return {"code": self.code.value, "quantity": str(self.quantity)}


@dataclass(frozen=True, slots=True)
class PositionSizingResult:
    action: str
    market: str
    quantity: Decimal
    unrounded_quantity: Decimal
    lot_size: Decimal
    risk_budget: Decimal
    risk_per_unit: Decimal
    risk_per_trade_rate: Decimal
    regime: str | None
    regime_multiplier: Decimal
    caps: tuple[PositionSizeCap, ...]
    limiting_caps: tuple[PositionSizeCapCode, ...]
    zero_reasons: tuple[PositionSizingReason, ...]
    entry_price: Decimal | None = None
    strategy_stop: Decimal | None = None
    strategy_atr: Decimal | None = None
    account_state_multiplier: Decimal = _ONE

    @property
    def actionable(self) -> bool:
        return self.quantity > 0 and not self.zero_reasons

    def as_evidence(self) -> dict[str, object]:
        return {
            "action": self.action,
            "market": self.market,
            "quantity": str(self.quantity),
            "unroundedQuantity": str(self.unrounded_quantity),
            "lotSize": str(self.lot_size),
            "entryPrice": (
                str(self.entry_price) if self.entry_price is not None else None
            ),
            "strategyStop": (
                str(self.strategy_stop) if self.strategy_stop is not None else None
            ),
            "strategyAtr": (
                str(self.strategy_atr) if self.strategy_atr is not None else None
            ),
            "riskBudget": str(self.risk_budget),
            "riskPerUnit": str(self.risk_per_unit),
            "riskPerTradeRate": str(self.risk_per_trade_rate),
            "regime": self.regime,
            "regimeMultiplier": str(self.regime_multiplier),
            "accountStateMultiplier": str(self.account_state_multiplier),
            "caps": [cap.as_evidence() for cap in self.caps],
            "limitingCaps": [cap.value for cap in self.limiting_caps],
            "zeroReasons": [reason.as_evidence() for reason in self.zero_reasons],
        }


def calculate_position_size(
    request: PositionSizingInput,
    *,
    config: PositionSizingConfig = DEFAULT_POSITION_SIZING_CONFIG,
) -> PositionSizingResult:
    """Return a deterministic, market-rounded PAPER quantity and its evidence."""

    action = str(request.action).upper()
    market = str(request.market).upper()
    reasons = _base_reasons(request, action=action, market=market, config=config)
    lot_size = (
        config.lot_size(market) if market in {"KRX", "US"} else _ZERO  # type: ignore[arg-type]
    )
    if action == "BUY":
        reasons.extend(_buy_reasons(request, config=config))
    elif action == "SELL":
        reasons.extend(_sell_reasons(request))
    if reasons:
        normalized_regime = _normalize_regime(request.regime)
        return _zero_result(
            action=action,
            market=market,
            lot_size=lot_size,
            reasons=tuple(reasons),
            risk_per_trade_rate=(
                request.risk_per_trade_rate
                if _is_finite_decimal(request.risk_per_trade_rate)
                else _ZERO
            ),
            regime=normalized_regime.name if normalized_regime is not None else None,
            regime_multiplier=(
                config.regime_multiplier(normalized_regime)
                if normalized_regime is not None
                else _ZERO
            ),
            account_state_multiplier=request.account_state_multiplier,
            entry_price=(
                request.entry_price
                if isinstance(request.entry_price, Decimal)
                else None
            ),
            strategy_stop=(
                request.strategy_stop
                if isinstance(request.strategy_stop, Decimal)
                else None
            ),
            strategy_atr=(
                request.strategy_atr
                if isinstance(request.strategy_atr, Decimal)
                else None
            ),
        )
    if action == "BUY":
        return _calculate_buy(request, market=market, lot_size=lot_size, config=config)
    return _calculate_sell(request, market=market, lot_size=lot_size)


def _base_reasons(
    request: PositionSizingInput,
    *,
    action: str,
    market: str,
    config: PositionSizingConfig,
) -> list[PositionSizingReason]:
    reasons: list[PositionSizingReason] = []
    if action not in {"BUY", "SELL"}:
        reasons.append(
            _reason(
                PositionSizingZeroCode.UNSUPPORTED_ACTION,
                "action",
                "action must be BUY or SELL",
            )
        )
    if market not in {"KRX", "US"}:
        reasons.append(
            _reason(
                PositionSizingZeroCode.UNSUPPORTED_MARKET,
                "market",
                "market must be KRX or US",
            )
        )
    if not _is_finite_decimal(request.entry_price) or request.entry_price <= 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_PRICE,
                "entry_price",
                "entry price must be a positive finite Decimal",
            )
        )
    price_time = _aware_datetime(request.price_as_of)
    evaluation_time = _aware_datetime(request.evaluated_at)
    if price_time is None or evaluation_time is None:
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_PRICE_TIMESTAMP,
                "price_as_of",
                "price_as_of and evaluated_at must be timezone-aware datetimes",
            )
        )
    elif price_time > evaluation_time:
        reasons.append(
            _reason(
                PositionSizingZeroCode.FUTURE_PRICE,
                "price_as_of",
                "price timestamp is later than evaluation time",
            )
        )
    elif evaluation_time - price_time >= config.max_price_age:
        reasons.append(
            _reason(
                PositionSizingZeroCode.STALE_PRICE,
                "price_as_of",
                f"price age exceeds {config.max_price_age}",
            )
        )
    return reasons


def _buy_reasons(
    request: PositionSizingInput,
    *,
    config: PositionSizingConfig,
) -> list[PositionSizingReason]:
    reasons: list[PositionSizingReason] = []
    if request.strategy_stop is None:
        reasons.append(
            _reason(
                PositionSizingZeroCode.MISSING_STOP,
                "strategy_stop",
                "deterministic strategy stop is required for BUY",
            )
        )
    elif not _is_finite_decimal(request.strategy_stop) or request.strategy_stop <= 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_STOP,
                "strategy_stop",
                "strategy stop must be a positive finite Decimal",
            )
        )
    elif (
        _is_finite_decimal(request.entry_price)
        and request.strategy_stop >= request.entry_price
    ):
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVERTED_STOP,
                "strategy_stop",
                "BUY stop must be below entry price",
            )
        )
    if request.strategy_atr is None:
        reasons.append(
            _reason(
                PositionSizingZeroCode.MISSING_ATR,
                "strategy_atr",
                "deterministic strategy ATR is required for BUY",
            )
        )
    elif not _is_finite_decimal(request.strategy_atr) or request.strategy_atr <= 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.NONPOSITIVE_ATR,
                "strategy_atr",
                "strategy ATR must be a positive finite Decimal",
            )
        )
    if not _is_finite_decimal(request.operating_budget):
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_BUDGET,
                "operating_budget",
                "operating budget must be a finite Decimal",
            )
        )
    elif request.operating_budget <= 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.ZERO_BUDGET,
                "operating_budget",
                "operating budget must be positive",
            )
        )
    if not _is_finite_decimal(request.budget_used) or request.budget_used < 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_PORTFOLIO_STATE,
                "budget_used",
                "budget used must be a nonnegative finite Decimal",
            )
        )
    if (
        not _is_finite_decimal(request.max_symbol_allocation)
        or request.max_symbol_allocation < 0
        or request.max_symbol_allocation > _ONE
    ):
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_RISK_POLICY,
                "max_symbol_allocation",
                "symbol allocation must be a finite Decimal between 0 and 1",
            )
        )
    elif request.max_symbol_allocation == 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.ZERO_SYMBOL_ALLOCATION,
                "max_symbol_allocation",
                "symbol allocation is zero",
            )
        )
    if (
        not _is_finite_decimal(request.current_symbol_invested)
        or request.current_symbol_invested < 0
    ):
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_PORTFOLIO_STATE,
                "current_symbol_invested",
                "current symbol investment must be a nonnegative finite Decimal",
            )
        )
    if (
        not _is_finite_decimal(request.risk_per_trade_rate)
        or request.risk_per_trade_rate <= 0
        or request.risk_per_trade_rate > _ONE
    ):
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_RISK_POLICY,
                "risk_per_trade_rate",
                "risk per trade rate must be a finite Decimal between 0 and 1",
            )
        )
    regime = _normalize_regime(request.regime)
    if regime is None:
        reasons.append(
            _reason(
                PositionSizingZeroCode.INVALID_REGIME,
                "regime",
                "regime is not recognized",
            )
        )
    elif config.regime_multiplier(regime) == 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.REGIME_BLOCKED,
                "regime",
                "new BUY positions are blocked by the regime risk policy",
            )
        )
    for field_name in ("average_volume", "average_turnover"):
        value = getattr(request, field_name)
        if value is None:
            reasons.append(
                _reason(
                    PositionSizingZeroCode.MISSING_LIQUIDITY,
                    field_name,
                    f"{field_name} is required to enforce participation ceilings",
                )
            )
        elif not _is_finite_decimal(value) or value <= 0:
            reasons.append(
                _reason(
                    PositionSizingZeroCode.INVALID_LIQUIDITY,
                    field_name,
                    f"{field_name} must be a positive finite Decimal",
                )
            )
    return reasons


def _sell_reasons(request: PositionSizingInput) -> list[PositionSizingReason]:
    reasons: list[PositionSizingReason] = []
    holding = request.current_holding_quantity
    if not _is_finite_decimal(holding):
        reasons.append(
            _reason(
                PositionSizingZeroCode.NONFINITE_QUANTITY,
                "current_holding_quantity",
                "PAPER holding quantity must be finite",
            )
        )
    elif holding <= 0:
        reasons.append(
            _reason(
                PositionSizingZeroCode.ZERO_HOLDING,
                "current_holding_quantity",
                "there is no positive PAPER holding to sell",
            )
        )
    requested = request.strategy_quantity
    if requested is not None:
        if not _is_finite_decimal(requested):
            reasons.append(
                _reason(
                    PositionSizingZeroCode.NONFINITE_QUANTITY,
                    "strategy_quantity",
                    "strategy SELL quantity must be finite",
                )
            )
        elif requested <= 0:
            reasons.append(
                _reason(
                    PositionSizingZeroCode.INVALID_SELL_QUANTITY,
                    "strategy_quantity",
                    "strategy SELL quantity must be positive",
                )
            )
    return reasons


def _calculate_buy(
    request: PositionSizingInput,
    *,
    market: str,
    lot_size: Decimal,
    config: PositionSizingConfig,
) -> PositionSizingResult:
    entry = request.entry_price
    stop = request.strategy_stop
    assert stop is not None
    regime = _normalize_regime(request.regime)
    assert regime is not None
    regime_multiplier = config.regime_multiplier(regime)
    risk_per_unit = entry - stop
    try:
        risk_budget = (
            request.operating_budget * request.risk_per_trade_rate * regime_multiplier
        )
        remaining_symbol_notional = max(
            _ZERO,
            request.operating_budget * request.max_symbol_allocation
            - request.current_symbol_invested,
        )
        remaining_owner_notional = max(
            _ZERO, request.operating_budget - request.budget_used
        )
        caps = [
            PositionSizeCap(
                PositionSizeCapCode.RISK_BUDGET, risk_budget / risk_per_unit
            ),
            PositionSizeCap(
                PositionSizeCapCode.SYMBOL_ALLOCATION,
                remaining_symbol_notional / entry,
            ),
            PositionSizeCap(
                PositionSizeCapCode.OWNER_BUDGET,
                remaining_owner_notional / entry,
            ),
        ]
        assert request.average_volume is not None
        assert request.average_turnover is not None
        caps.extend(
            (
                PositionSizeCap(
                    PositionSizeCapCode.AVERAGE_VOLUME,
                    request.average_volume * config.max_average_volume_participation,
                ),
                PositionSizeCap(
                    PositionSizeCapCode.AVERAGE_TURNOVER,
                    request.average_turnover
                    * config.max_average_turnover_participation
                    / entry,
                ),
            )
        )
        base_unrounded = min(cap.quantity for cap in caps)
        unrounded = base_unrounded * request.account_state_multiplier
    except DecimalException:
        return _zero_result(
            action="BUY",
            market=market,
            lot_size=lot_size,
            reasons=(
                _reason(
                    PositionSizingZeroCode.NONFINITE_QUANTITY,
                    "quantity",
                    "position sizing arithmetic did not produce a finite quantity",
                ),
            ),
        )
    if risk_budget <= 0:
        return _zero_with_calculation(
            action="BUY",
            market=market,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            unrounded=unrounded,
            code=PositionSizingZeroCode.ZERO_RISK_BUDGET,
            field="risk_budget",
            detail="risk budget is zero",
        )
    if remaining_owner_notional <= 0:
        return _zero_with_calculation(
            action="BUY",
            market=market,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            unrounded=unrounded,
            code=PositionSizingZeroCode.ZERO_BUDGET,
            field="budget_used",
            detail="owner operating budget is fully used",
        )
    if remaining_symbol_notional <= 0:
        return _zero_with_calculation(
            action="BUY",
            market=market,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            unrounded=unrounded,
            code=PositionSizingZeroCode.ZERO_SYMBOL_ALLOCATION,
            field="current_symbol_invested",
            detail="symbol allocation is fully used",
        )
    if not unrounded.is_finite():
        return _zero_with_calculation(
            action="BUY",
            market=market,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            unrounded=_ZERO,
            code=PositionSizingZeroCode.NONFINITE_QUANTITY,
            field="quantity",
            detail="position sizing cap is not finite",
        )
    quantity = _round_to_lot(unrounded, lot_size)
    if quantity is None:
        return _zero_with_calculation(
            action="BUY",
            market=market,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            unrounded=unrounded,
            code=PositionSizingZeroCode.NONFINITE_QUANTITY,
            field="quantity",
            detail="market lot rounding did not produce a finite quantity",
        )
    limiting = tuple(cap.code for cap in caps if cap.quantity == base_unrounded)
    if quantity <= 0:
        return PositionSizingResult(
            action="BUY",
            market=market,
            quantity=_ZERO,
            unrounded_quantity=unrounded,
            lot_size=lot_size,
            risk_budget=risk_budget,
            risk_per_unit=risk_per_unit,
            risk_per_trade_rate=request.risk_per_trade_rate,
            regime=regime.name,
            regime_multiplier=regime_multiplier,
            account_state_multiplier=request.account_state_multiplier,
            caps=tuple(caps),
            limiting_caps=limiting,
            zero_reasons=(
                _reason(
                    PositionSizingZeroCode.BELOW_MARKET_LOT,
                    "quantity",
                    "capped quantity is below the market lot size",
                ),
            ),
            entry_price=entry,
            strategy_stop=stop,
            strategy_atr=request.strategy_atr,
        )
    return PositionSizingResult(
        action="BUY",
        market=market,
        quantity=quantity,
        unrounded_quantity=unrounded,
        lot_size=lot_size,
        risk_budget=risk_budget,
        risk_per_unit=risk_per_unit,
        risk_per_trade_rate=request.risk_per_trade_rate,
        regime=regime.name,
        regime_multiplier=regime_multiplier,
        account_state_multiplier=request.account_state_multiplier,
        caps=tuple(caps),
        limiting_caps=limiting,
        zero_reasons=(),
        entry_price=entry,
        strategy_stop=stop,
        strategy_atr=request.strategy_atr,
    )


def _calculate_sell(
    request: PositionSizingInput,
    *,
    market: str,
    lot_size: Decimal,
) -> PositionSizingResult:
    requested = request.strategy_quantity or request.current_holding_quantity
    caps = (
        PositionSizeCap(
            PositionSizeCapCode.PAPER_HOLDING, request.current_holding_quantity
        ),
        PositionSizeCap(PositionSizeCapCode.STRATEGY_SELL, requested),
    )
    unrounded = min(cap.quantity for cap in caps)
    quantity = _round_to_lot(unrounded, lot_size)
    if quantity is None:
        return _zero_result(
            action="SELL",
            market=market,
            lot_size=lot_size,
            reasons=(
                _reason(
                    PositionSizingZeroCode.NONFINITE_QUANTITY,
                    "quantity",
                    "market lot rounding did not produce a finite quantity",
                ),
            ),
        )
    limiting = tuple(cap.code for cap in caps if cap.quantity == unrounded)
    if quantity <= 0:
        return PositionSizingResult(
            action="SELL",
            market=market,
            quantity=_ZERO,
            unrounded_quantity=unrounded,
            lot_size=lot_size,
            risk_budget=_ZERO,
            risk_per_unit=_ZERO,
            risk_per_trade_rate=_ZERO,
            regime=None,
            regime_multiplier=_ZERO,
            caps=caps,
            limiting_caps=limiting,
            zero_reasons=(
                _reason(
                    PositionSizingZeroCode.BELOW_MARKET_LOT,
                    "quantity",
                    "capped quantity is below the market lot size",
                ),
            ),
            entry_price=request.entry_price,
        )
    return PositionSizingResult(
        action="SELL",
        market=market,
        quantity=quantity,
        unrounded_quantity=unrounded,
        lot_size=lot_size,
        risk_budget=_ZERO,
        risk_per_unit=_ZERO,
        risk_per_trade_rate=_ZERO,
        regime=None,
        regime_multiplier=_ZERO,
        caps=caps,
        limiting_caps=limiting,
        zero_reasons=(),
        entry_price=request.entry_price,
    )


def _zero_with_calculation(
    *,
    action: str,
    market: str,
    lot_size: Decimal,
    risk_budget: Decimal,
    risk_per_unit: Decimal,
    risk_per_trade_rate: Decimal,
    regime: str,
    regime_multiplier: Decimal,
    caps: tuple[PositionSizeCap, ...],
    unrounded: Decimal,
    code: PositionSizingZeroCode,
    field: str,
    detail: str,
    account_state_multiplier: Decimal = _ONE,
) -> PositionSizingResult:
    return PositionSizingResult(
        action=action,
        market=market,
        quantity=_ZERO,
        unrounded_quantity=unrounded,
        lot_size=lot_size,
        risk_budget=risk_budget,
        risk_per_unit=risk_per_unit,
        risk_per_trade_rate=risk_per_trade_rate,
        regime=regime,
        regime_multiplier=regime_multiplier,
        account_state_multiplier=account_state_multiplier,
        caps=caps,
        limiting_caps=tuple(cap.code for cap in caps if cap.quantity == unrounded),
        zero_reasons=(_reason(code, field, detail),),
    )


def _zero_result(
    *,
    action: str,
    market: str,
    lot_size: Decimal,
    reasons: tuple[PositionSizingReason, ...],
    risk_per_trade_rate: Decimal = _ZERO,
    regime: str | None = None,
    regime_multiplier: Decimal = _ZERO,
    entry_price: Decimal | None = None,
    strategy_stop: Decimal | None = None,
    strategy_atr: Decimal | None = None,
    account_state_multiplier: Decimal = _ONE,
) -> PositionSizingResult:
    return PositionSizingResult(
        action=action,
        market=market,
        quantity=_ZERO,
        unrounded_quantity=_ZERO,
        lot_size=lot_size,
        risk_budget=_ZERO,
        risk_per_unit=_ZERO,
        risk_per_trade_rate=risk_per_trade_rate,
        regime=regime,
        regime_multiplier=regime_multiplier,
        account_state_multiplier=account_state_multiplier,
        caps=(),
        limiting_caps=(),
        zero_reasons=reasons,
        entry_price=entry_price,
        strategy_stop=strategy_stop,
        strategy_atr=strategy_atr,
    )


def _round_to_lot(quantity: Decimal, lot_size: Decimal) -> Decimal | None:
    try:
        lots = (quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = lots * lot_size
    except DecimalException:
        return None
    return rounded if rounded.is_finite() else None


def _normalize_regime(value: object) -> MarketRegime | None:
    if isinstance(value, MarketRegime):
        return value
    normalized = str(value).strip().upper()
    if normalized in {"BULL", "TRENDING_UP"}:
        return MarketRegime.BULL
    if normalized in {"BEAR", "TRENDING_DOWN"}:
        return MarketRegime.BEAR
    if normalized in {"SIDEWAYS", "RANGING"}:
        return MarketRegime.SIDEWAYS
    if normalized == "VOLATILE":
        return MarketRegime.VOLATILE
    return None


def _is_finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _reason(
    code: PositionSizingZeroCode,
    field: str,
    detail: str,
) -> PositionSizingReason:
    return PositionSizingReason(code=code, field=field, detail=detail)


__all__ = [
    "DEFAULT_POSITION_SIZING_CONFIG",
    "PositionSizeCap",
    "PositionSizeCapCode",
    "PositionSizingConfig",
    "PositionSizingInput",
    "PositionSizingReason",
    "PositionSizingResult",
    "PositionSizingZeroCode",
    "calculate_position_size",
]
