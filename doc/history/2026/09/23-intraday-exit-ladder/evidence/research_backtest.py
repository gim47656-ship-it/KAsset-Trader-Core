"""Same-entry-cycle 5-minute exit-arm research; run in the isolated server image."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.portfolio_backtest import (
    BacktestWindow,
    PortfolioBacktestConfig,
    run_portfolio_backtest,
)
from app.extensions.kasset.automation.position_manager import (
    ExitKind,
    PositionBar,
    PositionManagerConfig,
    _raise_trailing_stop,
    evaluate_position,
    evaluate_position_intraday,
    initialize_position,
)

D = Decimal
BASE = Path(sys.argv[1])
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 23, 23, 59, tzinfo=UTC)
INTERVAL = timedelta(minutes=5)


def load_rows(name):
    with (BASE / name).open(newline="", encoding="utf-8") as stream:
        first = stream.readline()
        assert first.strip() == "SET", first
        yield from csv.DictReader(stream)


def timestamp(raw):
    return datetime.fromisoformat(raw).astimezone(UTC)


def load():
    daily = defaultdict(list)
    for row in load_rows("research-daily-temporary.csv"):
        daily[row["symbol"]].append(
            PriceBar(
                timestamp(row["time"]),
                *(D(row[k]) for k in ("open", "high", "low", "close", "volume")),
            )
        )
    intraday = defaultdict(lambda: defaultdict(list))
    for filename in (
        "research-intraday-earlier-temporary.csv",
        "research-intraday-temporary.csv",
    ):
        for row in load_rows(filename):
            intraday[row["symbol"]][date.fromisoformat(row["session_date_kst"])].append(
                PriceBar(
                    timestamp(row["bucket"]),
                    *(D(row[k]) for k in ("open", "high", "low", "close", "volume")),
                )
            )
    return daily, intraday


def complete_session(bars):
    if len(bars) < 77 or bars[0].timestamp.hour != 0 or bars[0].timestamp.minute != 0:
        return False
    return all(
        current.timestamp - previous.timestamp == INTERVAL
        for previous, current in zip(bars, bars[1:], strict=False)
    )


def replay_cycle(
    symbol, entry_at, entry_price, quantity, daily, intraday, sessions, arm
):
    config = PositionManagerConfig()
    cost = PortfolioBacktestConfig().kr_cost
    # Daily engine fixes the entry price/quantity; exit paths only differ after entry.
    atr_rows = [b for b in daily[symbol] if b.timestamp < entry_at][-15:]
    ranges = [
        max(b.high - b.low, abs(b.high - previous.close), abs(b.low - previous.close))
        for previous, b in zip(atr_rows, atr_rows[1:], strict=False)
    ]
    if len(ranges) != 14:
        return None
    atr = sum(ranges, D("0")) / D(14)
    if atr <= 0 or entry_price - 3 * atr <= 0:
        return None
    state = initialize_position(
        market="KRX",
        symbol=symbol,
        entry_price=entry_price,
        initial_atr=atr,
        entry_at=entry_at,
        strategy_version="research-v1",
        position_cycle_id=1,
    )
    allocation = entry_price * quantity
    cash = -(allocation + max(allocation * cost.fee_rate, cost.min_fee_absolute))
    curve = []
    fills = []
    pending = None
    second_done = False
    bars_held = 0
    last_close = entry_price
    for day in sessions:
        if day < entry_at.date() or quantity <= 0:
            continue
        day_bars = intraday[symbol][day]
        for item in day_bars:
            if item.timestamp < entry_at or quantity <= 0:
                continue
            if pending is not None and item.timestamp >= pending[2]:
                kind, fraction, _signal_at = pending
                amount = (
                    quantity
                    if fraction == 1
                    else (quantity * fraction).quantize(D("1"), rounding=ROUND_DOWN)
                )
                if fraction < 1 and arm != "head" and quantity >= 2:
                    amount = min(quantity - 1, max(D("1"), amount))
                pending = None
                if amount > 0:
                    sell_price = item.open * (1 - cost.slippage_rate)
                    proceeds = sell_price * amount
                    cash += (
                        proceeds
                        - max(proceeds * cost.fee_rate, cost.min_fee_absolute)
                        - proceeds * cost.sell_tax_rate
                    )
                    quantity -= amount
                    fills.append(
                        (kind, str(item.timestamp), str(amount), str(sell_price))
                    )
                    if kind == "PARTIAL_SELL":
                        state = replace(state, partial_exit_completed=True)
                        if arm != "head" and state.entry_price > state.current_stop:
                            state = _raise_trailing_stop(
                                state,
                                raised_stop=state.entry_price,
                                effective_at=item.timestamp,
                            )
                    if kind == "SECOND_PARTIAL_SELL":
                        second_done = True
            if quantity <= 0:
                curve.append((item.timestamp, cash))
                break
            bar = PositionBar(
                as_of=item.timestamp + INTERVAL,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                starts_at=item.timestamp,
                ends_at=item.timestamp + INTERVAL,
            )
            last_close = bar.close
            if pending is None:
                signal = evaluate_position_intraday(
                    state, (bar,), bar_interval=INTERVAL
                )
                if (
                    signal is not None
                    and signal.kind is ExitKind.PARTIAL_SELL
                    and quantity == 1
                    and arm != "head"
                ):
                    floor = state.entry_price
                    if floor > state.current_stop:
                        state = _raise_trailing_stop(
                            state, raised_stop=floor, effective_at=bar.ends_at
                        )
                elif signal is not None:
                    pending = (
                        signal.kind.value,
                        signal.quantity_fraction,
                        signal.signal_at,
                    )
                elif (
                    arm == "second"
                    and state.partial_exit_completed
                    and not second_done
                    and bar.high >= entry_price + atr
                ):
                    pending = ("SECOND_PARTIAL_SELL", D("0.3"), bar.as_of)
            if arm.startswith("trail") and quantity > 0:
                width = D(arm.split("-")[1]) if state.partial_exit_completed else D("2")
                activation = (
                    entry_price + atr
                    if not state.partial_exit_completed
                    else entry_price
                )
                target = bar.high - width * atr
                if bar.high >= activation and target > state.current_stop:
                    state = _raise_trailing_stop(
                        state, raised_stop=target, effective_at=bar.ends_at
                    )
            curve.append((bar.as_of, cash + quantity * bar.close))
        daily_bar = next(
            (item for item in daily[symbol] if item.timestamp.date() == day), None
        )
        if (
            daily_bar is not None
            and quantity > 0
            and pending is None
            and daily_bar.timestamp > entry_at
        ):
            bars_held += 1
            evaluation = evaluate_position(
                state,
                PositionBar(
                    as_of=daily_bar.timestamp,
                    open=daily_bar.open,
                    high=daily_bar.high,
                    low=daily_bar.low,
                    close=daily_bar.close,
                    starts_at=daily_bar.timestamp,
                    ends_at=daily_bar.timestamp + timedelta(hours=6, minutes=30),
                ),
                bars_held=bars_held,
                config=config,
            )
            state = evaluation.state
            if evaluation.signal is not None:
                pending = (
                    evaluation.signal.kind.value,
                    evaluation.signal.quantity_fraction,
                    daily_bar.timestamp + timedelta(hours=6, minutes=30),
                )
    net = cash + quantity * last_close
    return {
        "net": net,
        "curve": curve,
        "closed": quantity == 0,
        "fills": fills,
        "unrealized_quantity": quantity,
        "entry_value": allocation,
    }


def metrics(cycles, initial_cash):
    events = []
    for index, cycle in enumerate(cycles):
        events.extend((at, index, net) for at, net in cycle["curve"])
    last = [D("0")] * len(cycles)
    peak = initial_cash
    mdd = D("0")
    for _at, index, net in sorted(events):
        last[index] = net
        equity = initial_cash + sum(last, D("0"))
        peak = max(peak, equity)
        if peak > 0:
            mdd = max(mdd, (peak - equity) / peak)
    closed = [item for item in cycles if item["closed"]]
    rates = [item["net"] / item["entry_value"] for item in closed]
    wins = [rate for rate in rates if rate > 0]
    losses = [rate for rate in rates if rate < 0]
    return {
        "total_return_pct": str(
            100 * sum((item["net"] for item in cycles), D("0")) / initial_cash
        ),
        "mdd_pct": str(100 * mdd),
        "cycles": len(closed),
        "censored_open": len(cycles) - len(closed),
        "win_rate_pct": str(100 * len(wins) / len(rates)) if rates else None,
        "payoff": str((sum(wins) / len(wins)) / (-sum(losses) / len(losses)))
        if wins and losses
        else None,
        "expectancy_pct": str(100 * sum(rates, D("0")) / len(rates)) if rates else None,
        "exit_legs": dict(
            Counter(fill[0] for item in cycles for fill in item["fills"])
        ),
    }


def main():
    daily, intraday = load()
    candidates = tuple(
        CandidateMetadata(symbol=s, market="KR", sources=("research_cohort",))
        for s in sorted(daily)
    )
    result = run_portfolio_backtest(
        candidates,
        {item.key: daily[item.symbol] for item in candidates},
        config=PortfolioBacktestConfig(),
        window=BacktestWindow(START, END),
    )
    positions = {}
    for trade in result.trades:
        key = (trade.symbol, trade.entry_at)
        if key not in positions:
            positions[key] = [trade.entry_price, D("0"), trade.exit_at]
        positions[key][1] += trade.quantity
        positions[key][2] = max(positions[key][2], trade.exit_at)
    for opened in result.open_positions:
        key = (opened.symbol, opened.entry_at)
        if key not in positions:
            positions[key] = [opened.entry_price, D("0"), END]
        positions[key][1] += opened.quantity
        positions[key][2] = END
    histogram = Counter(
        len(bars) for days in intraday.values() for bars in days.values()
    )
    all_sessions = sorted({day for days in intraday.values() for day in days})
    eligible = {}
    exclusions = Counter()
    for (symbol, entry_at), (price, quantity, _) in positions.items():
        required = [day for day in all_sessions if day >= entry_at.date()]
        bad = [
            day
            for day in required
            if not complete_session(intraday[symbol].get(day, []))
        ]
        if bad:
            exclusions["missing_or_noncontiguous_5m"] += 1
            continue
        eligible[(symbol, entry_at)] = (price, quantity)
    arms = (
        "head",
        "floor",
        "trail-1",
        "trail-1.5",
        "trail-2",
        "trail-2.5",
        "trail-3",
        "second",
    )
    outcomes = {}
    for arm in arms:
        replayed = [
            replay_cycle(
                symbol, at, price, quantity, daily, intraday, all_sessions, arm
            )
            for (symbol, at), (price, quantity) in eligible.items()
        ]
        valid = [item for item in replayed if item is not None]
        outcomes[arm] = metrics(valid, PortfolioBacktestConfig().initial_cash)
        outcomes[arm]["invalid_atr_cycles"] = len(replayed) - len(valid)
    print(
        json.dumps(
            {
                "method": "daily-engine breakout-baseline fixed entries, one-CPU 5m path, next completed bucket open, conservative fees/slippage",
                "daily_symbols": len(daily),
                "daily_bars": sum(map(len, daily.values())),
                "intraday_symbols": len(intraday),
                "intraday_bars": sum(
                    len(b) for days in intraday.values() for b in days.values()
                ),
                "intraday_days": [str(day) for day in all_sessions],
                "intraday_bars_per_symbol_day": dict(sorted(histogram.items())),
                "daily_engine_entry_cycles": len(positions),
                "daily_engine_entry_dates": dict(
                    Counter(str(k[1].date()) for k in positions)
                ),
                "daily_engine_return": str(result.total_return),
                "daily_engine_mdd": str(result.max_drawdown),
                "eligible_cycles": len(eligible),
                "excluded_cycles": dict(exclusions),
                "arms": outcomes,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
