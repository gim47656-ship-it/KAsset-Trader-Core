"""Offline, fixed-input KR exit research. Run only in a network-disabled server image.

CSV inputs are COPY TO STDOUT output with the leading SET line. No database connection.
The daily portfolio recomputes entries (turnover can change). Five-minute arms freeze
HEAD daily-engine entries and replay only exits; these returns are not portfolio returns.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from app.extensions.kasset.automation.candidate_ranker import CandidateMetadata
from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation import portfolio_backtest as pb
from app.extensions.kasset.automation import position_manager as pm

D = Decimal
BASE = Path(sys.argv[1])
START = datetime(2025, 1, 1, tzinfo=UTC)
ORIGINAL_END = datetime(2026, 9, 22, 23, 59, tzinfo=UTC)
INTERVAL = timedelta(minutes=5)
SESSION_CLOSE = timedelta(hours=6, minutes=30)
CASH = pb.PortfolioBacktestConfig().initial_cash
COST = pb.PortfolioBacktestConfig().kr_cost


@dataclass(frozen=True)
class Arm:
    stop: int = 3
    hold: int = 10
    force: bool = False
    second: D | None = None
    trail: D | None = None

    def label(self):
        if self == Arm():
            return "HEAD"
        return f"s{self.stop}-h{self.hold}{'F' if self.force else 'C'}-p{self.second or 0}-t{self.trail or 0}"
    def bars_after_entry(self):
        # PortfolioBacktest increments bars_held only AFTER the entry-date bar.
        return self.hold if self.hold == 10 else self.hold - 1



def rows(name):
    with (BASE / name).open(newline="", encoding="utf-8") as stream:
        first = stream.readline()
        if first.strip() != "SET":
            raise ValueError(f"unexpected COPY prefix {name}: {first!r}")
        yield from csv.DictReader(stream)


def stamp(raw):
    return datetime.fromisoformat(raw).astimezone(UTC)


def load():
    daily = defaultdict(list)
    for row in rows("daily.csv"):
        daily[row["symbol"]].append(PriceBar(stamp(row["time"]), *(D(row[k]) for k in ("open", "high", "low", "close", "volume"))))
    intraday = defaultdict(lambda: defaultdict(list))
    for row in rows("intraday.csv"):
        intraday[row["symbol"]][date.fromisoformat(row["session_date_kst"])].append(
            PriceBar(stamp(row["bucket"]), *(D(row[k]) for k in ("open", "high", "low", "close", "volume")))
        )
    for days in intraday.values():
        for bars in days.values():
            bars.sort(key=lambda b: b.timestamp)
    return daily, intraday


def stats(rates):
    wins = [r for r in rates if r > 0]
    losses = [r for r in rates if r < 0]
    return {
        "win_rate_pct": str(100 * D(len(wins)) / len(rates)) if rates else None,
        "payoff": str(sum(wins) / len(wins) / (-sum(losses) / len(losses))) if wins and losses else None,
        "expectancy_pct": str(100 * sum(rates, D(0)) / len(rates)) if rates else None,
    }


def daily_run(daily, arm):
    """Adapt only the position evaluator, retaining the portfolio engine unchanged."""
    cfg = pm.PositionManagerConfig(initial_stop_atr=D(arm.stop), max_holding_bars=arm.bars_after_entry())
    base_eval = pb.evaluate_position
    base_bar = pb.PositionBar
    special_reasons = {}
    # The portfolio engine supplies a date-label daily bar without starts_at.
    # The current evaluator requires provenance; research-only bridge, never source patch.
    def dated_bar(*args, **kwargs):
        kwargs["starts_at"] = kwargs["as_of"]
        kwargs["ends_at"] = kwargs["as_of"] + SESSION_CLOSE
        return base_bar(*args, **kwargs)

    base_queue = pb._queue_position_exits
    second_sent = set()
    def evaluate(state, bar, *, bars_held, trend_intact=True, config=cfg):
        key = (state.symbol, state.entry_at)
        effective = replace(cfg, max_holding_bars=999) if arm.force else cfg
        result = base_eval(state, bar, bars_held=bars_held, trend_intact=trend_intact, config=effective)
        # At the deadline, a partial-profit signal cannot defer a full exit.
        # A protective full-exit signal already satisfies the holding limit.
        if arm.force and bars_held >= arm.bars_after_entry() and (result.signal is None or result.signal.quantity_fraction < 1):
            special_reasons[(key, bar.as_of)] = "TIME_LIMIT"
            return pm.PositionEvaluation(replace(state, last_evaluated_at=bar.as_of), pm._signal(state, bar, kind=pm.ExitKind.TIME_STOP, fraction=D(1), price=bar.close, reason="unconditional holding limit"))
        if result.signal is not None:
            return result
        new_state = result.state
        if arm.second is not None and state.partial_exit_completed and key not in second_sent:
            target = state.entry_price + arm.second * state.initial_atr
            if bar.high >= target:
                second_sent.add(key)
                special_reasons[(key, bar.as_of)] = "SECOND_PARTIAL_SELL"
                return pm.PositionEvaluation(new_state, pm._signal(state, bar, kind=pm.ExitKind.PARTIAL_SELL, fraction=D("0.3"), price=max(bar.open, target), reason="second profit, 30% of remaining"))
        # Daily approximation: today's high raises tomorrow's stop, never triggers on
        # the same bar. Intraday high-water path is measured separately below.
        if arm.trail is not None and state.partial_exit_completed:
            target = max(bar.high, state.highest_close) - arm.trail * state.initial_atr
            if target > new_state.current_stop:
                new_state = pm._raise_trailing_stop(new_state, raised_stop=target, effective_at=bar.ends_at)
        return pm.PositionEvaluation(new_state, None)

    def queue(state, histories, keys, **kwargs):
        base_queue(state, histories, keys, **kwargs)
        for key, order in state.pending_exits.items():
            reason = special_reasons.get(((key[1], state.positions[key].entry_at), kwargs["timestamp"]))
            if reason is not None:
                state.pending_exits[key] = replace(order, reason=reason)

    pb.evaluate_position = evaluate
    pb.PositionBar = dated_bar
    pb._queue_position_exits = queue
    try:
        candidates = tuple(CandidateMetadata(symbol=s, market="KR", sources=("research_cohort",)) for s in sorted(daily))
        result = pb.run_portfolio_backtest(candidates, {c.key: daily[c.symbol] for c in candidates}, config=replace(pb.PortfolioBacktestConfig(), position_manager=cfg), window=pb.BacktestWindow(START, ORIGINAL_END))
    finally:
        pb.evaluate_position = base_eval
        pb._queue_position_exits = base_queue
        pb.PositionBar = base_bar
    cycles = defaultdict(lambda: [D(0), D(0)])
    for trade in result.trades:
        value = cycles[(trade.market, trade.symbol, trade.entry_at)]
        value[0] += trade.net_pnl
        value[1] += trade.entry_price * trade.quantity
    open_keys = {(p.market, p.symbol, p.entry_at) for p in result.open_positions}
    for opened in result.open_positions:
        cycles[(opened.market, opened.symbol, opened.entry_at)][1] += opened.entry_price * opened.quantity
    rates = [pnl / capital for key, (pnl, capital) in cycles.items() if key not in open_keys]
    historic_rates = [pnl / capital for pnl, capital in cycles.values()]
    return result, {
        "return_pct": str(100 * result.total_return),
        "mdd_pct": str(100 * result.max_drawdown),
        "completed_cycles": len(rates),
        "open_cycles": len(result.open_positions),
        "turnover_cycles": len(cycles),
        "exit_legs": dict(Counter(t.exit_reason for t in result.trades)),
        "historic_all_cycle_payoff": stats(historic_rates)["payoff"],
        **stats(rates),
    }


def complete(bars):
    return len(bars) == 77 and bars[0].timestamp.hour == 0 and bars[0].timestamp.minute == 0 and all(b.timestamp - a.timestamp == INTERVAL for a, b in zip(bars, bars[1:]))


def fixed_entries(result):
    positions = {}
    for trade in result.trades:
        key = trade.symbol, trade.entry_at
        positions.setdefault(key, [trade.entry_price, D(0)])
        positions[key][1] += trade.quantity
    for opened in result.open_positions:
        key = opened.symbol, opened.entry_at
        positions.setdefault(key, [opened.entry_price, D(0)])
        positions[key][1] += opened.quantity
    return positions


def replay(symbol, entry_at, price, starting_qty, daily, intraday, days, arm):
    cfg = pm.PositionManagerConfig(initial_stop_atr=D(arm.stop), max_holding_bars=arm.bars_after_entry())
    prev = [bar for bar in daily[symbol] if bar.timestamp < entry_at][-15:]
    ranges = [max(bar.high - bar.low, abs(bar.high - earlier.close), abs(bar.low - earlier.close)) for earlier, bar in zip(prev, prev[1:])]
    if len(ranges) != 14:
        return None
    atr = sum(ranges, D(0)) / 14
    if atr <= 0 or price - cfg.initial_stop_atr * atr <= 0:
        return None
    state = pm.initialize_position(market="KRX", symbol=symbol, entry_price=price, initial_atr=atr, entry_at=entry_at, strategy_version="research-v1", position_cycle_id=1, config=cfg)
    quantity = starting_qty
    allocation = price * quantity
    cash = -allocation - max(allocation * COST.fee_rate, COST.min_fee_absolute)
    curve = []
    fills = []
    pending = None
    second_done = False
    bars_held = 0
    last_close = price
    highest_intraday = price
    daily_lookup = {bar.timestamp.date(): bar for bar in daily[symbol]}
    for day in days:
        if day < entry_at.date() or quantity <= 0:
            continue
        for item in intraday[symbol][day]:
            if item.timestamp < entry_at or quantity <= 0:
                continue
            if pending is not None and item.timestamp >= pending[2]:
                kind, fraction, _ = pending
                amount = quantity if fraction == 1 else (quantity * fraction).quantize(D(1), rounding=ROUND_DOWN)
                if fraction < 1 and quantity >= 2:
                    amount = min(quantity - 1, max(D(1), amount))
                pending = None
                if amount:
                    fill_price = item.open * (1 - COST.slippage_rate)
                    proceeds = fill_price * amount
                    cash += proceeds - max(proceeds * COST.fee_rate, COST.min_fee_absolute) - proceeds * COST.sell_tax_rate
                    quantity -= amount
                    fills.append((kind, str(item.timestamp), str(amount), str(fill_price)))
                    if kind == "PARTIAL_SELL":
                        state = replace(state, partial_exit_completed=True)
                        if state.entry_price > state.current_stop:
                            state = pm._raise_trailing_stop(state, raised_stop=state.entry_price, effective_at=item.timestamp)
                    if kind == "SECOND_PARTIAL_SELL":
                        second_done = True
            if quantity <= 0:
                curve.append((item.timestamp, cash))
                break
            bar = pm.PositionBar(as_of=item.timestamp + INTERVAL, open=item.open, high=item.high, low=item.low, close=item.close, starts_at=item.timestamp, ends_at=item.timestamp + INTERVAL)
            last_close = bar.close
            if pending is None:
                signal = pm.evaluate_position_intraday(state, (bar,), bar_interval=INTERVAL, config=cfg)
                if signal is not None and signal.kind == pm.ExitKind.PARTIAL_SELL and quantity == 1:
                    if state.entry_price > state.current_stop:
                        state = pm._raise_trailing_stop(state, raised_stop=state.entry_price, effective_at=bar.ends_at)
                elif signal is not None:
                    pending = (signal.kind.value, signal.quantity_fraction, bar.as_of)
                elif arm.second is not None and state.partial_exit_completed and not second_done and bar.high >= price + arm.second * atr:
                    pending = ("SECOND_PARTIAL_SELL", D("0.3"), bar.as_of)
            if arm.trail is not None and state.partial_exit_completed:
                highest_intraday = max(highest_intraday, bar.high)
                target = highest_intraday - arm.trail * atr
                if target > state.current_stop:
                    state = pm._raise_trailing_stop(state, raised_stop=target, effective_at=bar.ends_at)
            curve.append((bar.as_of, cash + quantity * bar.close))
        daily_bar = daily_lookup.get(day)
        if daily_bar is not None and quantity > 0 and day > entry_at.date():
            bars_held += 1
            close_at = daily_bar.timestamp + SESSION_CLOSE
            if pending is not None:
                # A partial still awaiting its next open cannot evade the deadline.
                if arm.force and bars_held >= arm.bars_after_entry() and pending[1] < 1:
                    pending = ("TIME_LIMIT", D(1), close_at)
                continue
            # Daily labels are midnight UTC; this transition follows the
            # 06:30 UTC close, after the day's completed five-minute buckets.
            # OHLC already tested stop/partial, so do not apply it twice.
            evaluation = pm.evaluate_position(
                state,
                pm.PositionBar(as_of=close_at, open=daily_bar.close, high=daily_bar.close,
                               low=daily_bar.close, close=daily_bar.close,
                               starts_at=daily_bar.timestamp, ends_at=close_at),
                bars_held=bars_held,
                config=replace(cfg, max_holding_bars=999) if arm.force else cfg,
            )
            state = evaluation.state
            if arm.force and bars_held >= arm.bars_after_entry() and (evaluation.signal is None or evaluation.signal.quantity_fraction < 1):
                pending = ("TIME_LIMIT", D(1), close_at)
            elif evaluation.signal is not None:
                pending = (evaluation.signal.kind.value, evaluation.signal.quantity_fraction, close_at)
    return {"net": cash + quantity * last_close, "curve": curve, "closed": quantity == 0, "fills": fills, "entry_value": allocation}


def intraday_metrics(cycles):
    events = sorted((at, index, net) for index, cycle in enumerate(cycles) for at, net in cycle["curve"])
    last = [D(0)] * len(cycles)
    peak = CASH
    mdd = D(0)
    for _, index, net in events:
        last[index] = net
        equity = CASH + sum(last, D(0))
        peak = max(peak, equity)
        if peak > 0:
            mdd = max(mdd, (peak - equity) / peak)
    closed = [cycle for cycle in cycles if cycle["closed"]]
    rates = [cycle["net"] / cycle["entry_value"] for cycle in closed]
    return {"return_pct": str(100 * sum((cycle["net"] for cycle in cycles), D(0)) / CASH), "mdd_pct": str(100 * mdd), "completed_cycles": len(closed), "open_cycles": len(cycles) - len(closed), "turnover_cycles": len(cycles), "exit_legs": dict(Counter(fill[0] for cycle in cycles for fill in cycle["fills"])), **stats(rates)}


def main():
    daily, intraday = load()
    original, baseline = daily_run(daily, Arm())
    print(json.dumps({"phase": "reproduction", "baseline": baseline}, sort_keys=True), flush=True)
    arms = [Arm(), Arm(stop=2)]
    arms += [Arm(hold=n, force=f) for n in (2, 3) for f in (False, True)]
    arms += [Arm(second=D(x)) for x in ("1", "1.5")]
    arms += [Arm(trail=D(x)) for x in ("1", "1.5")]
    arms += [Arm(stop=2, hold=n, force=f, second=D(p), trail=D(k)) for n in (2, 3) for f in (False, True) for p in ("1", "1.5") for k in ("1", "1.5")]
    daily_results = {"HEAD": baseline}
    for arm in arms[1:]:
        _, daily_results[arm.label()] = daily_run(daily, arm)
        print(f"daily_arm_done {arm.label()}", file=sys.stderr, flush=True)
    # The 5m source may extend beyond the daily 417-session baseline; entry engine
    # still caps its signal window at 09-22. Exclude an in-progress 09-28
    # session entirely, but retain completed sessions with symbol-level gaps.
    all_days = sorted(day for day in {day for sessions in intraday.values() for day in sessions}
                      if any(complete(sessions.get(day, [])) for sessions in intraday.values()))
    positions = fixed_entries(original)
    exclusions = Counter()
    eligible = {}
    for (symbol, at), (price, qty) in positions.items():
        if at.date() < all_days[0]:
            exclusions["entry_before_complete_intraday_window"] += 1
            continue
        required = [day for day in all_days if day >= at.date()]
        if not required:
            exclusions["entry_after_intraday_window"] += 1
        elif any(not complete(intraday[symbol].get(day, [])) for day in required):
            exclusions["missing_or_noncontiguous_77_buckets"] += 1
        else:
            eligible[(symbol, at)] = (price, qty)
    intraday_results = {}
    for arm in arms:
        replayed = [replay(symbol, at, price, qty, daily, intraday, all_days, arm) for (symbol, at), (price, qty) in eligible.items()]
        valid = [cycle for cycle in replayed if cycle is not None]
        intraday_results[arm.label()] = {**intraday_metrics(valid), "invalid_atr_cycles": len(replayed) - len(valid)}
    print(json.dumps({"phase": "comparison", "source": {"daily_symbols": len(daily), "daily_bars": sum(map(len, daily.values())), "daily_first": str(min(b.timestamp for bars in daily.values() for b in bars)), "daily_last": str(max(b.timestamp for bars in daily.values() for b in bars)), "intraday_bars": sum(len(bars) for days in intraday.values() for bars in days.values()), "intraday_days": [str(day) for day in all_days], "excluded_incomplete_days": [str(day) for day in sorted({day for sessions in intraday.values() for day in sessions} - set(all_days))], "daily_entry_cycles": len(positions), "eligible_cycles": len(eligible), "excluded_cycles": dict(exclusions)}, "daily": daily_results, "intraday": intraday_results}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
