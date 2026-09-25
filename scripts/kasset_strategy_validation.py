"""KR 수급 순위·장기 B 가격팩터(급락 필터) 재검증 — advisory report only.

DB-only, read-only research CLI. It reads ``public.kr_candles_1d`` (venue KRX)
and ``public.investor_flow_snapshots`` (market kr) inside one REPEATABLE READ
READ ONLY transaction and writes a single local JSON report. It never places
orders, never touches policy/ledger/strategy-promotion state, and imports no
``app.*`` module (no Settings, no provider clients).

The strategies here are independent research approximations with fixed rules.
They are not copies of any open-source original nor of the operating strategy,
and the report is not walk-forward / out-of-sample evidence.

    DATABASE_URL=postgresql+asyncpg://... \\
      python -m scripts.kasset_strategy_validation --output /tmp/kr_validation.json
    python -m scripts.kasset_strategy_validation --as-of 2026-09-25 \\
      --lookback-days 800 --output /tmp/kr_validation.json
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

KST = timezone(timedelta(hours=9))
REPORT_SCHEMA = "kasset-kr-strategy-validation/v1"
INDEX_SYMBOLS = frozenset({"KOSPI", "KOSDAQ"})
INDEX_SOURCE = "toss_index"
FLOW_SOURCE = "naver_finance"
DEFAULT_LOOKBACK_DAYS = 800

STRATEGY_IDENTITY = (
    "독립 연구 근사(고정 규칙). 오픈소스 원본 전략이나 운영 전략의 복제가 아니며, "
    "이 보고서 생성은 전략 채택·매매 설정을 바꾸지 않는다."
)


@dataclass(frozen=True)
class CommonParams:
    initial_capital: float = 10_000_000.0
    max_positions: int = 10
    buy_fee_rate: float = 0.00015
    sell_cost_rate: float = 0.00195
    slippage_rate: float = 0.001
    min_price: float = 1_000.0
    min_adv20_krw: float = 1e9
    adv_sessions: int = 20
    session_min_share_of_median: float = 0.5
    diagnostic_block_sessions: int = 63


@dataclass(frozen=True)
class FlowParams:
    ema_fast: int = 20
    ema_slow: int = 50
    min_contiguous_sessions: int = 100
    high_lookback: int = 20
    high_buffer: float = 1.05
    flow_sessions: int = 5
    min_flow_score: float = 0.05
    max_new_per_day: int = 3
    atr_sessions: int = 20
    initial_stop_atr: float = 2.0
    trail_atr: float = 3.0
    hold_sessions: int = 10
    one_year_sessions: int = 250
    one_year_required_sessions: int = 240
    recent_sessions: int = 63
    recent_required_sessions: int = 60


@dataclass(frozen=True)
class PriceFactorParams:
    mom_skip: int = 5
    mom_lookback: int = 126
    prox_lookback: int = 120
    vol_lookback: int = 60
    vol_weight: float = 0.5
    ma_sessions: int = 50
    min_contiguous_sessions: int = 186
    rebalance_every: int = 21
    keep_rank: int = 20
    entry_rank: int = 10
    breadth_threshold: float = 0.4
    start_min_share_of_max_eligible: float = 0.5
    required_sessions: int = 126


B_VARIANTS = ("baseline", "breadth_block_new_buys", "breadth_liquidate_and_block")


# --------------------------------------------------------------------------
# Panel construction (pure)
# --------------------------------------------------------------------------


@dataclass
class Panel:
    sessions: list[date]
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    value: np.ndarray
    valid: np.ndarray
    locked: np.ndarray
    flow_net: np.ndarray
    index_closes: dict[str, dict[date, float]]
    coverage: dict[str, Any]


def _as_date_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values).dt.date


def build_panel(
    candles: pd.DataFrame,
    flow: pd.DataFrame,
    *,
    as_of: date,
    params: CommonParams = CommonParams(),
) -> Panel:
    """Build a sessions x symbols panel. Invalid/duplicate bars become NaN."""

    c = candles.copy()
    c["symbol"] = c["symbol"].astype(str)
    c["d"] = _as_date_series(c["d"])
    c = c[c["d"] <= as_of]
    raw_rows = len(c)

    index_mask = c["symbol"].isin(INDEX_SYMBOLS) | (c["source"] == INDEX_SOURCE)
    index_symbols = set(c.loc[index_mask, "symbol"])
    idx_rows = c[c["symbol"].isin(index_symbols)]
    eq = c[~c["symbol"].isin(index_symbols)]

    index_closes: dict[str, dict[date, float]] = {}
    index_cov: dict[str, Any] = {}
    for sym, grp in idx_rows.groupby("symbol"):
        toss = grp[grp["source"] == INDEX_SOURCE]
        dup = toss["d"].duplicated(keep=False)
        toss = toss[~dup]
        index_closes[sym] = {
            d: float(v)
            for d, v in zip(toss["d"], toss["close"], strict=True)
            if _finite(v) and v > 0
        }
        index_cov[sym] = {
            "rows_by_source": {
                str(k): int(v) for k, v in grp["source"].value_counts().items()
            },
            "toss_index_duplicate_dates": int(dup.sum()),
        }

    dup_mask = eq.duplicated(subset=["symbol", "d"], keep=False)
    dup_rows = int(dup_mask.sum())
    dup_keys = int(eq[dup_mask].drop_duplicates(subset=["symbol", "d"]).shape[0])
    eq = eq[~dup_mask]

    num = eq[["open", "high", "low", "close", "volume", "value"]].apply(
        pd.to_numeric, errors="coerce"
    )
    finite = np.isfinite(num.to_numpy(dtype=float)).all(axis=1)
    ok = (
        finite
        & (num["open"] > 0).to_numpy()
        & (num["high"] > 0).to_numpy()
        & (num["low"] > 0).to_numpy()
        & (num["close"] > 0).to_numpy()
        & (num["volume"] > 0).to_numpy()
        & (num["high"] >= num[["open", "close", "low"]].max(axis=1)).to_numpy()
        & (num["low"] <= num[["open", "close"]].min(axis=1)).to_numpy()
    )
    invalid_rows = int((~ok).sum())
    good = pd.concat([eq[["symbol", "d"]], num], axis=1)[ok]

    counts = good.groupby("d")["symbol"].count()
    median_count = float(counts.median()) if len(counts) else 0.0
    threshold = median_count * params.session_min_share_of_median
    session_dates = sorted(d for d, n in counts.items() if n >= threshold and n > 0)
    excluded_dates = sorted(
        d for d, n in counts.items() if not (n >= threshold and n > 0)
    )
    session_set = set(session_dates)
    rows_on_excluded = int(good["d"].isin(set(excluded_dates)).sum())
    good_in = good[good["d"].isin(session_set)]
    symbols = sorted(good_in["symbol"].unique())
    symbols_only_on_excluded = sorted(set(good["symbol"]) - set(symbols))

    def wide(col: str) -> np.ndarray:
        if not symbols:
            return np.full((len(session_dates), 0), np.nan)
        return (
            good_in.pivot(index="d", columns="symbol", values=col)
            .reindex(index=session_dates, columns=symbols)
            .to_numpy(dtype=float)
        )

    o, h, lo, cl, val = (wide(k) for k in ("open", "high", "low", "close", "value"))
    valid = np.isfinite(cl)
    locked = valid & (h == lo)

    missing_in_span = 0
    for j in range(valid.shape[1]):
        idx = np.flatnonzero(valid[:, j])
        if idx.size:
            missing_in_span += int(idx[-1] - idx[0] + 1 - idx.size)

    f = flow.copy()
    f["symbol"] = f["symbol"].astype(str)
    f["d"] = _as_date_series(f["d"])
    f = f[f["d"] <= as_of]
    flow_rows_total = len(f)
    by_source = {str(k): int(v) for k, v in f["source"].value_counts().items()}
    f = f[f["source"] == FLOW_SOURCE]
    f_dup = f.duplicated(subset=["symbol", "d"], keep=False)
    f = f[~f_dup]
    fn = pd.to_numeric(f["foreign_net"], errors="coerce")
    inn = pd.to_numeric(f["institution_net"], errors="coerce")
    null_foreign = int(fn.isna().sum())
    null_inst = int(inn.isna().sum())
    f = f.assign(net=fn + inn)  # NaN if either side is null; never zero-filled
    off_session = int((~f["d"].isin(session_set)).sum())
    flow_symbols_without_candles = len(set(f["symbol"]) - set(symbols))
    if symbols and len(f):
        flow_net = (
            f[f["d"].isin(session_set)]
            .pivot(index="d", columns="symbol", values="net")
            .reindex(index=session_dates, columns=symbols)
            .to_numpy(dtype=float)
        )
    else:
        flow_net = np.full((len(session_dates), len(symbols)), np.nan)

    coverage = {
        "candles": {
            "date_range": _range(c["d"]),
            "rows_read": raw_rows,
            "index_rows_excluded": int(len(idx_rows)),
            "index_symbols_excluded": sorted(index_symbols),
            "equity_rows": int(len(eq) + dup_rows),
            "rows_by_source": {
                str(k): int(v) for k, v in eq["source"].value_counts().items()
            },
            "duplicate_symbol_date_rows_dropped": dup_rows,
            "duplicate_symbol_date_keys": dup_keys,
            "invalid_ohlcv_rows_dropped": invalid_rows,
            "valid_rows": int(len(good)),
            "sessions": len(session_dates),
            "session_range": _range(pd.Series(session_dates, dtype=object)),
            "session_rule": (
                f"유효봉 종목 수 >= 일별 유효봉 종목 수 중앙값({median_count:.0f})의 "
                f"{params.session_min_share_of_median:.0%}인 날짜만 세션"
            ),
            "dates_excluded_by_session_rule": len(excluded_dates),
            "dates_excluded_sample": [d.isoformat() for d in excluded_dates[:20]],
            "rows_on_excluded_dates": rows_on_excluded,
            "symbols_only_on_excluded_dates": len(symbols_only_on_excluded),
            "symbols": len(symbols),
            "missing_symbol_sessions_within_span": missing_in_span,
            "locked_bars_high_eq_low": int(locked.sum()),
        },
        "flow": {
            "date_range": _range(f["d"]) if len(f) else None,
            "rows_read": flow_rows_total,
            "rows_by_source": by_source,
            "source_used": FLOW_SOURCE,
            "duplicate_symbol_date_rows_dropped": int(f_dup.sum()),
            "null_foreign_net_rows": null_foreign,
            "null_institution_net_rows": null_inst,
            "rows_on_non_session_dates": off_session,
            "symbols": int(f["symbol"].nunique()),
            "symbols_without_candles": flow_symbols_without_candles,
            "symbol_sessions_with_net": int(np.isfinite(flow_net).sum()),
        },
        "index": index_cov,
    }
    return Panel(
        sessions=session_dates,
        symbols=symbols,
        open=o,
        high=h,
        low=lo,
        close=cl,
        value=val,
        valid=valid,
        locked=locked,
        flow_net=flow_net,
        index_closes=index_closes,
        coverage=coverage,
    )


# --------------------------------------------------------------------------
# Causal indicators (pure). Row t only depends on rows <= t.
# --------------------------------------------------------------------------


@dataclass
class Indicators:
    run_len: np.ndarray
    atr: np.ndarray
    flow_signal: np.ndarray
    flow_score: np.ndarray
    ma: np.ndarray
    b_eligible: np.ndarray
    b_score: np.ndarray
    b_candidate: np.ndarray
    breadth: np.ndarray
    b_eligible_count: np.ndarray


def _roll(a: np.ndarray, n: int, how: str, ddof: int = 1) -> np.ndarray:
    r = pd.DataFrame(a).rolling(n, min_periods=n)
    out = r.std(ddof=ddof) if how == "std" else getattr(r, how)()
    return out.to_numpy(dtype=float)


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(a.shape, np.nan)
    if k < a.shape[0]:
        out[k:] = a[: a.shape[0] - k]
    return out


def _run_length(valid: np.ndarray) -> np.ndarray:
    out = np.zeros(valid.shape, dtype=np.int64)
    prev = np.zeros(valid.shape[1], dtype=np.int64)
    for t in range(valid.shape[0]):
        prev = np.where(valid[t], prev + 1, 0)
        out[t] = prev
    return out


def _ema(close: np.ndarray, run_len: np.ndarray, n: int) -> np.ndarray:
    """EMA over the current contiguous valid run, seeded by the SMA of n bars."""
    sma = _roll(close, n, "mean")
    alpha = 2.0 / (n + 1)
    out = np.full(close.shape, np.nan)
    prev = np.full(close.shape[1], np.nan)
    for t in range(close.shape[0]):
        cur = np.where(
            run_len[t] == n,
            sma[t],
            np.where(run_len[t] > n, alpha * close[t] + (1 - alpha) * prev, np.nan),
        )
        out[t] = cur
        prev = cur
    return out


def _zscore_rows(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    for t in range(x.shape[0]):
        m = mask[t]
        if not m.any():
            continue
        vals = x[t, m]
        sd = vals.std(ddof=0)
        out[t, m] = 0.0 if not (sd > 0) else (vals - vals.mean()) / sd
    return out


def compute_indicators(
    panel: Panel,
    common: CommonParams = CommonParams(),
    fp: FlowParams = FlowParams(),
    bp: PriceFactorParams = PriceFactorParams(),
) -> Indicators:
    cl, hi, lo = panel.close, panel.high, panel.low
    with np.errstate(invalid="ignore", divide="ignore"):
        run_len = _run_length(panel.valid)
        prev_close = _shift(cl, 1)
        tr = np.fmax(hi - lo, np.fmax(np.abs(hi - prev_close), np.abs(lo - prev_close)))
        tr[~np.isfinite(prev_close)] = np.nan
        atr = _roll(tr, fp.atr_sessions, "mean")
        adv = _roll(panel.value, common.adv_sessions, "mean")
        base = (
            panel.valid
            & (np.nan_to_num(cl) >= common.min_price)
            & (np.nan_to_num(adv) >= common.min_adv20_krw)
        )

        ema_f = _ema(cl, run_len, fp.ema_fast)
        ema_s = _ema(cl, run_len, fp.ema_slow)
        high_prior = _roll(_shift(hi, 1), fp.high_lookback, "max")
        flow5 = _roll(_shift(panel.flow_net, 1), fp.flow_sessions, "sum")
        flow_score = flow5 * cl / (fp.flow_sessions * adv)
        flow_signal = (
            base
            & (run_len >= fp.min_contiguous_sessions)
            & (ema_f > ema_s)
            & (cl > ema_f)
            & (cl <= high_prior * fp.high_buffer)
            & np.isfinite(flow_score)
            & (flow_score >= fp.min_flow_score)
        )

        ma = _roll(cl, bp.ma_sessions, "mean")
        mom = _shift(cl, bp.mom_skip) / _shift(cl, bp.mom_lookback) - 1.0
        prox = cl / _roll(hi, bp.prox_lookback, "max")
        vol = _roll(cl / prev_close - 1.0, bp.vol_lookback, "std")
        b_eligible = (
            base
            & (run_len >= bp.min_contiguous_sessions)
            & np.isfinite(mom)
            & np.isfinite(prox)
            & np.isfinite(vol)
            & np.isfinite(ma)
        )
        b_score = (
            _zscore_rows(mom, b_eligible)
            + _zscore_rows(prox, b_eligible)
            - bp.vol_weight * _zscore_rows(vol, b_eligible)
        )
        above = b_eligible & (cl > ma)
        elig_n = b_eligible.sum(axis=1)
        breadth = np.where(
            elig_n > 0, above.sum(axis=1) / np.maximum(elig_n, 1), np.nan
        )
    return Indicators(
        run_len=run_len,
        atr=atr,
        flow_signal=flow_signal,
        flow_score=np.where(flow_signal, flow_score, np.nan),
        ma=ma,
        b_eligible=b_eligible,
        b_score=np.where(b_eligible, b_score, np.nan),
        b_candidate=above,
        breadth=breadth,
        b_eligible_count=elig_n,
    )


# --------------------------------------------------------------------------
# Portfolio accounting (pure)
# --------------------------------------------------------------------------


@dataclass
class Position:
    col: int
    qty: int
    signal_idx: int
    entry_idx: int
    entry_fill: float
    buy_fee: float
    last_close: float
    last_close_idx: int
    stop: float | None = None
    highest_close: float = 0.0
    pending_exit: str | None = None
    pending_since_idx: int | None = None
    deferred_sessions: int = 0


@dataclass
class Order:
    col: int
    signal_idx: int
    target_alloc: float
    rank_score: float
    atr_at_signal: float | None = None


@dataclass
class RunState:
    cash: float
    positions: dict[int, Position] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    nav: list[tuple[int, float]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    buy_fees: float = 0.0
    sell_costs: float = 0.0
    slippage: float = 0.0
    missed_entries: dict[str, int] = field(default_factory=dict)
    deferred_exit_sessions: int = 0
    max_positions_seen: int = 0
    min_cash_seen: float = math.inf


class Simulator:
    def __init__(self, panel: Panel, common: CommonParams) -> None:
        self.p = panel
        self.c = common

    def tradable(self, t: int, col: int) -> bool:
        return bool(self.p.valid[t, col] and not self.p.locked[t, col])

    def _miss(self, st: RunState, reason: str) -> None:
        st.missed_entries[reason] = st.missed_entries.get(reason, 0) + 1

    def buy(self, st: RunState, t: int, order: Order) -> Position | None:
        if order.col in st.positions:
            self._miss(st, "already_held")
            return None
        if len(st.positions) >= self.c.max_positions:
            self._miss(st, "position_cap")
            return None
        if not self.tradable(t, order.col):
            self._miss(st, "not_tradable_next_open")
            return None
        raw = float(self.p.open[t, order.col])
        fill = raw * (1 + self.c.slippage_rate)
        alloc = min(order.target_alloc, st.cash)
        qty = (
            int(math.floor(alloc / (fill * (1 + self.c.buy_fee_rate))))
            if alloc > 0
            else 0
        )
        if qty < 1:
            self._miss(st, "allocation_below_one_share")
            return None
        notional = qty * fill
        fee = notional * self.c.buy_fee_rate
        st.cash -= notional + fee
        if st.cash < -1e-6:
            raise AssertionError("cash went negative")
        st.buy_fees += fee
        st.slippage += qty * raw * self.c.slippage_rate
        pos = Position(
            col=order.col,
            qty=qty,
            signal_idx=order.signal_idx,
            entry_idx=t,
            entry_fill=fill,
            buy_fee=fee,
            last_close=raw,
            last_close_idx=t,
            highest_close=0.0,
        )
        st.positions[order.col] = pos
        st.max_positions_seen = max(st.max_positions_seen, len(st.positions))
        return pos

    def sell(
        self, st: RunState, t: int, pos: Position, raw: float, reason: str
    ) -> None:
        fill = raw * (1 - self.c.slippage_rate)
        proceeds = pos.qty * fill
        cost = proceeds * self.c.sell_cost_rate
        st.cash += proceeds - cost
        st.sell_costs += cost
        st.slippage += pos.qty * raw * self.c.slippage_rate
        net = proceeds - cost - (pos.qty * pos.entry_fill + pos.buy_fee)
        sessions = self.p.sessions
        st.trades.append(
            {
                "symbol": self.p.symbols[pos.col],
                "signal_date": sessions[pos.signal_idx].isoformat(),
                "entry_date": sessions[pos.entry_idx].isoformat(),
                "exit_date": sessions[t].isoformat(),
                "qty": pos.qty,
                "entry_fill": _r(pos.entry_fill, 4),
                "exit_fill": _r(fill, 4),
                "buy_fee": _r(pos.buy_fee, 2),
                "sell_cost": _r(cost, 2),
                "net_pnl": _r(net, 2),
                "net_return": _r(net / (pos.qty * pos.entry_fill + pos.buy_fee), 6),
                "holding_sessions": t - pos.entry_idx,
                "exit_reason": reason,
                "exit_deferred_sessions": pos.deferred_sessions,
                "_net": net,
            }
        )
        del st.positions[pos.col]

    def open_phase(self, st: RunState, t: int, orders: list[Order]) -> None:
        for col in sorted(st.positions):
            pos = st.positions[col]
            if pos.pending_exit is None:
                continue
            if self.tradable(t, col):
                self.sell(st, t, pos, float(self.p.open[t, col]), pos.pending_exit)
            else:
                pos.deferred_sessions += 1
                st.deferred_exit_sessions += 1
        for order in orders:
            self.buy(st, t, order)

    def stop_phase(self, st: RunState, t: int) -> None:
        for col in sorted(st.positions):
            pos = st.positions[col]
            if pos.stop is None or not self.tradable(t, col):
                continue
            o, lo = float(self.p.open[t, col]), float(self.p.low[t, col])
            if pos.entry_idx < t and o <= pos.stop:
                self.sell(st, t, pos, o, "stop_gap_open")
            elif lo <= pos.stop:
                self.sell(st, t, pos, pos.stop, "stop")

    def mark(self, st: RunState, t: int) -> float:
        for col, pos in st.positions.items():
            if self.p.valid[t, col]:
                pos.last_close = float(self.p.close[t, col])
                pos.last_close_idx = t
        nav = st.cash + sum(p.qty * p.last_close for p in st.positions.values())
        st.nav.append((t, nav))
        st.min_cash_seen = min(st.min_cash_seen, st.cash)
        return nav

    def schedule_exit(self, pos: Position, t: int, reason: str) -> None:
        if pos.pending_exit is None:
            pos.pending_exit = reason
            pos.pending_since_idx = t


def run_flow_rank(
    panel: Panel,
    ind: Indicators,
    start: int,
    end: int,
    common: CommonParams = CommonParams(),
    fp: FlowParams = FlowParams(),
) -> RunState:
    """Flat-start flow-rank run: signals at closes start..end-1, fills next open."""
    sim = Simulator(panel, common)
    st = RunState(cash=common.initial_capital)
    orders: list[Order] = []
    for t in range(start, end + 1):
        if t > start:
            sim.open_phase(st, t, orders)
            for order in orders:
                pos = st.positions.get(order.col)
                if pos is not None and pos.entry_idx == t:
                    pos.stop = pos.entry_fill - fp.initial_stop_atr * float(
                        order.atr_at_signal
                    )
            sim.stop_phase(st, t)
        orders = []
        nav = sim.mark(st, t)
        if t == end:
            break
        for col in sorted(st.positions):
            pos = st.positions[col]
            if panel.valid[t, col]:
                c = float(panel.close[t, col])
                pos.highest_close = max(pos.highest_close, c)
                a = ind.atr[t, col]
                if np.isfinite(a) and pos.stop is not None:
                    pos.stop = max(
                        pos.stop, pos.highest_close - fp.trail_atr * float(a)
                    )
            if t - pos.entry_idx + 1 >= fp.hold_sessions:
                sim.schedule_exit(pos, t, "time_exit")
        cand = np.flatnonzero(ind.flow_signal[t])
        cand = [
            j
            for j in cand
            if j not in st.positions
            and np.isfinite(ind.atr[t, j])
            and ind.atr[t, j] > 0
        ]
        cand.sort(key=lambda j: (-float(ind.flow_score[t, j]), panel.symbols[j]))
        for j in cand[: fp.max_new_per_day]:
            orders.append(
                Order(
                    col=j,
                    signal_idx=t,
                    target_alloc=nav / common.max_positions,
                    rank_score=float(ind.flow_score[t, j]),
                    atr_at_signal=float(ind.atr[t, j]),
                )
            )
            st.signals.append(
                {
                    "signal_date": panel.sessions[t].isoformat(),
                    "symbol": panel.symbols[j],
                    "score": _r(float(ind.flow_score[t, j]), 6),
                }
            )
    return st


def run_price_factor(
    panel: Panel,
    ind: Indicators,
    start: int,
    end: int,
    variant: str,
    common: CommonParams = CommonParams(),
    bp: PriceFactorParams = PriceFactorParams(),
) -> RunState:
    """B price factor. Rebalance calendar = start + k*21 sessions for every variant."""
    if variant not in B_VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    sim = Simulator(panel, common)
    st = RunState(cash=common.initial_capital)
    orders: list[Order] = []
    for t in range(start, end + 1):
        if t > start:
            sim.open_phase(st, t, orders)
        orders = []
        nav = sim.mark(st, t)
        if t == end:
            break
        breadth = float(ind.breadth[t])
        weak = np.isfinite(breadth) and breadth < bp.breadth_threshold
        if variant == "breadth_liquidate_and_block" and weak:
            for pos in st.positions.values():
                sim.schedule_exit(pos, t, "breadth_liquidation")
        for col in sorted(st.positions):
            pos = st.positions[col]
            if panel.valid[t, col] and np.isfinite(ind.ma[t, col]):
                if panel.close[t, col] < ind.ma[t, col]:
                    sim.schedule_exit(pos, t, "close_below_ma50")
        if (t - start) % bp.rebalance_every != 0:
            continue
        event: dict[str, Any] = {
            "date": panel.sessions[t].isoformat(),
            "breadth": _r(breadth, 4) if np.isfinite(breadth) else None,
            "eligible": int(ind.b_eligible_count[t]),
        }
        if not np.isfinite(breadth):
            event["action"] = "skipped_no_eligible_universe"
            st.events.append(event)
            continue
        cand = np.flatnonzero(ind.b_candidate[t])
        ranked = sorted(
            cand, key=lambda j: (-float(ind.b_score[t, j]), panel.symbols[j])
        )
        keep = set(ranked[: bp.keep_rank])
        for col in sorted(st.positions):
            if col not in keep:
                sim.schedule_exit(st.positions[col], t, "rebalance_out_of_top20")
        kept = [p for p in st.positions.values() if p.pending_exit is None]
        blocked = weak and variant != "baseline"
        new: list[int] = []
        if not blocked:
            slots = common.max_positions - len(kept)
            new = [j for j in ranked[: bp.entry_rank] if j not in st.positions][
                : max(slots, 0)
            ]
        for j in new:
            orders.append(
                Order(
                    col=j,
                    signal_idx=t,
                    target_alloc=nav / common.max_positions,
                    rank_score=float(ind.b_score[t, j]),
                )
            )
            st.signals.append(
                {
                    "signal_date": panel.sessions[t].isoformat(),
                    "symbol": panel.symbols[j],
                    "score": _r(float(ind.b_score[t, j]), 6),
                }
            )
        event.update(
            candidates=len(ranked),
            kept=len(kept),
            new_orders=len(new),
            new_buys_blocked_by_breadth=blocked,
        )
        st.events.append(event)
    return st


# --------------------------------------------------------------------------
# Metrics & report
# --------------------------------------------------------------------------


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _r(x: float | None, nd: int) -> float | None:
    return round(float(x), nd) if x is not None and _finite(x) else None


def _range(s: pd.Series) -> dict[str, str] | None:
    s = s.dropna()
    if not len(s):
        return None
    return {"from": min(s).isoformat(), "to": max(s).isoformat()}


def _mdd(values: np.ndarray) -> float:
    peak = np.maximum.accumulate(values)
    return float((values / peak - 1.0).min())


def summarize_run(
    panel: Panel,
    st: RunState,
    start: int,
    end: int,
    required_sessions: int,
    common: CommonParams = CommonParams(),
) -> dict[str, Any]:
    init = common.initial_capital
    navs = np.array([v for _, v in st.nav], dtype=float)
    series = np.concatenate([[init], navs])
    final = float(navs[-1]) if navs.size else init
    closed = st.trades
    realized = sum(tr["_net"] for tr in closed)
    open_positions = []
    unrealized = 0.0
    for col in sorted(st.positions):
        pos = st.positions[col]
        u = pos.qty * (pos.last_close - pos.entry_fill) - pos.buy_fee
        unrealized += u
        open_positions.append(
            {
                "symbol": panel.symbols[col],
                "status": "unrealized_open_at_end",
                "entry_date": panel.sessions[pos.entry_idx].isoformat(),
                "qty": pos.qty,
                "entry_fill": _r(pos.entry_fill, 4),
                "mark_close": _r(pos.last_close, 4),
                "mark_date": panel.sessions[pos.last_close_idx].isoformat(),
                "stale_mark": pos.last_close_idx != end,
                "unrealized_pnl_after_buy_fee": _r(u, 2),
                "est_exit_costs_not_deducted": _r(
                    pos.qty
                    * pos.last_close
                    * (common.slippage_rate + common.sell_cost_rate),
                    2,
                ),
                "pending_exit_reason": pos.pending_exit,
            }
        )
    diff = final - (init + realized + unrealized)
    sessions_n = end - start + 1
    reasons = []
    if sessions_n < required_sessions:
        reasons.append(
            f"period_sessions_{sessions_n}_below_required_{required_sessions}"
        )
    if not closed:
        reasons.append("no_closed_trades")
    wins = sum(1 for tr in closed if tr["_net"] > 0)
    trades_out = [{k: v for k, v in tr.items() if k != "_net"} for tr in closed]
    return {
        "status": "inconclusive" if reasons else "ok",
        "inconclusive_reasons": reasons,
        "start": panel.sessions[start].isoformat(),
        "end": panel.sessions[end].isoformat(),
        "sessions": sessions_n,
        "first_possible_fill": panel.sessions[start + 1].isoformat()
        if end > start
        else None,
        "initial_capital": init,
        "final_nav": _r(final, 2),
        "total_net_return": _r(final / init - 1.0, 6),
        "max_drawdown_incl_initial": _r(_mdd(series), 6),
        "closed_trades": len(closed),
        "win_rate": _r(wins / len(closed), 4) if closed else None,
        "mean_holding_sessions": _r(
            float(np.mean([t["holding_sessions"] for t in closed])), 2
        )
        if closed
        else None,
        "realized_net_pnl": _r(realized, 2),
        "terminal_unrealized_pnl": _r(unrealized, 2),
        "terminal_open_positions": open_positions,
        "costs": {
            "buy_fees": _r(st.buy_fees, 2),
            "sell_fees_and_tax": _r(st.sell_costs, 2),
            "slippage_embedded_in_fills": _r(st.slippage, 2),
        },
        "ledger_check": {
            "final_nav_minus_initial_realized_unrealized": _r(diff, 6),
            "reconciled": abs(diff) <= 1e-6 * init,
            "min_cash": _r(st.min_cash_seen, 2),
            "cash_never_negative": st.min_cash_seen >= -1e-6,
            "max_positions_held": st.max_positions_seen,
            "position_cap": common.max_positions,
        },
        "execution": {
            "signals": len(st.signals),
            "missed_entries": dict(sorted(st.missed_entries.items())),
            "exit_deferred_sessions_missing_or_locked": st.deferred_exit_sessions,
        },
        "subperiod_diagnostics": _blocks(panel, st, common),
        "rebalance_events": st.events or None,
        "trades": trades_out,
        "daily_nav": [[panel.sessions[t].isoformat(), _r(v, 2)] for t, v in st.nav],
    }


def _blocks(panel: Panel, st: RunState, common: CommonParams) -> list[dict[str, Any]]:
    """Contiguous 63-session slices of one continuous run (not flat-start)."""
    out = []
    n = common.diagnostic_block_sessions
    prev = common.initial_capital
    for i in range(0, len(st.nav), n):
        chunk = st.nav[i : i + n]
        vals = np.array([prev] + [v for _, v in chunk], dtype=float)
        out.append(
            {
                "start": panel.sessions[chunk[0][0]].isoformat(),
                "end": panel.sessions[chunk[-1][0]].isoformat(),
                "sessions": len(chunk),
                "partial_block": len(chunk) < n,
                "net_return": _r(vals[-1] / vals[0] - 1.0, 6),
                "max_drawdown": _r(_mdd(vals), 6),
            }
        )
        prev = chunk[-1][1]
    return out


def index_reference(panel: Panel, start: int, end: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    s_d, e_d = panel.sessions[start], panel.sessions[end]
    for sym in sorted(panel.index_closes):
        series = panel.index_closes[sym]
        in_period = [d for d in panel.sessions[start : end + 1] if d in series]
        entry: dict[str, Any] = {
            "source": INDEX_SOURCE,
            "sessions_covered": len(in_period),
            "sessions_in_period": end - start + 1,
        }
        if s_d in series and e_d in series:
            entry["price_return_close_to_close"] = _r(
                series[e_d] / series[s_d] - 1.0, 6
            )
        else:
            entry["price_return_close_to_close"] = None
            entry["missing"] = "toss_index close missing at period start or end"
        out[sym] = entry
    if not out:
        return {"status": "missing", "reason": "toss_index series not found"}
    return {
        "note": "참고용 지수 가격수익률(비용·배당 미반영). 전략과 동일 조건 비교가 아님.",
        "series": out,
    }


def flow_period(panel: Panel, common: CommonParams, fp: FlowParams) -> dict[str, Any]:
    counts = np.isfinite(panel.flow_net).sum(axis=1)
    positive = counts[counts > 0]
    if not positive.size:
        return {"status": "missing", "reason": "no naver_finance flow rows on sessions"}
    threshold = float(np.median(positive)) * common.session_min_share_of_median
    covered = counts >= max(threshold, 1)
    last = int(np.flatnonzero(covered)[-1])
    first = last
    while first > 0 and covered[first - 1]:
        first -= 1
    end = min(len(panel.sessions) - 1, last + 1)
    start = max(
        first + fp.flow_sessions,
        fp.min_contiguous_sessions - 1,
        end - fp.one_year_sessions + 1,
    )
    info = {
        "coverage_rule": (
            f"flow 보유 종목 수 >= 중앙값({np.median(positive):.0f})의 "
            f"{common.session_min_share_of_median:.0%}인 연속 세션 구간"
        ),
        "covered_run": {
            "from": panel.sessions[first].isoformat(),
            "to": panel.sessions[last].isoformat(),
            "sessions": last - first + 1,
        },
    }
    if start >= end:
        info.update(status="missing", reason="covered flow run shorter than lag+warmup")
        return info
    info.update(status="ok", start=start, end=end)
    return info


def build_report(
    candles: pd.DataFrame,
    flow: pd.DataFrame,
    *,
    as_of: date,
    lookback_days: int,
    metadata: dict[str, Any] | None = None,
    common: CommonParams = CommonParams(),
    fp: FlowParams = FlowParams(),
    bp: PriceFactorParams = PriceFactorParams(),
) -> dict[str, Any]:
    panel = build_panel(candles, flow, as_of=as_of, params=common)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "lookback_days": lookback_days,
        "metadata": metadata or {},
        "advisory": {
            "advisory_only": True,
            "mutates_orders_policy_ledger_or_strategy": False,
            "strategy_identity": STRATEGY_IDENTITY,
            "evidence_class": "in-sample research comparison; not WFO/OOS",
        },
        "params": {
            "common": asdict(common),
            "flow_rank": asdict(fp),
            "price_factor_b": asdict(bp),
        },
        "coverage": panel.coverage,
        "limitations": _limitations(panel),
    }
    if len(panel.sessions) < 2 or not panel.symbols:
        report["flow_rank"] = {
            "status": "missing",
            "reason": "no usable candle sessions",
        }
        report["price_factor_b"] = {
            "status": "missing",
            "reason": "no usable candle sessions",
        }
        return report
    ind = compute_indicators(panel, common, fp, bp)
    report["coverage"]["exclusions_by_contiguity"] = {
        "flow_symbols_never_reaching_contiguous_sessions": int(
            (ind.run_len.max(axis=0) < fp.min_contiguous_sessions).sum()
        ),
        "b_symbols_never_reaching_contiguous_sessions": int(
            (ind.run_len.max(axis=0) < bp.min_contiguous_sessions).sum()
        ),
        "valid_symbol_sessions_below_flow_contiguity": int(
            (panel.valid & (ind.run_len < fp.min_contiguous_sessions)).sum()
        ),
        "valid_symbol_sessions_below_b_contiguity": int(
            (panel.valid & (ind.run_len < bp.min_contiguous_sessions)).sum()
        ),
    }

    fper = flow_period(panel, common, fp)
    flow_out: dict[str, Any] = {"period_definition": fper, "runs": {}}
    if fper.get("status") == "ok":
        s, e = fper["start"], fper["end"]
        fper["start"] = panel.sessions[s].isoformat()
        fper["end"] = panel.sessions[e].isoformat()
        for name, ps, req in (
            ("one_year", s, fp.one_year_required_sessions),
            (
                "recent_3m",
                max(s, e - fp.recent_sessions + 1),
                fp.recent_required_sessions,
            ),
        ):
            st = run_flow_rank(panel, ind, ps, e, common, fp)
            run = summarize_run(panel, st, ps, e, req, common)
            run["index_reference"] = index_reference(panel, ps, e)
            flow_out["runs"][name] = run
        flow_out["status"] = "ok"
    else:
        flow_out["status"] = "missing"
    report["flow_rank"] = flow_out

    counts = ind.b_eligible_count
    b_out: dict[str, Any] = {
        "note": (
            "원래 B(최대보유기간 없음) 그대로 비교. mean_holding_sessions는 참고값이며 "
            "1~2개월 보유 보장이 아님. breadth 임계값 0.4 고정, 최적화 없음."
        ),
        "runs": {},
    }
    if counts.max() <= 0:
        b_out.update(status="missing", reason="no eligible B cross-section")
    else:
        bs = int(
            np.flatnonzero(counts >= counts.max() * bp.start_min_share_of_max_eligible)[
                0
            ]
        )
        be = len(panel.sessions) - 1
        b_out["period_definition"] = {
            "rule": (
                f"eligible 단면 종목 수 >= 기간 최대치의 "
                f"{bp.start_min_share_of_max_eligible:.0%}가 되는 첫 세션부터 as_of까지"
            ),
            "start": panel.sessions[bs].isoformat(),
            "end": panel.sessions[be].isoformat(),
            "max_eligible": int(counts.max()),
        }
        if be <= bs:
            b_out.update(status="missing", reason="B period has no fill session")
        else:
            for v in B_VARIANTS:
                st = run_price_factor(panel, ind, bs, be, v, common, bp)
                b_out["runs"][v] = summarize_run(
                    panel, st, bs, be, bp.required_sessions, common
                )
            base = b_out["runs"]["baseline"]
            b_out["comparison_vs_baseline"] = {
                v: {
                    "total_net_return_delta": _delta(
                        b_out["runs"][v], base, "total_net_return"
                    ),
                    "max_drawdown_delta": _delta(
                        b_out["runs"][v], base, "max_drawdown_incl_initial"
                    ),
                }
                for v in B_VARIANTS[1:]
            }
            b_out["index_reference"] = index_reference(panel, bs, be)
            b_out["status"] = "ok"
    report["price_factor_b"] = b_out
    return report


def _delta(a: dict[str, Any], b: dict[str, Any], key: str) -> float | None:
    if a.get(key) is None or b.get(key) is None:
        return None
    return _r(a[key] - b[key], 6)


def _limitations(panel: Panel) -> list[str]:
    cov = panel.coverage["candles"]
    return [
        "PIT 전체 상장 유니버스가 아니라 현재 DB에 백필된 종목만 사용 — 상장폐지·과거 편출 종목이 빠진 survivor bias가 있다.",
        "수급 발표시각(publication timestamp)이 없어 당일 수급 대신 전거래일까지 5세션만 사용했다. 실제 확정 시각이 더 늦으면 여전히 낙관적일 수 있다.",
        "일봉은 수정주가 기반일 수 있어 과거 체결가·정수주·거래대금·비용 계산이 실제와 다를 수 있다.",
        "시가 체결은 해당 봉 open에 슬리피지 0.1%를 더한 가정이며, 호가·체결 가능 수량·VI·상하한가 대기열은 반영하지 않는다. high==low 봉은 잠김으로 보고 체결하지 않는다.",
        f"세션 캘린더: {cov['session_rule']}. 이 규칙으로 날짜 {cov['dates_excluded_by_session_rule']}개(행 {cov['rows_on_excluded_dates']}개, 해당 날짜에만 있던 종목 {cov['symbols_only_on_excluded_dates']}개)가 제외됐다.",
        "지표 창은 연속 유효 세션에서만 계산한다(중간 결측이 있으면 창이 끊겨 부적격). 연속성 때문에 제외된 종목·세션 수는 coverage.exclusions_by_contiguity에 있다.",
        "기간은 확보된 데이터에서 정한 in-sample 비교이며 엄격한 walk-forward/OOS 검증이 아니다. 임계값·파라미터 최적화는 하지 않았다.",
        "매도대금은 같은 시가에 즉시 재사용 가능하다고 가정한다(결제일 미반영).",
        "기말 보유는 미실현으로 따로 표시하며 완료 거래·승률에 넣지 않는다.",
        "as_of 기본값은 KST 오늘이며 DB 일봉은 동기화 단계에서 완료봉만 저장된다는 계약을 전제로 한다.",
    ]


# --------------------------------------------------------------------------
# DB read (read-only) and CLI
# --------------------------------------------------------------------------

CANDLE_SQL = """
SELECT symbol,
       (time AT TIME ZONE 'Asia/Seoul')::date AS d,
       open::float8 AS open, high::float8 AS high, low::float8 AS low,
       close::float8 AS close, volume::float8 AS volume, value::float8 AS value,
       source
FROM public.kr_candles_1d
WHERE venue = 'KRX' AND time >= $1 AND time < $2
"""

FLOW_SQL = """
SELECT symbol, snapshot_date AS d, foreign_net, institution_net, source
FROM public.investor_flow_snapshots
WHERE market = 'kr' AND snapshot_date >= $1 AND snapshot_date <= $2
"""


async def load_from_db(
    dsn: str, as_of: date, lookback_days: int
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    import asyncpg

    start = as_of - timedelta(days=lookback_days)
    t_from = datetime.combine(start, time(0), KST)
    t_to = datetime.combine(as_of + timedelta(days=1), time(0), KST)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            meta_row = await conn.fetchrow(
                "SELECT now() AS snapshot_at, current_setting('transaction_isolation') AS iso, "
                "current_setting('transaction_read_only') AS ro"
            )
            cbuf, fbuf = io.BytesIO(), io.BytesIO()
            await conn.copy_from_query(
                CANDLE_SQL, t_from, t_to, output=cbuf, format="csv", header=True
            )
            await conn.copy_from_query(
                FLOW_SQL, start, as_of, output=fbuf, format="csv", header=True
            )
    finally:
        await conn.close()
    cbuf.seek(0)
    fbuf.seek(0)
    candles = pd.read_csv(cbuf, dtype={"symbol": str, "source": str})
    flow = pd.read_csv(fbuf, dtype={"symbol": str, "source": str})
    meta = {
        "db_snapshot_at": meta_row["snapshot_at"].isoformat(),
        "transaction_isolation": meta_row["iso"],
        "transaction_read_only": meta_row["ro"],
        "query_window": {"from": start.isoformat(), "to": as_of.isoformat()},
    }
    return candles, flow, meta


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "KR 수급 순위·B 가격팩터 급락필터 재검증 (read-only DB, advisory JSON only). "
            "DATABASE_URL 환경변수만 사용한다."
        )
    )
    ap.add_argument(
        "--as-of",
        type=_parse_date,
        default=None,
        help="평가 기준일 YYYY-MM-DD (기본: KST 오늘)",
    )
    ap.add_argument("--output", type=Path, required=True, help="보고서 JSON 경로(로컬)")
    ap.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"조회 달력일 수 (기본 {DEFAULT_LOOKBACK_DAYS})",
    )
    args = ap.parse_args(argv)
    if args.lookback_days < 200:
        ap.error("--lookback-days must be >= 200")
    return args


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, allow_nan=False)
        tmp = Path(fh.name)
    os.replace(tmp, path)


def _summary(report: dict[str, Any], path: Path) -> dict[str, Any]:
    def run_line(run: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "status",
            "start",
            "end",
            "total_net_return",
            "max_drawdown_incl_initial",
            "closed_trades",
            "win_rate",
        )
        return {k: run.get(k) for k in keys}

    return {
        "output": str(path),
        "as_of": report["as_of"],
        "flow_rank": {
            k: run_line(v) for k, v in report["flow_rank"].get("runs", {}).items()
        }
        or report["flow_rank"].get("status"),
        "price_factor_b": {
            k: run_line(v) for k, v in report["price_factor_b"].get("runs", {}).items()
        }
        or report["price_factor_b"].get("status"),
    }


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    dsn = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    as_of = args.as_of or datetime.now(KST).date()
    try:
        candles, flow, meta = await load_from_db(dsn, as_of, args.lookback_days)
    except Exception as exc:  # never echo the DSN
        print(f"database read failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    report = build_report(
        candles, flow, as_of=as_of, lookback_days=args.lookback_days, metadata=meta
    )
    write_report(args.output, report)
    print(json.dumps(_summary(report, args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
