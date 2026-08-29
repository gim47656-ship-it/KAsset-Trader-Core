"""일반 뉴스 AI 요약의 제한 backfill 작업 및 CLI 진입점."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.core.db import AsyncSessionLocal
from app.services.news_summary_service import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    NewsSummaryGenerator,
    summarize_pending_news,
)

logger = logging.getLogger(__name__)


async def run_news_summary_backfill(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    market: str | None = None,
    feed_source: str | None = None,
    generator: NewsSummaryGenerator | None = None,
) -> dict[str, object]:
    """제한된 한 batch를 실행하고 행별 성공·스킵·실패 건수를 반환한다."""

    try:
        async with AsyncSessionLocal() as db:
            result = await summarize_pending_news(
                db,
                batch_size=batch_size,
                market=market,
                feed_source=feed_source,
                generator=generator,
            )
    except Exception as exc:
        logger.exception("일반 뉴스 요약 backfill 실패")
        return {
            "status": "failed",
            "selected": 0,
            "summarized": 0,
            "skipped_existing": 0,
            "skipped_insufficient": 0,
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


def _feed_source(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("feed source must not be blank")
    return normalized


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="news_articles의 미분석 일반 뉴스를 제한 batch로 한국어 요약"
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=f"한 번에 처리할 행 수(1~{MAX_BATCH_SIZE})",
    )
    parser.add_argument(
        "--market",
        choices=("kr", "us", "crypto"),
        help="특정 시장으로 제한",
    )
    parser.add_argument(
        "--feed-source",
        type=_feed_source,
        help="특정 일반 뉴스 공급자로 제한(예: google_news)",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    result = await run_news_summary_backfill(
        batch_size=args.batch_size,
        market=args.market,
        feed_source=args.feed_source,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
