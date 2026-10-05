#!/usr/bin/env python3
"""진입을 고정하고 사전 고정한 청산 변형을 비교한다 (주문·DB 쓰기 없음)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.extensions.kasset.automation.exit_rule_comparison import (  # noqa: E402
    PRESET_EXIT_VARIANTS,
    ExitRuleComparisonResult,
    run_exit_rule_comparison,
)
from app.extensions.kasset.automation.portfolio_backtest import (  # noqa: E402
    CONSERVATIVE_COST_PROFILE,
    LIVE_MATCHED_COST_PROFILE,
    BacktestWindow,
    PortfolioBacktestConfig,
    UniverseEvidence,
)
from app.extensions.kasset.automation.promotion_evidence import (  # noqa: E402
    PromotionEvidenceBuildError,
    load_portfolio_evidence_source,
)

#: 보수적 수수료·슬리피지에 실제 KRX 매도세를 더한다.
COMPARISON_CONFIG = PortfolioBacktestConfig(
    kr_cost=replace(
        CONSERVATIVE_COST_PROFILE["KR"],
        sell_tax_rate=LIVE_MATCHED_COST_PROFILE["KR"].sell_tax_rate,
    ),
)

_FOOTNOTE = (
    "진입은 기준 설정(current) 실행에서 고정했고, 모든 변형에서 끝까지 청산된 "
    "진입만 집계합니다. 포트폴리오 수익률이 아니라 진입당 청산 효과입니다."
)


def format_comparison_table(result: ExitRuleComparisonResult) -> str:
    output = [
        f"end_at={result.end_at.isoformat()} "
        f"baseline_hash={result.baseline_determinism_hash or 'n/a'}",
        f"fixed_entries={result.fixed_entry_count} "
        f"compared={result.compared_entry_count} "
        f"excluded={result.fixed_entry_count - result.compared_entry_count}",
        "",
        "| variant | n | total_net | mean_net | mean_ret | mean_atr | win | "
        "hold | limit_down | vol_cap | exits |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for summary in result.summaries:
        exits = ", ".join(f"{reason}={count}" for reason, count in summary.exit_reasons)
        output.append(
            f"| {summary.name} | {summary.entry_count} "
            f"| {_fmt(summary.total_net_pnl)} | {_fmt(summary.mean_net_pnl)} "
            f"| {_fmt(summary.mean_return_on_notional)} "
            f"| {_fmt(summary.mean_net_pnl_per_atr)} | {_fmt(summary.win_rate)} "
            f"| {_fmt(summary.mean_holding_bars)} | {summary.limit_down_deferrals} "
            f"| {summary.volume_capped_bars} | {exits} |"
        )
    excluded_reasons: dict[str, int] = {}
    for item in result.excluded:
        label = f"{item.variant}:{item.reason}"
        excluded_reasons[label] = excluded_reasons.get(label, 0) + 1
    if excluded_reasons:
        excluded_text = ", ".join(
            f"{label}={count}" for label, count in sorted(excluded_reasons.items())
        )
        output.append("")
        output.append(f"excluded: {excluded_text}")
    output.append("")
    output.append(_FOOTNOTE)
    return "\n".join(output)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KAsset fixed-entry exit-rule comparison (read-only)"
    )
    parser.add_argument("--as-of", type=_aware_datetime)
    parser.add_argument("--kr-cohort-id")
    parser.add_argument("--us-cohort-id")
    parser.add_argument(
        "--variants",
        type=_variant_names,
        default=tuple(PRESET_EXIT_VARIANTS),
        help="comma-separated preset names; default: all presets",
    )
    parser.add_argument("--signal-start-at", type=_aware_datetime)
    parser.add_argument("--end-at", type=_aware_datetime)
    args = parser.parse_args(argv)
    if (args.signal_start_at is None) != (args.end_at is None):
        parser.error("--signal-start-at and --end-at must be given together")
    return args


async def run(args: argparse.Namespace) -> int:
    cohort_ids = {
        market: cohort_id
        for market, cohort_id in (
            ("kr", args.kr_cohort_id),
            ("us", args.us_cohort_id),
        )
        if cohort_id is not None
    }
    try:
        window = (
            BacktestWindow(signal_start_at=args.signal_start_at, end_at=args.end_at)
            if args.signal_start_at is not None
            else None
        )
        async with AsyncSessionLocal() as db:
            source = await load_portfolio_evidence_source(
                db,
                as_of=args.as_of,
                cohort_ids=cohort_ids,
            )
        universe_evidence = UniverseEvidence(
            source="durable_research_cohort",
            point_in_time_membership=all(
                item.point_in_time_available for item in source.readiness.markets
            ),
            includes_delisted=all(
                item.includes_delisted for item in source.readiness.markets
            ),
            as_of=source.as_of,
            notes=(
                "Immutable cohort membership, member rank, and effective date verified",
            ),
        )
        result = run_exit_rule_comparison(
            source.candidates,
            source.bars_by_candidate,
            variants={name: PRESET_EXIT_VARIANTS[name] for name in args.variants},
            config=COMPARISON_CONFIG,
            benchmark_bars_by_market=cast(Any, source.benchmark_bars_by_market),
            benchmark_bars_by_candidate=source.benchmark_bars_by_candidate,
            universe_evidence=universe_evidence,
            window=window,
        )
        print(format_comparison_table(result))
        return 0
    except (PromotionEvidenceBuildError, ValueError, ArithmeticError) as exc:
        print(f"exit-rule comparison failed: {exc}", file=sys.stderr)
        return 2


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _variant_names(value: str) -> Sequence[str]:
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    unknown = [name for name in names if name not in PRESET_EXIT_VARIANTS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(
            f"unknown variants {unknown!r}; presets: {', '.join(PRESET_EXIT_VARIANTS)}"
        )
    return names


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 ISO-8601 이어야 합니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("날짜에는 timezone이 필요합니다.")
    return parsed.astimezone(UTC)


async def main(argv: list[str] | None = None) -> int:
    return await run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
