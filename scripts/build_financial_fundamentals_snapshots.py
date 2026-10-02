#!/usr/bin/env python3
"""Build financial_fundamentals_snapshots rows from DART (ROB-422 PR1, KR-only).

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without committing.
Pass --commit only after explicit operator approval. Production migration apply
(`alembic upgrade head`) and any scheduler activation remain operator-gated (spec §9).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first KR fundamentals snapshots builder (ROB-422 PR1)."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to specific 6-digit KR symbols. Repeatable.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max active DART-eligible KR common-stock symbols. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Iterate the full active DART-eligible KR common-stock universe. Exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--with-quarterly",
        dest="include_quarterly",
        action="store_true",
        help="Also build quarterly periods (annual-only by default; spec §2.3).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually write to the database (fetches from DART). Default is "
            "--dry-run: fetch-validate, no writes (still consumes DART budget). "
            "Use --estimate-only for a no-fetch projection."
        ),
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help=(
            "Print the projected DART request count and exit WITHOUT fetching "
            "(0 budget consumed). Mutually exclusive with --commit. Contrast "
            "with --dry-run, which fetches to validate and DOES consume budget."
        ),
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial fundamentals backfill (required "
            "for any --commit, since fundamentals is incremental by DART budget)."
        ),
    )
    parser.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        help=(
            "One-shot backfill: skip symbols that already have a snapshot so "
            "re-runs advance through uncollected DART-eligible common stocks. "
            "A collected symbol is never revisited in this mode; use "
            "--refresh-due for continuous collection."
        ),
    )
    parser.add_argument(
        "--refresh-due",
        dest="refresh_due",
        action="store_true",
        help=(
            "Continuous collection: re-fetch symbols missing the newest ended "
            "period (weekly recheck while unfiled), with partial rows, never "
            "collected, or older than the 30-day correction sweep. Selection "
            "is fitted to the daily DART budget (worst-case calls per symbol); "
            "with --all the budget is the only bound. Exclusive with "
            "--skip-existing and --symbol."
        ),
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.estimate_only and args.commit:
        parser.error("--estimate-only is mutually exclusive with --commit")
    if args.refresh_due and (args.skip_existing or args.symbol):
        parser.error(
            "--refresh-due is mutually exclusive with --skip-existing and --symbol"
        )
    if args.limit is None:
        args.limit = 20
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


def _print_result(result, *, estimate_only: bool) -> None:
    if estimate_only:
        print(
            f"\n--estimate-only: projected {result.projected_requests} DART "
            f"requests for {result.symbols_resolved} {result.market.upper()} "
            f"symbols; no fetch, no rows written.\n"
        )
        for warning in result.warnings:
            print(f"  - {warning}")
        return
    print(
        f"\nbuilt {result.snapshots_built} fundamentals snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}, projected DART requests "
        f"<= {result.projected_requests}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    if result.samples:
        print("samples:")
        for sample in result.samples[:10]:
            print(f"  {sample}")
    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if result.budget_exhausted:
        print(
            "\nDART BUDGET EXHAUSTED: fetch stopped early and nothing was "
            "written. Not a successful run.\n"
        )
    elif result.no_rows_collected:
        print(
            f"\nNO ROWS COLLECTED: {result.symbols_resolved} symbols were fetched "
            "but every fetch failed or came back empty; nothing was written. "
            "Not a successful run.\n"
        )
    elif result.symbols_resolved == 0:
        print("\nnothing due: no fetch, no rows written.\n")
    elif not result.committed:
        print("\n--dry-run: no rows written.\n")
    else:
        print(f"\ncommitted {result.snapshots_built} rows.\n")


async def run(args: argparse.Namespace) -> int:
    from app.jobs import financial_fundamentals_snapshots as snapshot_job
    from app.services.snapshot_commit_guard import PartialCommitBlocked

    if not args.estimate_only:
        print(
            "NOTE: --dry-run still fetches from DART and consumes the daily "
            "request budget; use --estimate-only for a no-fetch projection."
        )
    try:
        result = await snapshot_job.run_financial_fundamentals_snapshot_build(
            snapshot_job.FinancialFundamentalsSnapshotBuildRequest(
                market=args.market,
                symbols=tuple(args.symbol),
                limit=args.limit,
                all_symbols=args.all,
                include_quarterly=args.include_quarterly,
                concurrency=args.concurrency,
                commit=args.commit,
                estimate_only=args.estimate_only,
                allow_partial=args.allow_partial,
                skip_existing=args.skip_existing,
                refresh_due=args.refresh_due,
            )
        )
    except PartialCommitBlocked as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result, estimate_only=args.estimate_only)
    if result.budget_exhausted:
        return 3
    return 4 if result.no_rows_collected else 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-financial-fundamentals-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
