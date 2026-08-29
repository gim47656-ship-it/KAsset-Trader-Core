"""공시 AI 요약의 수동 backfill 작업 및 CLI 진입점."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.core.db import AsyncSessionLocal
from app.services.disclosures.summary_service import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    DisclosureBodyFetcher,
    DisclosureSummaryGenerator,
    summarize_pending_disclosures,
)

logger = logging.getLogger(__name__)


async def run_disclosure_summary_backfill(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    feed_source: str | None = None,
    fetcher: DisclosureBodyFetcher | None = None,
    generator: DisclosureSummaryGenerator | None = None,
) -> dict[str, object]:
    """제한된 한 batch를 실행하고 행별 성공/실패 건수를 반환한다."""
    try:
        async with AsyncSessionLocal() as db:
            result = await summarize_pending_disclosures(
                db,
                batch_size=batch_size,
                feed_source=feed_source,
                fetcher=fetcher,
                generator=generator,
            )
    except Exception as exc:
        logger.exception("공시 요약 backfill 실패")
        return {
            "status": "failed",
            "selected": 0,
            "summarized": 0,
            "skipped_existing": 0,
            "failed": 0,
            "error": str(exc)[:2000],
        }
    return result.to_dict()


def _batch_size(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(
            f"batch size must be between 1 and {MAX_BATCH_SIZE}"
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="news_articles의 SEC/DART 미요약 공시를 제한 batch로 요약"
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=f"한 번에 처리할 행 수(1~{MAX_BATCH_SIZE})",
    )
    parser.add_argument(
        "--feed-source",
        choices=("dart", "sec"),
        help="특정 공시 공급자로 제한",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    result = await run_disclosure_summary_backfill(
        batch_size=args.batch_size,
        feed_source=args.feed_source,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
