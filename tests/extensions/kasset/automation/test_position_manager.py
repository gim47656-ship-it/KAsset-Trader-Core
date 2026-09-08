"""Deterministic PAPER position-manager behavior."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation import position_manager_service
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.intraday_data import (
    CompletedIntradayBars,
    IntradayBarsUnavailable,
)
from app.extensions.kasset.automation.market_session import RegularSession
from app.extensions.kasset.automation.position_manager import (
    ExitKind,
    ExitLevelVersion,
    ManagedPositionState,
    PositionBar,
    adopt_initial_atr,
    apply_stop_loss_floor,
    evaluate_position,
    evaluate_position_intraday,
    initialize_position,
)
from app.extensions.kasset.automation.position_manager_service import (
    PaperPositionManagerService,
    _intraday_position_bars,
    _persistable_state,
    _state_matches_position_cycle,
    position_recommendation_id,
)
from app.extensions.kasset.automation.strategy_promotion_service import (
    recommendation_strategy_identity,
)
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    KAssetPaperPositionState,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.trading import InstrumentType, User
from app.services.market_events.session_calendar import regular_session_bounds

D = Decimal
ENTRY_AT = datetime(2026, 8, 3, tzinfo=UTC)
_ARTIFACT_FINGERPRINT = "a" * 64
_INTRADAY_INTERVAL = timedelta(minutes=5)
#: (bucket 시작 offset(분), open, high, low, close)
_IntradayTick = tuple[int, str, str, str, str]


def _state(
    *,
    partial: bool = False,
    stop: str = "70",
    high: str = "100",
    position_cycle_id: int = 101,
    exit_levels_effective_at: datetime | None = None,
    exit_level_history: tuple[ExitLevelVersion, ...] = (),
) -> ManagedPositionState:
    return ManagedPositionState(
        market="KRX",
        symbol="005930",
        entry_price=D("100"),
        initial_atr=D("10"),
        initial_stop=D("70"),
        current_stop=D(stop),
        highest_close=D(high),
        partial_exit_completed=partial,
        entry_at=ENTRY_AT,
        last_evaluated_at=None,
        strategy_version="breakout-portfolio-v1",
        position_cycle_id=position_cycle_id,
        exit_levels_effective_at=exit_levels_effective_at,
        exit_level_history=exit_level_history,
    )


def _bar(
    day: int,
    *,
    open_: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
) -> PositionBar:
    return PositionBar(
        as_of=ENTRY_AT + timedelta(days=day),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
    )


def _paper_position(
    *,
    position_id: int = 101,
    account_id: int = 17,
    symbol: str = "005930",
    market: str = "KRX",
    quantity: str = "10",
    avg_price: str = "100",
) -> PaperPosition:
    return PaperPosition(
        id=position_id,
        account_id=account_id,
        symbol=symbol,
        instrument_type=(
            InstrumentType.equity_kr if market == "KRX" else InstrumentType.equity_us
        ),
        quantity=D(quantity),
        avg_price=D(avg_price),
        total_invested=D(quantity) * D(avg_price),
        created_at=ENTRY_AT,
    )


def _state_row(
    *,
    position_id: int = 101,
    owner_user_id: int = 23,
    account_id: int = 17,
    market: str = "KRX",
    symbol: str = "005930",
    partial: bool = False,
    initial_atr: Decimal | None = D("10"),
    stop: str = "70",
    high: str = "100",
    last_evaluated_at: datetime | None = None,
    last_exit_signal_key: str | None = None,
    strategy_fingerprint: str | None = _ARTIFACT_FINGERPRINT,
    exit_levels_effective_at: datetime | None = None,
    exit_level_history: list[dict[str, object]] | None = None,
) -> KAssetPaperPositionState:
    return KAssetPaperPositionState(
        position_cycle_id=position_id,
        paper_position_id=position_id,
        owner_user_id=owner_user_id,
        paper_account_id=account_id,
        market=market,
        symbol=symbol,
        entry_price=D("100"),
        initial_atr=initial_atr,
        initial_stop=D(stop),
        current_stop=D(stop),
        highest_close=D(high),
        partial_exit_completed=partial,
        opened_at=ENTRY_AT,
        closed_at=None,
        last_evaluated_at=last_evaluated_at,
        last_exit_signal_key=last_exit_signal_key,
        strategy_key="qullamaggie_breakout_portfolio",
        strategy_version="1.0.0",
        strategy_fingerprint=strategy_fingerprint,
        exit_levels_effective_at=exit_levels_effective_at,
        exit_level_history=exit_level_history,
    )


def _candle(
    day: int,
    *,
    open_: str = "105",
    high: str = "107",
    low: str = "103",
    close: str = "105",
) -> SimpleNamespace:
    return SimpleNamespace(
        time_utc=ENTRY_AT + timedelta(days=day),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
    )


def _atr_candles(*, first_day: int = -14) -> list[SimpleNamespace]:
    """ATR 14를 만들 수 있는 완료 KRX 거래일 15봉(TR=4 고정)."""

    candidate = first_day + 14
    offsets: list[int] = []
    while len(offsets) < 15:
        day = ENTRY_AT + timedelta(days=candidate)
        if regular_session_bounds("kr", day.date()) is not None:
            offsets.append(candidate)
        candidate -= 1
    return [_candle(day) for day in reversed(offsets)]


def _intraday(
    ticks: Sequence[_IntradayTick],
    *,
    day: int = 1,
    symbol: str = "005930",
    market: str = "KRX",
) -> CompletedIntradayBars:
    opens_at = ENTRY_AT + timedelta(days=day)
    session = RegularSession(
        market="kr" if market == "KRX" else "us",
        session_date=opens_at.date(),
        opens_at=opens_at,
        closes_at=opens_at + timedelta(hours=6, minutes=30),
    )
    bars = tuple(
        PriceBar(
            timestamp=opens_at + timedelta(minutes=minute),
            open=D(open_),
            high=D(high),
            low=D(low),
            close=D(close),
            volume=D("1000"),
        )
        for minute, open_, high, low, close in ticks
    )
    return CompletedIntradayBars(
        symbol=symbol,
        market="KRX" if market == "KRX" else "US",
        period="5m",
        bar_interval=_INTRADAY_INTERVAL,
        session=session,
        bars=bars,
        source="toss",
        data_as_of=bars[-1].timestamp + _INTRADAY_INTERVAL,
    )


def _manager(db: MagicMock, *, now: datetime) -> PaperPositionManagerService:
    service = PaperPositionManagerService(
        db,
        now=now,
        strategy_fingerprint=_ARTIFACT_FINGERPRINT,
    )
    service._policy.evaluate_hard_risk = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            checks=(),
            as_evidence=lambda: {"passed": True, "checks": []},
        )
    )
    return service


@pytest.mark.unit
def test_initialize_position_uses_three_atr_stop() -> None:
    state = initialize_position(
        market="US",
        symbol="NVDA",
        entry_price=D("120"),
        initial_atr=D("5"),
        entry_at=ENTRY_AT,
        strategy_version="breakout-portfolio-v1",
    )

    assert state.initial_stop == D("105")
    assert state.current_stop == D("105")
    assert state.highest_close == D("120")


@pytest.mark.unit
def test_stop_floor_lifts_a_wide_atr_stop_to_three_percent_below_the_fill() -> None:
    """저장된 ATR 손절선이 -30%여도 실제 체결 평단 -3%까지 끌어올린다."""

    floored = apply_stop_loss_floor(
        _state(),
        filled_average_price=D("100"),
        effective_at=ENTRY_AT + timedelta(days=2),
    )

    assert floored.initial_stop == D("97")
    assert floored.current_stop == D("97")
    assert floored.exit_levels_effective_at == ENTRY_AT + timedelta(days=2)
    assert floored.exit_level_history == (
        ExitLevelVersion(None, D("10"), D("70"), D("70")),
    )


@pytest.mark.unit
def test_stop_floor_keeps_a_tighter_trailing_stop() -> None:
    """trailing으로 올려둔 손절선은 바닥이 끌어내리지 않는다."""

    trailed = apply_stop_loss_floor(
        _state(partial=True, stop="110", high="140"),
        filled_average_price=D("100"),
        effective_at=ENTRY_AT + timedelta(days=2),
    )

    assert trailed.current_stop == D("110")
    assert trailed.initial_stop == D("97")


@pytest.mark.unit
def test_stop_floor_keeps_a_tighter_low_volatility_atr_stop() -> None:
    """3 ATR이 3%보다 좁은 저변동 종목은 더 타이트한 ATR 손절선으로 청산한다."""

    quiet = apply_stop_loss_floor(
        initialize_position(
            market="KRX",
            symbol="005930",
            entry_price=D("100"),
            initial_atr=D("0.5"),
            entry_at=ENTRY_AT,
            strategy_version="breakout-portfolio-v1",
            position_cycle_id=101,
        ),
        filled_average_price=D("100"),
        effective_at=ENTRY_AT,
    )
    # 바닥 97이 아니라 ATR 손절선 100 - 3*0.5 이 유효 손절선이어야 한다.
    bars = _intraday([(0, "99", "100", "98", "98.5")], day=3)

    signal = evaluate_position_intraday(
        quiet,
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.reference_price == D("98.5")


@pytest.mark.unit
def test_floored_stop_triggers_when_the_low_exactly_touches_it() -> None:
    """등호 경계: 저가가 바닥에 정확히 닿으면 손절이 나온다."""

    floored = apply_stop_loss_floor(
        _state(),
        filled_average_price=D("100"),
        effective_at=ENTRY_AT + timedelta(days=2),
    )
    bars = _intraday([(0, "99", "100", "97", "98")], day=3)

    signal = evaluate_position_intraday(
        floored,
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.reference_price == D("97")


@pytest.mark.unit
def test_stop_floor_refuses_to_invent_a_level_without_a_fill_average() -> None:
    with pytest.raises(ValueError, match="filled_average_price"):
        apply_stop_loss_floor(
            _state(),
            filled_average_price=D("0"),
            effective_at=ENTRY_AT,
        )


@pytest.mark.unit
def test_intraday_tightening_uses_old_stop_for_the_overlapping_bucket() -> None:
    activation = ENTRY_AT + timedelta(days=3, minutes=7)
    tightened = apply_stop_loss_floor(
        _state(),
        filled_average_price=D("100"),
        effective_at=activation,
    )
    bars = _intraday(
        [
            # 00:05~00:10은 activation을 가로지르므로 70만 유효하다.
            (5, "99", "100", "96", "98"),
            # 다음 온전한 bucket부터 새 97이 exact-touch로 발동한다.
            (10, "98", "99", "97", "98"),
        ],
        day=3,
    )

    signal = evaluate_position_intraday(
        tightened,
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.reference_price == D("97")
    assert signal.signal_at == bars.bars[1].timestamp + bars.bar_interval


@pytest.mark.unit
def test_repeated_tightening_preserves_each_intermediate_stop_version() -> None:
    first_activation = ENTRY_AT + timedelta(days=3, minutes=5)
    second_activation = ENTRY_AT + timedelta(days=3, minutes=10)
    first = apply_stop_loss_floor(
        _state(),
        filled_average_price=D("100"),
        effective_at=first_activation,
    )
    second = apply_stop_loss_floor(
        first,
        filled_average_price=D("110"),
        effective_at=second_activation,
    )
    bars = _intraday(
        [
            (0, "90", "91", "80", "89"),
            # 97 version이 유효한 구간. 최신 106.70을 소급하면 안 된다.
            (5, "99", "100", "96", "98"),
        ],
        day=3,
    )

    signal = evaluate_position_intraday(
        second,
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.reference_price == D("97")
    assert [version.current_stop for version in second.exit_level_history] == [
        D("70"),
        D("97"),
    ]


@pytest.mark.unit
def test_later_atr_only_enables_targets_after_its_activation() -> None:
    activation = ENTRY_AT + timedelta(days=3, minutes=7)
    fixed = replace(
        _state(stop="97"),
        initial_atr=None,
        initial_stop=D("97"),
    )
    adopted = adopt_initial_atr(
        fixed,
        initial_atr=D("4"),
        effective_at=activation,
    )
    before = _intraday([(5, "110", "115", "105", "114")], day=3)
    after = _intraday(
        [
            (5, "110", "115", "105", "114"),
            (10, "110", "115", "105", "114"),
        ],
        day=3,
    )

    assert (
        evaluate_position_intraday(
            adopted,
            _intraday_position_bars(before),
            bar_interval=before.bar_interval,
        )
        is None
    )
    signal = evaluate_position_intraday(
        adopted,
        _intraday_position_bars(after),
        bar_interval=after.bar_interval,
    )
    assert signal is not None
    assert signal.kind is ExitKind.PARTIAL_SELL
    assert signal.reference_price == D("112")
    assert signal.signal_at == after.bars[1].timestamp + after.bar_interval


@pytest.mark.unit
def test_delayed_daily_trailing_protects_the_intermediate_session() -> None:
    activation = ENTRY_AT + timedelta(days=3)
    tightened = apply_stop_loss_floor(
        _state(partial=True),
        filled_average_price=D("100"),
        effective_at=activation,
    )
    delayed = PositionBar(
        as_of=ENTRY_AT + timedelta(days=1),
        open=D("100"),
        high=D("145"),
        low=D("75"),
        close=D("140"),
        starts_at=ENTRY_AT + timedelta(days=1),
        ends_at=ENTRY_AT + timedelta(days=1, hours=6, minutes=30),
    )

    replayed = evaluate_position(tightened, delayed, bars_held=2)

    assert replayed.signal is None
    assert replayed.state.current_stop == D("110")
    intermediate = PositionBar(
        as_of=ENTRY_AT + timedelta(days=2),
        open=D("115"),
        high=D("116"),
        low=D("100"),
        close=D("106"),
        starts_at=ENTRY_AT + timedelta(days=2),
        ends_at=ENTRY_AT + timedelta(days=2, hours=6, minutes=30),
    )
    stopped = evaluate_position(replayed.state, intermediate, bars_held=3)
    assert stopped.signal is not None
    assert stopped.signal.kind is ExitKind.TRAILING_STOP
    assert stopped.signal.reference_price == D("110")
    assert stopped.signal.current_stop == D("110")


@pytest.mark.unit
def test_partial_profit_sells_half_once_at_three_atr() -> None:
    first = evaluate_position(
        _state(),
        _bar(1, open_="125", high="132", low="90", close="128"),
        bars_held=1,
    )

    assert first.signal is not None
    assert first.signal.kind is ExitKind.PARTIAL_SELL
    assert first.signal.quantity_fraction == D("0.5")
    assert first.signal.reference_price == D("130")
    assert first.state.partial_exit_completed is True

    second = evaluate_position(
        first.state,
        _bar(2, open_="128", high="140", low="100", close="135"),
        bars_held=2,
    )
    assert second.signal is None


@pytest.mark.unit
def test_close_based_trailing_stop_only_applies_from_next_bar() -> None:
    first = evaluate_position(
        _state(partial=True),
        _bar(1, open_="100", high="112", low="75", close="110"),
        bars_held=2,
    )
    assert first.signal is None
    assert first.state.current_stop == D("80")

    second = evaluate_position(
        first.state,
        _bar(2, open_="85", high="90", low="79", close="82"),
        bars_held=3,
    )
    assert second.signal is not None
    assert second.signal.kind is ExitKind.TRAILING_STOP
    assert second.signal.reference_price == D("80")


@pytest.mark.unit
def test_gap_stop_uses_open_and_wins_over_same_bar_profit_target() -> None:
    result = evaluate_position(
        _state(),
        _bar(1, open_="65", high="135", low="60", close="120"),
        bars_held=1,
    )

    assert result.signal is not None
    assert result.signal.kind is ExitKind.STOP_GAP
    assert result.signal.quantity_fraction == D("1")
    assert result.signal.reference_price == D("65")


@pytest.mark.unit
def test_time_stop_requires_max_bars_and_insufficient_progress() -> None:
    result = evaluate_position(
        _state(high="102"),
        _bar(10, open_="101", high="104", low="99", close="103"),
        bars_held=10,
    )

    assert result.signal is not None
    assert result.signal.kind is ExitKind.TIME_STOP
    assert result.signal.reference_price == D("103")


@pytest.mark.unit
def test_broken_trend_exits_remaining_quantity() -> None:
    result = evaluate_position(
        _state(partial=True, stop="80", high="130"),
        _bar(5, open_="120", high="125", low="110", close="112"),
        bars_held=5,
        trend_intact=False,
    )

    assert result.signal is not None
    assert result.signal.kind is ExitKind.TREND_BROKEN
    assert result.signal.quantity_fraction == D("1")


@pytest.mark.unit
def test_signal_key_is_deterministic_and_duplicate_bar_is_rejected() -> None:
    bar = _bar(1, open_="65", high="80", low="60", close="70")
    first = evaluate_position(_state(), bar, bars_held=1)
    repeated = evaluate_position(_state(), bar, bars_held=1)

    assert first.signal is not None
    assert repeated.signal is not None
    assert first.signal.idempotency_key == repeated.signal.idempotency_key
    with pytest.raises(ValueError, match="newer than last_evaluated_at"):
        evaluate_position(first.state, bar, bars_held=1)


@pytest.mark.unit
def test_signal_key_separates_reentry_position_cycles() -> None:
    bar = _bar(1, open_="65", high="80", low="60", close="70")
    first_cycle = evaluate_position(
        _state(position_cycle_id=101),
        bar,
        bars_held=1,
    )
    reentry_cycle = evaluate_position(
        _state(position_cycle_id=202),
        bar,
        bars_held=1,
    )

    assert first_cycle.signal is not None
    assert reentry_cycle.signal is not None
    assert first_cycle.signal.idempotency_key != reentry_cycle.signal.idempotency_key


@pytest.mark.unit
def test_recommendation_id_is_deterministic_and_owner_scoped() -> None:
    assert position_recommendation_id("exit:NVDA:2026-08-29", 7) == (
        "exit:NVDA:2026-08-29:7"
    )
    assert position_recommendation_id("exit:NVDA:2026-08-29", 8) != (
        "exit:NVDA:2026-08-29:7"
    )


@pytest.mark.unit
def test_partial_state_is_not_committed_before_paper_execution() -> None:
    previous = _state()
    evaluated = evaluate_position(
        previous,
        _bar(1, open_="125", high="132", low="90", close="128"),
        bars_held=1,
    )

    assert evaluated.signal is not None
    persisted = _persistable_state(previous, evaluated.state, evaluated.signal.kind)
    assert evaluated.state.partial_exit_completed is True
    assert persisted.partial_exit_completed is False


@pytest.mark.unit
def test_intraday_stop_uses_stored_level_without_a_new_daily_bar() -> None:
    """금요일까지 평가된 포지션도 월요일 장중에 손절선이 깨지면 신호가 나온다."""

    state = _state()
    monday = _intraday(
        [
            (0, "95", "96", "92", "93"),
            (5, "92", "93", "69", "71"),
        ],
        day=3,
    )

    signal = evaluate_position_intraday(
        state,
        _intraday_position_bars(monday),
        bar_interval=monday.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.quantity_fraction == D("1")
    assert signal.reference_price == D("70")
    # 신호 시각은 그 bucket이 닫힌 시각이다.
    assert signal.signal_at == monday.bars[1].timestamp + monday.bar_interval

    # 다음 tick에서 이후 bucket이 더 붙어도 같은 청산을 가리켜야 한다.
    next_tick = _intraday(
        [
            (0, "95", "96", "92", "93"),
            (5, "92", "93", "69", "71"),
            (10, "71", "72", "68", "69"),
        ],
        day=3,
    )
    repeated = evaluate_position_intraday(
        state,
        _intraday_position_bars(next_tick),
        bar_interval=next_tick.bar_interval,
    )

    assert repeated is not None
    assert repeated.idempotency_key == signal.idempotency_key


@pytest.mark.unit
def test_intraday_ignores_buckets_that_started_before_entry() -> None:
    """진입 직전 저가는 진입 후 손절 도달이 아니다."""

    bars = _intraday(
        [
            (0, "95", "96", "60", "94"),
            (5, "94", "95", "61", "93"),
            (10, "93", "94", "90", "92"),
        ],
        day=1,
    )
    entered_mid_bucket = replace(
        _state(),
        entry_at=bars.session.opens_at + timedelta(minutes=7),
    )

    assert (
        evaluate_position_intraday(
            entered_mid_bucket,
            _intraday_position_bars(bars),
            bar_interval=bars.bar_interval,
        )
        is None
    )

    after_entry_break = _intraday(
        [
            (0, "95", "96", "60", "94"),
            (10, "93", "94", "65", "92"),
        ],
        day=1,
    )
    signal = evaluate_position_intraday(
        entered_mid_bucket,
        _intraday_position_bars(after_entry_break),
        bar_interval=after_entry_break.bar_interval,
    )

    assert signal is not None
    assert signal.signal_at == after_entry_break.bars[1].timestamp + _INTRADAY_INTERVAL


@pytest.mark.unit
def test_intraday_never_trails_the_stop_from_session_highs() -> None:
    """분봉 고가/종가로 손절선을 끌어올리지 않는다. 저장된 손절선만 본다."""

    bars = _intraday(
        [
            (0, "100", "131", "99", "130"),
            (5, "130", "130", "95", "96"),
        ],
        day=3,
    )

    # 일봉 trailing(고가 130 - 3ATR)이라면 95는 손절이지만, 저장 손절선은 70이다.
    assert (
        evaluate_position_intraday(
            _state(partial=True),
            _intraday_position_bars(bars),
            bar_interval=bars.bar_interval,
        )
        is None
    )


@pytest.mark.unit
def test_intraday_partial_target_uses_stored_entry_and_atr() -> None:
    bars = _intraday([(0, "125", "132", "124", "128")], day=3)

    signal = evaluate_position_intraday(
        _state(),
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.PARTIAL_SELL
    assert signal.quantity_fraction == D("0.5")
    assert signal.reference_price == D("130")


@pytest.mark.unit
def test_intraday_full_stop_outranks_an_earlier_partial_hit() -> None:
    """부분익절 도달이 같은 세션의 손절선 관통을 가려서는 안 된다."""

    bars = _intraday(
        [
            (0, "125", "131", "124", "130"),
            (5, "130", "130", "69", "71"),
        ],
        day=3,
    )

    signal = evaluate_position_intraday(
        _state(),
        _intraday_position_bars(bars),
        bar_interval=bars.bar_interval,
    )

    assert signal is not None
    assert signal.kind is ExitKind.STOP
    assert signal.quantity_fraction == D("1")
    assert signal.reference_price == D("70")
    assert signal.signal_at == bars.bars[1].timestamp + _INTRADAY_INTERVAL


@pytest.mark.asyncio
async def test_data_error_skip_logs_owner_market_symbol_and_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    position = SimpleNamespace(
        instrument_type=InstrumentType.equity_kr,
        symbol="005930",
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [(position, 17)]))
    db.commit = AsyncMock()

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    db.begin_nested.side_effect = _Nested
    repository = SimpleNamespace(
        fetch_recent_batch=AsyncMock(return_value={"005930": ()})
    )
    monkeypatch.setattr(
        position_manager_service,
        "DailyCandlesRepository",
        lambda **_kwargs: repository,
    )
    service = PaperPositionManagerService(
        db,
        now=ENTRY_AT,
        strategy_fingerprint=_ARTIFACT_FINGERPRINT,
    )
    service._manage_position = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("invalid position data")
    )

    with caplog.at_level(logging.WARNING, logger=position_manager_service.__name__):
        created = await service.run_owner(23)

    assert created == ()
    assert "owner=23" in caplog.text
    assert "market=KRX" in caplog.text
    assert "symbol=005930" in caplog.text
    assert "exception=ValueError" in caplog.text


@pytest.mark.asyncio
async def test_new_buy_creates_fresh_state_from_position_average_price() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=1, hours=8))
    position = _paper_position(avg_price="100")

    created = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=_atr_candles(),
    )

    assert created is None
    state_row = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    assert state_row.position_cycle_id == position.id
    assert state_row.paper_position_id == position.id
    assert state_row.entry_price == position.avg_price
    assert state_row.entry_price != _atr_candles()[-1].close
    assert state_row.initial_atr == D("4")
    # ATR 손절선(100 - 3*4 = 88)보다 실제 체결 평단 -3%가 더 타이트하다.
    assert state_row.initial_stop == D("97")
    assert state_row.current_stop == D("97")
    assert state_row.strategy_key == "qullamaggie_breakout_portfolio"
    assert state_row.strategy_version == "1.0.0"
    assert state_row.strategy_fingerprint == _ARTIFACT_FINGERPRINT


@pytest.mark.asyncio
async def test_first_management_does_not_backdate_a_bootstrap_stop() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    bootstrap_at = ENTRY_AT + timedelta(days=3, hours=8)
    first = _manager(db, now=bootstrap_at)
    position = _paper_position(avg_price="100")

    historical = await first._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(1, open_="95", high="96", low="60", close="70")],
    )

    assert historical is None
    state_row = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    assert state_row.exit_levels_effective_at == bootstrap_at
    assert state_row.current_stop == D("97")
    assert state_row.last_evaluated_at == _candle(1).time_utc
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )

    db.scalar = AsyncMock(return_value=state_row)
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=1))
    protected = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[],
        intraday=_intraday([(0, "98", "99", "97", "98")], day=4),
    )

    assert protected is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert recommendation.reference_price == "97.00"


@pytest.mark.asyncio
async def test_exit_recommendation_has_authorizable_strategy_identity() -> None:
    state_row = _state_row(strategy_fingerprint=None)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=1, hours=8))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(1, open_="65", high="80", low="60", close="70")],
    )
    assert state_row.strategy_fingerprint == _ARTIFACT_FINGERPRINT

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    strategy_evidence = next(
        item
        for item in recommendation.evidence
        if item.get("kind") == "strategy_promotion"
    )
    assert strategy_evidence == {
        "title": "PAPER strategy promotion identity",
        "source": "kasset_strategy_promotion",
        "kind": "strategy_promotion",
        "strategyKey": "qullamaggie_breakout_portfolio",
        "version": "1.0.0",
        "artifactFingerprint": _ARTIFACT_FINGERPRINT,
    }
    identity = recommendation_strategy_identity(recommendation)
    assert identity is not None
    assert identity.strategy_key == "qullamaggie_breakout_portfolio"
    assert identity.version == "1.0.0"
    assert identity.artifact_fingerprint == _ARTIFACT_FINGERPRINT


@pytest.mark.asyncio
async def test_partial_fill_keeps_same_cycle_and_marks_remaining_state() -> None:
    state_row = _state_row()
    position = _paper_position()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()

    first_service = _manager(db, now=ENTRY_AT + timedelta(days=1, hours=8))
    recommendation_id = await first_service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(1, open_="125", high="132", low="120", close="128")],
    )

    assert recommendation_id is not None
    partial = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert state_row.partial_exit_completed is False
    partial.paper_execution_status = "SUCCEEDED"
    position.quantity = D("5")
    db.scalar = AsyncMock(side_effect=[state_row, partial])
    db.get = AsyncMock(return_value=None)
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=2, hours=8))
    duplicate = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(2, open_="128", high="129", low="100", close="128")],
    )

    assert duplicate is None
    assert state_row.position_cycle_id == 101
    assert state_row.paper_position_id == 101
    assert state_row.partial_exit_completed is True
    assert state_row.current_stop == D("98")


@pytest.mark.asyncio
async def test_reentry_cycle_does_not_reuse_old_highest_close_or_partial_state() -> (
    None
):
    closed_cycle = _state_row(position_id=101, partial=True, high="180")
    closed_cycle.paper_position_id = None
    closed_cycle.closed_at = ENTRY_AT + timedelta(days=1)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=1, hours=8))

    created = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(position_id=202),
        rows=_atr_candles(),
    )

    assert created is None
    reentry = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    assert reentry.position_cycle_id != closed_cycle.position_cycle_id
    assert reentry.paper_position_id == 202
    assert reentry.highest_close == D("100")
    assert reentry.highest_close != closed_cycle.highest_close
    assert reentry.partial_exit_completed is False


@pytest.mark.asyncio
async def test_unclaimed_partial_is_expired_before_emergency_full_exit() -> None:
    partial_id = "position-exit:partial:23"
    state_row = _state_row(last_exit_signal_key=partial_id)
    previous = AIRecommendation(
        id=partial_id,
        owner_user_id=23,
        action="SELL",
        decision="APPROVED",
        market="KRX",
        symbol="005930",
        currency="KRW",
        rationale=[],
        risks=[],
        evidence=[{"kind": "position_exit", "exitKind": "PARTIAL_SELL"}],
        source="kasset-automation",
        created_at=ENTRY_AT,
        valid_until=ENTRY_AT + timedelta(days=5),
        decided_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[state_row, previous])
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    now = ENTRY_AT + timedelta(days=1, hours=8)
    service = _manager(db, now=now)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(1, open_="65", high="80", low="60", close="70")],
    )

    assert recommendation_id is not None
    assert previous.valid_until == now
    assert previous.updated_at == now
    full_exit = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert full_exit.id == recommendation_id
    assert full_exit.suggested_quantity == "10"
    assert state_row.last_exit_signal_key == recommendation_id


def _exit_recommendation(
    identifier: str,
    *,
    kind: str,
    bar_as_of: datetime | None = None,
    decision: str = "APPROVED",
    execution_status: str | None = None,
    valid_until: datetime = ENTRY_AT + timedelta(days=5),
) -> AIRecommendation:
    evidence: dict[str, object] = {"kind": "position_exit", "exitKind": kind}
    if bar_as_of is not None:
        evidence["barAsOf"] = bar_as_of.isoformat()
    row = AIRecommendation(
        id=identifier,
        owner_user_id=23,
        action="SELL",
        decision=decision,
        market="KRX",
        symbol="005930",
        currency="KRW",
        rationale=[],
        risks=[],
        evidence=[evidence],
        source="kasset-automation",
        created_at=ENTRY_AT,
        valid_until=valid_until,
        decided_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    row.paper_execution_status = execution_status
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_until", (ENTRY_AT + timedelta(days=5), ENTRY_AT))
async def test_claimed_partial_blocks_a_parallel_full_exit(
    valid_until: datetime,
) -> None:
    """집행 중인 부분익절이 있으면 전량 청산을 병행 생성하지 않는다."""

    partial_id = "position-exit:claimed-partial:23"
    state_row = _state_row(last_exit_signal_key=partial_id)
    claimed = _exit_recommendation(
        partial_id,
        kind="PARTIAL_SELL",
        execution_status="CLAIMED",
        valid_until=valid_until,
    )
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[state_row, claimed])
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "95", "96", "69", "71")], day=3),
    )

    assert recommendation_id is None
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )
    # claim 결과를 다음 tick에서 다시 화해할 수 있어야 한다.
    assert claimed.valid_until == valid_until
    assert state_row.last_exit_signal_key == partial_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "execution_status", "valid_until"),
    (
        ("REJECTED", None, ENTRY_AT + timedelta(days=5)),
        ("APPROVED", "FAILED", ENTRY_AT + timedelta(days=5)),
        ("APPROVED", None, ENTRY_AT + timedelta(days=3)),
    ),
)
async def test_terminated_intraday_exit_retries_on_a_later_bucket(
    decision: str,
    execution_status: str | None,
    valid_until: datetime,
) -> None:
    """거절·집행실패·기한만료 뒤에도 이후 완료 bucket으로 다시 보호한다."""

    first_bucket_end = ENTRY_AT + timedelta(days=3, minutes=5)
    terminated_id = "position-exit:terminated:23"
    state_row = _state_row(last_exit_signal_key=terminated_id)
    terminated = _exit_recommendation(
        terminated_id,
        kind="STOP",
        bar_as_of=first_bucket_end,
        decision=decision,
        execution_status=execution_status,
        valid_until=valid_until,
    )
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[state_row, terminated])
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    position = _paper_position(quantity="6")
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[],
        intraday=_intraday(
            [
                (0, "95", "96", "69", "71"),
                (5, "71", "72", "68", "69"),
            ],
            day=3,
        ),
    )

    assert recommendation_id is not None
    assert recommendation_id != terminated_id
    retried = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    exit_evidence = next(
        item for item in retried.evidence if item.get("kind") == "position_exit"
    )
    # 종료된 추천의 bucket은 다시 쓰지 않고, 그 이후 bucket으로 나간다.
    assert exit_evidence["barAsOf"] == (
        (ENTRY_AT + timedelta(days=3, minutes=10)).isoformat()
    )
    # 최신 잔량으로 산정한다.
    assert retried.suggested_quantity == "6"
    assert state_row.last_exit_signal_key == recommendation_id
    assert state_row.last_evaluated_at is None


@pytest.mark.asyncio
async def test_terminated_intraday_exit_does_not_repeat_the_same_bucket() -> None:
    """종료된 추천의 bucket만 남아 있으면 같은 id를 다시 만들지 않는다."""

    first_bucket_end = ENTRY_AT + timedelta(days=3, minutes=5)
    terminated_id = "position-exit:terminated-same:23"
    state_row = _state_row(last_exit_signal_key=terminated_id)
    terminated = _exit_recommendation(
        terminated_id,
        kind="STOP",
        bar_as_of=first_bucket_end,
        decision="REJECTED",
    )
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[state_row, terminated])
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "95", "96", "69", "71")], day=3),
    )

    assert recommendation_id is None
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )
    # 재시작 뒤에도 재시도 경계를 알 수 있어야 한다.
    assert state_row.last_exit_signal_key == terminated_id


@pytest.mark.asyncio
async def test_duplicate_manager_run_emits_one_exit_for_same_cycle_bar() -> None:
    state_row = _state_row()
    recommendations: dict[str, AIRecommendation] = {}
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.delete = AsyncMock()
    db.flush = AsyncMock()

    async def get_recommendation(
        model: type[AIRecommendation],
        key: str,
    ) -> AIRecommendation | None:
        assert model is AIRecommendation
        return recommendations.get(key)

    def add_row(row: object) -> None:
        if isinstance(row, AIRecommendation):
            recommendations[row.id] = row

    db.get = AsyncMock(side_effect=get_recommendation)
    db.add = MagicMock(side_effect=add_row)
    now = ENTRY_AT + timedelta(days=1, hours=8)
    first_service = _manager(db, now=now)
    second_service = _manager(db, now=now)
    position = _paper_position()
    rows = [_candle(1, open_="65", high="80", low="60", close="70")]

    first = await first_service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=rows,
    )
    db.scalar = AsyncMock(side_effect=[state_row, recommendations[first]])
    repeated = await second_service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=rows,
    )

    assert first is not None
    assert repeated is None
    assert list(recommendations) == [first]


@pytest.mark.asyncio
async def test_intraday_exit_fires_after_the_last_daily_bar_was_evaluated() -> None:
    """실측 결함: 마지막 완료 일봉이 이미 평가돼 포지션 전체가 건너뛰어졌다."""

    daily = _candle(1)
    state_row = _state_row(last_evaluated_at=daily.time_utc)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    now = daily.time_utc + timedelta(days=2, hours=1)
    service = _manager(db, now=now)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[daily],
        intraday=_intraday(
            [
                (0, "99", "100", "98", "99"),
                (5, "98", "99", "69", "71"),
            ],
            day=3,
        ),
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["exitKind"] == ExitKind.STOP.value
    assert exit_evidence["evaluationHorizon"] == "intraday"
    assert exit_evidence["barPeriod"] == "5m"
    assert recommendation.suggested_quantity == "10"
    assert state_row.last_exit_signal_key == recommendation_id
    # 분봉 시각이 일봉 커서를 덮으면 당일 완료 일봉이 영구히 막힌다.
    assert state_row.last_evaluated_at == daily.time_utc


@pytest.mark.asyncio
async def test_intraday_exit_fires_when_entry_is_newer_than_daily_history() -> None:
    """실측 결함: 진입이 마지막 일봉보다 늦어 일봉 평가가 성립하지 않았다."""

    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    position = _paper_position()
    position.created_at = ENTRY_AT + timedelta(days=2)
    first_now = ENTRY_AT + timedelta(days=3, hours=1)
    service = _manager(db, now=first_now)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=_atr_candles(),
        intraday=_intraday([(0, "90", "91", "87", "88")], day=3),
    )

    assert recommendation_id is None
    state_row = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    # 최초 관리 당시 ATR/floor는 과거 bucket에 소급하지 않고 그 시각부터 보호한다.
    assert state_row.initial_stop == D("97")
    assert state_row.exit_levels_effective_at == first_now
    assert state_row.last_evaluated_at is None

    db.scalar = AsyncMock(return_value=state_row)
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=1))
    recommendation_id = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=_atr_candles(),
        intraday=_intraday([(0, "90", "91", "87", "88")], day=4),
    )

    assert recommendation_id is not None


@pytest.mark.asyncio
async def test_stale_daily_history_does_not_block_the_intraday_exit() -> None:
    """일봉 적재가 멈춰도 보유 종목의 손절 평가는 멈추지 않는다."""

    state_row = _state_row()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=9, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(1)],
        intraday=_intraday([(0, "71", "72", "69", "70")], day=9),
    )

    assert recommendation_id is not None


@pytest.mark.asyncio
async def test_repeated_intraday_ticks_emit_one_exit_for_the_same_bucket() -> None:
    """tick 시각이 달라도 같은 완료 bucket은 한 건만 만든다."""

    state_row = _state_row(last_evaluated_at=_candle(1).time_utc)
    recommendations: dict[str, AIRecommendation] = {}
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.flush = AsyncMock()

    async def get_recommendation(
        model: type[AIRecommendation],
        key: str,
    ) -> AIRecommendation | None:
        assert model is AIRecommendation
        return recommendations.get(key)

    def add_row(row: object) -> None:
        if isinstance(row, AIRecommendation):
            recommendations[row.id] = row

    db.get = AsyncMock(side_effect=get_recommendation)
    db.add = MagicMock(side_effect=add_row)
    position = _paper_position()
    first_tick = _intraday([(0, "95", "96", "69", "71")], day=3)
    later_tick = _intraday(
        [
            (0, "95", "96", "69", "71"),
            (5, "71", "72", "68", "69"),
        ],
        day=3,
    )

    first = await _manager(
        db,
        now=first_tick.data_as_of + timedelta(minutes=2),
    )._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(1)],
        intraday=first_tick,
    )
    db.scalar = AsyncMock(side_effect=[state_row, recommendations[first]])
    repeated = await _manager(
        db,
        now=later_tick.data_as_of + timedelta(minutes=2),
    )._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(1)],
        intraday=later_tick,
    )

    assert first is not None
    assert repeated is None
    assert list(recommendations) == [first]


@pytest.mark.asyncio
async def test_intraday_full_stop_outranks_the_daily_partial_signal() -> None:
    """일봉이 부분익절을 내도 당일 장중 손절선 관통이 우선한다."""

    state_row = _state_row()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(1, open_="125", high="132", low="120", close="128")],
        intraday=_intraday([(0, "99", "100", "69", "71")], day=3),
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["exitKind"] == ExitKind.STOP.value
    assert exit_evidence["evaluationHorizon"] == "intraday"
    assert recommendation.suggested_quantity == "10"
    # 부분익절은 나가지 않았으므로 상태에 확정되어서도 안 된다.
    assert state_row.partial_exit_completed is False
    # 일봉 자체는 평가됐으므로 일봉 커서는 그 봉으로 전진한다.
    assert state_row.last_evaluated_at == _candle(1).time_utc


@pytest.mark.asyncio
async def test_stored_stop_is_protected_without_any_daily_history() -> None:
    """일봉 유니버스에 없는 보유 종목도 저장된 손절선으로 보호된다."""

    state_row = _state_row()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "95", "96", "69", "71")], day=3),
    )

    assert recommendation_id is not None
    assert state_row.last_evaluated_at is None


@pytest.mark.asyncio
async def test_hanjin_daily_catchup_uses_the_stop_valid_during_that_session() -> None:
    """9/7 장 종료 뒤 생긴 -3% floor가 9/7 저가를 소급 청산하지 않는다."""

    old_stop = D("125342.85714286")
    activated_at = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    state_row = _state_row(
        symbol="180640",
        last_evaluated_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    state_row.entry_price = D("145100")
    state_row.initial_atr = D("6585.71428571")
    state_row.initial_stop = old_stop
    state_row.current_stop = old_stop
    state_row.highest_close = D("145100")
    state_row.opened_at = datetime(2026, 9, 4, 2, 25, tzinfo=UTC)
    daily = SimpleNamespace(
        time_utc=datetime(2026, 9, 7, tzinfo=UTC),
        open=D("147000"),
        high=D("149700"),
        low=D("137000"),
        close=D("148900"),
    )
    position = _paper_position(symbol="180640", quantity="2", avg_price="145100")
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=activated_at)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[daily],
    )

    assert recommendation_id is None
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )
    assert state_row.last_evaluated_at == daily.time_utc
    assert state_row.initial_stop == D("140747")
    assert state_row.current_stop == D("140747")
    assert state_row.exit_levels_effective_at == activated_at
    assert state_row.exit_level_history == [
        {
            "effectiveAt": None,
            "initialAtr": "6585.71428571",
            "initialStop": "125342.85714286",
            "currentStop": "125342.85714286",
        }
    ]


@pytest.mark.asyncio
async def test_restart_evaluates_delayed_daily_bar_with_its_old_stop_version() -> None:
    activation = ENTRY_AT + timedelta(days=3)
    state_row = _state_row(
        stop="97",
        last_evaluated_at=_candle(1).time_utc,
        exit_levels_effective_at=activation,
        exit_level_history=[
            {
                "effectiveAt": None,
                "initialAtr": "10",
                "initialStop": "70",
                "currentStop": "70",
            }
        ],
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=8))

    recommendation_id = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(2, open_="75", high="80", low="69", close="72")],
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert recommendation.reference_price == "70"
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["currentStop"] == "70"
    assert state_row.current_stop == D("97")


@pytest.mark.asyncio
async def test_delayed_trailing_survives_json_restart_and_stops_the_next_day() -> None:
    activation = ENTRY_AT + timedelta(days=3)
    state_row = _state_row(
        partial=True,
        stop="97",
        exit_levels_effective_at=activation,
        exit_level_history=[
            {
                "effectiveAt": None,
                "initialAtr": "10",
                "initialStop": "70",
                "currentStop": "70",
            }
        ],
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    first = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=8))

    first_recommendation = await first._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[_candle(1, open_="100", high="145", low="75", close="140")],
    )

    assert first_recommendation is None
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=8))
    recommendation_id = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[
            _candle(1, open_="100", high="145", low="75", close="140"),
            _candle(2, open_="115", high="116", low="100", close="106"),
        ],
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert recommendation.reference_price == "110"
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["exitKind"] == ExitKind.TRAILING_STOP.value
    assert exit_evidence["initialStop"] == "70"
    assert exit_evidence["currentStop"] == "110"


@pytest.mark.asyncio
async def test_daily_replay_keeps_an_old_stop_crossing_after_an_earlier_partial() -> (
    None
):
    """여러 미처리 일봉에서 old full stop은 새 floor와 앞선 partial에 가리지 않는다."""

    state_row = _state_row()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=8))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[
            _candle(1, open_="125", high="135", low="90", close="128"),
            _candle(2, open_="80", high="82", low="69", close="71"),
        ],
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["exitKind"] == ExitKind.STOP.value
    assert exit_evidence["evaluationHorizon"] == "daily"
    assert recommendation.reference_price == "70"
    assert state_row.last_evaluated_at == _candle(2).time_utc
    # Full exit가 이미 성립했으므로 이 run에서 새 floor로 바꿔 증거를 오염시키지 않는다.
    assert state_row.current_stop == D("70")


@pytest.mark.asyncio
async def test_stop_floor_follows_the_actual_fill_average_not_the_stored_entry() -> (
    None
):
    """추가매수로 평단이 움직이면 손실률의 분모도 그 실제 평단이다."""

    # 사이클 시작 당시 평단은 100이었지만 원장의 실제 평단은 110이다.
    state_row = _state_row()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    first_now = ENTRY_AT + timedelta(days=3, hours=1)
    service = _manager(db, now=first_now)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(avg_price="110", quantity="20"),
        rows=[],
        intraday=_intraday([(0, "108", "109", "106", "107")], day=3),
    )

    # activation 전 시작한 bucket은 새 106.70으로 소급 청산하지 않는다.
    assert recommendation_id is None
    assert state_row.current_stop == D("106.70")
    assert state_row.exit_levels_effective_at == first_now
    assert state_row.entry_price == D("100")

    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=1))
    recommendation_id = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(avg_price="110", quantity="20"),
        rows=[],
        intraday=_intraday([(0, "108", "109", "106", "107")], day=4),
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    assert recommendation.reference_price == "106.70"
    assert recommendation.suggested_quantity == "20"


@pytest.mark.asyncio
async def test_new_position_without_daily_history_uses_the_fixed_stop() -> None:
    """ATR을 만들 근거가 없어도 실제 체결 평단 -3%로는 보호한다."""

    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    first_now = ENTRY_AT + timedelta(days=3, hours=1)
    service = _manager(db, now=first_now)

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "99", "100", "96", "96")], day=3),
    )

    assert recommendation_id is None
    created = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    # ATR을 발명하지 않는다. 손절선은 평단 100 * 0.97 하나뿐이다.
    assert created.initial_atr is None
    assert created.initial_stop == D("97.00")
    assert created.current_stop == D("97.00")
    assert created.exit_levels_effective_at == first_now

    db.scalar = AsyncMock(return_value=created)
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=4, hours=1))
    recommendation_id = await restarted._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "99", "100", "96", "96")], day=4),
    )

    assert recommendation_id is not None
    recommendation = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], AIRecommendation)
    )
    exit_evidence = next(
        item for item in recommendation.evidence if item.get("kind") == "position_exit"
    )
    assert exit_evidence["exitKind"] == ExitKind.STOP.value
    assert exit_evidence["initialAtr"] is None
    assert recommendation.reference_price == "97.00"
    assert recommendation.suggested_quantity == "10"


@pytest.mark.asyncio
async def test_fixed_stop_without_atr_never_invents_a_take_profit() -> None:
    """ATR이 없으면 부분익절선도 없다. 손절 보호가 가짜 익절을 만들어선 안 된다."""

    state_row = _state_row(initial_atr=None, stop="97")
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    # ATR 10이 저장돼 있었다면 부분익절선 100 + 3*10 = 130을 관통한 봉이다.
    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=[],
        intraday=_intraday([(0, "129", "132", "128", "131")], day=3),
    )

    assert recommendation_id is None
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )


#: 일봉을 쓸 수 없게 만드는 두 가지 사유. 어느 쪽도 ATR의 근거가 될 수 없다.
_UNUSABLE_DAILY_WINDOWS = (
    pytest.param(4, id="future"),
    pytest.param(-20, id="stale"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_day", _UNUSABLE_DAILY_WINDOWS)
async def test_unusable_daily_history_does_not_derive_an_atr(first_day: int) -> None:
    """미래·오래된 일봉 15봉이 있어도 ATR을 만들지 않고 고정 손절선만 쓴다."""

    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    # ATR 4를 채택했다면 부분익절선 100 + 3*4 = 112를 장중 고가 115가 관통한다.
    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=_atr_candles(first_day=first_day),
        intraday=_intraday([(0, "101", "115", "100", "114")], day=3),
    )

    assert recommendation_id is None
    assert not any(
        isinstance(call_.args[0], AIRecommendation) for call_ in db.add.call_args_list
    )
    created = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    assert created.initial_atr is None
    assert created.initial_stop == D("97.00")
    assert created.current_stop == D("97.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_day", _UNUSABLE_DAILY_WINDOWS)
async def test_unusable_daily_history_does_not_hydrate_a_fixed_stop_state(
    first_day: int,
) -> None:
    """고정 손절선으로 보호 중인 보유분에 쓸 수 없는 일봉의 ATR을 채워 넣지 않는다."""

    state_row = _state_row(initial_atr=None, stop="97")
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=3, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=_atr_candles(first_day=first_day),
        intraday=_intraday([(0, "101", "115", "100", "114")], day=3),
    )

    assert recommendation_id is None
    assert state_row.initial_atr is None
    assert state_row.current_stop == D("97")


@pytest.mark.asyncio
async def test_later_atr_fills_the_state_without_loosening_the_fixed_stop() -> None:
    """일봉 근거가 생겨도 이미 들고 있던 고정 손절선을 넓히지 않는다."""

    # 고정 손절선 97로만 보호 중이던 보유분. 새로 생긴 ATR 4의 손절선은 88이다.
    state_row = _state_row(initial_atr=None, stop="97")
    db = MagicMock()
    db.scalar = AsyncMock(return_value=state_row)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=1, hours=1))

    recommendation_id = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=_atr_candles(),
    )

    assert recommendation_id is None
    assert state_row.initial_atr == D("4")
    assert state_row.initial_stop == D("97")
    assert state_row.current_stop == D("97")


@pytest.mark.asyncio
async def test_restart_reconciles_mismatched_state_to_current_position_cycle() -> None:
    stale = _state_row(owner_user_id=99, high="180", partial=True)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=stale)
    db.get = AsyncMock(return_value=None)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    now = ENTRY_AT + timedelta(days=1)
    service = _manager(db, now=now)

    created = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=_atr_candles(),
    )

    assert created is None
    assert stale.position_cycle_id == 101
    assert stale.paper_position_id == 101
    assert stale.closed_at is None
    assert stale.owner_user_id == 23
    assert stale.paper_account_id == 17
    assert stale.market == "KRX"
    assert stale.symbol == "005930"
    assert stale.highest_close == D("100")
    assert stale.partial_exit_completed is False
    assert not any(
        isinstance(call_.args[0], KAssetPaperPositionState)
        for call_ in db.add.call_args_list
    )


@pytest.mark.asyncio
async def test_restart_closes_wrong_cycle_and_creates_current_cycle() -> None:
    stale = _state_row(position_id=999, high="180", partial=True)
    stale.paper_position_id = 101
    db = MagicMock()
    db.scalar = AsyncMock(return_value=stale)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.add = MagicMock()
    now = ENTRY_AT + timedelta(days=1)
    service = _manager(db, now=now)

    created = await service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
        rows=_atr_candles(),
    )

    assert created is None
    assert stale.position_cycle_id == 999
    assert stale.paper_position_id is None
    assert stale.closed_at == now
    fresh = next(
        call_.args[0]
        for call_ in db.add.call_args_list
        if isinstance(call_.args[0], KAssetPaperPositionState)
    )
    assert fresh.position_cycle_id == 101
    assert fresh.paper_position_id == 101
    assert fresh.highest_close == D("100")
    assert fresh.partial_exit_completed is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_user_id", 24),
        ("paper_account_id", 18),
        ("market", "US"),
        ("symbol", "AAPL"),
    ),
)
def test_position_cycle_audit_identity_isolated(
    field: str,
    value: object,
) -> None:
    row = _state_row()
    setattr(row, field, value)

    assert not _state_matches_position_cycle(
        row,
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=_paper_position(),
    )


@pytest.mark.asyncio
async def test_closed_cycle_survives_position_delete_as_audit(
    db_session: AsyncSession,
    user: User,
) -> None:
    user_id = int(user.id)
    account_id = 8_300_000_000 + user_id
    position_id = 8_400_000_000 + user_id
    account = PaperAccount(
        id=account_id,
        name=f"position-cycle-{user_id}",
        initial_capital=D("1000000"),
        cash_krw=D("999000"),
        cash_usd=D("0"),
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        AndroidPaperAccount(
            owner_user_id=user_id,
            paper_account_id=account_id,
        )
    )
    position = _paper_position(position_id=position_id, account_id=account_id)
    db_session.add(position)
    await db_session.flush()
    state_row = _state_row(
        position_id=position_id,
        owner_user_id=user_id,
        account_id=account_id,
    )
    db_session.add(state_row)
    await db_session.flush()

    closed_at = ENTRY_AT + timedelta(days=1)
    state_row.paper_position_id = None
    state_row.closed_at = closed_at
    await db_session.flush()
    await db_session.delete(position)
    await db_session.flush()

    preserved = await db_session.get(KAssetPaperPositionState, position_id)
    assert preserved is not None
    assert preserved.position_cycle_id == position_id
    assert preserved.paper_position_id is None
    assert preserved.closed_at == closed_at


@pytest.mark.asyncio
async def test_intraday_load_is_bounded_to_open_markets_and_held_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """장이 닫힌 시장은 조회하지 않고, 보유 종목당 한 번만 조회한다."""

    session = _intraday([(0, "100", "101", "99", "100")]).session
    calls: list[tuple[str, str, object]] = []

    async def _load(
        *,
        symbol: str,
        market: str,
        as_of: datetime,
        session: RegularSession,
    ) -> CompletedIntradayBars | IntradayBarsUnavailable:
        calls.append((market, symbol, session))
        if symbol == "000660":
            return IntradayBarsUnavailable(
                symbol=symbol,
                market="KRX",
                period="5m",
                blocked_reason="intraday_bars_stale",
                detail="provider lag",
            )
        return _intraday(
            [(0, "100", "101", "99", "100")],
            symbol=symbol,
            market=market,
        )

    monkeypatch.setattr(
        position_manager_service,
        "current_regular_session",
        lambda market, moment: session if market == "KRX" else None,
    )
    monkeypatch.setattr(position_manager_service, "load_completed_session_bars", _load)
    service = _manager(MagicMock(), now=ENTRY_AT + timedelta(days=1))

    loaded = await service._load_intraday(
        {
            "KRX": [
                (_paper_position(), 17),
                (_paper_position(position_id=102, symbol="000660"), 17),
            ],
            "US": [
                (_paper_position(position_id=103, symbol="NVDA", market="US"), 17),
            ],
        }
    )

    assert list(loaded) == [("KRX", "005930")]
    assert [(market, symbol) for market, symbol, _ in calls] == [
        ("KRX", "005930"),
        ("KRX", "000660"),
    ]
    assert all(used is session for _m, _s, used in calls)
