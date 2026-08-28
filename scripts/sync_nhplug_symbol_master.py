#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.jobs.nhplug_symbol_master import run_nhplug_symbol_master_sync
from app.services.nhplug_symbol_master_service import (
    build_nhplug_symbol_master_snapshot,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync the NH PLUG symbol master.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and diff the source files without writing database rows.",
    )
    mode.add_argument(
        "--parse-only",
        action="store_true",
        help="Download and parse the source files without connecting to the database.",
    )
    return parser.parse_args(argv)


async def main(*, dry_run: bool = False, parse_only: bool = False) -> int:
    setup_logging_and_sentry(service_name="nhplug-symbol-master-sync")

    async def _job() -> int:
        if parse_only:
            snapshot = await build_nhplug_symbol_master_snapshot()
            krx_count = sum(1 for market, _symbol in snapshot if market == "KRX")
            us_count = sum(1 for market, _symbol in snapshot if market == "US")
            logger.info(
                "NH PLUG symbol master parse completed: total=%d krx=%d us=%d",
                len(snapshot),
                krx_count,
                us_count,
            )
            return 0
        result = await run_nhplug_symbol_master_sync(dry_run=dry_run)
        if result.get("status") != "completed":
            logger.error("NH PLUG symbol master sync failed: %s", result)
            return 1
        logger.info(
            "NH PLUG symbol master %s completed: %s",
            "dry-run" if dry_run else "sync",
            result,
        )
        return 0

    return await run_async_job(_job, process="sync_nhplug_symbol_master")


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(
        asyncio.run(main(dry_run=args.dry_run, parse_only=args.parse_only))
    )
