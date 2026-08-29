#!/usr/bin/env python3
"""Collect KIS KR lifecycle/corporate-action evidence for known symbols.

The default mode performs provider network reads and reports prepared evidence
without any database writes. ``--commit`` is required to persist rows or update
direct lifecycle metadata on ``kr_symbol_universe``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run-first KIS lifecycle and corporate-action evidence sync for "
            "known KR symbols."
        )
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help=(
            "Known 6-character KR symbol. Repeat for multiple symbols. Without "
            "this option, current active/common KR universe rows are selected."
        ),
    )
    parser.add_argument(
        "--from-date",
        type=_iso_date,
        required=True,
        help="Corporate-action history start date (YYYY-MM-DD, inclusive).",
    )
    parser.add_argument(
        "--to-date",
        type=_iso_date,
        required=True,
        help="Corporate-action history end date (YYYY-MM-DD, inclusive).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum symbols after deterministic ordering. The implicit active "
            "universe defaults to 20; explicit --symbol values are unlimited."
        ),
    )
    parser.add_argument(
        "--resume-after",
        default=None,
        help="Process symbols lexically after this known 6-character KR symbol.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist evidence and direct lifecycle metadata; default is dry-run.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.from_date > args.to_date:
        parser.error("--from-date must be on or before --to-date")
    if args.limit is None and not args.symbol:
        args.limit = 20
    return args


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kis.client import KISClient
    from app.services.kr_lifecycle_action_service import (
        run_kr_lifecycle_action_sync,
        select_kr_symbols,
    )

    async with AsyncSessionLocal() as selection_db:
        symbols = await select_kr_symbols(
            selection_db,
            explicit_symbols=args.symbol,
            limit=args.limit,
            resume_after=args.resume_after,
        )

    client = KISClient()
    try:
        report = await run_kr_lifecycle_action_sync(
            client=client,
            session_factory=AsyncSessionLocal,
            symbols=symbols,
            from_date=args.from_date,
            to_date=args.to_date,
            commit=args.commit,
        )
    finally:
        await client.close()

    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if report.failures else 0


async def main(argv: list[str] | None = None) -> int:
    from app.core.cli import setup_logging_and_sentry

    args = parse_args(argv)
    setup_logging_and_sentry(service_name="sync-kr-lifecycle-actions")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
