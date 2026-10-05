from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.extensions.kasset.automation.candidate_ranker import (
    CandidateKey,
    CandidateMetadata,
)
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.exit_rule_comparison import (
    ExcludedEntry,
    ExitExecutionConstraints,
    FixedEntry,
    compare_exit_variants,
    run_exit_rule_comparison,
    simulate_fixed_entry,
)
from app.extensions.kasset.automation.portfolio_backtest import (
    MarketExecutionCost,
    PortfolioBacktestConfig,
    UniverseEvidence,
    run_portfolio_backtest,
)
from app.extensions.kasset.automation.position_manager import PositionManagerConfig

_START = datetime(2025, 1, 1, tzinfo=UTC)
_NO_CONSTRAINTS = ExitExecutionConstraints(
    krx_daily_limit_rate=None,
    exit_volume_participation=None,
)
_CONFIG = PortfolioBacktestConfig(
    initial_cash=Decimal("100000"),
    max_positions=1,
    candidate_top_n=1,
    risk_per_trade_rate=Decimal("0.02"),
    max_symbol_allocation=Decimal("0.50"),
    kr_cost=MarketExecutionCost(
        Decimal("0.00015"), Decimal("0.001"), sell_tax_rate=Decimal("0.0018")
    ),
    us_cost=MarketExecutionCost(
        Decimal("0.001"), Decimal("0.0005"), sell_tax_rate=Decimal("0.002")
    ),
)


def _breakout_bars(count: int = 330) -> tuple[PriceBar, ...]:
    closes = [
        Decimal("100") + Decimal(index) / Decimal("100") for index in range(count)
    ]
    overrides = {
        255: Decimal("112"),
        256: Decimal("114"),
        257: Decimal("118"),
        258: Decimal("119"),
        259: Decimal("105"),
        260: Decimal("104"),
        261: Decimal("103"),
    }
    for index, close in overrides.items():
        closes[index] = close
    opens = {
        256: Decimal("113"),
        258: Decimal("119"),
        259: Decimal("110"),
        260: Decimal("104"),
    }
    output: list[PriceBar] = []
    for index, close in enumerate(closes):
        open_price = opens.get(index, closes[index - 1] if index else close)
        output.append(
            PriceBar(
                timestamp=_START + timedelta(days=index),
                open=open_price,
                high=max(open_price, close) + Decimal("1"),
                low=min(open_price, close) - Decimal("1"),
                close=close,
                volume=Decimal("1000000"),
            )
        )
    return tuple(output)


def _half_scale(bars: tuple[PriceBar, ...]) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            timestamp=bar.timestamp,
            open=bar.open / Decimal("2"),
            high=bar.high / Decimal("2"),
            low=bar.low / Decimal("2"),
            close=bar.close / Decimal("2"),
            volume=bar.volume,
        )
        for bar in bars
    )


def _bar(
    day: int,
    open_price: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000000",
) -> PriceBar:
    return PriceBar(
        timestamp=_START + timedelta(days=day),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def _kr_entry(symbol: str = "005930", quantity: str = "100") -> FixedEntry:
    return FixedEntry(
        market="KR",
        symbol=symbol,
        entry_signal_at=_START,
        entry_at=_START + timedelta(days=1),
        entry_price=Decimal("10000"),
        quantity=Decimal(quantity),
        initial_atr=Decimal("500"),
    )


def _trend_intact(_key: CandidateKey, _as_of: datetime) -> bool:
    return True


def _identity(item: Any) -> tuple[str, str, datetime]:
    return (item.market, item.symbol, item.entry_at)


# 진입가 10000, ATR 500 → 초기 손절 9000. day2 저가 8900이 손절선을 건드린다.
_STOP_SIGNAL_PREFIX = (
    _bar(0, "10000", "10100", "9900", "10000"),
    _bar(1, "10000", "10100", "9950", "10000"),
    _bar(2, "9500", "9600", "8900", "8950"),
)


def test_current_variant_replays_baseline_exits_exactly() -> None:
    candidate = CandidateMetadata(symbol="ALPHA", market="US", sources=("synthetic",))
    bars = _breakout_bars()
    evidence = UniverseEvidence(
        source="synthetic_point_in_time",
        point_in_time_membership=True,
        includes_delisted=True,
        as_of=bars[-1].timestamp,
    )
    baseline = run_portfolio_backtest(
        (candidate,),
        {candidate.key: bars},
        config=_CONFIG,
        benchmark_bars_by_market={"US": _half_scale(bars)},
        universe_evidence=evidence,
    )

    result = run_exit_rule_comparison(
        (candidate,),
        {candidate.key: bars},
        variants={"current": PositionManagerConfig()},
        config=_CONFIG,
        constraints=_NO_CONSTRAINTS,
        benchmark_bars_by_market={"US": _half_scale(bars)},
        universe_evidence=evidence,
    )

    assert result.baseline_determinism_hash == baseline.determinism_hash
    assert result.compared_entry_count >= 1
    closed = [
        outcome for outcome in result.outcomes if outcome.simulation.status == "closed"
    ]
    assert len(closed) == result.compared_entry_count
    replayed_trade_count = 0
    for outcome in closed:
        entry = outcome.entry
        trades = [t for t in baseline.trades if _identity(t) == _identity(entry)]
        expected_price = entry.entry_price.quantize(Decimal("0.00000001"))
        assert trades
        assert all(trade.entry_price == expected_price for trade in trades)
        replayed = [
            (fill.exit_at, fill.reason, fill.quantity, fill.fill_price, fill.net_pnl)
            for fill in outcome.simulation.fills
        ]
        expected = [
            (
                trade.exit_at,
                trade.exit_reason,
                trade.quantity,
                trade.exit_price,
                trade.net_pnl,
            )
            for trade in trades
        ]
        assert replayed == expected
        replayed_trade_count += len(trades)
    open_entries = {_identity(position) for position in baseline.open_positions}
    expected_count = sum(_identity(t) not in open_entries for t in baseline.trades)
    assert replayed_trade_count == expected_count


def test_locked_limit_down_defers_the_krx_exit_to_the_next_open() -> None:
    bars = (
        *_STOP_SIGNAL_PREFIX,
        # 전일 종가 8950의 -30% = 6265, 한 틱(10원) 안에서 하루 종일 잠김.
        _bar(3, "6270", "6270", "6270", "6270", volume="1000"),
        _bar(4, "6100", "6300", "6000", "6200"),
    )
    common = {
        "variant": PositionManagerConfig(),
        "config": _CONFIG,
        "trend_intact": _trend_intact,
        "end_at": bars[-1].timestamp,
    }

    locked = simulate_fixed_entry(
        _kr_entry(),
        bars,
        constraints=ExitExecutionConstraints(exit_volume_participation=None),
        **common,
    )
    unconstrained = simulate_fixed_entry(
        _kr_entry(), bars, constraints=_NO_CONSTRAINTS, **common
    )

    exits = [(fill.exit_at, fill.reason) for fill in locked.fills]
    assert locked.status == "closed"
    assert exits == [(bars[4].timestamp, "STOP")]
    assert locked.fills[0].fill_price == Decimal("6093.9")
    assert locked.limit_down_deferrals == 1
    assert locked.holding_bars == 3
    assert [fill.exit_at for fill in unconstrained.fills] == [bars[3].timestamp]
    assert unconstrained.limit_down_deferrals == 0
    assert locked.net_pnl < unconstrained.net_pnl


def test_volume_cap_splits_the_exit_across_later_bars() -> None:
    bars = (
        *_STOP_SIGNAL_PREFIX,
        _bar(3, "8800", "8900", "8700", "8800", volume="5000"),
        _bar(4, "8700", "8800", "8600", "8700", volume="5000"),
    )

    simulation = simulate_fixed_entry(
        _kr_entry(quantity="100"),
        bars,
        variant=PositionManagerConfig(),
        config=_CONFIG,
        constraints=ExitExecutionConstraints(
            krx_daily_limit_rate=None,
            exit_volume_participation=Decimal("0.01"),
        ),
        trend_intact=_trend_intact,
        end_at=bars[-1].timestamp,
    )

    fills = [(f.exit_at, f.reason, f.quantity) for f in simulation.fills]
    assert simulation.status == "closed"
    assert fills == [
        (bars[3].timestamp, "STOP", Decimal("50")),
        (bars[4].timestamp, "STOP", Decimal("50")),
    ]
    assert simulation.volume_capped_bars == 1


def test_entry_open_at_end_in_any_variant_leaves_every_summary() -> None:
    flat = _kr_entry(symbol="000001")
    stopped = _kr_entry(symbol="000002")
    flat_bars = tuple(_bar(day, "10000", "10050", "9950", "10000") for day in range(7))
    stopped_bars = (
        *_STOP_SIGNAL_PREFIX,
        *(_bar(day, "8900", "9000", "8800", "8900") for day in range(3, 7)),
    )

    result = compare_exit_variants(
        (flat, stopped),
        {flat.key: flat_bars, stopped.key: stopped_bars},
        variants={
            "short": PositionManagerConfig(max_holding_bars=1),
            "long": PositionManagerConfig(max_holding_bars=50),
        },
        config=_CONFIG,
        constraints=_NO_CONSTRAINTS,
        trend_intact=_trend_intact,
        end_at=flat_bars[-1].timestamp,
    )

    assert result.fixed_entry_count == 2
    assert result.compared_entry_count == 1
    assert result.excluded == (
        ExcludedEntry(
            market="KR",
            symbol="000001",
            entry_at=flat.entry_at,
            variant="long",
            reason="open_at_end",
        ),
    )
    short_outcome = next(
        outcome
        for outcome in result.outcomes
        if outcome.entry == flat and outcome.variant == "short"
    )
    assert [fill.reason for fill in short_outcome.simulation.fills] == ["TIME_STOP"]
    summaries = [(s.name, s.entry_count, s.exit_reasons) for s in result.summaries]
    assert summaries == [
        ("short", 1, (("STOP", 1),)),
        ("long", 1, (("STOP", 1),)),
    ]
