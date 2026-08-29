"""Deterministic PAPER position-manager behavior."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.automation import position_manager_service
from app.extensions.kasset.automation.position_manager import (
    ExitKind,
    ManagedPositionState,
    PositionBar,
    evaluate_position,
    initialize_position,
)
from app.extensions.kasset.automation.position_manager_service import (
    PaperPositionManagerService,
    _persistable_state,
    position_recommendation_id,
)
from app.models.trading import InstrumentType

D = Decimal
ENTRY_AT = datetime(2026, 8, 1, tzinfo=UTC)


def _state(*, partial: bool = False, stop: str = "70", high: str = "100") -> ManagedPositionState:
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
    db.execute = AsyncMock(
        return_value=SimpleNamespace(all=lambda: [(position, 17)])
    )
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
    service = PaperPositionManagerService(db, now=ENTRY_AT)
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
