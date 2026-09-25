"""Behavior checks for the read-only KR strategy validation CLI (pure paths only)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts import kasset_strategy_validation as sv

pytestmark = pytest.mark.unit

VOLUME = 300_000.0
SLIP = sv.CommonParams().slippage_rate


def _sessions(n: int) -> list[date]:
    out, d = [], date(2025, 1, 6)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _candles(sessions: list[date], closes: dict[str, np.ndarray]) -> pd.DataFrame:
    rows = []
    for sym, cl in closes.items():
        for t, d in enumerate(sessions):
            c = float(cl[t])
            o = float(cl[t - 1]) * 1.001 if t else c
            rows.append(
                {
                    "symbol": sym,
                    "d": d,
                    "open": o,
                    "high": max(o, c) * 1.003,
                    "low": min(o, c) * 0.997,
                    "close": c,
                    "volume": VOLUME,
                    "value": c * VOLUME,
                    "source": "toss",
                }
            )
    return pd.DataFrame(rows)


def _flow(
    sessions: list[date], nets: dict[str, list[tuple[float | None, float | None]]]
):
    rows = [
        {
            "symbol": sym,
            "d": sessions[t],
            "foreign_net": f,
            "institution_net": i,
            "source": "naver_finance",
        }
        for sym, series in nets.items()
        for t, (f, i) in enumerate(series)
    ]
    return pd.DataFrame(
        rows, columns=["symbol", "d", "foreign_net", "institution_net", "source"]
    )


def _set_bar(df: pd.DataFrame, sym: str, d: date, **vals: float) -> pd.DataFrame:
    mask = (df["symbol"] == sym) & (df["d"] == d)
    for k, v in vals.items():
        df.loc[mask, k] = v
    return df


T_FLOW = 160
X_SPIKE = 120  # one-day flow spike for "000010"


def _flow_fixture(with_b: bool = True):
    sessions = _sessions(T_FLOW)
    trend = 10_000.0 * 1.002 ** np.arange(T_FLOW)
    closes = {"000010": trend, "000020": trend.copy(), "000030": trend.copy()}
    spike = [(0.0, 0.0)] * T_FLOW
    spike[X_SPIKE] = (500_000.0, 0.0)
    nets = {"000010": spike}
    if with_b:
        steady = [(500_000.0, 0.0)] * T_FLOW
        steady[130] = (500_000.0, None)  # null institution -> window unusable
        nets["000020"] = steady
    return sessions, _candles(sessions, closes), _flow(sessions, nets)


def _panel(candles: pd.DataFrame, flow: pd.DataFrame, sessions: list[date]):
    panel = sv.build_panel(candles, flow, as_of=sessions[-1])
    return panel, sv.compute_indicators(panel)


def test_flow_signal_uses_only_prior_sessions_and_null_is_not_zero() -> None:
    sessions, candles, flow = _flow_fixture()
    panel, ind = _panel(candles, flow, sessions)
    a, b = panel.symbols.index("000010"), panel.symbols.index("000020")

    # Spike on day X is invisible at X's own close; used for X+1..X+5 only.
    assert not ind.flow_signal[X_SPIKE, a]
    assert ind.flow_signal[X_SPIKE + 1 : X_SPIKE + 6, a].all()
    assert not ind.flow_signal[X_SPIKE + 6, a]
    # A null institution_net on day 130 removes every window containing it
    # (zero-filling would keep 4x500k and still signal).
    assert ind.flow_signal[130, b]
    assert not ind.flow_signal[131:136, b].any()
    assert ind.flow_signal[136, b]


def test_flow_entry_fills_at_next_session_open_with_slippage() -> None:
    sessions, candles, flow = _flow_fixture(with_b=False)
    panel, ind = _panel(candles, flow, sessions)
    st = sv.run_flow_rank(panel, ind, 100, T_FLOW - 1)
    rep = sv.summarize_run(panel, st, 100, T_FLOW - 1, 1)
    (trade,) = [t for t in rep["trades"] if t["symbol"] == "000010"]
    a = panel.symbols.index("000010")
    assert trade["signal_date"] == sessions[X_SPIKE + 1].isoformat()
    assert trade["entry_date"] == sessions[X_SPIKE + 2].isoformat()
    assert trade["entry_fill"] == pytest.approx(
        panel.open[X_SPIKE + 2, a] * (1 + SLIP), rel=1e-6
    )
    # 10 sessions held counting the entry day, exit at the following open.
    assert trade["exit_reason"] == "time_exit"
    assert trade["holding_sessions"] == 10


def test_future_price_edits_do_not_change_past_signals_nav_or_trades() -> None:
    sessions, candles, flow = _flow_fixture()
    k = 128
    edited = candles.copy()
    future = edited["d"] >= sessions[k]
    for col in ("open", "high", "low", "close"):
        edited.loc[future, col] = edited.loc[future, col] * 0.6
    base_panel, base_ind = _panel(candles, flow, sessions)
    edit_panel, edit_ind = _panel(edited, flow, sessions)

    base = sv.run_flow_rank(base_panel, base_ind, 100, T_FLOW - 1)
    edit = sv.run_flow_rank(edit_panel, edit_ind, 100, T_FLOW - 1)
    cut = sessions[k].isoformat()
    assert [s for s in base.signals if s["signal_date"] < cut] == [
        s for s in edit.signals if s["signal_date"] < cut
    ]
    assert [v for t, v in base.nav if t < k] == [v for t, v in edit.nav if t < k]
    assert [t for t in base.trades if t["exit_date"] < cut] == [
        t for t in edit.trades if t["exit_date"] < cut
    ]
    assert base.nav[-1][1] != edit.nav[-1][1]  # the edit is actually visible later


def test_gap_below_stop_exits_at_open_not_at_stop() -> None:
    sessions, candles, flow = _flow_fixture(with_b=False)
    g = X_SPIKE + 4  # entry is at X+2
    prev_close = float(
        candles[
            (candles.symbol == "000010") & (candles.d == sessions[g - 1])
        ].close.iloc[0]
    )
    gap_open = prev_close * 0.8
    candles = _set_bar(
        candles,
        "000010",
        sessions[g],
        open=gap_open,
        high=gap_open * 1.01,
        low=gap_open * 0.99,
        close=gap_open,
    )
    panel, ind = _panel(candles, flow, sessions)
    st = sv.run_flow_rank(panel, ind, 100, T_FLOW - 1)
    (trade,) = [
        t
        for t in st.trades
        if t["symbol"] == "000010"
        and t["entry_date"] == sessions[X_SPIKE + 2].isoformat()
    ]
    assert trade["exit_reason"] == "stop_gap_open"
    assert trade["exit_date"] == sessions[g].isoformat()
    assert trade["exit_fill"] == pytest.approx(gap_open * (1 - SLIP), rel=1e-6)


@pytest.mark.parametrize("blocked_bar", ["locked", "missing"])
def test_exit_is_not_filled_on_locked_or_missing_bar_and_stays_pending(
    blocked_bar: str,
) -> None:
    sessions, candles, flow = _flow_fixture(with_b=False)
    exit_day = X_SPIKE + 2 + 10  # time exit scheduled at close of the 10th session
    if blocked_bar == "missing":
        candles = candles[
            ~((candles.symbol == "000010") & (candles.d == sessions[exit_day]))
        ]
    else:
        prev = float(
            candles[
                (candles.symbol == "000010") & (candles.d == sessions[exit_day - 1])
            ].close.iloc[0]
        )
        candles = _set_bar(
            candles,
            "000010",
            sessions[exit_day],
            open=prev,
            high=prev,
            low=prev,
            close=prev,
        )
    panel, ind = _panel(candles, flow, sessions)
    st = sv.run_flow_rank(panel, ind, 100, T_FLOW - 1)
    (trade,) = [t for t in st.trades if t["symbol"] == "000010"]
    assert trade["exit_date"] == sessions[exit_day + 1].isoformat()
    assert trade["exit_deferred_sessions"] == 1
    assert trade["holding_sessions"] == 11
    assert st.deferred_exit_sessions == 1


def test_open_position_at_end_is_unrealized_not_a_closed_trade() -> None:
    sessions, candles, flow = _flow_fixture(with_b=False)
    end = X_SPIKE + 5
    panel, ind = _panel(candles, flow, sessions)
    st = sv.run_flow_rank(panel, ind, 100, end)
    rep = sv.summarize_run(panel, st, 100, end, 1)
    assert rep["closed_trades"] == 0
    assert rep["win_rate"] is None and rep["mean_holding_sessions"] is None
    assert "no_closed_trades" in rep["inconclusive_reasons"]
    assert rep["status"] == "inconclusive"
    (pos,) = rep["terminal_open_positions"]
    assert pos["symbol"] == "000010" and pos["status"] == "unrealized_open_at_end"
    assert rep["ledger_check"]["reconciled"]
    assert rep["final_nav"] == pytest.approx(
        rep["initial_capital"] + rep["terminal_unrealized_pnl"], abs=0.01
    )


def test_cash_and_position_cap_hold_when_every_symbol_signals() -> None:
    sessions = _sessions(T_FLOW)
    trend = 10_000.0 * 1.002 ** np.arange(T_FLOW)
    syms = [f"{i:06d}" for i in range(10, 40)]
    candles = _candles(sessions, dict.fromkeys(syms, trend))
    flow = _flow(sessions, {s: [(500_000.0, 0.0)] * T_FLOW for s in syms})
    panel, ind = _panel(candles, flow, sessions)
    st = sv.run_flow_rank(panel, ind, 100, T_FLOW - 1)
    rep = sv.summarize_run(panel, st, 100, T_FLOW - 1, 1)
    lc = rep["ledger_check"]
    assert lc["max_positions_held"] == 10
    assert lc["cash_never_negative"] and lc["reconciled"]
    nav_by_date = dict(rep["daily_nav"])
    for tr in rep["trades"]:
        cost = tr["qty"] * tr["entry_fill"] + tr["buy_fee"]
        assert cost <= nav_by_date[tr["signal_date"]] / 10 + 1.0  # 1 KRW rounding slack
    # Max 3 new orders per signal day.
    per_day: dict[str, int] = {}
    for s in st.signals:
        per_day[s["signal_date"]] = per_day.get(s["signal_date"], 0) + 1
    assert max(per_day.values()) <= 3


T_B = 320
B_START = 190


def _b_fixture():
    sessions = _sessions(T_B)
    rng = np.random.default_rng(7)
    closes = {}
    for i in range(20):
        strong = i < 5
        drift = np.full(T_B, 0.003 if strong else 0.0015)
        if not strong:
            drift[260:] = -0.015
        steps = 1 + drift + 0.004 * rng.standard_normal(T_B)
        steps[0] = 1.0
        closes[f"{i + 100:06d}"] = 10_000.0 * np.cumprod(steps)
    empty_flow = _flow(sessions, {})
    return sessions, _candles(sessions, closes), empty_flow


def test_b_breadth_filters_keep_calendar_and_act_only_on_weak_breadth() -> None:
    sessions, candles, flow = _b_fixture()
    panel, ind = _panel(candles, flow, sessions)
    runs = {
        v: sv.run_price_factor(panel, ind, B_START, T_B - 1, v) for v in sv.B_VARIANTS
    }
    dates = {v: [e["date"] for e in st.events] for v, st in runs.items()}
    assert (
        dates["baseline"]
        == dates["breadth_block_new_buys"]
        == dates["breadth_liquidate_and_block"]
    )
    idx = {d.isoformat(): t for t, d in enumerate(sessions)}

    weak_events = [
        e
        for e in runs["baseline"].events
        if e["breadth"] is not None and e["breadth"] < 0.4
    ]
    assert weak_events, "fixture must produce a weak-breadth rebalance"
    for e in runs["breadth_block_new_buys"].events:
        if e["breadth"] is not None and e["breadth"] < 0.4:
            assert e["new_buys_blocked_by_breadth"] and e["new_orders"] == 0
    assert not any(
        t["exit_reason"] == "breadth_liquidation" for t in runs["baseline"].trades
    )
    assert not any(
        t["exit_reason"] == "breadth_liquidation"
        for t in runs["breadth_block_new_buys"].trades
    )

    liq = [
        t
        for t in runs["breadth_liquidate_and_block"].trades
        if t["exit_reason"] == "breadth_liquidation"
    ]
    assert liq
    for t in liq:  # decided on the previous session's close breadth
        assert ind.breadth[idx[t["exit_date"]] - 1] < 0.4
    for v in ("breadth_block_new_buys", "breadth_liquidate_and_block"):
        for s in runs[v].signals:
            assert ind.breadth[idx[s["signal_date"]]] >= 0.4
    for v, st in runs.items():
        rep = sv.summarize_run(panel, st, B_START, T_B - 1, 1)
        assert rep["ledger_check"]["reconciled"], v
        assert rep["ledger_check"]["max_positions_held"] <= 10, v


def test_b_future_price_edits_do_not_change_past_selection() -> None:
    sessions, candles, flow = _b_fixture()
    k = 280
    edited = candles.copy()
    future = edited["d"] >= sessions[k]
    edited.loc[future, "close"] = edited.loc[future, "close"] * 1.3
    edited.loc[future, "high"] = edited.loc[future, ["high", "close"]].max(axis=1)
    base_panel, base_ind = _panel(candles, flow, sessions)
    edit_panel, edit_ind = _panel(edited, flow, sessions)
    cut = sessions[k].isoformat()
    for v in sv.B_VARIANTS:
        base = sv.run_price_factor(base_panel, base_ind, B_START, T_B - 1, v)
        edit = sv.run_price_factor(edit_panel, edit_ind, B_START, T_B - 1, v)
        assert [s for s in base.signals if s["signal_date"] < cut] == [
            s for s in edit.signals if s["signal_date"] < cut
        ]
        assert [x for t, x in base.nav if t < k] == [x for t, x in edit.nav if t < k]
