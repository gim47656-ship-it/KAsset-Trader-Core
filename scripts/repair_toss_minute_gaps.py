#!/usr/bin/env python3
"""Find and refill holes in research.kr_candles_1m_toss for one KST session date.

DEFAULTS TO --dry-run: reads the stored minutes, prints the missing symbols and
minutes and the Toss calls a repair would make, and touches neither Toss nor
the database. Pass --commit only after explicit operator approval; it walks
Toss's ``before`` cursor back from each symbol's newest missing minute and
upserts only that session date's bars.

Runbook: docs/runbooks/toss-minute-gap-repair.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-date",
        type=date.fromisoformat,
        required=True,
        help="KST session date to inspect, e.g. 2026-09-30.",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols to limit the scan (default: every symbol).",
    )
    parser.add_argument(
        "--max-calls",
        type=_positive_int,
        default=None,
        help="Stop a --commit run after this many Toss calls.",
    )
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=10,
        help="How many of the largest symbol gaps to list.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Call Toss and upsert the missing bars. Default is dry-run/no writes.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.research_candles.toss_minute_gap_repair import (
        plan_session_gaps,
        repair_session_gaps,
    )
    from app.services.research_candles.toss_minute_repository import (
        TossMinuteCandleRepository,
    )
    from app.services.research_candles.toss_minute_source import (
        TossMinuteCandleSource,
    )

    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    async with AsyncSessionLocal() as session:
        repository = TossMinuteCandleRepository(session)
        plan = await plan_session_gaps(
            repository, session_date=args.session_date, symbols=symbols or None
        )
        summary: dict[str, object] = {
            "mode": "commit" if args.commit else "dry-run",
            **plan.summary(top=args.top),
        }
        if args.commit and plan.gaps:
            source = TossMinuteCandleSource.from_settings()
            try:
                outcome = await repair_session_gaps(
                    plan=plan,
                    source=source,
                    repository=repository,
                    commit=session.commit,
                    max_calls=args.max_calls,
                )
            finally:
                await source.close()
            summary["repair"] = outcome.summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
