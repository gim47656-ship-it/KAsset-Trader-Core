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
    #: 1차 익절 도달선. KR 후보의 ATR 중앙값이 진입가의 3~5%라 +3 ATR은 하루
    #: 안에 닿지 않는다. 장중 평가(:func:`evaluate_position_intraday`)가 실제로
    #: 판정할 수 있는 구간으로 내리고, 대신 일부 수량만 턴다.
    partial_profit_atr: Decimal = Decimal("0.5")
    partial_fraction: Decimal = Decimal("0.3")
    trailing_stop_atr: Decimal = Decimal("3")
    #: 부분익절 전 구간에서 보호선을 켜기 시작하는 최소 진전폭. 진입 직후의
    #: 잡음이 손절선을 끌어올리지 않도록 활성화를 이 진전폭 뒤로 미룬다.
    early_trailing_activation_atr: Decimal = Decimal("1")
    #: 부분익절 전 구간의 보호선 폭. 아직 아무것도 실현하지 못한 구간이므로
    #: 잔여 runner용 ``trailing_stop_atr``보다 좁게 둔다.
    early_trailing_stop_atr: Decimal = Decimal("2")
    #: 부분익절 뒤 잔여 수량의 최소 보호선을 ``진입가 + 이 배수 * ATR``로 둔다.
    #: ``0``은 본전이다. 고정 퍼센트가 아니라 ATR 배수이며 손절선은 계속
    #: 단조 상승만 한다.
    post_partial_floor_atr: Decimal = Decimal("0")
    max_holding_bars: int = 10
    no_progress_atr: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        positive_decimals = (
            self.initial_stop_atr,
            self.partial_profit_atr,
            self.trailing_stop_atr,
            self.early_trailing_activation_atr,
            self.early_trailing_stop_atr,
            self.no_progress_atr,
        )
        if any(not value.is_finite() or value <= _ZERO for value in positive_decimals):
            raise ValueError("position-manager ATR parameters must be positive")
        if (
            not self.post_partial_floor_atr.is_finite()
            or self.post_partial_floor_atr < _ZERO
        ):
            raise ValueError("post_partial_floor_atr must be finite and non-negative")
        if (
            not self.partial_fraction.is_finite()
            or self.partial_fraction <= _ZERO
            or self.partial_fraction >= _ONE
        ):
            raise ValueError("partial_fraction must be between zero and one")
        if self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be positive")


@dataclass(frozen=True, slots=True)
class ExitLevelVersion:
    """활성 시각 순서로 보존하는 이전 stop/ATR snapshot."""

    effective_at: datetime | None
    initial_atr: Decimal
    initial_stop: Decimal
    current_stop: Decimal

    def __post_init__(self) -> None:
        if self.effective_at is not None:
            if (
                self.effective_at.tzinfo is None
                or self.effective_at.utcoffset() is None
            ):
                raise ValueError("exit-level effective_at must be timezone-aware")
            object.__setattr__(
                self,
                "effective_at",
                self.effective_at.astimezone(UTC),
            )
        if not self.initial_atr.is_finite() or self.initial_atr <= _ZERO:
            raise ValueError("exit-level initial_atr must be positive and finite")
        for name, value in (
            ("initial_stop", self.initial_stop),
            ("current_stop", self.current_stop),
        ):
            if not value.is_finite() or value <= _ZERO:
                raise ValueError(f"exit-level {name} must be positive and finite")


@dataclass(frozen=True, slots=True)
class ManagedPositionState:
    market: str
    symbol: str
    entry_price: Decimal
    #: 진입 시점의 ATR. 초기·trailing 손절선과 부분익절선, TIME_STOP의 유일한
    #: 변동성 근거다.
    initial_atr: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    highest_close: Decimal
    partial_exit_completed: bool
    entry_at: datetime
    last_evaluated_at: datetime | None
    strategy_version: str
    position_cycle_id: int | None = None
    #: 현재 stop/ATR snapshot이 유효해진 시각. ``None``은 migration 전부터
    #: 존재하던 legacy snapshot으로, provenance를 추측하지 않고 과거 전체에
    #: 유효했던 것으로 취급한다.
    exit_levels_effective_at: datetime | None = None
    #: 늦게 적재된 관측도 당시 유효했던 stop으로 평가할 수 있게 보존하는
    #: 이전 snapshot들. activation 오름차순이며 첫 legacy version만 ``None``을 쓴다.
    exit_level_history: tuple[ExitLevelVersion, ...] = ()

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
            ("initial_stop", self.initial_stop),
            ("current_stop", self.current_stop),
            ("highest_close", self.highest_close),
        ):
            if not value.is_finite() or value <= _ZERO:
                raise ValueError(f"{name} must be positive and finite")
        if not self.initial_atr.is_finite() or self.initial_atr <= _ZERO:
            raise ValueError("initial_atr must be positive and finite")
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
        if self.exit_levels_effective_at is not None:
            if (
                self.exit_levels_effective_at.tzinfo is None
                or self.exit_levels_effective_at.utcoffset() is None
            ):
                raise ValueError("exit_levels_effective_at must be timezone-aware")
            object.__setattr__(
                self,
                "exit_levels_effective_at",
                self.exit_levels_effective_at.astimezone(UTC),
            )
        previous_effective_at: datetime | None = None
        for index, version in enumerate(self.exit_level_history):
            if index > 0 and version.effective_at is None:
                raise ValueError("only the first exit-level version may be legacy")
            if (
                previous_effective_at is not None
                and version.effective_at is not None
                and version.effective_at <= previous_effective_at
            ):
                raise ValueError("exit-level history must be strictly ordered")
            if version.effective_at is not None:
                previous_effective_at = version.effective_at
        if self.exit_level_history:
            if self.exit_levels_effective_at is None:
                raise ValueError("current exit-level version needs an effective_at")
            if (
                previous_effective_at is not None
                and self.exit_levels_effective_at <= previous_effective_at
            ):
                raise ValueError("current exit-level version must follow its history")


@dataclass(frozen=True, slots=True)
class PositionBar:
    as_of: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    #: 실제 OHLC 관측 구간. ``as_of``는 결정론 신호 identity용 일봉 label일 수
    #: 있으므로 activation 비교에는 이 두 instant만 사용한다.
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("bar as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        starts_at = self.as_of if self.starts_at is None else self.starts_at
        ends_at = self.as_of if self.ends_at is None else self.ends_at
        for name, value in (("starts_at", starts_at), ("ends_at", ends_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"bar {name} must be timezone-aware")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if starts_at > ends_at:
            raise ValueError("bar starts_at must not follow ends_at")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)
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
    initial_atr: Decimal
    initial_stop: Decimal
    current_stop: Decimal


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
    exit_levels_effective_at: datetime | None = None,
    config: PositionManagerConfig = PositionManagerConfig(),
) -> ManagedPositionState:
    """ATR 근거로 초기 손절선(진입가 - ``initial_stop_atr`` ATR)을 세운다.

    ATR이 없으면 손절선을 발명하지 않는다. 호출자는 ATR을 만들 근거가 없을 때
    이 함수를 부르지 않고 그 보유분을 관리하지 않는 쪽을 택한다.
    """

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
        exit_levels_effective_at=exit_levels_effective_at or entry_at,
    )


def _transition_exit_levels(
    state: ManagedPositionState,
    *,
    initial_atr: Decimal,
    initial_stop: Decimal,
    current_stop: Decimal,
    effective_at: datetime,
) -> ManagedPositionState:
    """같은 instant 변경은 합치고, 새 activation이면 이전 snapshot을 보존한다."""

    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
        raise ValueError("exit-level effective_at must be timezone-aware")
    activation = effective_at.astimezone(UTC)
    if activation < state.entry_at:
        raise ValueError("exit-level effective_at must not precede entry_at")
    if (
        initial_atr == state.initial_atr
        and initial_stop == state.initial_stop
        and current_stop == state.current_stop
    ):
        return state
    if (
        state.exit_levels_effective_at is not None
        and activation < state.exit_levels_effective_at
    ):
        raise ValueError("exit-level activation must not precede current version")
    if activation == state.exit_levels_effective_at:
        return replace(
            state,
            initial_atr=initial_atr,
            initial_stop=max(state.initial_stop, initial_stop),
            current_stop=max(state.current_stop, current_stop),
        )
    previous = ExitLevelVersion(
        effective_at=state.exit_levels_effective_at,
        initial_atr=state.initial_atr,
        initial_stop=state.initial_stop,
        current_stop=state.current_stop,
    )
    return replace(
        state,
        initial_atr=initial_atr,
        initial_stop=initial_stop,
        current_stop=current_stop,
        exit_levels_effective_at=activation,
        exit_level_history=(*state.exit_level_history, previous),
    )


def _raise_trailing_stop(
    state: ManagedPositionState,
    *,
    raised_stop: Decimal,
    effective_at: datetime,
) -> ManagedPositionState:
    """실제 close 위치에 trailing version을 넣고 이후 보호선에도 전파한다."""

    if not raised_stop.is_finite() or raised_stop <= _ZERO:
        raise ValueError("trailing stop must be positive and finite")
    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
        raise ValueError("trailing stop effective_at must be timezone-aware")
    activation = effective_at.astimezone(UTC)
    if activation < state.entry_at:
        raise ValueError("trailing stop effective_at must not precede entry_at")

    versions = [
        *state.exit_level_history,
        ExitLevelVersion(
            effective_at=state.exit_levels_effective_at,
            initial_atr=state.initial_atr,
            initial_stop=state.initial_stop,
            current_stop=state.current_stop,
        ),
    ]
    insertion = len(versions)
    for index, version in enumerate(versions):
        if version.effective_at is not None and version.effective_at >= activation:
            insertion = index
            break

    if insertion < len(versions) and versions[insertion].effective_at == activation:
        changed_from = insertion
    else:
        source_index = insertion - 1
        if source_index < 0:
            raise ValueError("exit-level history does not cover trailing activation")
        source = versions[source_index]
        versions.insert(
            insertion,
            ExitLevelVersion(
                effective_at=activation,
                initial_atr=source.initial_atr,
                initial_stop=source.initial_stop,
                current_stop=max(source.current_stop, raised_stop),
            ),
        )
        changed_from = insertion

    versions = [
        (
            replace(version, current_stop=max(version.current_stop, raised_stop))
            if index >= changed_from
            else version
        )
        for index, version in enumerate(versions)
    ]
    current = versions[-1]
    return replace(
        state,
        initial_atr=current.initial_atr,
        initial_stop=current.initial_stop,
        current_stop=current.current_stop,
        exit_levels_effective_at=current.effective_at,
        exit_level_history=tuple(versions[:-1]),
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


def _exit_levels_cover_observation(
    state: ManagedPositionState,
    starts_at: datetime,
) -> bool:
    earliest = (
        state.exit_level_history[0].effective_at
        if state.exit_level_history
        else state.exit_levels_effective_at
    )
    return earliest is None or starts_at >= earliest


def _exit_levels_for_bar(
    state: ManagedPositionState,
    bar: PositionBar,
) -> tuple[Decimal, Decimal, Decimal]:
    """이 관측 구간이 시작할 때 유효했던 snapshot을 돌려준다."""

    starts_at = bar.starts_at
    if starts_at is None:
        raise ValueError("position bar starts_at is required")
    effective_at = state.exit_levels_effective_at
    if effective_at is None or starts_at >= effective_at:
        return state.initial_atr, state.initial_stop, state.current_stop
    for version in reversed(state.exit_level_history):
        if version.effective_at is None or starts_at >= version.effective_at:
            return version.initial_atr, version.initial_stop, version.current_stop
    raise ValueError("exit-level history does not cover the observation")


def _signal(
    state: ManagedPositionState,
    bar: PositionBar,
    *,
    kind: ExitKind,
    fraction: Decimal,
    price: Decimal,
    reason: str,
    exit_levels: tuple[Decimal, Decimal, Decimal] | None = None,
) -> PositionExitSignal:
    initial_atr, initial_stop, current_stop = exit_levels or (
        state.initial_atr,
        state.initial_stop,
        state.current_stop,
    )
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
        initial_atr=initial_atr,
        initial_stop=initial_stop,
        current_stop=current_stop,
    )


def _protective_stop(
    state: ManagedPositionState,
    *,
    highest_close: Decimal,
    initial_atr: Decimal,
    config: PositionManagerConfig,
) -> Decimal | None:
    """이 일봉까지의 관측으로 정당화되는 보호선. 올릴 것이 없으면 ``None``.

    부분익절 전에는 아무것도 실현하지 못한 구간이라 진전폭이
    ``early_trailing_activation_atr``을 넘은 뒤부터 좁은
    ``early_trailing_stop_atr``로 따라 올린다. 활성화 전에는 ``None``을
    돌려주어 진입 직후의 잡음이 손절선을 끌어올리지 못하게 한다.

    부분익절 뒤에는 잔여 runner에게 넓은 ``trailing_stop_atr``을 주되, 이미
    일부를 실현했으므로 ``진입가 + post_partial_floor_atr * ATR`` 아래로는
    내려가지 않는다. 이 바닥이 없으면 1차 익절이 상승만 잘라내고 잔여 수량의
    위험은 그대로 -3 ATR에 남아 손익비가 오히려 나빠진다(백테스트 근거는
    ``doc/history/2026/09/22-sell-strategy-app-surface/evidence/`` 참조).

    반환값은 후보일 뿐이고 실제 적용은 호출부가 현재 손절선보다 높을 때만
    한다. 초기 손절선은 여기서 계산하지 않으므로 ``entry - 3 ATR`` 계약은
    그대로이며, 손절선은 계속 단조 상승만 한다.
    """

    if state.partial_exit_completed:
        return max(
            state.entry_price + config.post_partial_floor_atr * initial_atr,
            highest_close - config.trailing_stop_atr * initial_atr,
        )
    if (
        highest_close - state.entry_price
        < config.early_trailing_activation_atr * initial_atr
    ):
        return None
    return highest_close - config.early_trailing_stop_atr * initial_atr


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

    청산 사다리는 다섯 단이다. 초기 손절선 ``진입가 - initial_stop_atr * ATR``,
    진전폭이 ``early_trailing_activation_atr``을 넘은 뒤 켜지는
    ``highest_close - early_trailing_stop_atr * ATR`` 보호선,
    ``+partial_profit_atr``의 부분익절, 그 뒤 잔여 수량의
    ``진입가 + post_partial_floor_atr * ATR`` 바닥과
    ``highest_close - trailing_stop_atr * ATR`` 보호선, 그리고 진전폭이
    사라진 포지션을 거두는 TIME_STOP이다. 보호선은 단조 상승만 하므로 어느
    단도 초기 손절선을 넓히거나 좁히지 않는다.
    """

    if bar.as_of <= state.entry_at:
        raise ValueError("position bar must be after entry_at")
    if state.last_evaluated_at is not None and bar.as_of <= state.last_evaluated_at:
        raise ValueError("position bar must be newer than last_evaluated_at")
    if bars_held < 1:
        raise ValueError("bars_held must be positive")
    starts_at = bar.starts_at
    if starts_at is None:
        raise ValueError("position bar starts_at is required")
    if not _exit_levels_cover_observation(state, starts_at):
        # 최초 관리 전 관측에는 persisted stop provenance가 없다. 과거를
        # 평가 완료로만 넘기고 현재 bootstrap protection을 소급하지 않는다.
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=None,
        )

    initial_atr, initial_stop, current_stop = _exit_levels_for_bar(state, bar)
    trailed = current_stop > initial_stop
    if bar.open <= current_stop:
        kind = ExitKind.TRAILING_STOP_GAP if trailed else ExitKind.STOP_GAP
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=kind,
                fraction=_ONE,
                price=bar.open,
                reason="시가가 당시 유효한 손절선 아래에서 형성되어 전량 청산합니다.",
                exit_levels=(initial_atr, initial_stop, current_stop),
            ),
        )
    if bar.low <= current_stop:
        kind = ExitKind.TRAILING_STOP if trailed else ExitKind.STOP
        return PositionEvaluation(
            state=replace(state, last_evaluated_at=bar.as_of),
            signal=_signal(
                state,
                bar,
                kind=kind,
                fraction=_ONE,
                price=current_stop,
                reason="당일 저가가 당시 유효한 손절선에 닿아 전량 청산합니다.",
                exit_levels=(initial_atr, initial_stop, current_stop),
            ),
        )

    partial_target = state.entry_price + config.partial_profit_atr * initial_atr
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
                exit_levels=(initial_atr, initial_stop, current_stop),
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
                exit_levels=(initial_atr, initial_stop, current_stop),
            ),
        )

    # 진전폭은 **현재** 종가로 판정한다. 과거 최고 종가를 쓰면 한 번이라도
    # 올라간 포지션이 이후 평단 밑을 계속 기어도 진전폭이 latch되어 TIME_STOP을
    # 영구히 회피한다.
    progress = bar.close - state.entry_price
    if (
        bars_held >= config.max_holding_bars
        and progress < config.no_progress_atr * initial_atr
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
                exit_levels=(initial_atr, initial_stop, current_stop),
            ),
        )

    highest_close = max(state.highest_close, bar.close)
    updated = state
    protective_stop = _protective_stop(
        state,
        highest_close=highest_close,
        initial_atr=initial_atr,
        config=config,
    )
    if protective_stop is not None and protective_stop > current_stop:
        ends_at = bar.ends_at
        if ends_at is None:
            raise ValueError("position bar ends_at is required")
        updated = _raise_trailing_stop(
            state,
            raised_stop=protective_stop,
            effective_at=ends_at,
        )
    return PositionEvaluation(
        state=replace(
            updated,
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

    # ATR과 stop은 bucket 시작 시점에 유효했던 하나의 snapshot에서 함께 읽는다.
    # activation을 가로지르는 bucket의 pre-activation 저가로 새 stop을 발동하지
    # 않으면서도, history에 남은 이전 stop crossing은 계속 보존한다.
    partial: PositionExitSignal | None = None
    for bar in sorted(bars, key=lambda item: item.as_of):
        if bar.as_of - bar_interval < state.entry_at:
            continue
        if after is not None and bar.as_of <= after:
            continue
        starts_at = bar.starts_at
        if starts_at is None:
            raise ValueError("position bar starts_at is required")
        if not _exit_levels_cover_observation(state, starts_at):
            continue
        initial_atr, initial_stop, current_stop = _exit_levels_for_bar(state, bar)
        trailed = current_stop > initial_stop
        partial_target = state.entry_price + config.partial_profit_atr * initial_atr
        if bar.open <= current_stop:
            return _signal(
                state,
                bar,
                kind=ExitKind.TRAILING_STOP_GAP if trailed else ExitKind.STOP_GAP,
                fraction=_ONE,
                price=bar.open,
                reason="장중 봉 시가가 당시 유효한 손절선 아래여서 전량 청산합니다.",
                exit_levels=(initial_atr, initial_stop, current_stop),
            )
        if bar.low <= current_stop:
            return _signal(
                state,
                bar,
                kind=ExitKind.TRAILING_STOP if trailed else ExitKind.STOP,
                fraction=_ONE,
                price=current_stop,
                reason="장중 저가가 당시 유효한 손절선에 닿아 전량 청산합니다.",
                exit_levels=(initial_atr, initial_stop, current_stop),
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
                exit_levels=(initial_atr, initial_stop, current_stop),
            )
    return partial
