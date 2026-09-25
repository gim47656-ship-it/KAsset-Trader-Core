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
        edited.loc[future, col] = edited.loc[future, col] * 0.8
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


def _pead_fixture(n: int = 170, count: int = 35):
    sessions = _sessions(n)
    closes = {
        f"{j:06d}": np.full(n, 10_000.0 + (count - j) * 100)
        for j in range(1, count + 1)
    }
    candles = _candles(sessions, closes)
    rows = []
    for j in range(1, count + 1):
        for year, income, filing in (
            (2024, 100.0, sessions[0]),
            (2025, float(100 + j * 10), sessions[25]),
        ):
            rows.append(
                {
                    "symbol": f"{j:06d}",
                    "fiscal_period": f"{year}Q1",
                    "period_type": "quarterly",
                    "period_end_date": date(year, 3, 31),
                    "filing_date": filing,
                    "effective_at": filing,
                    "discrete_net_income": income,
                    "net_income": income,
                    "data_state": "fresh",
                }
            )
    fundamentals = pd.DataFrame(rows)
    panel = sv.build_panel(candles, _flow(sessions, {}), as_of=sessions[-1])
    return sessions, candles, panel, fundamentals


def test_pead_filing_next_session_future_edit_and_missing_yoy() -> None:
    sessions, _, panel, fundamentals = _pead_fixture()
    baseline = sv.pead_yoy_panel(panel, fundamentals, sv.PeadParams())
    j = panel.symbols.index("000021")
    assert not np.isfinite(baseline[25, j])
    assert baseline[26, j] == pytest.approx(2.1)
    fallback = fundamentals.copy()
    fallback.loc[
        (fallback.symbol == "000021") & (fallback.fiscal_period == "2025Q1"),
        "filing_date",
    ] = None
    assert sv.pead_yoy_panel(panel, fallback, sv.PeadParams())[26, j] == pytest.approx(
        2.1
    )
    fallback.loc[
        (fallback.symbol == "000021") & (fallback.fiscal_period == "2025Q1"),
        "effective_at",
    ] = None
    assert not np.isfinite(
        sv.pead_yoy_panel(panel, fallback, sv.PeadParams())[:, j]
    ).any()
    edited = fundamentals.copy()
    edited.loc[
        (edited.symbol == "000021") & (edited.fiscal_period == "2025Q1"),
        "discrete_net_income",
    ] = 9999.0
    after = sv.pead_yoy_panel(panel, edited, sv.PeadParams())
    np.testing.assert_array_equal(
        np.isfinite(baseline[:26, j]), np.isfinite(after[:26, j])
    )
    future = fundamentals.loc[
        (fundamentals.symbol == "000021") & (fundamentals.fiscal_period == "2025Q1")
    ].copy()
    future["fiscal_period"] = "2026Q1"
    future["filing_date"] = sessions[100]
    future["effective_at"] = sessions[100]
    future["discrete_net_income"] = 400.0
    future["net_income"] = 400.0
    later = pd.concat([fundamentals, future], ignore_index=True)
    before_edit = sv.pead_yoy_panel(panel, later, sv.PeadParams())
    later.loc[later.fiscal_period == "2026Q1", "discrete_net_income"] = 900.0
    after_edit = sv.pead_yoy_panel(panel, later, sv.PeadParams())
    np.testing.assert_array_equal(before_edit[:101, j], after_edit[:101, j])
    assert before_edit[101, j] != after_edit[101, j]
    edited.loc[
        (edited.symbol == "000021") & (edited.fiscal_period == "2024Q1"),
        "discrete_net_income",
    ] = np.nan
    missing = sv.pead_yoy_panel(panel, edited, sv.PeadParams())
    assert not np.isfinite(missing[:, j]).any()


def test_pead_stale_and_tranche_overlap_duplicate_skip() -> None:
    sessions, _, panel, fundamentals = _pead_fixture(n=180)
    scores = sv.pead_yoy_panel(panel, fundamentals, sv.PeadParams())
    j = panel.symbols.index("000021")
    assert np.isfinite(scores[146, j])
    assert not np.isfinite(scores[147, j])
    st = sv.run_pead(panel, scores, 26, len(sessions) - 1)
    assert [event["date"] for event in st.events[:4]] == [
        sessions[t].isoformat() for t in (26, 46, 66, 86)
    ]
    assert st.events[0]["new_orders"] == 10
    assert st.events[1]["new_orders"] == 5
    assert st.events[1]["tranche_shortfall"] == 5
    assert st.events[2]["new_orders"] == 0
    assert st.max_positions_seen <= 30
    assert all(tr["holding_sessions"] == 60 for tr in st.trades)
    assert all(tr["exit_reason"] == "tranche_maturity" for tr in st.trades)
    summary = sv.summarize_run(
        panel,
        st,
        26,
        len(sessions) - 1,
        1,
        sv.dataclass_replace(sv.CommonParams(), max_positions=30),
    )
    assert summary["ledger_check"]["reconciled"]


def test_pead_pending_old_tranche_blocks_fourth_fill() -> None:
    sessions, candles, _, _ = _pead_fixture(n=110, count=65)
    _set_bar(candles, "000021", sessions[87], close=22_000.0, high=22_100.0)
    panel = sv.build_panel(candles, _flow(sessions, {}), as_of=sessions[-1])
    scores = np.ones(panel.close.shape)
    st = sv.run_pead(panel, scores, 26, 100)
    fourth = st.events[3]
    assert fourth["date"] == sessions[86].isoformat()
    assert fourth["new_orders"] == 10
    assert fourth["tranche_unfilled_due_overlap"] == 10
    assert st.missed_entries["tranche_cap_pending_exit"] == 10
    assert st.max_positions_seen <= 30


def test_abnormal_jump_excludes_window_and_defers_held_exit() -> None:
    sessions = _sessions(100)
    candles = _candles(sessions, {"000001": np.full(100, 10_000.0)})
    _set_bar(candles, "000001", sessions[50], close=14_000.0, high=14_100.0)
    panel = sv.build_panel(candles, _flow(sessions, {}), as_of=sessions[-1])
    assert panel.coverage["candles"]["abnormal_jump_excluded"] == {
        "bars": 1,
        "symbols": 1,
    }
    assert panel.entry_excluded[30:71, 0].all()
    assert not panel.entry_excluded[29, 0]
    sim = sv.Simulator(panel, sv.CommonParams())
    ind = sv.compute_indicators(panel)
    assert not ind.flow_signal[30:71, 0].any()
    assert not ind.b_eligible[30:71, 0].any()
    blank = sv.RunState(cash=10_000_000)
    assert sim.buy(blank, 60, sv.Order(0, 59, 1_000_000, 1.0)) is None
    assert blank.missed_entries == {"abnormal_jump_window": 1}
    st = sv.RunState(cash=10_000_000)
    pos = sim.buy(st, 25, sv.Order(0, 24, 1_000_000, 1.0))
    assert pos is not None
    sim.schedule_exit(pos, 49, "test_exit")
    sim.open_phase(st, 50, [])
    assert not st.trades and st.deferred_exit_sessions == 1
    assert sim.buy(st, 60, sv.Order(0, 59, 1_000_000, 1.0)) is None
    sim.open_phase(st, 51, [])
    assert st.trades[0]["exit_date"] == sessions[51].isoformat()


def test_b_split_is_flat_started_on_midpoint() -> None:
    sessions, candles, flow = _b_fixture()
    report = sv.build_report(candles, flow, as_of=sessions[-1], lookback_days=800)
    b = report["price_factor_b"]
    full = b["runs"]["baseline"]
    first = b["split_runs"]["first_half"]["baseline"]
    second = b["split_runs"]["second_half"]["baseline"]
    assert set(b["split_runs"]["first_half"]) == set(sv.B_VARIANTS)
    assert set(b["split_runs"]["second_half"]) == set(sv.B_VARIANTS)
    assert first["sessions"] + second["sessions"] == full["sessions"]
    assert sessions.index(date.fromisoformat(first["end"])) + 1 == sessions.index(
        date.fromisoformat(second["start"])
    )
    assert (
        second["first_possible_fill"]
        == sessions[sessions.index(date.fromisoformat(second["start"])) + 1].isoformat()
    )
    assert first["initial_capital"] == second["initial_capital"]
    assert all(tr["entry_date"] > second["start"] for tr in second["trades"])
    assert report["pead"]["status"] == "inconclusive"
