"""Pure contracts shared by KAsset strategy and PAPER automation code."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from app.extensions.kasset.api.paper_schemas import OrderRequest, RiskAssessment


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyName(StrEnum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    VOLATILITY_TREND = "volatility_trend"


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One normalized OHLCV bar.

    Values are converted through ``str`` so callers can supply provider numeric
    types without introducing binary floating-point arithmetic into strategies.
    Validation intentionally happens at the strategy boundary, where malformed
    input can produce a non-actionable HOLD result instead of a partial signal.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                object.__setattr__(self, name, Decimal(str(value)))


@dataclass(frozen=True, slots=True)
class RationaleEvidence:
    code: str
    value: str
    description: str


@dataclass(frozen=True, slots=True)
class StrategyResult:
    action: Action
    confidence: Decimal
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    rationale: tuple[str, ...]
    evidence: tuple[RationaleEvidence, ...]
    strategy: StrategyName
    version: str
    symbol: str
    market: Literal["KRX", "US"]
    as_of: datetime
    valid_until: datetime


class DeterministicStrategy(Protocol):
    name: StrategyName
    version: str
    minimum_bars: int

    def evaluate(
        self,
        bars: Sequence[PriceBar],
        *,
        symbol: str,
        market: Literal["KRX", "US"],
        as_of: datetime,
    ) -> StrategyResult: ...


@dataclass(frozen=True, slots=True)
class ExternalEvidence:
    """Normalized external analysis; this type performs no provider call."""

    source: str
    symbol: str
    market: Literal["KRX", "US"]
    action: Action
    confidence: Decimal
    as_of: datetime
    valid_until: datetime
    rationale: tuple[str, ...]
    evidence: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class RecommendationDraft:
    owner_user_id: str
    action: Action
    market: Literal["KRX", "US"]
    symbol: str
    name: str | None
    headline: str
    rationale: tuple[str, ...]
    risks: tuple[str, ...]
    evidence: tuple[Mapping[str, object], ...]
    confidence: Decimal
    reference_price: Decimal | None
    suggested_quantity: Decimal | None
    source: str
    created_at: datetime
    valid_until: datetime


@runtime_checkable
class RecommendationPersistence(Protocol):
    async def create_recommendation(
        self,
        *,
        owner_user_id: str,
        draft: RecommendationDraft,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class OwnerExecutionPolicy:
    owner_user_id: str
    paper_automation_enabled: bool
    global_kill_switch_enabled: bool
    trading_mode: Literal["PAPER", "LIVE", "DISABLED"]


@runtime_checkable
class ExecutionSafetyGate(Protocol):
    async def get_policy(
        self,
        *,
        owner_user_id: str,
        now: datetime,
    ) -> OwnerExecutionPolicy: ...


class ClaimedRecommendation(Protocol):
    id: str
    owner_user_id: str
    paper_execution_token: str
    paper_execution_claimed_at: datetime
    paper_execution_lease_expires_at: datetime
    paper_execution_attempt_count: int
    decision: str
    action: str
    market: str
    symbol: str
    suggested_quantity: Decimal | str | None
    valid_until: datetime


@dataclass(frozen=True, slots=True)
class PaperExecutionClaim:
    """Convenience value object satisfying ``ClaimedRecommendation`` in tests."""

    id: str
    owner_user_id: str
    paper_execution_token: str
    paper_execution_claimed_at: datetime
    paper_execution_lease_expires_at: datetime
    paper_execution_attempt_count: int
    decision: str
    action: str
    market: str
    symbol: str
    suggested_quantity: Decimal | str | None
    valid_until: datetime


@runtime_checkable
class RecommendationService(Protocol):
    async def claim_for_paper_execution(
        self,
        owner_user_id: str,
        now: datetime,
    ) -> ClaimedRecommendation | None: ...

    async def complete_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> None: ...

    async def reconcile_paper_execution_completion(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        paper_order_id: str,
        now: datetime,
    ) -> bool: ...

    async def fail_paper_execution(
        self,
        owner_user_id: str,
        recommendation_id: str,
        claim_token: str,
        error: str,
        now: datetime,
    ) -> None: ...


@runtime_checkable
class PaperOrderFacade(Protocol):
    async def preview(
        self,
        db: Any,
        owner_user_id: str,
        request: OrderRequest,
    ) -> RiskAssessment: ...

    async def get_by_client_order_id(
        self,
        db: Any,
        owner_user_id: str,
        client_order_id: str,
    ) -> object | None: ...

    async def reconcile(
        self,
        db: Any,
        owner_user_id: str,
        order: object,
    ) -> object: ...

    async def submit(
        self,
        db: Any,
        owner_user_id: str,
        request: OrderRequest,
    ) -> tuple[object, bool]: ...


@dataclass(frozen=True, slots=True)
class PaperExecutionOutcome:
    status: Literal["IDLE", "BLOCKED", "REJECTED", "SUBMITTED", "FAILED"]
    reason: str
    recommendation_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BacktestSignal:
    signal_at: datetime
    execute_at: datetime
    action: Action


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: StrategyName
    strategy_version: str
    initial_capital: Decimal
    final_equity: Decimal
    trade_count: int
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trades: tuple[BacktestTrade, ...] = field(default_factory=tuple)
    signals: tuple[BacktestSignal, ...] = field(default_factory=tuple)


def utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
