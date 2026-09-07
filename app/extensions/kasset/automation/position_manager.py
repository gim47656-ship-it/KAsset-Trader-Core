"""Deterministic PAPER position lifecycle; never submits broker orders."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

_ZERO = Decimal("0")
_ONE = Decimal("1")


class ExitKind(StrEnum):
    STOP = "STOP"
    STOP_GAP = "STOP_GAP"
    PARTIAL_SELL = "PARTIAL_SELL"
    TRAILING_STOP = "TRAILING_STOP"
    TRAILING_STOP_GAP = "TRAILING_STOP_GAP"
    TIME_STOP = "TIME_STOP"
    TREND_BROKEN = "TREND_BROKEN"


@dataclass(frozen=True, slots=True)
class PositionManagerConfig:
    initial_stop_atr: Decimal = Decimal("3")
    partial_profit_atr: Decimal = Decimal("3")
    partial_fraction: Decimal = Decimal("0.5")
    trailing_stop_atr: Decimal = Decimal("3")
    max_holding_bars: int = 10
    no_progress_atr: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        positive_decimals = (
            self.initial_stop_atr,
            self.partial_profit_atr,
            self.trailing_stop_atr,
            self.no_progress_atr,
        )
        if any(not value.is_finite() or value <= _ZERO for value in positive_decimals):
            raise ValueError("position-manager ATR parameters must be positive")
        if (
            not self.partial_fraction.is_finite()
            or self.partial_fraction <= _ZERO
            or self.partial_fraction >= _ONE
        ):
            raise ValueError("partial_fraction must be between zero and one")
        if self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be positive")


@dataclass(frozen=True, slots=True)
class ManagedPositionState:
    market: str
    symbol: str
    entry_price: Decimal
    initial_atr: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    highest_close: Decimal
    partial_exit_completed: bool
    entry_at: datetime
    last_evaluated_at: datetime | None
    strategy_version: str
    position_cycle_id: int | None = None

    def __post_init__(self) -> None:
        market = self.market.strip().upper()
        symbol = self.symbol.strip().upper()
        strategy_version = self.strategy_version.strip()
        if market not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        if not symbol or not strategy_version:
            raise ValueError("symbol and strategy_version are required")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "strategy_version", strategy_version)
        if self.position_cycle_id is not None and self.position_cycle_id < 1:
            raise ValueError("position_cycle_id must be positive")
        for name, value in (
            ("entry_price", self.entry_price),
            ("initial_atr", self.initial_atr),
            ("initial_stop", self.initial_stop),
            ("current_stop", self.current_stop),
            ("highest_close", self.highest_close),
        ):
            if not value.is_finite() or value <= _ZERO:
                raise ValueError(f"{name} must be positive and finite")
        if self.entry_at.tzinfo is None or self.entry_at.utcoffset() is None:
            raise ValueError("entry_at must be timezone-aware")
        object.__setattr__(self, "entry_at", self.entry_at.astimezone(UTC))
        if self.last_evaluated_at is not None:
            if (
                self.last_evaluated_at.tzinfo is None
                or self.last_evaluated_at.utcoffset() is None
            ):
                raise ValueError("last_evaluated_at must be timezone-aware")
            object.__setattr__(
                self,
                "last_evaluated_at",
                self.last_evaluated_at.astimezone(UTC),
            )


@dataclass(frozen=True, slots=True)
class PositionBar:
    as_of: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("bar as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= _ZERO for value in prices):
            raise ValueError("bar prices must be positive and finite")
        if self.high < max(self.open, self.low, self.close) or self.low > min(
            self.open,
            self.high,
            self.close,
        ):
            raise ValueError("bar OHLC is inconsistent")


@dataclass(frozen=True, slots=True)
class PositionExitSignal:
    kind: ExitKind
    quantity_fraction: Decimal
    reference_price: Decimal
    signal_at: datetime
    reason: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PositionEvaluation:
    state: ManagedPositionState
    signal: PositionExitSignal | None


def initialize_position(
    *,
    market: str,
    symbol: str,
    entry_price: Decimal,
    initial_atr: Decimal,
    entry_at: datetime,
    strategy_version: str,
    position_cycle_id: int | None = None,
    config: PositionManagerConfig = PositionManagerConfig(),
) -> ManagedPositionState:
    if not entry_price.is_finite() or entry_price <= _ZERO:
        raise ValueError("entry_price must be positive and finite")
    if not initial_atr.is_finite() or initial_atr <= _ZERO:
        raise ValueError("initial_atr must be positive and finite")
    initial_stop = entry_price - config.initial_stop_atr * initial_atr
    if initial_stop <= _ZERO:
        raise ValueError("initial ATR stop must stay above zero")
    return ManagedPositionState(
        market=market,
        symbol=symbol,
        entry_price=entry_price,
        initial_atr=initial_atr,
        initial_stop=initial_stop,
        current_stop=initial_stop,
        highest_close=entry_price,
        partial_exit_completed=False,
        entry_at=entry_at,
        last_evaluated_at=None,
        strategy_version=strategy_version,
        position_cycle_id=position_cycle_id,
    )


def exit_signal_key(
    *,
    market: str,
    symbol: str,
    kind: ExitKind,
    signal_at: datetime,
    position_cycle_id: int | None = None,
) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market not in {"KRX", "US"} or not normalized_symbol:
        raise ValueError("market and symbol must identify a PAPER position")
    if signal_at.tzinfo is None or signal_at.utcoffset() is None:
        raise ValueError("signal_at must be timezone-aware")
    if position_cycle_id is not None and position_cycle_id < 1:
        raise ValueError("position_cycle_id must be positive")
    material = "|".join(
        (
            normalized_market,
            normalized_symbol,
            str(position_cycle_id) if position_cycle_id is not None else "",
            kind.value,
            signal_at.astimezone(UTC).isoformat(),
        )
    ).encode()
    return f"position-exit:{hashlib.sha256(material).hexdigest()[:24]}"


def _signal(
    state: ManagedPositionState,
    bar: PositionBar,
    *,
    kind: ExitKind,
    fraction: Decimal,
    price: Decimal,
    reason: str,
) -> PositionExitSignal:
    return PositionExitSignal(
        kind=kind,
        quantity_fraction=fraction,
        reference_price=price,
        signal_at=bar.as_of,
        reason=reason,
        idempotency_key=exit_signal_key(
            market=state.market,
            symbol=state.symbol,
            kind=kind,
            signal_at=bar.as_of,
            position_cycle_id=state.position_cycle_id,
        ),
    )


def evaluate_position(
    state: ManagedPositionState,
    bar: PositionBar,
    *,
    bars_held: int,
    trend_intact: bool = True,
    config: PositionManagerConfig = PositionManagerConfig(),
) -> PositionEvaluation:
    """Evaluate one completed **daily** bar; raised close-based stops apply next bar.

    이 함수가 상태 전이의 유일한 주인이다. trailing stop 상향, TIME_STOP,
    TREND_BROKEN은 모두 완료 일봉 horizon의 판정이며 일봉 backtest
    (:mod:`portfolio_backtest`)가 같은 의미로 재현한다. 장중 보호 평가는
    상태를 바꾸지 않는 :func:`evaluate_position_intraday`가 따로 맡는다.
    """

    if bar.as_of <= state.entry_at:
        raise ValueError("position bar must be after entry_at")
    if state.last_evaluated_at is not None and bar.as_of <= state.last_evaluated_at:
        raise ValueError("position bar must be newer than last_evaluated_at")
    if bars_held < 1:
        raise ValueError("bars_held must be positive")

    trailed = state.current_stop > state.initial_stop
    if bar.open <= state.current_stop:
        kind = ExitKind.TRAILING_STOP_GAP if trailed else ExitKind.STOP_GAP
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=kind,
                fraction=_ONE,
                price=bar.open,
                reason="시가가 기존 손절선 아래에서 형성되어 전량 청산합니다.",
            ),
        )
    if bar.low <= state.current_stop:
        kind = ExitKind.TRAILING_STOP if trailed else ExitKind.STOP
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=kind,
                fraction=_ONE,
                price=state.current_stop,
                reason="당일 저가가 기존 손절선에 닿아 전량 청산합니다.",
            ),
        )

    partial_target = state.entry_price + config.partial_profit_atr * state.initial_atr
    if not state.partial_exit_completed and bar.high >= partial_target:
        fill_reference = bar.open if bar.open >= partial_target else partial_target
        updated = replace(
            state,
            partial_exit_completed=True,
            highest_close=max(state.highest_close, bar.close),
            last_evaluated_at=bar.as_of,
        )
        return PositionEvaluation(
            state=updated,
            signal=_signal(
                state,
                bar,
                kind=ExitKind.PARTIAL_SELL,
                fraction=config.partial_fraction,
                price=fill_reference,
                reason=(
                    f"진입가 대비 +{config.partial_profit_atr} ATR에 도달해 "
                    f"보유수량의 {config.partial_fraction * Decimal('100')}%를 익절합니다."
                ),
            ),
        )

    if not trend_intact:
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=ExitKind.TREND_BROKEN,
                fraction=_ONE,
                price=bar.close,
                reason="추세 구조가 훼손되어 잔여수량을 청산합니다.",
            ),
        )

    progress = max(state.highest_close, bar.close) - state.entry_price
    if (
        bars_held >= config.max_holding_bars
        and progress < config.no_progress_atr * state.initial_atr
    ):
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=ExitKind.TIME_STOP,
                fraction=_ONE,
                price=bar.close,
                reason="최대 보유기간 동안 최소 진전폭을 만들지 못해 청산합니다.",
            ),
        )

    highest_close = max(state.highest_close, bar.close)
    current_stop = state.current_stop
    if state.partial_exit_completed:
        current_stop = max(
            current_stop,
            highest_close - config.trailing_stop_atr * state.initial_atr,
        )
    return PositionEvaluation(
        state=replace(
            state,
            current_stop=current_stop,
            highest_close=highest_close,
            last_evaluated_at=bar.as_of,
        ),
        signal=None,
    )


def evaluate_position_intraday(
    state: ManagedPositionState,
    bars: Sequence[PositionBar],
    *,
    bar_interval: timedelta,
    after: datetime | None = None,
    config: PositionManagerConfig = PositionManagerConfig(),
) -> PositionExitSignal | None:
    """Read stored levels against completed intraday buckets. Never mutates state.

    보유 종목의 손절은 다음 일봉을 기다릴 수 없다. 금요일 일봉까지만 평가된
    포지션도 월요일 장중에 저장된 손절선이 깨지면 그 자리에서 보호 청산이
    나와야 한다. 그래서 이 함수는 완료된 정규장 bucket만 읽어 **저장된**
    손절선·부분익절선 도달 여부만 판정한다.

    일봉 horizon과 의도적으로 분리한 것들:

    - 상태 전이가 없다. trailing stop 상향, ``highest_close``,
      ``last_evaluated_at``은 건드리지 않는다. 분봉 종가로 손절선을 올리면
      장중 잡음이 손절선을 끌어올리고, 분봉 시각을 ``last_evaluated_at``에
      쓰면 같은 날 완료 일봉이 영구히 "이미 평가됨"으로 막힌다.
    - ``TIME_STOP``/``TREND_BROKEN``은 만들지 않는다. 둘 다 일봉 보유기간과
      일봉 추세가 근거이므로 분을 보유 일수로 세는 순간 의미가 깨진다.

    진입 전에 시작한 bucket은 버린다. 진입 직전의 저가는 진입 후 손절 도달이
    아니다. 신호 시각은 그 bucket의 종료 시각이므로 같은 세션을 몇 번 다시
    평가해도 ``idempotency_key``가 같고, 중복 매도가 생기지 않는다.

    창 안에 전량 손절이 하나라도 있으면 **그중 가장 이른 것**을 돌려준다.
    시간순 첫 신호를 그대로 내면, 오전에 부분익절선을 찍고 오후에 손절선을
    관통한 세션에서 부분익절만 계속 반환되고 전량 손절이 영구히 가려진다.
    부분익절은 창 전체에 전량 손절이 없을 때만 돌려준다.

    ``after``가 있으면 그 시각 이후에 닫힌 bucket만 본다. 직전 보호 추천이
    거절·집행실패·기한만료로 종료됐을 때, 그 추천이 가리키는 bucket을 다시
    돌려서는 새 추천이 될 수 없다. 결정론 id가 같아 이미 종료된 행에 걸리고
    그날 재시도가 사라진다. 그래서 호출자가 마지막 종료 추천의 bucket 시각을
    경계로 넘긴다. 같은 bucket은 여전히 같은 id로 중복이 없고, 그 이후의 새
    bucket은 새 id와 최신 수량으로 다시 판정된다.
    """

    if bar_interval <= timedelta(0):
        raise ValueError("bar_interval must be positive")

    trailed = state.current_stop > state.initial_stop
    partial_target = state.entry_price + config.partial_profit_atr * state.initial_atr
    partial: PositionExitSignal | None = None
    for bar in sorted(bars, key=lambda item: item.as_of):
        if bar.as_of - bar_interval < state.entry_at:
            continue
        if after is not None and bar.as_of <= after:
            continue
        if bar.open <= state.current_stop:
            return _signal(
                state,
                bar,
                kind=ExitKind.TRAILING_STOP_GAP if trailed else ExitKind.STOP_GAP,
                fraction=_ONE,
                price=bar.open,
                reason="장중 봉 시가가 기존 손절선 아래여서 전량 청산합니다.",
            )
        if bar.low <= state.current_stop:
            return _signal(
                state,
                bar,
                kind=ExitKind.TRAILING_STOP if trailed else ExitKind.STOP,
                fraction=_ONE,
                price=state.current_stop,
                reason="장중 저가가 기존 손절선에 닿아 전량 청산합니다.",
            )
        if (
            partial is None
            and not state.partial_exit_completed
            and bar.high >= partial_target
        ):
            partial = _signal(
                state,
                bar,
                kind=ExitKind.PARTIAL_SELL,
                fraction=config.partial_fraction,
                price=bar.open if bar.open >= partial_target else partial_target,
                reason=(
                    f"장중에 진입가 대비 +{config.partial_profit_atr} ATR에 도달해 "
                    f"보유수량의 {config.partial_fraction * Decimal('100')}%를 익절합니다."
                ),
            )
    return partial
