"""Google News RSS 종목 수집 작업과 수동 실행 진입점."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.news import NewsIngestionRun
from app.models.symbol_master import SymbolMaster
from app.services.google_news_ingestion import (
    REQUEST_INTERVAL_SECONDS,
    ClientOrNone,
    GoogleNewsSymbol,
    ingest_google_news_rss,
)
from app.services.google_news_rss import (
    DEFAULT_EXCLUDED_SOURCES,
    MAX_ITEMS_PER_SYMBOL,
    market_config,
)

logger = logging.getLogger(__name__)


async def load_google_news_symbols(
    db: AsyncSession,
    *,
    market: str,
    stock_symbols: Sequence[str] | None = None,
) -> list[GoogleNewsSymbol]:
    """활성 시장 마스터에서 코드와 검색용 회사명을 읽는다."""
    config = market_config(market)
    requested = {
        value.strip().upper() for value in (stock_symbols or ()) if value.strip()
    }
    source_market = "KRX" if config.market == "kr" else "US"
    name_column = SymbolMaster.name if config.market == "kr" else SymbolMaster.name_en
    stmt = (
        select(SymbolMaster.symbol, name_column)
        .where(
            SymbolMaster.market == source_market,
            SymbolMaster.is_active.is_(True),
        )
        .order_by(SymbolMaster.symbol)
    )
    if requested:
        stmt = stmt.where(SymbolMaster.symbol.in_(requested))
    rows = (await db.execute(stmt)).all()
    found = {symbol for symbol, _name in rows}
    missing = sorted(requested - found)
    if missing:
        raise ValueError(
            f"inactive or missing {config.market} symbols: {', '.join(missing)}"
        )
    return [GoogleNewsSymbol(symbol=symbol, name=name or "") for symbol, name in rows]


async def run_google_news_rss_ingestion(
    *,
    market: str,
    stock_symbols: Sequence[str] | None = None,
    client: ClientOrNone = None,
    request_interval_seconds: float = REQUEST_INTERVAL_SECONDS,
    max_items_per_symbol: int = MAX_ITEMS_PER_SYMBOL,
    excluded_sources: Sequence[str] | None = None,
) -> dict[str, object]:
    """단일 시장 회차를 실행하고 운영자가 읽을 구조화 결과를 반환한다."""
    run_uuid = str(uuid.uuid4())
    try:
        async with AsyncSessionLocal() as db:
            symbols = await load_google_news_symbols(
                db,
                market=market,
                stock_symbols=stock_symbols,
            )
            inserted, updated, skipped = await ingest_google_news_rss(
                db,
                market=market,
                symbols=symbols,
                run_uuid=run_uuid,
                client=client,
                request_interval_seconds=request_interval_seconds,
                max_items_per_symbol=max_items_per_symbol,
                excluded_sources=(
                    DEFAULT_EXCLUDED_SOURCES
                    if excluded_sources is None
                    else excluded_sources
                ),
            )
            run = (
                await db.execute(
                    select(NewsIngestionRun).where(
                        NewsIngestionRun.run_uuid == run_uuid
                    )
                )
            ).scalar_one()
            status = run.status
            error = run.error_message
    except Exception as exc:
        logger.exception(
            "Google News RSS 잡 실패: market=%s run_uuid=%s", market, run_uuid
        )
        return {
            "status": "failed",
            "run_uuid": run_uuid,
            "market": market,
            "error": str(exc)[:2000],
        }

    result: dict[str, object] = {
        "status": status,
        "run_uuid": run_uuid,
        "market": market,
        "symbol_count": len(symbols),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }
    if error:
        result["error"] = error
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google News RSS를 news_articles에 시장별 수집"
    )
    parser.add_argument("--market", choices=("kr", "us"), required=True)
    parser.add_argument(
        "--symbol",
        dest="stock_symbols",
        action="append",
        help="시장 마스터의 종목코드로 좁힘(반복 가능)",
    )
    parser.add_argument(
        "--exclude-source",
        dest="excluded_sources",
        action="append",
        help="제외할 RSS source 이름(반복 가능, 지정 시 기본 목록 대체)",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    result = await run_google_news_rss_ingestion(
        market=args.market,
        stock_symbols=args.stock_symbols,
        excluded_sources=args.excluded_sources,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
