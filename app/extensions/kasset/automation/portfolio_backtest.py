"""Pure, deterministic KR/US portfolio backtesting with next-bar execution."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Literal, cast

from app.extensions.kasset.automation.benchmark_relative_strength import (
    compute_benchmark_return_60_from_bars,
)
from app.extensions.kasset.automation.candidate_ranker import (
    BenchmarkReturn,
    CandidateKey,
    CandidateMetadata,
    CandidateRanker,
    CandidateRankResult,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    DeterministicStrategy,
    PriceBar,
    StrategyResult,
    utc_datetime,
)
from app.extensions.kasset.automation.position_manager import (
    ManagedPositionState,
    PositionBar,
    PositionManagerConfig,
    evaluate_position,
    initialize_position,
)
from app.extensions.kasset.automation.position_sizing import (
    DEFAULT_POSITION_SIZING_CONFIG,
    PositionSizingConfig,
    PositionSizingInput,
    calculate_position_size,
)
from app.extensions.kasset.automation.producer import compose_weighted_ensemble
from app.extensions.kasset.automation.regime import (
    MarketRegime,
    RegimeAssessment,
    assess_market_regime,
)
from app.extensions.kasset.automation.strategies import STRATEGIES
from app.extensions.kasset.automation.strategy_promotion import (
    DEFAULT_PAPER_STRATEGY_KEY,
    DEFAULT_PAPER_STRATEGY_VERSION,
)

MarketKey = Literal["KR", "US"]
ExecutionMarket = Literal["KRX", "US"]
_ZERO = Decimal("0")
_ONE = Decimal("1")
_VALUE_QUANTUM = Decimal("0.00000001")
_SUPPORTED_MARKETS = frozenset({"KR", "US"})


@dataclass(frozen=True, slots=True)
class MarketExecutionCost:
    """One market's proportional fee and adverse open-price slippage."""

    fee_rate: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        for field_name in ("fee_rate", "slippage_rate"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, field_name, value)
            if not value.is_finite() or not _ZERO <= value < _ONE:
                raise ValueError(f"{field_name} must be finite and in [0, 1)")


@dataclass(frozen=True, slots=True)
class UniverseEvidence:
    """Caller-supplied evidence about the historical universe construction."""

    source: str = "caller_supplied_fixed_universe"
    point_in_time_membership: bool = False
    includes_delisted: bool = False
    as_of: datetime | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        source = self.source.strip()
        if not source:
            raise ValueError("universe evidence source is required")
        object.__setattr__(self, "source", source)
        if self.as_of is not None:
            object.__setattr__(
                self,
                "as_of",
                utc_datetime(self.as_of, field_name="universe_evidence.as_of"),
            )
        object.__setattr__(
            self,
            "notes",
            tuple(note.strip() for note in self.notes if note.strip()),
        )


@dataclass(frozen=True, slots=True)
class PortfolioBacktestConfig:
    initial_cash: Decimal = Decimal("100000000")
    max_positions: int = 5
    candidate_top_n: int = 12
    risk_per_trade_rate: Decimal = Decimal("0.01")
    max_symbol_allocation: Decimal = Decimal("0.20")
    kr_cost: MarketExecutionCost = MarketExecutionCost(
        fee_rate=Decimal("0.0015"), slippage_rate=Decimal("0.0010")
    )
    us_cost: MarketExecutionCost = MarketExecutionCost(
        fee_rate=Decimal("0.0010"), slippage_rate=Decimal("0.0005")
    )
    strategy_key: str = DEFAULT_PAPER_STRATEGY_KEY
    strategy_version: str = DEFAULT_PAPER_STRATEGY_VERSION
    position_sizing: PositionSizingConfig = DEFAULT_POSITION_SIZING_CONFIG
    position_manager: PositionManagerConfig = PositionManagerConfig()
    execution_delay_bars: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "initial_cash",
            "risk_per_trade_rate",
            "max_symbol_allocation",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, field_name, value)
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
        if self.initial_cash <= _ZERO:
            raise ValueError("initial_cash must be positive")
        if not _ZERO < self.risk_per_trade_rate <= _ONE:
            raise ValueError("risk_per_trade_rate must be in (0, 1]")
        if not _ZERO < self.max_symbol_allocation <= _ONE:
            raise ValueError("max_symbol_allocation must be in (0, 1]")
        if self.max_positions < 1:
            raise ValueError("max_positions must be positive")
        if self.candidate_top_n < self.max_positions:
            raise ValueError("candidate_top_n must cover max_positions")
        if type(self.execution_delay_bars) is not int or self.execution_delay_bars < 1:
            raise ValueError("execution_delay_bars must be a positive integer")
        strategy_key = self.strategy_key.strip()
        strategy_version = self.strategy_version.strip()
        if not strategy_key or not strategy_version:
            raise ValueError("strategy_key and strategy_version are required")
        object.__setattr__(self, "strategy_key", strategy_key)
        object.__setattr__(self, "strategy_version", strategy_version)

    def cost_for(self, market: MarketKey) -> MarketExecutionCost:
        return self.kr_cost if market == "KR" else self.us_cost


@dataclass(frozen=True, slots=True)
class BacktestWindow:
    """Decision window; earlier bars are warm-up data and cannot create orders."""

    signal_start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = utc_datetime(self.signal_start_at, field_name="signal_start_at")
        end = utc_datetime(self.end_at, field_name="end_at")
        if start >= end:
            raise ValueError("signal_start_at must be before end_at")
        object.__setattr__(self, "signal_start_at", start)
        object.__setattr__(self, "end_at", end)


class SignalStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    UNFILLED_END_OF_DATA = "UNFILLED_END_OF_DATA"


@dataclass(frozen=True, slots=True)
class PortfolioSignal:
    market: MarketKey
    symbol: str
    action: Action
    signal_at: datetime
    observed_bar_count: int
    rank_position: int | None
    reason: str
    regime: MarketRegime | None = None
    status: SignalStatus = SignalStatus.PENDING
    execution_at: datetime | None = None
    reference_open: Decimal | None = None
    fill_price: Decimal | None = None
    fee: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class PortfolioTrade:
    market: MarketKey
    symbol: str
    entry_signal_at: datetime
    entry_at: datetime
    exit_signal_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioOpenPosition:
    market: MarketKey
    symbol: str
    entry_signal_at: datetime
    entry_at: datetime
    entry_price: Decimal
    quantity: Decimal
    last_close: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class MarketBenchmarkReturn:
    market: MarketKey
    start_at: datetime
    end_at: datetime
    start_close: Decimal
    end_close: Decimal
    total_return: Decimal


@dataclass(frozen=True, slots=True)
class BacktestEvidence:
    code: str
    value: str
    detail: str


@dataclass(frozen=True, slots=True)
class PortfolioBacktestResult:
    strategy_key: str
    strategy_version: str
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    total_return: Decimal
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    max_drawdown: Decimal
    trade_count: int
    win_rate: Decimal
    expectancy: Decimal
    fees_paid: Decimal
    slippage_cost: Decimal
    signals: tuple[PortfolioSignal, ...]
    trades: tuple[PortfolioTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_positions: tuple[PortfolioOpenPosition, ...]
    benchmark_by_market: tuple[MarketBenchmarkReturn, ...]
    evidence: tuple[BacktestEvidence, ...]
    determinism_hash: str

    def as_evidence(self) -> dict[str, object]:
        return cast(dict[str, object], _json_safe(asdict(self)))


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_bars: int = 260
    test_bars: int = 20
    step_bars: int = 20

    def __post_init__(self) -> None:
        if self.train_bars < 2:
            raise ValueError("train_bars must be at least two")
        if self.test_bars < 1 or self.step_bars < 1:
            raise ValueError("test_bars and step_bars must be positive")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_index: int
    train_start_at: datetime
    train_end_at: datetime
    test_start_at: datetime
    test_end_at: datetime
    train_result: PortfolioBacktestResult
    test_result: PortfolioBacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[WalkForwardFold, ...]
    mean_test_return: Decimal
    mean_test_excess_return: Decimal | None
    evidence: tuple[BacktestEvidence, ...]
    determinism_hash: str

    def as_evidence(self) -> dict[str, object]:
        return cast(dict[str, object], _json_safe(asdict(self)))


@dataclass(frozen=True, slots=True)
class BacktestPerformanceSlice:
    """Comparable period/regime result derived only from executed portfolio paths."""

    label: str
    start_at: datetime
    end_at: datetime
    total_return: Decimal
    trade_count: int
    win_rate: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class CostStressScenario:
    multiplier: int
    total_return: Decimal
    max_drawdown: Decimal
    trade_count: int
    determinism_hash: str


@dataclass(frozen=True, slots=True)
class SymbolRemovalScenario:
    removed_market: MarketKey
    removed_symbol: str
    total_return: Decimal
    excess_return: Decimal | None
    determinism_hash: str


@dataclass(frozen=True, slots=True)
class PortfolioBacktestDiagnostics:
    """Required promotion evidence beyond one aggregate backtest result."""

    baseline: PortfolioBacktestResult
    turnover_ratio: Decimal
    period_performance: tuple[BacktestPerformanceSlice, ...]
    regime_performance: tuple[BacktestPerformanceSlice, ...]
    cost_stress: tuple[CostStressScenario, ...]
    symbol_removal: tuple[SymbolRemovalScenario, ...]
    delayed_execution: PortfolioBacktestResult
    evidence: tuple[BacktestEvidence, ...]
    determinism_hash: str

    def as_evidence(self) -> dict[str, object]:
        return cast(dict[str, object], _json_safe(asdict(self)))


@dataclass(frozen=True, slots=True)
class _OpenPrice:
    timestamp: datetime
    price: Decimal
    observed_bar_count: int


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    key: CandidateKey
    signal_index: int
    stop: Decimal
    atr: Decimal
    regime: MarketRegime
    average_volume: Decimal | None
    average_turnover: Decimal | None
    rank_position: int


@dataclass(frozen=True, slots=True)
class _PendingExit:
    key: CandidateKey
    signal_index: int
    quantity_fraction: Decimal
    reason: str


@dataclass(slots=True)
class _Position:
    market: MarketKey
    symbol: str
    entry_signal_at: datetime
    entry_at: datetime
    entry_price: Decimal
    entry_quantity: Decimal
    quantity: Decimal
    entry_fee_remaining: Decimal
    entry_slippage_remaining: Decimal
    manager_state: ManagedPositionState
    bars_held: int
    last_close: Decimal


@dataclass(slots=True)
class _RunState:
    cash: Decimal
    positions: dict[CandidateKey, _Position]
    pending_entries: dict[CandidateKey, _PendingEntry]
    pending_exits: dict[CandidateKey, _PendingExit]
    signals: list[PortfolioSignal]
    trades: list[PortfolioTrade]
    equity_curve: list[EquityPoint]
    fees_paid: Decimal = _ZERO
    slippage_cost: Decimal = _ZERO
    peak_equity: Decimal = _ZERO


def run_portfolio_backtest(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    config: PortfolioBacktestConfig = PortfolioBacktestConfig(),
    benchmark_bars_by_market: Mapping[MarketKey, Sequence[PriceBar]] | None = None,
    universe_evidence: UniverseEvidence = UniverseEvidence(),
    strategies: Sequence[DeterministicStrategy] = STRATEGIES,
    ranker: CandidateRanker | None = None,
    window: BacktestWindow | None = None,
) -> PortfolioBacktestResult:
    """Run a long-only portfolio over completed bars without same-bar fills.

    The event loop exposes only ``_OpenPrice`` to the execution functions. Full
    OHLCV bars are appended to strategy history only after pending orders have
    executed, so neither a same-close fill nor an intrabar fill is representable.
    """

    metadata = _validate_candidates(candidates)
    normalized_bars = _normalize_candidate_bars(metadata, bars_by_candidate)
    normalized_benchmarks = _normalize_benchmarks(benchmark_bars_by_market or {})
    strategy_tuple = tuple(strategies)
    if not strategy_tuple:
        raise ValueError("at least one deterministic strategy is required")
    active_ranker = ranker or CandidateRanker()

    event_rows: dict[datetime, list[tuple[CandidateKey, PriceBar]]] = defaultdict(list)
    for key, bars in normalized_bars.items():
        for bar in bars:
            if window is None or bar.timestamp <= window.end_at:
                event_rows[bar.timestamp].append((key, bar))
    if not event_rows:
        raise ValueError("at least one price bar is required")

    histories: dict[CandidateKey, list[PriceBar]] = {
        candidate.key: [] for candidate in metadata
    }
    state = _RunState(
        cash=config.initial_cash,
        positions={},
        pending_entries={},
        pending_exits={},
        signals=[],
        trades=[],
        equity_curve=[],
        peak_equity=config.initial_cash,
    )
    exclusion_counts: Counter[str] = Counter()
    ranking_decisions = 0
    record_start = window.signal_start_at if window is not None else min(event_rows)

    for timestamp in sorted(event_rows):
        current = sorted(event_rows[timestamp], key=lambda item: item[0])
        opens = {
            key: _OpenPrice(timestamp, bar.open, len(histories[key]))
            for key, bar in current
        }
        _execute_pending_exits(state, opens, config=config)
        _execute_pending_entries(state, opens, config=config)

        current_keys: set[CandidateKey] = set()
        for key, bar in current:
            histories[key].append(bar)
            current_keys.add(key)
            position = state.positions.get(key)
            if position is not None:
                position.last_close = bar.close

        if timestamp >= record_start:
            _record_equity(state, timestamp)

        if window is not None and timestamp < window.signal_start_at:
            continue

        regimes = _assess_regimes(histories)
        _queue_position_exits(
            state,
            histories,
            current_keys,
            timestamp=timestamp,
            strategies=strategy_tuple,
            regimes=regimes,
            config=config,
        )

        ranking = active_ranker.rank(
            metadata,
            histories,
            as_of=timestamp,
            allowed_markets=_SUPPORTED_MARKETS,
            benchmark_returns_60=_rolling_benchmark_returns(
                normalized_benchmarks,
                as_of=timestamp,
                maximum_age=active_ranker.config.maximum_bar_age,
            ),
        )
        ranking_decisions += 1
        exclusion_counts.update(
            item.exclusion_reason or "unknown" for item in ranking.excluded
        )
        _queue_entries(
            state,
            ranking.ranked[: config.candidate_top_n],
            histories,
            current_keys,
            timestamp=timestamp,
            strategies=strategy_tuple,
            regimes=regimes,
        )

    state.signals = [
        replace(signal, status=SignalStatus.UNFILLED_END_OF_DATA)
        if signal.status == SignalStatus.PENDING
        else signal
        for signal in state.signals
    ]
    final_timestamp = max(event_rows)
    if not state.equity_curve or state.equity_curve[-1].timestamp != final_timestamp:
        _record_equity(state, final_timestamp)

    final_equity = state.equity_curve[-1].equity
    total_return = _q(final_equity / config.initial_cash - _ONE)
    max_drawdown = max((point.drawdown for point in state.equity_curve), default=_ZERO)
    wins = sum(trade.net_pnl > _ZERO for trade in state.trades)
    win_rate = _q(Decimal(wins) / Decimal(len(state.trades))) if state.trades else _ZERO
    expectancy = (
        _q(
            sum((trade.net_pnl for trade in state.trades), start=_ZERO)
            / Decimal(len(state.trades))
        )
        if state.trades
        else _ZERO
    )
    benchmark_by_market, benchmark_return = _benchmark_returns(
        normalized_benchmarks,
        start_at=record_start,
        end_at=final_timestamp,
    )
    excess_return = (
        _q(total_return - benchmark_return) if benchmark_return is not None else None
    )
    open_positions = tuple(
        PortfolioOpenPosition(
            market=position.market,
            symbol=position.symbol,
            entry_signal_at=position.entry_signal_at,
            entry_at=position.entry_at,
            entry_price=_q(position.entry_price),
            quantity=_q(position.quantity),
            last_close=_q(position.last_close),
            unrealized_pnl=_q(
                (position.last_close - position.entry_price) * position.quantity
                - position.entry_fee_remaining
            ),
        )
        for _, position in sorted(state.positions.items())
    )
    evidence = _build_evidence(
        metadata,
        normalized_bars,
        universe_evidence=universe_evidence,
        benchmark_return=benchmark_return,
        ranking_decisions=ranking_decisions,
        exclusion_counts=exclusion_counts,
        open_position_count=len(open_positions),
    ) + (
        BacktestEvidence(
            code="EXECUTION_DELAY",
            value=f"bars={config.execution_delay_bars}",
            detail="Signals fill only after the configured number of later symbol bars.",
        ),
    )
    result = PortfolioBacktestResult(
        strategy_key=config.strategy_key,
        strategy_version=config.strategy_version,
        initial_cash=_q(config.initial_cash),
        final_cash=_q(state.cash),
        final_equity=_q(final_equity),
        total_return=total_return,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        max_drawdown=_q(max_drawdown),
        trade_count=len(state.trades),
        win_rate=win_rate,
        expectancy=expectancy,
        fees_paid=_q(state.fees_paid),
        slippage_cost=_q(state.slippage_cost),
        signals=tuple(state.signals),
        trades=tuple(state.trades),
        equity_curve=tuple(state.equity_curve),
        open_positions=open_positions,
        benchmark_by_market=benchmark_by_market,
        evidence=evidence,
        determinism_hash="",
    )
    return replace(result, determinism_hash=_stable_hash(result))


def run_walk_forward(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    config: PortfolioBacktestConfig = PortfolioBacktestConfig(),
    walk_forward: WalkForwardConfig = WalkForwardConfig(),
    benchmark_bars_by_market: Mapping[MarketKey, Sequence[PriceBar]] | None = None,
    universe_evidence: UniverseEvidence = UniverseEvidence(),
    strategies: Sequence[DeterministicStrategy] = STRATEGIES,
    ranker: CandidateRanker | None = None,
) -> WalkForwardResult:
    """Evaluate rolling train/test folds without carrying positions across folds."""

    metadata = _validate_candidates(candidates)
    normalized = _normalize_candidate_bars(metadata, bars_by_candidate)
    timestamps = sorted({bar.timestamp for bars in normalized.values() for bar in bars})
    required = walk_forward.train_bars + walk_forward.test_bars
    if len(timestamps) < required:
        raise ValueError("insufficient timestamps for one complete walk-forward fold")

    folds: list[WalkForwardFold] = []
    fold_start = 0
    while fold_start + required <= len(timestamps):
        train_times = timestamps[fold_start : fold_start + walk_forward.train_bars]
        test_times = timestamps[
            fold_start + walk_forward.train_bars : fold_start + required
        ]
        train_start, train_end = train_times[0], train_times[-1]
        test_start, test_end = test_times[0], test_times[-1]
        train_bars = _slice_candidate_bars(
            normalized, start_at=train_start, end_at=train_end
        )
        test_bars = _slice_candidate_bars(
            normalized, start_at=train_start, end_at=test_end
        )
        train_benchmarks = _slice_benchmarks(
            benchmark_bars_by_market or {}, start_at=train_start, end_at=train_end
        )
        test_benchmarks = _slice_benchmarks(
            benchmark_bars_by_market or {}, start_at=train_start, end_at=test_end
        )
        train_result = run_portfolio_backtest(
            metadata,
            train_bars,
            config=config,
            benchmark_bars_by_market=train_benchmarks,
            universe_evidence=universe_evidence,
            strategies=strategies,
            ranker=ranker,
        )
        test_result = run_portfolio_backtest(
            metadata,
            test_bars,
            config=config,
            benchmark_bars_by_market=test_benchmarks,
            universe_evidence=universe_evidence,
            strategies=strategies,
            ranker=ranker,
            window=BacktestWindow(signal_start_at=train_end, end_at=test_end),
        )
        folds.append(
            WalkForwardFold(
                fold_index=len(folds) + 1,
                train_start_at=train_start,
                train_end_at=train_end,
                test_start_at=test_start,
                test_end_at=test_end,
                train_result=train_result,
                test_result=test_result,
            )
        )
        fold_start += walk_forward.step_bars

    mean_test_return = _q(
        sum((fold.test_result.total_return for fold in folds), start=_ZERO)
        / Decimal(len(folds))
    )
    excess_values = [
        fold.test_result.excess_return
        for fold in folds
        if fold.test_result.excess_return is not None
    ]
    mean_test_excess = (
        _q(sum(excess_values, start=_ZERO) / Decimal(len(excess_values)))
        if len(excess_values) == len(folds)
        else None
    )
    evidence = (
        BacktestEvidence(
            code="WALK_FORWARD_BOUNDARY",
            value=f"folds={len(folds)}",
            detail=(
                "Each test fold receives only its rolling train window plus test bars; "
                "positions and cash are reset between folds."
            ),
        ),
        BacktestEvidence(
            code="WALK_FORWARD_EXECUTION",
            value="last_train_close_to_next_test_open",
            detail=(
                "The last train close may create a signal, but its earliest possible "
                "fill is the first later test-bar open."
            ),
        ),
    )
    result = WalkForwardResult(
        folds=tuple(folds),
        mean_test_return=mean_test_return,
        mean_test_excess_return=mean_test_excess,
        evidence=evidence,
        determinism_hash="",
    )
    return replace(result, determinism_hash=_stable_hash(result))


def run_portfolio_diagnostics(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    config: PortfolioBacktestConfig = PortfolioBacktestConfig(),
    benchmark_bars_by_market: Mapping[MarketKey, Sequence[PriceBar]] | None = None,
    universe_evidence: UniverseEvidence = UniverseEvidence(),
    strategies: Sequence[DeterministicStrategy] = STRATEGIES,
    ranker: CandidateRanker | None = None,
    window: BacktestWindow | None = None,
) -> PortfolioBacktestDiagnostics:
    """Produce stress, breakdown, turnover, and counterfactual promotion evidence."""

    metadata = _validate_candidates(candidates)

    def run(
        scenario_candidates: Sequence[CandidateMetadata],
        scenario_bars: Mapping[CandidateKey, Sequence[PriceBar]],
        scenario_config: PortfolioBacktestConfig,
    ) -> PortfolioBacktestResult:
        return run_portfolio_backtest(
            scenario_candidates,
            scenario_bars,
            config=scenario_config,
            benchmark_bars_by_market=benchmark_bars_by_market,
            universe_evidence=universe_evidence,
            strategies=strategies,
            ranker=ranker,
            window=window,
        )

    baseline = run(metadata, bars_by_candidate, config)
    average_equity = sum(
        (point.equity for point in baseline.equity_curve), start=_ZERO
    ) / Decimal(len(baseline.equity_curve))
    entry_turnover = sum(
        (trade.entry_price * trade.quantity for trade in baseline.trades),
        start=sum(
            (
                position.entry_price * position.quantity
                for position in baseline.open_positions
            ),
            start=_ZERO,
        ),
    )
    exit_turnover = sum(
        (trade.exit_price * trade.quantity for trade in baseline.trades),
        start=_ZERO,
    )
    turnover_ratio = _q(
        (entry_turnover + exit_turnover) / average_equity
        if average_equity > _ZERO
        else _ZERO
    )

    equity_by_year: dict[int, list[EquityPoint]] = defaultdict(list)
    for point in baseline.equity_curve:
        equity_by_year[point.timestamp.year].append(point)
    trades_by_year: dict[int, list[PortfolioTrade]] = defaultdict(list)
    for trade in baseline.trades:
        trades_by_year[trade.exit_at.year].append(trade)
    period_slices: list[BacktestPerformanceSlice] = []
    starting_equity = config.initial_cash
    for year, points in sorted(equity_by_year.items()):
        period_slices.append(
            _performance_slice(
                str(year),
                points[0].timestamp,
                points[-1].timestamp,
                starting_equity,
                points[-1].equity,
                trades_by_year.get(year, ()),
            )
        )
        starting_equity = points[-1].equity
    period_performance = tuple(period_slices)

    entry_regimes = {
        (signal.market, signal.symbol, signal.signal_at): signal.regime
        for signal in baseline.signals
        if signal.action == Action.BUY
        and signal.status == SignalStatus.EXECUTED
        and signal.regime is not None
    }
    trades_by_regime: dict[MarketRegime, list[PortfolioTrade]] = defaultdict(list)
    for trade in baseline.trades:
        regime = entry_regimes.get((trade.market, trade.symbol, trade.entry_signal_at))
        if regime is not None:
            trades_by_regime[regime].append(trade)
    regime_performance = tuple(
        _performance_slice(
            regime.value,
            min(trade.entry_at for trade in trades),
            max(trade.exit_at for trade in trades),
            config.initial_cash,
            config.initial_cash + sum((trade.net_pnl for trade in trades), start=_ZERO),
            trades,
        )
        for regime, trades in sorted(
            trades_by_regime.items(), key=lambda item: item[0].value
        )
    )

    stress: list[CostStressScenario] = []
    for multiplier in (1, 2, 3):
        if multiplier == 1:
            result = baseline
        else:
            stressed_config = replace(
                config,
                kr_cost=_scaled_cost(config.kr_cost, multiplier),
                us_cost=_scaled_cost(config.us_cost, multiplier),
            )
            result = run(metadata, bars_by_candidate, stressed_config)
        stress.append(
            CostStressScenario(
                multiplier=multiplier,
                total_return=result.total_return,
                max_drawdown=result.max_drawdown,
                trade_count=result.trade_count,
                determinism_hash=result.determinism_hash,
            )
        )

    removal: list[SymbolRemovalScenario] = []
    if len(metadata) > 1:
        for removed in metadata:
            remaining = tuple(item for item in metadata if item.key != removed.key)
            remaining_bars = {
                item.key: bars_by_candidate.get(item.key, ()) for item in remaining
            }
            result = run(remaining, remaining_bars, config)
            removal.append(
                SymbolRemovalScenario(
                    removed_market=removed.market,
                    removed_symbol=removed.symbol,
                    total_return=result.total_return,
                    excess_return=result.excess_return,
                    determinism_hash=result.determinism_hash,
                )
            )

    delayed_execution = run(
        metadata,
        bars_by_candidate,
        replace(config, execution_delay_bars=config.execution_delay_bars + 1),
    )
    evidence = (
        BacktestEvidence(
            code="COST_STRESS",
            value="1x,2x,3x",
            detail="Fees and slippage are both multiplied for each deterministic run.",
        ),
        BacktestEvidence(
            code="COUNTERFACTUAL",
            value=(
                f"symbol_removals={len(removal)};"
                f"delay_bars={config.execution_delay_bars + 1}"
            ),
            detail="Each symbol-removal run and the one-bar-later execution run reset state.",
        ),
        BacktestEvidence(
            code="TURNOVER",
            value=str(turnover_ratio),
            detail="Executed entry and exit notionals divided by average portfolio equity.",
        ),
    )
    diagnostics = PortfolioBacktestDiagnostics(
        baseline=baseline,
        turnover_ratio=turnover_ratio,
        period_performance=period_performance,
        regime_performance=regime_performance,
        cost_stress=tuple(stress),
        symbol_removal=tuple(removal),
        delayed_execution=delayed_execution,
        evidence=evidence,
        determinism_hash="",
    )
    return replace(
        diagnostics,
        determinism_hash=_stable_hash(diagnostics),
    )


def _performance_slice(
    label: str,
    start_at: datetime,
    end_at: datetime,
    starting_equity: Decimal,
    ending_equity: Decimal,
    trades: Sequence[PortfolioTrade],
) -> BacktestPerformanceSlice:
    wins = sum(trade.net_pnl > _ZERO for trade in trades)
    trade_count = len(trades)
    return BacktestPerformanceSlice(
        label=label,
        start_at=start_at,
        end_at=end_at,
        total_return=_q(
            ending_equity / starting_equity - _ONE if starting_equity > _ZERO else _ZERO
        ),
        trade_count=trade_count,
        win_rate=(_q(Decimal(wins) / Decimal(trade_count)) if trade_count else _ZERO),
        net_pnl=_q(sum((trade.net_pnl for trade in trades), start=_ZERO)),
    )


def _scaled_cost(cost: MarketExecutionCost, multiplier: int) -> MarketExecutionCost:
    return MarketExecutionCost(
        fee_rate=cost.fee_rate * Decimal(multiplier),
        slippage_rate=cost.slippage_rate * Decimal(multiplier),
    )


def _validate_candidates(
    candidates: Sequence[CandidateMetadata],
) -> tuple[CandidateMetadata, ...]:
    normalized = tuple(candidates)
    if not normalized:
        raise ValueError("at least one candidate is required")
    keys = [candidate.key for candidate in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("candidate market/symbol keys must be unique")
    return tuple(sorted(normalized, key=lambda candidate: candidate.key))


def _normalize_candidate_bars(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
) -> dict[CandidateKey, tuple[PriceBar, ...]]:
    known_keys = {candidate.key for candidate in candidates}
    unknown = set(bars_by_candidate) - known_keys
    if unknown:
        raise ValueError(f"bars contain unknown candidate keys: {sorted(unknown)!r}")
    return {
        candidate.key: _normalize_bars(
            bars_by_candidate.get(candidate.key, ()),
            field_name=f"bars[{candidate.market},{candidate.symbol}]",
            allow_empty=True,
        )
        for candidate in candidates
    }


def _normalize_benchmarks(
    benchmarks: Mapping[MarketKey, Sequence[PriceBar]],
) -> dict[MarketKey, tuple[PriceBar, ...]]:
    normalized: dict[MarketKey, tuple[PriceBar, ...]] = {}
    for raw_market, bars in benchmarks.items():
        market = str(raw_market).strip().upper()
        if market not in _SUPPORTED_MARKETS:
            raise ValueError("benchmark market must be KR or US")
        normalized[cast(MarketKey, market)] = _normalize_bars(
            bars, field_name=f"benchmark[{market}]", allow_empty=True
        )
    return normalized


def _rolling_benchmark_returns(
    benchmarks: Mapping[MarketKey, Sequence[PriceBar]],
    *,
    as_of: datetime,
    maximum_age: timedelta,
) -> dict[str, BenchmarkReturn]:
    output: dict[str, BenchmarkReturn] = {}
    for market, bars in benchmarks.items():
        benchmark_symbol = "KOSPI" if market == "KR" else "SPY"
        result = compute_benchmark_return_60_from_bars(
            tuple(bar for bar in bars if bar.timestamp <= as_of),
            market=market,
            benchmark_symbol=benchmark_symbol,
            as_of=as_of,
            maximum_age=maximum_age,
        )
        if result is not None:
            output[market] = result
    return output


def _normalize_bars(
    bars: Sequence[PriceBar],
    *,
    field_name: str,
    allow_empty: bool,
) -> tuple[PriceBar, ...]:
    if not bars and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    normalized: list[PriceBar] = []
    previous: datetime | None = None
    for raw_bar in bars:
        timestamp = utc_datetime(
            raw_bar.timestamp, field_name=f"{field_name}.timestamp"
        )
        if previous is not None and timestamp <= previous:
            raise ValueError(f"{field_name} timestamps must be strictly increasing")
        previous = timestamp
        bar = PriceBar(
            timestamp=timestamp,
            open=raw_bar.open,
            high=raw_bar.high,
            low=raw_bar.low,
            close=raw_bar.close,
            volume=raw_bar.volume,
        )
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not value.is_finite() or value <= _ZERO for value in prices):
            raise ValueError(f"{field_name} OHLC must be finite and positive")
        if not bar.volume.is_finite() or bar.volume < _ZERO:
            raise ValueError(f"{field_name} volume must be finite and non-negative")
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise ValueError(f"{field_name} OHLC range is inconsistent")
        normalized.append(bar)
    return tuple(normalized)


def _assess_regimes(
    histories: Mapping[CandidateKey, Sequence[PriceBar]],
) -> dict[MarketKey, RegimeAssessment]:
    return {
        market: assess_market_regime(
            {
                symbol: bars
                for (candidate_market, symbol), bars in histories.items()
                if candidate_market == market
            }
        )
        for market in cast(tuple[MarketKey, ...], ("KR", "US"))
    }


def _evaluate_ensemble(
    key: CandidateKey,
    bars: Sequence[PriceBar],
    *,
    timestamp: datetime,
    strategies: Sequence[DeterministicStrategy],
    regime: RegimeAssessment,
):
    market, symbol = key
    execution_market = _execution_market(market)
    results = tuple(
        strategy.evaluate(
            bars,
            symbol=symbol,
            market=execution_market,
            as_of=timestamp,
        )
        for strategy in strategies
    )
    return compose_weighted_ensemble(results, regime.weights)


def _queue_entries(
    state: _RunState,
    ranked: Sequence[CandidateRankResult],
    histories: Mapping[CandidateKey, Sequence[PriceBar]],
    current_keys: set[CandidateKey],
    *,
    timestamp: datetime,
    strategies: Sequence[DeterministicStrategy],
    regimes: Mapping[MarketKey, RegimeAssessment],
) -> None:
    for candidate in ranked:
        key = candidate.key
        if (
            key not in current_keys
            or key in state.positions
            or key in state.pending_entries
            or not candidate.eligible_for_new_buy
            or candidate.atr_14 is None
            or candidate.rank_position is None
        ):
            continue
        regime = regimes[candidate.market]
        decision = _evaluate_ensemble(
            key,
            histories[key],
            timestamp=timestamp,
            strategies=strategies,
            regime=regime,
        )
        if decision.action != Action.BUY:
            continue
        stop = _median_level(decision.agreeing, "stop")
        if stop is None:
            continue
        signal_index = len(state.signals)
        state.signals.append(
            PortfolioSignal(
                market=candidate.market,
                symbol=candidate.symbol,
                action=Action.BUY,
                signal_at=timestamp,
                observed_bar_count=len(histories[key]),
                rank_position=candidate.rank_position,
                reason="ranked_top_n_regime_weighted_buy",
                regime=regime.regime,
            )
        )
        state.pending_entries[key] = _PendingEntry(
            key=key,
            signal_index=signal_index,
            stop=stop,
            atr=candidate.atr_14,
            regime=regime.regime,
            average_volume=candidate.average_volume_20,
            average_turnover=candidate.average_turnover_20,
            rank_position=candidate.rank_position,
        )


def _queue_position_exits(
    state: _RunState,
    histories: Mapping[CandidateKey, Sequence[PriceBar]],
    current_keys: set[CandidateKey],
    *,
    timestamp: datetime,
    strategies: Sequence[DeterministicStrategy],
    regimes: Mapping[MarketKey, RegimeAssessment],
    config: PortfolioBacktestConfig,
) -> None:
    for key, position in sorted(state.positions.items()):
        if key not in current_keys or key in state.pending_exits:
            continue
        if timestamp <= position.entry_at:
            continue
        regime = regimes[position.market]
        decision = _evaluate_ensemble(
            key,
            histories[key],
            timestamp=timestamp,
            strategies=strategies,
            regime=regime,
        )
        position.bars_held += 1
        bar = histories[key][-1]
        evaluation = evaluate_position(
            position.manager_state,
            PositionBar(
                as_of=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            ),
            bars_held=position.bars_held,
            trend_intact=decision.action != Action.SELL,
            config=config.position_manager,
        )
        position.manager_state = evaluation.state
        if evaluation.signal is None:
            continue
        signal_index = len(state.signals)
        state.signals.append(
            PortfolioSignal(
                market=position.market,
                symbol=position.symbol,
                action=Action.SELL,
                signal_at=timestamp,
                observed_bar_count=len(histories[key]),
                rank_position=None,
                reason=evaluation.signal.kind.value,
            )
        )
        state.pending_exits[key] = _PendingExit(
            key=key,
            signal_index=signal_index,
            quantity_fraction=evaluation.signal.quantity_fraction,
            reason=evaluation.signal.kind.value,
        )


def _execute_pending_entries(
    state: _RunState,
    opens: Mapping[CandidateKey, _OpenPrice],
    *,
    config: PortfolioBacktestConfig,
) -> None:
    pending = sorted(
        (
            order
            for key, order in state.pending_entries.items()
            if key in opens
            and opens[key].timestamp > state.signals[order.signal_index].signal_at
            and opens[key].observed_bar_count
            >= state.signals[order.signal_index].observed_bar_count
            + config.execution_delay_bars
            - 1
        ),
        key=lambda order: (order.rank_position, order.key),
    )
    for order in pending:
        open_price = opens[order.key]
        signal = state.signals[order.signal_index]
        del state.pending_entries[order.key]
        if len(state.positions) >= config.max_positions:
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason="max_positions_reached",
            )
            continue
        market, symbol = order.key
        cost = config.cost_for(market)
        fill_price = open_price.price * (_ONE + cost.slippage_rate)
        budget_used = sum(
            (
                position.entry_price * position.quantity
                for position in state.positions.values()
            ),
            start=_ZERO,
        )
        sizing = calculate_position_size(
            PositionSizingInput(
                action="BUY",
                market=_execution_market(market),
                entry_price=fill_price,
                price_as_of=open_price.timestamp,
                evaluated_at=open_price.timestamp,
                operating_budget=config.initial_cash,
                budget_used=budget_used,
                max_symbol_allocation=config.max_symbol_allocation,
                current_symbol_invested=_ZERO,
                current_holding_quantity=_ZERO,
                risk_per_trade_rate=config.risk_per_trade_rate,
                regime=order.regime,
                strategy_stop=order.stop,
                strategy_atr=order.atr,
                average_volume=order.average_volume,
                average_turnover=order.average_turnover,
            ),
            config=config.position_sizing,
        )
        if not sizing.actionable:
            codes = ",".join(reason.code.value for reason in sizing.zero_reasons)
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason=f"position_sizing:{codes}",
            )
            continue
        affordable = state.cash / (fill_price * (_ONE + cost.fee_rate))
        quantity = _round_down(min(sizing.quantity, affordable), sizing.lot_size)
        if quantity <= _ZERO:
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason="insufficient_cash_after_costs",
            )
            continue
        try:
            manager_state = initialize_position(
                market=_execution_market(market),
                symbol=symbol,
                entry_price=fill_price,
                initial_atr=order.atr,
                entry_at=open_price.timestamp,
                strategy_version=config.strategy_version,
                config=config.position_manager,
            )
        except ValueError as exc:
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason=f"position_manager:{type(exc).__name__}",
            )
            continue
        notional = fill_price * quantity
        fee = notional * cost.fee_rate
        slippage = open_price.price * cost.slippage_rate * quantity
        state.cash -= notional + fee
        state.fees_paid += fee
        state.slippage_cost += slippage
        state.positions[order.key] = _Position(
            market=market,
            symbol=symbol,
            entry_signal_at=signal.signal_at,
            entry_at=open_price.timestamp,
            entry_price=fill_price,
            entry_quantity=quantity,
            quantity=quantity,
            entry_fee_remaining=fee,
            entry_slippage_remaining=slippage,
            manager_state=manager_state,
            bars_held=0,
            last_close=fill_price,
        )
        state.signals[order.signal_index] = replace(
            signal,
            status=SignalStatus.EXECUTED,
            execution_at=open_price.timestamp,
            reference_open=_q(open_price.price),
            fill_price=_q(fill_price),
            fee=_q(fee),
        )


def _execute_pending_exits(
    state: _RunState,
    opens: Mapping[CandidateKey, _OpenPrice],
    *,
    config: PortfolioBacktestConfig,
) -> None:
    pending = sorted(
        (
            order
            for key, order in state.pending_exits.items()
            if key in opens
            and opens[key].timestamp > state.signals[order.signal_index].signal_at
            and opens[key].observed_bar_count
            >= state.signals[order.signal_index].observed_bar_count
            + config.execution_delay_bars
            - 1
        ),
        key=lambda order: order.key,
    )
    for order in pending:
        open_price = opens[order.key]
        signal = state.signals[order.signal_index]
        del state.pending_exits[order.key]
        position = state.positions.get(order.key)
        if position is None:
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason="position_already_closed",
            )
            continue
        cost = config.cost_for(position.market)
        if order.quantity_fraction >= _ONE:
            quantity = position.quantity
        else:
            lot = config.position_sizing.lot_size(_execution_market(position.market))
            quantity = _round_down(position.quantity * order.quantity_fraction, lot)
        if quantity <= _ZERO:
            state.signals[order.signal_index] = replace(
                signal,
                status=SignalStatus.SKIPPED,
                execution_at=open_price.timestamp,
                reference_open=_q(open_price.price),
                reason="partial_exit_below_market_lot",
            )
            continue
        fill_price = open_price.price * (_ONE - cost.slippage_rate)
        exit_notional = fill_price * quantity
        exit_fee = exit_notional * cost.fee_rate
        ratio = quantity / position.quantity
        if quantity == position.quantity:
            entry_fee = position.entry_fee_remaining
            entry_slippage = position.entry_slippage_remaining
        else:
            entry_fee = position.entry_fee_remaining * ratio
            entry_slippage = position.entry_slippage_remaining * ratio
        exit_slippage = open_price.price * cost.slippage_rate * quantity
        gross_pnl = (fill_price - position.entry_price) * quantity
        net_pnl = gross_pnl - entry_fee - exit_fee
        state.cash += exit_notional - exit_fee
        state.fees_paid += exit_fee
        state.slippage_cost += exit_slippage
        state.trades.append(
            PortfolioTrade(
                market=position.market,
                symbol=position.symbol,
                entry_signal_at=position.entry_signal_at,
                entry_at=position.entry_at,
                exit_signal_at=signal.signal_at,
                exit_at=open_price.timestamp,
                entry_price=_q(position.entry_price),
                exit_price=_q(fill_price),
                quantity=_q(quantity),
                gross_pnl=_q(gross_pnl),
                entry_fee=_q(entry_fee),
                exit_fee=_q(exit_fee),
                slippage_cost=_q(entry_slippage + exit_slippage),
                net_pnl=_q(net_pnl),
                exit_reason=order.reason,
            )
        )
        position.quantity -= quantity
        position.entry_fee_remaining -= entry_fee
        position.entry_slippage_remaining -= entry_slippage
        if position.quantity <= _ZERO:
            del state.positions[order.key]
        state.signals[order.signal_index] = replace(
            signal,
            status=SignalStatus.EXECUTED,
            execution_at=open_price.timestamp,
            reference_open=_q(open_price.price),
            fill_price=_q(fill_price),
            fee=_q(exit_fee),
        )


def _record_equity(state: _RunState, timestamp: datetime) -> None:
    market_value = sum(
        (
            position.quantity * position.last_close
            for position in state.positions.values()
        ),
        start=_ZERO,
    )
    equity = state.cash + market_value
    state.peak_equity = max(state.peak_equity, equity)
    drawdown = (
        (state.peak_equity - equity) / state.peak_equity
        if state.peak_equity > _ZERO
        else _ZERO
    )
    state.equity_curve.append(
        EquityPoint(
            timestamp=timestamp,
            cash=_q(state.cash),
            market_value=_q(market_value),
            equity=_q(equity),
            drawdown=_q(drawdown),
        )
    )


def _benchmark_returns(
    benchmarks: Mapping[MarketKey, Sequence[PriceBar]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[MarketBenchmarkReturn, ...], Decimal | None]:
    results: list[MarketBenchmarkReturn] = []
    for market, bars in sorted(benchmarks.items()):
        bounded = [bar for bar in bars if start_at <= bar.timestamp <= end_at]
        if len(bounded) < 2:
            continue
        first, last = bounded[0], bounded[-1]
        results.append(
            MarketBenchmarkReturn(
                market=market,
                start_at=first.timestamp,
                end_at=last.timestamp,
                start_close=_q(first.close),
                end_close=_q(last.close),
                total_return=_q(last.close / first.close - _ONE),
            )
        )
    combined = (
        _q(
            sum((item.total_return for item in results), start=_ZERO)
            / Decimal(len(results))
        )
        if results
        else None
    )
    return tuple(results), combined


def _build_evidence(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    universe_evidence: UniverseEvidence,
    benchmark_return: Decimal | None,
    ranking_decisions: int,
    exclusion_counts: Mapping[str, int],
    open_position_count: int,
) -> tuple[BacktestEvidence, ...]:
    missing = sorted(
        f"{candidate.market}:{candidate.symbol}"
        for candidate in candidates
        if not bars_by_candidate[candidate.key]
    )
    quality_value = (
        f"candidates={len(candidates)};bars="
        f"{sum(len(bars) for bars in bars_by_candidate.values())};"
        f"missingSeries={len(missing)};rankingDecisions={ranking_decisions}"
    )
    survivorship_status = (
        "point_in_time_with_delisted"
        if universe_evidence.point_in_time_membership
        and universe_evidence.includes_delisted
        else "survivorship_caveat"
    )
    exclusion_text = ",".join(
        f"{reason}:{count}" for reason, count in sorted(exclusion_counts.items())
    )
    return (
        BacktestEvidence(
            code="EXECUTION_TIMING",
            value="next_available_bar_open",
            detail=(
                "Signals are created only after a complete observation bar and are "
                "eligible only at the first later bar open."
            ),
        ),
        BacktestEvidence(
            code="NO_LOOKAHEAD_BOUNDARY",
            value="incremental_histories_and_open_only_execution",
            detail=(
                "Strategies and ranking receive incrementally appended completed bars; "
                "execution receives timestamp and open price only."
            ),
        ),
        BacktestEvidence(
            code="DATA_QUALITY",
            value=quality_value,
            detail=(
                f"Missing series: {','.join(missing) or 'none'}. "
                f"Rank exclusions: {exclusion_text or 'none'}."
            ),
        ),
        BacktestEvidence(
            code="SURVIVORSHIP",
            value=survivorship_status,
            detail=(
                f"source={universe_evidence.source};"
                f"pointInTime={universe_evidence.point_in_time_membership};"
                f"includesDelisted={universe_evidence.includes_delisted};"
                "A fixed present-day universe can overstate historical results."
            ),
        ),
        BacktestEvidence(
            code="BENCHMARK",
            value=(
                str(benchmark_return) if benchmark_return is not None else "missing"
            ),
            detail=(
                "Portfolio benchmark return is the equal-weighted mean of supplied "
                "market benchmark returns; missing benchmarks leave excess return null."
            ),
        ),
        BacktestEvidence(
            code="BASE_CURRENCY",
            value="caller_normalized",
            detail=(
                "KR and US prices are assumed to be pre-normalized to one portfolio "
                "base currency; the engine performs no implicit FX conversion."
            ),
        ),
        BacktestEvidence(
            code="END_OF_DATA",
            value=f"openPositions={open_position_count}",
            detail=(
                "Open positions are marked to the final close and are not liquidated at "
                "that same close because same-bar execution is forbidden."
            ),
        ),
    )


def _execution_market(market: MarketKey) -> ExecutionMarket:
    return "KRX" if market == "KR" else "US"


def _median_level(results: Sequence[StrategyResult], field_name: str) -> Decimal | None:
    values = sorted(
        value
        for result in results
        if (value := getattr(result, field_name, None)) is not None
        and value.is_finite()
        and value > _ZERO
    )
    return values[len(values) // 2] if values else None


def _round_down(value: Decimal, lot: Decimal) -> Decimal:
    return (value / lot).to_integral_value(rounding=ROUND_DOWN) * lot


def _q(value: Decimal) -> Decimal:
    return value.quantize(_VALUE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _slice_candidate_bars(
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> dict[CandidateKey, tuple[PriceBar, ...]]:
    return {
        key: tuple(bar for bar in bars if start_at <= bar.timestamp <= end_at)
        for key, bars in bars_by_candidate.items()
    }


def _slice_benchmarks(
    benchmarks: Mapping[MarketKey, Sequence[PriceBar]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> dict[MarketKey, tuple[PriceBar, ...]]:
    return {
        market: tuple(bar for bar in bars if start_at <= bar.timestamp <= end_at)
        for market, bars in benchmarks.items()
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _stable_hash(value: object) -> str:
    payload = asdict(value)  # type: ignore[arg-type]
    payload.pop("determinism_hash", None)
    encoded = json.dumps(
        _json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BacktestPerformanceSlice",
    "BacktestEvidence",
    "BacktestWindow",
    "EquityPoint",
    "CostStressScenario",
    "MarketBenchmarkReturn",
    "MarketExecutionCost",
    "PortfolioBacktestConfig",
    "PortfolioBacktestResult",
    "PortfolioBacktestDiagnostics",
    "PortfolioOpenPosition",
    "PortfolioSignal",
    "PortfolioTrade",
    "SignalStatus",
    "SymbolRemovalScenario",
    "UniverseEvidence",
    "WalkForwardConfig",
    "WalkForwardFold",
    "WalkForwardResult",
    "run_portfolio_backtest",
    "run_portfolio_diagnostics",
    "run_walk_forward",
]
