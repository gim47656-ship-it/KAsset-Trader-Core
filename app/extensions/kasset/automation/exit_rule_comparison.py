"""진입을 고정하고 청산 설정만 바꿔 비교하는 오프라인 연구 도구.

포트폴리오 백테스트는 청산 설정이 바뀌면 현금·동시 보유 한도 때문에 이후 진입도
달라진다. 이 모듈은 기준 설정으로 한 번 실행해 실제로 체결된 진입 묶음을 고정하고,
그 진입마다 :class:`PositionManagerConfig` 변형을 독립적으로 재생해 순수한 청산
효과만 비교한다.

체결 규약은 :func:`run_portfolio_backtest`의 청산과 같다. 완료 일봉에서
:func:`evaluate_position`이 신호를 내면 ``execution_delay_bars`` 뒤 봉의 시가에
불리한 슬리피지·수수료·매도세를 반영해 체결하고, 체결 대기 중에는 재평가하지 않는다.
그 위에 두 가지 실행 제약을 덧붙인다.

- KRX 하한가 잠김: 체결 봉이 하루 종일 한 가격(고가=저가)이고 그 가격이 전일 종가
  대비 가격제한폭 하단이면 매도하지 못하고 다음 봉 시가로 넘긴다.
- 거래량 상한: 한 봉에서 청산할 수 있는 수량을 그 봉 거래량의 일정 비율로 제한하고
  남은 수량은 다음 봉으로 넘긴다.

모든 변형에서 데이터 끝(또는 ``BacktestWindow.end_at``)까지 전량 청산된 진입만
비교에 넣는다. 한 변형이라도 끝까지 들고 있거나 청산 상태를 만들지 못한 진입은
모든 변형의 집계에서 함께 빠지고 사유가 남는다. 강제 청산으로 구간 경계 손익을
만들지 않는다.

``STRATEGY_CODE_PATHS``에 없는 파일이라 운영 전략 fingerprint와 PAPER 승격 근거를
바꾸지 않는다. 주문·DB 쓰기·스케줄러 연결은 없다.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from app.extensions.kasset.automation.candidate_ranker import (
    CandidateKey,
    CandidateMetadata,
    CandidateRanker,
    _mean,
    _true_ranges,
)
from app.extensions.kasset.automation.contracts import (
    Action,
    DeterministicStrategy,
    PriceBar,
)
from app.extensions.kasset.automation.portfolio_backtest import (
    BacktestWindow,
    CandidateBenchmarkSeries,
    MarketExecutionCost,
    MarketKey,
    PortfolioBacktestConfig,
    PortfolioBacktestResult,
    SignalStatus,
    UniverseEvidence,
    _assess_regimes,
    _evaluate_breakout_ensemble,
    _execution_market,
    _normalize_candidate_bars,
    _q,
    _round_down,
    _validate_candidates,
    run_portfolio_backtest,
)
from app.extensions.kasset.automation.position_manager import (
    PositionBar,
    PositionManagerConfig,
    evaluate_position,
    initialize_position,
)
from app.extensions.kasset.automation.position_sizing import (
    DEFAULT_POSITION_SIZING_CONFIG,
)
from app.extensions.kasset.automation.regime import RegimeAssessment
from app.extensions.kasset.automation.strategies import STRATEGIES
from app.services.quote_parity_shadow import krx_tick_size

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: 비교 전에 고정한 청산 변형. 결과를 보고 고르지 않도록 코드에 박아 두며,
#: 새 변형은 결과를 보기 전에 이유와 함께 여기에 추가한다.
PRESET_EXIT_VARIANTS: Mapping[str, PositionManagerConfig] = MappingProxyType(
    {
        # 현재 운영 사다리(-2 ATR 손절, +0.5 ATR 30% 부분익절, 3거래일 조건부 시간청산).
        "current": PositionManagerConfig(),
        # 2026-09-30 이전 초기 손절. 09-28 일봉 연구에서 -2 ATR보다 나았던 값이다.
        "initial_stop_3atr": PositionManagerConfig(initial_stop_atr=Decimal("3")),
        # 09-28 연구에서 3거래일 조건부와 비교했던 10봉 보유 상한.
        "hold_10_bars": PositionManagerConfig(max_holding_bars=10),
        # 1차 익절을 +1.5 ATR로 늦춰 작은 이익을 일찍 자르는 효과를 본다.
        "late_partial_1_5atr": PositionManagerConfig(partial_profit_atr=Decimal("1.5")),
        # 부분익절 뒤 잔여 runner 추적폭을 3 → 2 ATR로 좁힌다.
        "runner_trail_2atr": PositionManagerConfig(trailing_stop_atr=Decimal("2")),
        # 부분익절 뒤 바닥을 본전에서 진입가 + 0.5 ATR로 올린다.
        "post_partial_floor_0_5atr": PositionManagerConfig(
            post_partial_floor_atr=Decimal("0.5")
        ),
    }
)

TrendIntact = Callable[[CandidateKey, datetime], bool]
SimulationStatus = Literal["closed", "open_at_end", "position_manager_rejected"]


@dataclass(frozen=True, slots=True)
class ExitExecutionConstraints:
    """기존 백테스트 체결 위에 덧붙이는 청산 실행 제약."""

    #: KRX 일일 가격제한폭. ``None``이면 하한가 잠김을 보지 않는다.
    krx_daily_limit_rate: Decimal | None = Decimal("0.30")
    #: 체결 봉 거래량 대비 한 봉의 청산 수량 상한. 기본값은 진입 사이징의
    #: 평균거래량 참여율과 같다. ``None``이면 제한하지 않는다.
    exit_volume_participation: Decimal | None = (
        DEFAULT_POSITION_SIZING_CONFIG.max_average_volume_participation
    )

    def __post_init__(self) -> None:
        for field_name in ("krx_daily_limit_rate", "exit_volume_participation"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, field_name, value)
            if not value.is_finite() or not _ZERO < value <= _ONE:
                raise ValueError(f"{field_name} must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class FixedEntry:
    """기준 실행에서 실제로 체결되어 모든 변형에 똑같이 주는 진입."""

    market: MarketKey
    symbol: str
    entry_signal_at: datetime
    entry_at: datetime
    entry_price: Decimal
    quantity: Decimal
    initial_atr: Decimal

    @property
    def key(self) -> CandidateKey:
        return (self.market, self.symbol)


@dataclass(frozen=True, slots=True)
class ExitFill:
    exit_signal_at: datetime
    exit_at: datetime
    reason: str
    quantity: Decimal
    fill_price: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class ExitSimulation:
    status: SimulationStatus
    fills: tuple[ExitFill, ...]
    net_pnl: Decimal
    #: 진입 봉부터 마지막 청산 체결 봉까지의 봉 수.
    holding_bars: int
    #: 하한가 잠김으로 청산 체결을 다음 봉으로 미룬 횟수.
    limit_down_deferrals: int
    #: 거래량 상한 때문에 남은 청산 수량을 다음 봉으로 넘긴 횟수.
    volume_capped_bars: int


@dataclass(frozen=True, slots=True)
class EntryVariantOutcome:
    entry: FixedEntry
    variant: str
    simulation: ExitSimulation


@dataclass(frozen=True, slots=True)
class ExcludedEntry:
    market: MarketKey
    symbol: str
    entry_at: datetime
    variant: str
    reason: SimulationStatus


@dataclass(frozen=True, slots=True)
class ExitVariantSummary:
    name: str
    entry_count: int
    total_net_pnl: Decimal
    mean_net_pnl: Decimal
    #: 진입 금액 대비 순손익 평균.
    mean_return_on_notional: Decimal
    #: ``순손익 / (수량 × 진입 ATR)`` 평균. 변형마다 손절폭이 달라도 같은 단위다.
    mean_net_pnl_per_atr: Decimal
    win_rate: Decimal
    mean_holding_bars: Decimal
    limit_down_deferrals: int
    volume_capped_bars: int
    exit_reasons: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ExitRuleComparisonResult:
    end_at: datetime
    fixed_entry_count: int
    compared_entry_count: int
    summaries: tuple[ExitVariantSummary, ...]
    excluded: tuple[ExcludedEntry, ...]
    outcomes: tuple[EntryVariantOutcome, ...]
    baseline_determinism_hash: str | None = None


@dataclass(slots=True)
class _PendingExit:
    signal_index: int
    signal_at: datetime
    fraction: Decimal
    reason: str
    #: 첫 체결 시도 때 정해지는 이 주문의 남은 수량.
    remaining: Decimal | None = None


class BreakoutTrendLookup:
    """기준 백테스트와 같은 돌파 앙상블·시장 레짐으로 ``trend_intact``를 판정한다.

    청산 설정과 무관한 값이라 (종목, 시각)마다 한 번만 계산해 모든 변형이 공유한다.
    """

    def __init__(
        self,
        bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
        *,
        strategies: Sequence[DeterministicStrategy] = STRATEGIES,
    ) -> None:
        self._bars = bars_by_candidate
        self._timestamps = {
            key: tuple(bar.timestamp for bar in series)
            for key, series in bars_by_candidate.items()
        }
        self._strategies = tuple(strategies)
        self._regimes: dict[datetime, Mapping[MarketKey, RegimeAssessment]] = {}
        self._cache: dict[tuple[CandidateKey, datetime], bool] = {}

    def _history(self, key: CandidateKey, as_of: datetime) -> Sequence[PriceBar]:
        end = bisect_right(self._timestamps[key], as_of)
        return self._bars[key][:end]

    def __call__(self, key: CandidateKey, as_of: datetime) -> bool:
        cached = self._cache.get((key, as_of))
        if cached is not None:
            return cached
        regimes = self._regimes.get(as_of)
        if regimes is None:
            regimes = _assess_regimes(
                {candidate: self._history(candidate, as_of) for candidate in self._bars}
            )
            self._regimes[as_of] = regimes
        decision = _evaluate_breakout_ensemble(
            key,
            self._history(key, as_of),
            timestamp=as_of,
            strategies=self._strategies,
            regime=regimes[key[0]],
        )
        intact = decision.action != Action.SELL
        self._cache[(key, as_of)] = intact
        return intact


def extract_fixed_entries(
    result: PortfolioBacktestResult,
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    config: PortfolioBacktestConfig,
    history_bars: int,
) -> tuple[FixedEntry, ...]:
    """기준 실행의 체결된 BUY마다 진입가·수량·진입 ATR을 복원한다.

    진입 ATR은 :class:`CandidateRanker`의 ``atr_14``와 같은 정의(최근
    ``history_bars`` 이력의 마지막 14개 true range 평균)를 신호 봉까지의 이력으로
    다시 계산한다. 수량은 같은 진입의 모든 청산 체결과 남은 보유 수량의 합이다.
    """

    quantities: dict[tuple[str, str, datetime], Decimal] = {}
    for trade in result.trades:
        identity = (trade.market, trade.symbol, trade.entry_at)
        quantities[identity] = quantities.get(identity, _ZERO) + trade.quantity
    for position in result.open_positions:
        identity = (position.market, position.symbol, position.entry_at)
        quantities[identity] = quantities.get(identity, _ZERO) + position.quantity

    timestamps = {
        key: tuple(bar.timestamp for bar in series)
        for key, series in bars_by_candidate.items()
    }
    entries: list[FixedEntry] = []
    for signal in result.signals:
        if signal.action != Action.BUY or signal.status != SignalStatus.EXECUTED:
            continue
        execution_at = signal.execution_at
        reference_open = signal.reference_open
        if execution_at is None or reference_open is None:
            raise ValueError("executed BUY signal is missing its fill evidence")
        quantity = quantities.get((signal.market, signal.symbol, execution_at))
        if quantity is None or quantity <= _ZERO:
            raise ValueError(
                f"executed BUY {signal.market}:{signal.symbol} has no position quantity"
            )
        key: CandidateKey = (signal.market, signal.symbol)
        end = bisect_right(timestamps[key], signal.signal_at)
        history = bars_by_candidate[key][:end]
        cost = config.cost_for(signal.market)
        slippage_rate = (
            cost.slippage_rate if config.slippage_mode == "adverse_rate" else _ZERO
        )
        entries.append(
            FixedEntry(
                market=signal.market,
                symbol=signal.symbol,
                entry_signal_at=signal.signal_at,
                entry_at=execution_at,
                entry_price=reference_open * (_ONE + slippage_rate),
                quantity=quantity,
                initial_atr=_mean(_true_ranges(history[-history_bars:])[-14:]),
            )
        )
    return tuple(
        sorted(entries, key=lambda item: (item.entry_at, item.market, item.symbol))
    )


def simulate_fixed_entry(
    entry: FixedEntry,
    bars: Sequence[PriceBar],
    *,
    variant: PositionManagerConfig,
    config: PortfolioBacktestConfig,
    constraints: ExitExecutionConstraints,
    trend_intact: TrendIntact,
    end_at: datetime,
) -> ExitSimulation:
    """고정 진입 하나를 한 청산 변형으로 데이터 끝까지 재생한다."""

    if config.entry_fill != "next_open":
        raise ValueError("exit-rule comparison supports next_open fills only")
    execution_market = _execution_market(entry.market)
    cost = config.cost_for(entry.market)
    slippage_rate = (
        cost.slippage_rate if config.slippage_mode == "adverse_rate" else _ZERO
    )
    lot = config.position_sizing.lot_size(execution_market)
    try:
        state = initialize_position(
            market=execution_market,
            symbol=entry.symbol,
            entry_price=entry.entry_price,
            initial_atr=entry.initial_atr,
            entry_at=entry.entry_at,
            strategy_version=config.strategy_version,
            config=variant,
        )
    except ValueError:
        return _simulation(
            "position_manager_rejected",
            (),
            holding_bars=0,
            limit_down_deferrals=0,
            volume_capped_bars=0,
        )

    timestamps = [bar.timestamp for bar in bars]
    entry_index = bisect_left(timestamps, entry.entry_at)
    if entry_index >= len(bars) or bars[entry_index].timestamp != entry.entry_at:
        raise ValueError(f"entry bar {entry.entry_at.isoformat()} is missing")

    quantity = entry.quantity
    entry_fee_remaining = max(
        entry.entry_price * entry.quantity * cost.fee_rate,
        cost.min_fee_absolute,
    )
    fills: list[ExitFill] = []
    pending: _PendingExit | None = None
    bars_held = 0
    limit_down_deferrals = 0
    volume_capped_bars = 0

    for index in range(entry_index + 1, len(bars)):
        bar = bars[index]
        if bar.timestamp > end_at:
            break
        if (
            pending is not None
            and index >= pending.signal_index + config.execution_delay_bars
        ):
            remaining = pending.remaining
            if remaining is None:
                remaining = (
                    quantity
                    if pending.fraction >= _ONE
                    else _round_down(quantity * pending.fraction, lot)
                )
                pending.remaining = remaining
            fill_quantity = remaining
            if remaining <= _ZERO:
                # 기준 백테스트처럼 최소 단위 미만 부분청산은 버리고 같은 봉을 평가한다.
                pending = None
            elif _locked_limit_down(
                entry.market,
                bar,
                previous_close=bars[index - 1].close,
                constraints=constraints,
            ):
                limit_down_deferrals += 1
                fill_quantity = _ZERO
            elif constraints.exit_volume_participation is not None:
                capacity = _round_down(
                    bar.volume * constraints.exit_volume_participation, lot
                )
                if capacity < remaining:
                    volume_capped_bars += 1
                    fill_quantity = capacity
            if pending is not None and fill_quantity > _ZERO:
                entry_fee = (
                    entry_fee_remaining
                    if fill_quantity == quantity
                    else entry_fee_remaining * (fill_quantity / quantity)
                )
                fill = _exit_fill(
                    entry,
                    pending,
                    bar,
                    quantity=fill_quantity,
                    entry_fee=entry_fee,
                    cost=cost,
                    slippage_rate=slippage_rate,
                )
                fills.append(fill)
                quantity -= fill_quantity
                entry_fee_remaining -= entry_fee
                remaining -= fill_quantity
                pending.remaining = remaining
                if remaining <= _ZERO:
                    pending = None
                if quantity <= _ZERO:
                    return _simulation(
                        "closed",
                        fills,
                        holding_bars=index - entry_index,
                        limit_down_deferrals=limit_down_deferrals,
                        volume_capped_bars=volume_capped_bars,
                    )
        if pending is not None:
            continue
        bars_held += 1
        evaluation = evaluate_position(
            state,
            PositionBar(
                as_of=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            ),
            bars_held=bars_held,
            trend_intact=trend_intact(entry.key, bar.timestamp),
            config=variant,
        )
        state = evaluation.state
        if evaluation.signal is not None:
            pending = _PendingExit(
                signal_index=index,
                signal_at=bar.timestamp,
                fraction=evaluation.signal.quantity_fraction,
                reason=evaluation.signal.kind.value,
            )

    return _simulation(
        "open_at_end",
        fills,
        holding_bars=0,
        limit_down_deferrals=limit_down_deferrals,
        volume_capped_bars=volume_capped_bars,
    )


def compare_exit_variants(
    entries: Sequence[FixedEntry],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    variants: Mapping[str, PositionManagerConfig],
    config: PortfolioBacktestConfig,
    constraints: ExitExecutionConstraints,
    trend_intact: TrendIntact,
    end_at: datetime,
) -> ExitRuleComparisonResult:
    """모든 변형에서 끝까지 청산된 진입만으로 변형별 성과를 집계한다."""

    if not variants:
        raise ValueError("at least one exit variant is required")
    if any(not name.strip() for name in variants):
        raise ValueError("exit variant names must not be blank")

    outcomes: list[EntryVariantOutcome] = []
    excluded: list[ExcludedEntry] = []
    compared: list[tuple[FixedEntry, dict[str, ExitSimulation]]] = []
    for entry in entries:
        simulations = {
            name: simulate_fixed_entry(
                entry,
                bars_by_candidate[entry.key],
                variant=variant,
                config=config,
                constraints=constraints,
                trend_intact=trend_intact,
                end_at=end_at,
            )
            for name, variant in variants.items()
        }
        outcomes.extend(
            EntryVariantOutcome(entry=entry, variant=name, simulation=simulation)
            for name, simulation in simulations.items()
        )
        not_closed = [
            ExcludedEntry(
                market=entry.market,
                symbol=entry.symbol,
                entry_at=entry.entry_at,
                variant=name,
                reason=simulation.status,
            )
            for name, simulation in simulations.items()
            if simulation.status != "closed"
        ]
        if not_closed:
            excluded.extend(not_closed)
        else:
            compared.append((entry, simulations))

    summaries = tuple(
        _summarize(
            name,
            [(entry, simulations[name]) for entry, simulations in compared],
        )
        for name in variants
    )
    return ExitRuleComparisonResult(
        end_at=end_at,
        fixed_entry_count=len(entries),
        compared_entry_count=len(compared),
        summaries=summaries,
        excluded=tuple(excluded),
        outcomes=tuple(outcomes),
    )


def run_exit_rule_comparison(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    variants: Mapping[str, PositionManagerConfig] = PRESET_EXIT_VARIANTS,
    config: PortfolioBacktestConfig = PortfolioBacktestConfig(),
    constraints: ExitExecutionConstraints = ExitExecutionConstraints(),
    benchmark_bars_by_market: Mapping[MarketKey, Sequence[PriceBar]] | None = None,
    benchmark_bars_by_candidate: (
        Mapping[CandidateKey, CandidateBenchmarkSeries] | None
    ) = None,
    universe_evidence: UniverseEvidence = UniverseEvidence(),
    strategies: Sequence[DeterministicStrategy] = STRATEGIES,
    ranker: CandidateRanker | None = None,
    window: BacktestWindow | None = None,
) -> ExitRuleComparisonResult:
    """``config``로 기준 백테스트를 돌려 진입을 고정한 뒤 청산 변형을 비교한다.

    고정 진입은 ``config.position_manager``의 청산으로 만들어진 현금·보유 흐름에서
    나온다. 진입 경로는 기본 돌파 baseline이다.
    """

    metadata = _validate_candidates(candidates)
    bars = _normalize_candidate_bars(metadata, bars_by_candidate)
    active_ranker = ranker or CandidateRanker()
    baseline = run_portfolio_backtest(
        metadata,
        bars,
        config=config,
        benchmark_bars_by_market=benchmark_bars_by_market,
        benchmark_bars_by_candidate=benchmark_bars_by_candidate,
        universe_evidence=universe_evidence,
        strategies=strategies,
        ranker=active_ranker,
        window=window,
    )
    end_at = (
        window.end_at
        if window is not None
        else max(bar.timestamp for series in bars.values() for bar in series)
    )
    entries = extract_fixed_entries(
        baseline,
        bars,
        config=config,
        history_bars=active_ranker.config.history_bars,
    )
    result = compare_exit_variants(
        entries,
        bars,
        variants=variants,
        config=config,
        constraints=constraints,
        trend_intact=BreakoutTrendLookup(bars, strategies=strategies),
        end_at=end_at,
    )
    return replace(result, baseline_determinism_hash=baseline.determinism_hash)


def _locked_limit_down(
    market: MarketKey,
    bar: PriceBar,
    *,
    previous_close: Decimal,
    constraints: ExitExecutionConstraints,
) -> bool:
    if market != "KR" or constraints.krx_daily_limit_rate is None:
        return False
    if bar.high != bar.low:
        return False
    floor = previous_close * (_ONE - constraints.krx_daily_limit_rate)
    # 실제 하한가는 호가 단위로 맞춰지므로 한 틱 여유를 둔다.
    return bar.low <= floor + krx_tick_size(floor)


def _exit_fill(
    entry: FixedEntry,
    pending: _PendingExit,
    bar: PriceBar,
    *,
    quantity: Decimal,
    entry_fee: Decimal,
    cost: MarketExecutionCost,
    slippage_rate: Decimal,
) -> ExitFill:
    fill_price = bar.open * (_ONE - slippage_rate)
    exit_notional = fill_price * quantity
    exit_fee = max(exit_notional * cost.fee_rate, cost.min_fee_absolute)
    exit_tax = exit_notional * cost.sell_tax_rate
    net_pnl = (
        (fill_price - entry.entry_price) * quantity - entry_fee - exit_fee - exit_tax
    )
    return ExitFill(
        exit_signal_at=pending.signal_at,
        exit_at=bar.timestamp,
        reason=pending.reason,
        quantity=_q(quantity),
        fill_price=_q(fill_price),
        net_pnl=_q(net_pnl),
    )


def _simulation(
    status: SimulationStatus,
    fills: Sequence[ExitFill],
    *,
    holding_bars: int,
    limit_down_deferrals: int,
    volume_capped_bars: int,
) -> ExitSimulation:
    return ExitSimulation(
        status=status,
        fills=tuple(fills),
        net_pnl=sum((fill.net_pnl for fill in fills), start=_ZERO),
        holding_bars=holding_bars,
        limit_down_deferrals=limit_down_deferrals,
        volume_capped_bars=volume_capped_bars,
    )


def _summarize(
    name: str,
    rows: Sequence[tuple[FixedEntry, ExitSimulation]],
) -> ExitVariantSummary:
    count = len(rows)
    if count == 0:
        return ExitVariantSummary(
            name=name,
            entry_count=0,
            total_net_pnl=_ZERO,
            mean_net_pnl=_ZERO,
            mean_return_on_notional=_ZERO,
            mean_net_pnl_per_atr=_ZERO,
            win_rate=_ZERO,
            mean_holding_bars=_ZERO,
            limit_down_deferrals=0,
            volume_capped_bars=0,
            exit_reasons=(),
        )
    reasons = Counter(fill.reason for _, simulation in rows for fill in simulation.fills)
    total = sum((simulation.net_pnl for _, simulation in rows), start=_ZERO)
    return_sum = sum(
        (
            simulation.net_pnl / (entry.entry_price * entry.quantity)
            for entry, simulation in rows
        ),
        start=_ZERO,
    )
    atr_sum = sum(
        (
            simulation.net_pnl / (entry.quantity * entry.initial_atr)
            for entry, simulation in rows
        ),
        start=_ZERO,
    )
    wins = sum(simulation.net_pnl > _ZERO for _, simulation in rows)
    holding = sum(simulation.holding_bars for _, simulation in rows)
    denominator = Decimal(count)
    return ExitVariantSummary(
        name=name,
        entry_count=count,
        total_net_pnl=_q(total),
        mean_net_pnl=_q(total / denominator),
        mean_return_on_notional=_q(return_sum / denominator),
        mean_net_pnl_per_atr=_q(atr_sum / denominator),
        win_rate=_q(Decimal(wins) / denominator),
        mean_holding_bars=_q(Decimal(holding) / denominator),
        limit_down_deferrals=sum(
            simulation.limit_down_deferrals for _, simulation in rows
        ),
        volume_capped_bars=sum(simulation.volume_capped_bars for _, simulation in rows),
        exit_reasons=tuple(sorted(reasons.items())),
    )
