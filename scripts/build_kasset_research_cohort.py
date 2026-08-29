#!/usr/bin/env python3
"""Build a current-forward KAsset PAPER research cohort.

Defaults to dry-run. The current universe eligibility join means the result is
not historical point-in-time membership evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first current-forward KAsset research cohort builder."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument(
        "--valuation-source",
        required=True,
        help=(
            "Exact market_valuation_snapshots source, for example "
            "naver_finance (KR) or yahoo (US)."
        ),
    )
    parser.add_argument(
        "--size",
        type=int,
        default=100,
        help="Top market-cap core size (default: 100).",
    )
    parser.add_argument(
        "--force-symbol",
        action="append",
        default=[],
        help=(
            "Eligible positive-valuation symbol to add without displacing the "
            "top-ranked core. Repeatable."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist the immutable cohort; default is dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.size < 1:
        parser.error("--size must be >= 1")
    return args


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.kasset_research_cohort_service import (
        build_kasset_research_cohort,
    )

    async with AsyncSessionLocal() as db:
        if args.commit:
            async with db.begin():
                result = await build_kasset_research_cohort(
                    db,
                    market=args.market,
                    valuation_source=args.valuation_source,
                    requested_size=args.size,
                    forced_symbols=tuple(args.force_symbol),
                    commit=True,
                )
        else:
            result = await build_kasset_research_cohort(
                db,
                market=args.market,
                valuation_source=args.valuation_source,
                requested_size=args.size,
                forced_symbols=tuple(args.force_symbol),
                commit=False,
            )

    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


async def main(argv: list[str] | None = None) -> int:
    from app.core.cli import setup_logging_and_sentry

    args = parse_args(argv)
    setup_logging_and_sentry(service_name="build-kasset-research-cohort")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
