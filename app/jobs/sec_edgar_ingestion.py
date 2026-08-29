"""SEC EDGAR 공시 수집 서비스의 작업 및 수동 실행 진입점."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.symbol_master import SymbolMaster
from app.services.disclosures.sec_edgar import (
    CompanyTickerCache,
    SecEdgarHttpClient,
    SecRateLimiter,
    company_ticker_cache,
)
from app.services.disclosures.sec_ingestion import ingest_sec_edgar

logger = logging.getLogger(__name__)


async def load_sec_symbols(
    db: AsyncSession,
    *,
    stock_symbols: Sequence[str] | None = None,
) -> list[str]:
    """활성 US 심볼 마스터에서 SEC 수집 대상을 읽는다."""
    requested = {
        value.strip().upper() for value in (stock_symbols or ()) if value.strip()
    }
    stmt = (
        select(SymbolMaster.symbol)
        .where(
            SymbolMaster.market == "US",
            SymbolMaster.is_active.is_(True),
        )
        .order_by(SymbolMaster.symbol)
    )
    if requested:
        stmt = stmt.where(SymbolMaster.symbol.in_(requested))
    symbols = list((await db.scalars(stmt)).all())
    missing = sorted(requested - set(symbols))
    if missing:
        raise ValueError(f"inactive or missing us symbols: {', '.join(missing)}")
    return symbols


async def run_sec_edgar_ingestion(
    *,
    since_date: date,
    stock_symbols: Sequence[str] | None = None,
    http_client: SecEdgarHttpClient | None = None,
    user_agent: str | None = None,
    rate_limiter: SecRateLimiter | None = None,
    ticker_cache: CompanyTickerCache = company_ticker_cache,
) -> dict[str, object]:
    """한 SEC 수집 회차를 실행하고 운영자가 읽을 구조화 결과를 반환한다."""
    run_uuid = str(uuid.uuid4())
    try:
        async with AsyncSessionLocal() as db:
            symbols = await load_sec_symbols(db, stock_symbols=stock_symbols)
            result = await ingest_sec_edgar(
                db,
                symbols=symbols,
                since_date=since_date,
                run_uuid=run_uuid,
                http_client=http_client,
                user_agent=user_agent,
                rate_limiter=rate_limiter,
                ticker_cache=ticker_cache,
                summarize_after_ingest=True,
            )
    except Exception as exc:
        logger.exception("SEC EDGAR 잡 실패: run_uuid=%s", run_uuid)
        return {
            "status": "failed",
            "run_uuid": run_uuid,
            "since_date": since_date.isoformat(),
            "error": str(exc)[:2000],
        }

    response: dict[str, object] = {
        "status": result.status,
        "run_uuid": result.run_uuid,
        "since_date": since_date.isoformat(),
        "symbol_count": len(symbols),
        "successful_symbols": result.successful_symbols,
        "inserted": result.inserted,
        "updated": result.updated,
        "skipped": result.skipped,
        "skipped_symbols": [
            {"symbol": issue.symbol, "reason": issue.reason}
            for issue in result.skipped_symbols
        ],
        "failed_symbols": [
            {"symbol": issue.symbol, "reason": issue.reason}
            for issue in result.failed_symbols
        ],
        "form_counts": dict(sorted(result.form_counts.items())),
    }
    return response


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SEC EDGAR 공시를 news_articles에 수집"
    )
    parser.add_argument(
        "--since-date",
        type=date.fromisoformat,
        required=True,
        help="이 filingDate부터 최신 공시까지 수집(YYYY-MM-DD)",
    )
    parser.add_argument(
        "--symbol",
        dest="stock_symbols",
        action="append",
        help="US 심볼 마스터의 종목코드로 좁힘(반복 가능)",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    result = await run_sec_edgar_ingestion(
        since_date=args.since_date,
        stock_symbols=args.stock_symbols,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
