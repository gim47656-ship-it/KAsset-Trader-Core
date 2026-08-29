"""Deterministic PAPER position-manager behavior."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
    _state_matches_position_cycle,
    position_recommendation_id,
)
from app.extensions.kasset.models import (
    AndroidPaperAccount,
    KAssetPaperPositionState,
)
from app.models.ai_recommendations import AIRecommendation
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.trading import InstrumentType, User

D = Decimal
ENTRY_AT = datetime(2026, 8, 1, tzinfo=UTC)


def _state(
    *,
    partial: bool = False,
    stop: str = "70",
    high: str = "100",
    position_cycle_id: int = 101,
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
    high: str = "100",
    last_evaluated_at: datetime | None = None,
    last_exit_signal_key: str | None = None,
) -> KAssetPaperPositionState:
    return KAssetPaperPositionState(
        position_cycle_id=position_id,
        paper_position_id=position_id,
        owner_user_id=owner_user_id,
        paper_account_id=account_id,
        market=market,
        symbol=symbol,
        entry_order_id=None,
        entry_price=D("100"),
        initial_atr=D("10"),
        initial_stop=D("70"),
        current_stop=D("70"),
        highest_close=D(high),
        partial_exit_completed=partial,
        opened_at=ENTRY_AT,
        closed_at=None,
        last_evaluated_at=last_evaluated_at,
        last_exit_signal_key=last_exit_signal_key,
        strategy_key="qullamaggie_breakout_portfolio",
        strategy_version="1.0.0",
        strategy_fingerprint=None,
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


def _atr_candles() -> list[SimpleNamespace]:
    return [_candle(day) for day in range(-13, 2)]


def _manager(db: MagicMock, *, now: datetime) -> PaperPositionManagerService:
    service = PaperPositionManagerService(db, now=now)
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


@pytest.mark.asyncio
async def test_new_buy_creates_fresh_state_from_position_average_price() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    service = _manager(db, now=ENTRY_AT + timedelta(days=1))
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
    assert state_row.initial_stop == D("88")
    assert state_row.entry_order_id is None
    assert state_row.strategy_key == "qullamaggie_breakout_portfolio"
    assert state_row.strategy_version == "1.0.0"
    assert state_row.strategy_fingerprint is None


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

    first_service = _manager(db, now=ENTRY_AT + timedelta(days=1))
    recommendation_id = await first_service._manage_position(
        owner_user_id=23,
        account_id=17,
        market="KRX",
        position=position,
        rows=[_candle(1, open_="125", high="132", low="90", close="128")],
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
    restarted = _manager(db, now=ENTRY_AT + timedelta(days=2))
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
    service = _manager(db, now=ENTRY_AT + timedelta(days=1))

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
    assert reentry.highest_close == D("105")
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
    now = ENTRY_AT + timedelta(days=1)
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
    now = ENTRY_AT + timedelta(days=1)
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
    assert stale.highest_close == D("105")
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
    assert fresh.highest_close == D("105")
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


@pytest.mark.unit
def test_cycle_model_and_migration_preserve_closed_audit() -> None:
    table = KAssetPaperPositionState.__table__
    assert tuple(table.primary_key.columns.keys()) == ("position_cycle_id",)
    position_fk = next(
        fk
        for fk in table.foreign_keys
        if fk.target_fullname == "paper.paper_positions.id"
    )
    assert position_fk.ondelete == "SET NULL"
    assert table.c.paper_position_id.nullable
    assert table.c.entry_order_id.nullable
    assert table.c.strategy_key.nullable
    assert table.c.strategy_version.nullable
    assert table.c.strategy_fingerprint.nullable
    assert any(
        index.name == "uq_kasset_position_state_owner_active_holding" and index.unique
        for index in table.indexes
    )

    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic"
        / "versions"
        / "20260830_kasset_position_cycles.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260829_kasset_promotion"' in migration
    assert 'ondelete="SET NULL"' in migration
    assert "closed_at IS NULL" in migration
