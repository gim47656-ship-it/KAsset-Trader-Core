"""DART 공시 수집 서비스의 작업 및 수동 실행 진입점."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.disclosures.ingestion import (
    DartPartialIngestionError,
    ingest_dart_disclosures,
)

logger = logging.getLogger(__name__)


def resolve_date_range(
    *,
    from_date: date | None,
    to_date: date | None,
    recent_days: int,
    today: date | None = None,
) -> tuple[date, date]:
    """명시 범위를 우선하고, 없으면 오늘을 포함한 최근 N일을 계산한다."""
    if recent_days < 1:
        raise ValueError("recent_days must be at least 1")
    effective_today = today or date.today()
    effective_end = to_date or effective_today
    effective_start = from_date or (effective_end - timedelta(days=recent_days - 1))
    if effective_start > effective_end:
        raise ValueError("from_date must be on or before to_date")
    return effective_start, effective_end


async def run_dart_disclosure_ingestion(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    recent_days: int = 1,
    stock_symbols: Sequence[str] | None = None,
    dart_client: Any | None = None,
) -> dict[str, object]:
    """한 DART 수집 회차를 실행하고 운영자가 읽을 구조화 결과를 반환한다."""
    run_uuid = str(uuid.uuid4())
    try:
        start_date, end_date = resolve_date_range(
            from_date=from_date,
            to_date=to_date,
            recent_days=recent_days,
        )
    except ValueError as exc:
        return {
            "status": "failed",
            "run_uuid": run_uuid,
            "error": str(exc),
        }

    async with AsyncSessionLocal() as db:
        try:
            inserted, updated, skipped = await ingest_dart_disclosures(
                db,
                start_date=start_date,
                end_date=end_date,
                stock_symbols=stock_symbols,
                run_uuid=run_uuid,
                dart_client=dart_client,
                summarize_after_ingest=True,
            )
        except DartPartialIngestionError as exc:
            return {
                "status": "partial",
                "run_uuid": run_uuid,
                "from_date": start_date.isoformat(),
                "to_date": end_date.isoformat(),
                "inserted": exc.counts.inserted,
                "updated": exc.counts.updated,
                "skipped": exc.counts.skipped,
                "error": str(exc.cause),
            }
        except Exception as exc:
            logger.error("DART 수집 잡 실패: run_uuid=%s", run_uuid)
            return {
                "status": "failed",
                "run_uuid": run_uuid,
                "from_date": start_date.isoformat(),
                "to_date": end_date.isoformat(),
                "error": str(exc)[:2000],
            }

    return {
        "status": "success",
        "run_uuid": run_uuid,
        "from_date": start_date.isoformat(),
        "to_date": end_date.isoformat(),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DART 공시를 news_articles에 수집")
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--to-date", type=date.fromisoformat)
    parser.add_argument("--recent-days", type=int, default=1)
    parser.add_argument(
        "--symbol",
        dest="stock_symbols",
        action="append",
        help="목록 응답의 stock_code로 좁힐 종목코드(반복 가능)",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    result = await run_dart_disclosure_ingestion(
        from_date=args.from_date,
        to_date=args.to_date,
        recent_days=args.recent_days,
        stock_symbols=args.stock_symbols,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
