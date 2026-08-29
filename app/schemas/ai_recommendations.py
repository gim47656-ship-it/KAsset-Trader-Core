"""Android transport schemas for persisted AI recommendation review."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from app.models.ai_recommendations import (
    RecommendationAction,
    RecommendationDecision,
    RecommendationMarket,
    RecommendationStatusGroup,
    TerminalRecommendationDecision,
)

DecimalText = Annotated[str, Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]
_DECIMAL_TEXT = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")

_STRATEGY_LABELS = {
    "MOMENTUM": "모멘텀",
    "MEAN_REVERSION": "평균회귀",
    "BREAKOUT": "돌파",
    "VOLATILITY_TREND": "변동성추세",
}
_ACTION_LABELS = {
    "BUY": "매수",
    "SELL": "매도",
    "HOLD": "관망",
}
_LEGACY_VOTE_PREFIX = "Deterministic strategy votes:"


def _localized_strategy_votes(detail: dict[str, Any]) -> str | None:
    raw_votes = detail.get("strategyVotes")
    if not isinstance(raw_votes, list):
        return None
    labels: list[str] = []
    for raw_vote in raw_votes:
        if not isinstance(raw_vote, dict):
            continue
        strategy_key = str(raw_vote.get("strategy") or "").strip().upper()
        action_key = str(raw_vote.get("vote") or "").strip().upper()
        strategy = _STRATEGY_LABELS.get(strategy_key)
        action = _ACTION_LABELS.get(action_key)
        if strategy is not None and action is not None:
            labels.append(f"{strategy}={action}")
    if not labels:
        return None
    return f"전략 투표 결과는 {', '.join(labels)}입니다."


def _localized_rationale(
    rationale: list[object],
    detail: dict[str, Any],
) -> list[str]:
    vote_summary = _localized_strategy_votes(detail)
    localized: list[str] = []
    for raw_item in rationale:
        item = str(raw_item).strip()
        if not item:
            continue
        if item.startswith(_LEGACY_VOTE_PREFIX) and vote_summary is not None:
            localized.append(vote_summary)
        else:
            localized.append(item)
    return localized


def _validate_decimal_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise ValueError("must be a plain decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("must be a valid decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("must be a finite decimal string")
    return value


def _validate_aware_timestamp(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamp must include a timezone")
    return value


def _serialize_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


class RecommendationEvidence(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    title: str | None = None
    source: str | None = None
    published_at: datetime | None = Field(default=None, alias="publishedAt")

    _published_at_timezone = field_validator("published_at")(_validate_aware_timestamp)

    @field_serializer("published_at", when_used="json")
    def serialize_published_at(self, value: datetime | None) -> str | None:
        return _serialize_timestamp(value)


class StrategyVoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    vote: Literal["BUY", "SELL", "HOLD"]
    weight: DecimalText
    score: DecimalText


class RecommendationEventEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["NEWS", "DISCLOSURE"]
    title: str
    source: str | None = None
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    summary: str | None = None
    url: str | None = None

    _published_at_timezone = field_validator("published_at")(_validate_aware_timestamp)

    @field_serializer("published_at", when_used="json")
    def serialize_published_at(self, value: datetime | None) -> str | None:
        return _serialize_timestamp(value)


class RecommendationRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: DecimalText
    position: int = Field(ge=1)
    total: int = Field(ge=1)
    note: str


class RecommendationPositionSizeCap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    quantity: DecimalText


class RecommendationPositionSizingReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    field: str
    detail: str


class RecommendationPositionSizing(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: Literal["BUY", "SELL"]
    market: Literal["KRX", "US"]
    quantity: DecimalText
    unrounded_quantity: DecimalText = Field(alias="unroundedQuantity")
    lot_size: DecimalText = Field(alias="lotSize")
    entry_price: DecimalText | None = Field(default=None, alias="entryPrice")
    strategy_stop: DecimalText | None = Field(default=None, alias="strategyStop")
    strategy_atr: DecimalText | None = Field(default=None, alias="strategyAtr")
    risk_budget: DecimalText = Field(alias="riskBudget")
    risk_per_unit: DecimalText = Field(alias="riskPerUnit")
    risk_per_trade_rate: DecimalText = Field(alias="riskPerTradeRate")
    regime: str | None = None
    regime_multiplier: DecimalText = Field(alias="regimeMultiplier")
    caps: list[RecommendationPositionSizeCap]
    limiting_caps: list[str] = Field(alias="limitingCaps")
    zero_reasons: list[RecommendationPositionSizingReason] = Field(alias="zeroReasons")


class RecommendationPortfolio(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target_weight: DecimalText | None = Field(default=None, alias="targetWeight")
    target_quantity: DecimalText | None = Field(default=None, alias="targetQuantity")
    cash_after: DecimalText | None = Field(default=None, alias="cashAfter")
    note: str
    position_sizing: RecommendationPositionSizing | None = Field(
        default=None,
        alias="positionSizing",
    )


class RecommendationHardRiskCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    passed: bool
    detail: str


class RecommendationHardRisk(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    passed: bool
    checks: list[RecommendationHardRiskCheck]
    blocked_reason: str | None = Field(default=None, alias="blockedReason")


class PaperOrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    recommendation_id: str | None = Field(default=None, alias="recommendationId")
    market: str
    symbol: str
    name: str | None = None
    side: Literal["BUY", "SELL"]
    quantity: DecimalText
    price: DecimalText | None = None
    currency: Literal["KRW", "USD"]
    status: str
    at: datetime
    reject_reason: str | None = Field(default=None, alias="rejectReason")

    _at_timezone = field_validator("at")(_validate_aware_timestamp)

    @field_serializer("at", when_used="json")
    def serialize_at(self, value: datetime) -> str:
        return _serialize_timestamp(value) or ""


class RecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    action: RecommendationAction
    decision: RecommendationDecision
    status: str | None = None
    market: RecommendationMarket
    symbol: str
    name: str | None = None
    currency: Literal["KRW", "USD"] | None = None
    headline: str | None = None
    rationale: list[str]
    risks: list[str]
    evidence: list[RecommendationEvidence]
    confidence: DecimalText | None = None
    reference_price: DecimalText | None = Field(default=None, alias="referencePrice")
    suggested_quantity: DecimalText | None = Field(
        default=None,
        alias="suggestedQuantity",
    )
    source: str | None = None
    created_at: datetime = Field(alias="createdAt")
    valid_until: datetime | None = Field(default=None, alias="validUntil")
    decided_at: datetime | None = Field(default=None, alias="decidedAt")
    regime: str | None = None
    regime_detail: str | None = Field(default=None, alias="regimeDetail")
    strategy_votes: list[StrategyVoteResponse] = Field(
        default_factory=list, alias="strategyVotes"
    )
    ai_rationale: list[str] = Field(default_factory=list, alias="aiRationale")
    event_evidence: list[RecommendationEventEvidence] = Field(
        default_factory=list, alias="eventEvidence"
    )
    entry_price: DecimalText | None = Field(default=None, alias="entryPrice")
    stop_price: DecimalText | None = Field(default=None, alias="stopPrice")
    target_price: DecimalText | None = Field(default=None, alias="targetPrice")
    ranking: RecommendationRanking | None = None
    portfolio: RecommendationPortfolio | None = None
    hard_risk: RecommendationHardRisk | None = Field(default=None, alias="hardRisk")
    paper_order: PaperOrderResult | None = Field(default=None, alias="paperOrder")

    _decimal_strings = field_validator(
        "confidence",
        "reference_price",
        "suggested_quantity",
        "entry_price",
        "stop_price",
        "target_price",
        mode="before",
    )(_validate_decimal_text)
    _timestamp_timezones = field_validator(
        "created_at",
        "valid_until",
        "decided_at",
    )(_validate_aware_timestamp)

    @field_serializer(
        "created_at",
        "valid_until",
        "decided_at",
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return _serialize_timestamp(value)


class RecommendationListResponse(BaseModel):
    recommendations: list[RecommendationResponse]


class RecommendationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: TerminalRecommendationDecision


class _AITradingSettingsFields(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    risk_level: int = Field(ge=1, le=5, strict=True, alias="riskLevel")
    operating_budget: Decimal = Field(gt=0, alias="operatingBudget")
    daily_target_rate_pct: Decimal = Field(
        ge=0, le=10, alias="dailyTargetRatePct"
    )
    max_daily_loss_rate_pct: Decimal = Field(
        ge=0, le=20, alias="maxDailyLossRatePct"
    )
    kill_switch: bool = Field(alias="killSwitch")
    currency: Literal["KRW", "USD"]
    custom_max_buys_per_day: int | None = Field(
        default=None, ge=1, strict=True, alias="customMaxBuysPerDay"
    )
    custom_max_sells_per_day: int | None = Field(
        default=None, ge=1, strict=True, alias="customMaxSellsPerDay"
    )

    @field_serializer(
        "operating_budget",
        "daily_target_rate_pct",
        "max_daily_loss_rate_pct",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class AITradingSettingsUpdate(_AITradingSettingsFields):
    max_daily_loss_rate_pct: Decimal = Field(
        ge=Decimal("0.1"), le=20, alias="maxDailyLossRatePct"
    )


class AITradingDerivedLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    daily_target_amount: Decimal = Field(ge=0, alias="dailyTargetAmount")
    max_daily_loss_amount: Decimal = Field(ge=0, alias="maxDailyLossAmount")
    max_symbol_allocation_pct: Decimal = Field(
        gt=0, le=100, alias="maxSymbolAllocationPct"
    )
    max_concurrent_holdings: int = Field(ge=0, alias="maxConcurrentHoldings")
    max_buys_per_day: int = Field(ge=0, alias="maxBuysPerDay")
    max_orders_per_day: int = Field(ge=0, alias="maxOrdersPerDay")
    max_sells_per_day: int = Field(ge=0, alias="maxSellsPerDay")
    max_custom_buys_per_day: int = Field(ge=1, alias="maxCustomBuysPerDay")
    max_custom_sells_per_day: int = Field(ge=1, alias="maxCustomSellsPerDay")
    max_custom_orders_per_day: int = Field(ge=2, alias="maxCustomOrdersPerDay")
    risk_per_trade_rate: Decimal = Field(ge=0, le=1, alias="riskPerTradeRate")
    same_symbol_reentry_limit: int = Field(
        ge=0, alias="sameSymbolReentryLimit"
    )
    min_ai_confidence: Decimal = Field(ge=0, le=1, alias="minAiConfidence")

    @field_serializer(
        "daily_target_amount",
        "max_daily_loss_amount",
        "max_symbol_allocation_pct",
        "risk_per_trade_rate",
        "min_ai_confidence",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class AITradingSettings(_AITradingSettingsFields):
    derived_limits: AITradingDerivedLimits = Field(alias="derivedLimits")


class AITradingStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["APPROVAL", "AUTO_PAPER"]
    settings: AITradingSettingsUpdate


class AITradingUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    realized_pnl_today: Decimal = Field(alias="realizedPnlToday")
    realized_loss_today: Decimal = Field(alias="realizedLossToday")
    buys_today: int = Field(alias="buysToday")
    sells_today: int = Field(alias="sellsToday")
    orders_today: int = Field(alias="ordersToday")
    concurrent_holdings: int = Field(alias="concurrentHoldings")
    budget_used: Decimal = Field(alias="budgetUsed")

    @field_serializer(
        "realized_pnl_today",
        "realized_loss_today",
        "budget_used",
        when_used="json",
    )
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, "f")


class AITradingStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["APPROVAL", "AUTO_PAPER"]
    paper: Literal[True] = True
    trading_mode: Literal["PAPER"] = Field(default="PAPER", alias="tradingMode")
    settings: AITradingSettings
    usage: AITradingUsageResponse
    kill_switch: bool = Field(alias="killSwitch")
    updated_at: datetime = Field(alias="updatedAt")
    executions: list[PaperOrderResult] = Field(default_factory=list)

    _updated_at_timezone = field_validator("updated_at")(_validate_aware_timestamp)

    @field_serializer("updated_at", when_used="json")
    def serialize_updated_at(self, value: datetime) -> str:
        return _serialize_timestamp(value) or ""


class RecommendationError(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class RecommendationErrorEnvelope(BaseModel):
    error: RecommendationError


def build_recommendation_response(
    row: Any,
    *,
    paper_order: Any | None = None,
    resolved_name: str | None = None,
) -> RecommendationResponse:
    """Project one persistence row plus its optional PAPER execution."""

    evidence = list(getattr(row, "evidence", None) or [])
    detail = next(
        (
            item
            for item in evidence
            if isinstance(item, dict) and item.get("kind") == "ai_vertical_slice"
        ),
        {},
    )
    decision = str(row.decision)
    execution_status = getattr(row, "paper_execution_status", None)
    status_value = {
        "SUCCEEDED": "EXECUTED",
        "FAILED": "FAILED",
        "CLAIMED": "EXECUTING",
    }.get(str(execution_status))
    if status_value is None and detail:
        status_value = decision
    hard_risk_value = detail.get("hardRisk")
    if isinstance(hard_risk_value, dict):
        hard_risk_value = dict(hard_risk_value)
        if str(execution_status) == "FAILED":
            execution_error = str(
                getattr(row, "paper_execution_error", None) or "PAPER_EXECUTION_FAILED"
            )
            hard_risk_value["passed"] = False
            hard_risk_value["blockedReason"] = execution_error
            checks = list(hard_risk_value.get("checks") or [])
            checks.append(
                {
                    "rule": "EXECUTION_RECHECK",
                    "passed": False,
                    "detail": execution_error,
                }
            )
            hard_risk_value["checks"] = checks
    stored_name = str(getattr(row, "name", None) or "").strip()
    display_name = (
        resolved_name
        if not stored_name or stored_name == str(row.symbol).strip()
        else stored_name
    )
    payload: dict[str, object] = {
        "id": row.id,
        "action": row.action,
        "decision": row.decision,
        "status": status_value,
        "market": row.market,
        "symbol": row.symbol,
        "name": display_name,
        "currency": row.currency,
        "headline": row.headline,
        "rationale": _localized_rationale(list(row.rationale), detail),
        "risks": list(row.risks),
        "evidence": evidence,
        "confidence": row.confidence,
        "referencePrice": row.reference_price,
        "suggestedQuantity": row.suggested_quantity,
        "source": row.source,
        "createdAt": row.created_at,
        "validUntil": row.valid_until,
        "decidedAt": row.decided_at,
        "regime": detail.get("regime"),
        "regimeDetail": detail.get("regimeDetail"),
        "strategyVotes": detail.get("strategyVotes", []),
        "aiRationale": detail.get("aiRationale", []),
        "eventEvidence": detail.get("eventEvidence", []),
        "entryPrice": detail.get("entryPrice"),
        "stopPrice": detail.get("stopPrice"),
        "targetPrice": detail.get("targetPrice"),
        "ranking": detail.get("ranking"),
        "portfolio": detail.get("portfolio"),
        "hardRisk": hard_risk_value,
    }
    if paper_order is not None:
        payload["paperOrder"] = {
            "id": paper_order.id,
            "recommendationId": row.id,
            "market": paper_order.market,
            "symbol": paper_order.symbol,
            "name": paper_order.name,
            "side": paper_order.side,
            "quantity": format(Decimal(paper_order.quantity), "f"),
            "price": (
                format(Decimal(paper_order.average_fill_price), "f")
                if paper_order.average_fill_price is not None
                else (
                    format(Decimal(paper_order.limit_price), "f")
                    if paper_order.limit_price is not None
                    else None
                )
            ),
            "currency": paper_order.currency,
            "status": paper_order.status,
            "at": paper_order.created_at,
            "rejectReason": paper_order.reject_reason,
        }
    return RecommendationResponse.model_validate(payload)


__all__ = [
    "AITradingDerivedLimits",
    "AITradingSettings",
    "AITradingSettingsUpdate",
    "AITradingStateResponse",
    "AITradingStateUpdate",
    "AITradingUsageResponse",
    "PaperOrderResult",
    "RecommendationDecisionRequest",
    "RecommendationErrorEnvelope",
    "RecommendationEvidence",
    "RecommendationListResponse",
    "RecommendationResponse",
    "RecommendationStatusGroup",
    "TerminalRecommendationDecision",
    "build_recommendation_response",
]
