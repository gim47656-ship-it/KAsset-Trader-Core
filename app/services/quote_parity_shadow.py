"""비활성 KIS↔Toss 시세 비교 규칙의 순수 계산 기록.

KIS가 운영 경로에서 제거되었으므로 비교 실행기는 두 공급자 중 하나만으로
기준을 약화하지 않는다. 기존 coverage/currency/divergence 계산은 과거 보고서
해석을 위해 남기되, ``run_quote_parity_probe``는 공급자를 호출하지 않고
``disabled``/``blocked``를 반환한다.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# 2023-01 KRX cash-equity tick ladder (KOSPI/KOSDAQ). NOT the Upbit KRW ladder
# in app/services/paper_fills.py:14-27. First band whose threshold <= price wins.
_KRX_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("500000"), Decimal("1000")),
    (Decimal("200000"), Decimal("500")),
    (Decimal("50000"), Decimal("100")),
    (Decimal("20000"), Decimal("50")),
    (Decimal("5000"), Decimal("10")),
    (Decimal("2000"), Decimal("5")),
    (Decimal("0"), Decimal("1")),
)


def krx_tick_size(price: Decimal) -> Decimal:
    """KRX equity tick for ``price`` (KRW). Non-positive -> smallest tick."""
    if price <= 0:
        return Decimal("1")
    for threshold, unit in _KRX_TICK_BANDS:
        if price >= threshold:
            return unit
    return Decimal("1")  # pragma: no cover - last band threshold is 0


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Deterministic nearest-rank percentile; ``None`` for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((pct / 100.0) * len(ordered))
    idx = min(max(rank, 1), len(ordered)) - 1
    return ordered[idx]


@dataclass(frozen=True)
class CoverageReport:
    requested_count: int
    echoed_count: int
    matched: list[str]
    silent_drops: list[str]
    allowlisted_misses: list[str]
    unexpected_echoes: list[str]
    coverage_ratio: float


def classify_coverage(
    requested: Sequence[str],
    echoed_symbols: Iterable[str],
    *,
    allowlist: frozenset[str] = frozenset(),
) -> CoverageReport:
    # SAME echo-match as fetch_toss_batch_prices (invest_price_fallback.py:105-112).
    by_upper = {s.upper(): s for s in requested}
    allow_upper = {a.upper() for a in allowlist}
    echoed = [str(e) for e in echoed_symbols]
    echoed_upper = {e.upper() for e in echoed}

    matched = [by_upper[u] for u in by_upper if u in echoed_upper]
    missing = [by_upper[u] for u in by_upper if u not in echoed_upper]
    allowlisted_misses = [s for s in missing if s.upper() in allow_upper]
    silent_drops = [s for s in missing if s.upper() not in allow_upper]
    unexpected_echoes = [e for e in echoed if e.upper() not in by_upper]

    req_n = len(by_upper)
    return CoverageReport(
        requested_count=req_n,
        echoed_count=len(echoed),
        matched=matched,
        silent_drops=silent_drops,
        allowlisted_misses=allowlisted_misses,
        unexpected_echoes=unexpected_echoes,
        coverage_ratio=(len(matched) / req_n) if req_n else 1.0,
    )


_EXPECTED_CURRENCY = {"KR": "KRW", "US": "USD"}


@dataclass(frozen=True)
class CurrencyReport:
    checked_count: int
    miskeys: list[dict[str, str]]
    miskey_count: int


def check_currency(rows: Sequence[tuple[str, str, str]]) -> CurrencyReport:
    miskeys: list[dict[str, str]] = []
    checked = 0
    for symbol, market, currency in rows:
        expected = _EXPECTED_CURRENCY.get(str(market).upper())
        if expected is None:
            continue  # unknown market: not our jurisdiction, not a failure
        checked += 1
        if str(currency).upper() != expected:
            miskeys.append(
                {
                    "symbol": str(symbol),
                    "market": str(market).upper(),
                    "expected": expected,
                    "got": str(currency).upper(),
                }
            )
    return CurrencyReport(
        checked_count=checked, miskeys=miskeys, miskey_count=len(miskeys)
    )


@dataclass(frozen=True)
class DivergenceStats:
    market: str
    count: int
    median_bps: float | None
    p99_bps: float | None
    median_ticks: float | None
    p99_ticks: float | None
    worst: list[dict]


def summarize_divergence(
    pairs: Sequence[tuple[str, Decimal, Decimal]],
    *,
    market: str,
    top_n: int = 20,
) -> DivergenceStats:
    is_kr = str(market).upper() == "KR"
    rows: list[dict] = []
    for symbol, toss, kis in pairs:
        if kis <= 0:
            continue
        bps = abs(float(toss) - float(kis)) / float(kis) * 10000.0
        ticks = (
            abs(float(toss) - float(kis)) / float(krx_tick_size(kis)) if is_kr else None
        )
        rows.append(
            {
                "symbol": symbol,
                "toss": float(toss),
                "kis": float(kis),
                "bps": bps,
                "ticks": ticks,
            }
        )
    bps_vals = [r["bps"] for r in rows]
    tick_vals = [r["ticks"] for r in rows if r["ticks"] is not None]
    rows.sort(key=lambda r: r["bps"], reverse=True)
    return DivergenceStats(
        market=str(market).upper(),
        count=len(rows),
        median_bps=statistics.median(bps_vals) if bps_vals else None,
        p99_bps=_percentile(bps_vals, 99),
        median_ticks=statistics.median(tick_vals) if tick_vals else None,
        p99_ticks=_percentile(tick_vals, 99),
        worst=rows[:top_n],
    )


@dataclass(frozen=True)
class LatencyStats:
    label: str
    call_count: int
    error_count: int
    error_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    total_wall_ms: float


def summarize_latency(
    label: str,
    samples_ms: Sequence[float],
    *,
    error_count: int,
    total_wall_ms: float,
) -> LatencyStats:
    call_count = len(samples_ms) + error_count
    return LatencyStats(
        label=label,
        call_count=call_count,
        error_count=error_count,
        error_rate=(error_count / call_count) if call_count else 0.0,
        p50_ms=_percentile(samples_ms, 50),
        p95_ms=_percentile(samples_ms, 95),
        p99_ms=_percentile(samples_ms, 99),
        total_wall_ms=total_wall_ms,
    )


@dataclass(frozen=True)
class GoBars:
    coverage_min: float = 0.995
    max_silent_drops: int = 0
    kr_p99_max_ticks: float = 1.0
    us_p99_max_bps: float = 10.0
    max_currency_miskeys: int = 0
    require_toss_wall_le_kis: bool = True
    require_toss_error_rate_le_kis: bool = True


@dataclass(frozen=True)
class BarResult:
    name: str
    status: str  # "pass" | "fail" | "not_evaluable"
    detail: str


@dataclass(frozen=True)
class GoNoGoDecision:
    decision: str  # "go" | "no_go" | "blocked"
    bars: list[BarResult]


def _bar(name: str, ok: bool, detail: str) -> BarResult:
    return BarResult(name=name, status="pass" if ok else "fail", detail=detail)


def evaluate_go_no_go(
    *,
    coverage: CoverageReport,
    kr_div: DivergenceStats,
    us_div: DivergenceStats,
    currency: CurrencyReport,
    toss_latency: LatencyStats,
    kis_latency: LatencyStats,
    us_kis_live_last: bool,
    bars: GoBars = GoBars(),
) -> GoNoGoDecision:
    results: list[BarResult] = []

    results.append(
        _bar(
            "coverage",
            coverage.coverage_ratio >= bars.coverage_min,
            f"coverage_ratio={coverage.coverage_ratio:.4f} min={bars.coverage_min}",
        )
    )
    results.append(
        _bar(
            "silent_drops",
            len(coverage.silent_drops) <= bars.max_silent_drops,
            f"silent_drops={len(coverage.silent_drops)} max={bars.max_silent_drops}",
        )
    )
    kr_ok = kr_div.p99_ticks is None or kr_div.p99_ticks <= bars.kr_p99_max_ticks
    results.append(
        _bar(
            "kr_divergence",
            kr_ok,
            f"kr_p99_ticks={kr_div.p99_ticks} max={bars.kr_p99_max_ticks}",
        )
    )

    # ROB-708 precondition: US divergence is a daily-close-vs-live artifact until
    # _kis_fetch_us moves to a live-last quote. Do NOT pass/fail it — mark it
    # not_evaluable so the operator cannot mistake a blocked run for a go.
    if not us_kis_live_last:
        results.append(
            BarResult(
                name="us_divergence",
                status="not_evaluable",
                detail=(
                    "blocked on ROB-708 — KIS US layer is daily-close (period=D), "
                    "not live-last; US divergence is not a valid promotion signal"
                ),
            )
        )
    else:
        us_ok = us_div.p99_bps is None or us_div.p99_bps <= bars.us_p99_max_bps
        results.append(
            _bar(
                "us_divergence",
                us_ok,
                f"us_p99_bps={us_div.p99_bps} max={bars.us_p99_max_bps}",
            )
        )

    results.append(
        _bar(
            "currency",
            currency.miskey_count <= bars.max_currency_miskeys,
            f"miskeys={currency.miskey_count} max={bars.max_currency_miskeys}",
        )
    )
    if bars.require_toss_wall_le_kis:
        results.append(
            _bar(
                "latency_wall",
                toss_latency.total_wall_ms <= kis_latency.total_wall_ms,
                f"toss_wall_ms={toss_latency.total_wall_ms} "
                f"kis_wall_ms={kis_latency.total_wall_ms}",
            )
        )
    if bars.require_toss_error_rate_le_kis:
        results.append(
            _bar(
                "error_rate",
                toss_latency.error_rate <= kis_latency.error_rate,
                f"toss_err={toss_latency.error_rate} kis_err={kis_latency.error_rate}",
            )
        )

    if any(b.status == "not_evaluable" for b in results):
        decision = "blocked"
    elif any(b.status == "fail" for b in results):
        decision = "no_go"
    else:
        decision = "go"
    return GoNoGoDecision(decision=decision, bars=results)


async def run_quote_parity_probe(
    *,
    kr_symbols: list[str],
    us_symbols: list[str],
) -> dict[str, Any]:
    """공급자 호출 없이 비활성 역사 규칙임을 명시한다."""

    reason = (
        "provider_unsupported: KIS is non-operational; the historical "
        "KIS/Toss parity rule cannot be evaluated or weakened to Toss-only"
    )
    return {
        "status": "disabled",
        "rule": "historical_kis_toss_quote_parity",
        "reason": reason,
        "universe": {
            "kr_count": len(kr_symbols),
            "us_count": len(us_symbols),
        },
        "go_no_go": {
            "decision": "blocked",
            "bars": [
                {
                    "name": "historical_baseline",
                    "status": "not_evaluable",
                    "detail": reason,
                }
            ],
        },
    }
