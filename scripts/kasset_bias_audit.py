#!/usr/bin/env python3
"""비용·체결 규약을 누적 적용해 포트폴리오 백테스트 편향을 감사한다."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.extensions.kasset.automation.candidate_ranker import (  # noqa: E402
    CandidateKey,
    CandidateMetadata,
)
from app.extensions.kasset.automation.contracts import PriceBar  # noqa: E402
from app.extensions.kasset.automation.portfolio_backtest import (  # noqa: E402
    CONSERVATIVE_COST_PROFILE,
    LIVE_MATCHED_COST_PROFILE,
    CandidateBenchmarkSeries,
    MarketKey,
    PortfolioBacktestConfig,
    PortfolioBacktestResult,
    UniverseEvidence,
    run_portfolio_backtest,
)
from app.extensions.kasset.automation.promotion_evidence import (  # noqa: E402
    PromotionEvidenceBuildError,
    load_portfolio_evidence_source,
)

_FOOTNOTE = (
    "각 Δ는 위의 모든 변경이 적용된 상태에서의 한계 효과이며, "
    "position sizing이 running equity를 따라가므로 경로 의존적입니다."
)


@dataclass(frozen=True, slots=True)
class BiasAuditRow:
    scenario: str
    result: PortfolioBacktestResult
    delta_trades: int | None
    delta_total_return: Decimal | None
    delta_excess_return: Decimal | None


def run_bias_audit(
    candidates: Sequence[CandidateMetadata],
    bars_by_candidate: Mapping[CandidateKey, Sequence[PriceBar]],
    *,
    base_config: PortfolioBacktestConfig = PortfolioBacktestConfig(),
    benchmark_bars_by_market: (Mapping[MarketKey, Sequence[PriceBar]] | None) = None,
    benchmark_bars_by_candidate: (
        Mapping[CandidateKey, CandidateBenchmarkSeries] | None
    ) = None,
    universe_evidence: UniverseEvidence = UniverseEvidence(),
) -> tuple[BiasAuditRow, ...]:
    """하나의 불변 입력 데이터에 네 누적 규약을 실행한다."""

    scenarios = (
        (
            "baseline",
            replace(
                base_config,
                kr_cost=CONSERVATIVE_COST_PROFILE["KR"],
                us_cost=CONSERVATIVE_COST_PROFILE["US"],
                entry_fill="next_open",
                slippage_mode="adverse_rate",
            ),
        ),
        (
            "cost_profile",
            replace(
                base_config,
                kr_cost=LIVE_MATCHED_COST_PROFILE["KR"],
                us_cost=LIVE_MATCHED_COST_PROFILE["US"],
                entry_fill="next_open",
                slippage_mode="adverse_rate",
            ),
        ),
        (
            "slippage_mode",
            replace(
                base_config,
                kr_cost=LIVE_MATCHED_COST_PROFILE["KR"],
                us_cost=LIVE_MATCHED_COST_PROFILE["US"],
                entry_fill="next_open",
                slippage_mode="none",
            ),
        ),
        (
            "entry_fill",
            replace(
                base_config,
                kr_cost=LIVE_MATCHED_COST_PROFILE["KR"],
                us_cost=LIVE_MATCHED_COST_PROFILE["US"],
                entry_fill="signal_close",
                slippage_mode="none",
            ),
        ),
    )
    rows: list[BiasAuditRow] = []
    previous: PortfolioBacktestResult | None = None
    for scenario, config in scenarios:
        result = run_portfolio_backtest(
            candidates,
            bars_by_candidate,
            config=config,
            benchmark_bars_by_market=benchmark_bars_by_market,
            benchmark_bars_by_candidate=benchmark_bars_by_candidate,
            universe_evidence=universe_evidence,
        )
        rows.append(
            BiasAuditRow(
                scenario=scenario,
                result=result,
                delta_trades=(
                    None
                    if previous is None
                    else result.trade_count - previous.trade_count
                ),
                delta_total_return=(
                    None
                    if previous is None
                    else result.total_return - previous.total_return
                ),
                delta_excess_return=(
                    None
                    if previous is None
                    or previous.excess_return is None
                    or result.excess_return is None
                    else result.excess_return - previous.excess_return
                ),
            )
        )
        previous = result
    return tuple(rows)


def format_bias_audit_table(rows: Sequence[BiasAuditRow]) -> str:
    """누적 감사 결과를 안정적인 가독성 표로 만든다."""

    headers = (
        "scenario",
        "trades",
        "Δtrades",
        "total_return",
        "Δ",
        "excess_return",
        "Δ",
        "max_drawdown",
        "win_rate",
        "sharpe",
        "fees_paid",
        "taxes_paid",
    )
    body = [
        (
            row.scenario,
            str(row.result.trade_count),
            _format_int_delta(row.delta_trades),
            _format_decimal(row.result.total_return),
            _format_decimal_delta(row.delta_total_return),
            _format_decimal(row.result.excess_return),
            _format_decimal_delta(row.delta_excess_return),
            _format_decimal(row.result.max_drawdown),
            _format_decimal(row.result.win_rate),
            _format_decimal(row.result.sharpe),
            _format_decimal(row.result.fees_paid),
            _format_decimal(row.result.taxes_paid),
        )
        for row in rows
    ]
    widths = tuple(
        max(len(header), *(len(row[index]) for row in body))
        for index, header in enumerate(headers)
    )
    output = [
        " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "-+-".join("-" * width for width in widths),
    ]
    output.extend(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in body
    )
    output.append(_FOOTNOTE)
    return "\n".join(output)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KAsset portfolio backtest cumulative bias audit"
    )
    parser.add_argument("--as-of", type=_aware_datetime)
    parser.add_argument("--kr-cohort-id")
    parser.add_argument("--us-cohort-id")
    return parser.parse_args(argv)


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
        rows = run_bias_audit(
            source.candidates,
            source.bars_by_candidate,
            benchmark_bars_by_market=cast(Any, source.benchmark_bars_by_market),
            benchmark_bars_by_candidate=source.benchmark_bars_by_candidate,
            universe_evidence=universe_evidence,
        )
        print(format_bias_audit_table(rows))
        return 0
    except (PromotionEvidenceBuildError, ValueError, ArithmeticError) as exc:
        print(f"bias audit failed: {exc}", file=sys.stderr)
        return 2


def _format_decimal(value: Decimal | None) -> str:
    return "n/a" if value is None else format(value, "f")


def _format_decimal_delta(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:+f}"


def _format_int_delta(value: int | None) -> str:
    return "—" if value is None else f"{value:+d}"


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of는 ISO-8601 이어야 합니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of에는 timezone이 필요합니다.")
    return parsed.astimezone(UTC)


async def main(argv: list[str] | None = None) -> int:
    return await run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
