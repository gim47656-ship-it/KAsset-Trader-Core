"""Google News RSS를 시장별 후보 종목과 함께 통합 뉴스 저장소에 적재한다."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import symbol_news_store
from app.services.google_news_rss import (
    DEFAULT_EXCLUDED_SOURCES,
    GOOGLE_NEWS_FEED_SOURCE,
    MAX_ITEMS_PER_SYMBOL,
    GoogleNewsHttpClient,
    GoogleNewsRssError,
    build_symbol_query,
    fetch_google_news_rss,
    market_config,
)
from app.services.news_summary_service import summarize_ingested_news
from app.services.symbol_news_store import (
    FeedArticleInput,
    FeedArticleUpsertCounts,
)

logger = logging.getLogger(__name__)

REQUEST_INTERVAL_SECONDS = 1.0
_HTTP_TIMEOUT_SECONDS = 15.0
_ZERO_COUNTS = FeedArticleUpsertCounts(inserted=0, updated=0, skipped=0)


@dataclass(frozen=True)
class GoogleNewsSymbol:
    symbol: str
    name: str


@dataclass(frozen=True)
class _CollectedFeeds:
    articles_by_url: dict[str, FeedArticleInput]
    urls_by_symbol: dict[str, set[str]]
    duplicate_count: int
    truncated_count: int
    excluded_count: int
    successful_symbols: int
    errors: tuple[str, ...]


type ClientOrNone = GoogleNewsHttpClient | None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


async def _collect_feeds(
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    client: GoogleNewsHttpClient,
    request_interval_seconds: float,
    max_items_per_symbol: int,
    excluded_sources: Collection[str],
) -> _CollectedFeeds:
    articles_by_url: dict[str, FeedArticleInput] = {}
    urls_by_symbol: dict[str, set[str]] = {}
    duplicate_count = 0
    truncated_count = 0
    excluded_count = 0
    successful_symbols = 0
    errors: list[str] = []

    for index, candidate in enumerate(symbols):
        if index > 0 and request_interval_seconds > 0:
            await asyncio.sleep(request_interval_seconds)
        try:
            query = build_symbol_query(market=market, name=candidate.name)
            feed = await fetch_google_news_rss(
                market=market,
                query=query,
                client=client,
                max_items=max_items_per_symbol,
                excluded_sources=excluded_sources,
            )
        except (GoogleNewsRssError, httpx.HTTPError, ValueError) as exc:
            errors.append(f"{candidate.symbol}: {exc}")
            logger.warning(
                "Google News RSS 종목 수집 실패: market=%s symbol=%s error=%s",
                market,
                candidate.symbol,
                exc,
            )
            continue

        successful_symbols += 1
        truncated_count += feed.truncated_count
        excluded_count += feed.excluded_count
        symbol_urls = urls_by_symbol.setdefault(candidate.symbol, set())
        for item in feed.items:
            symbol_urls.add(item.url)
            if item.url in articles_by_url:
                duplicate_count += 1
                continue
            articles_by_url[item.url] = item

    return _CollectedFeeds(
        articles_by_url=articles_by_url,
        urls_by_symbol=urls_by_symbol,
        duplicate_count=duplicate_count,
        truncated_count=truncated_count,
        excluded_count=excluded_count,
        successful_symbols=successful_symbols,
        errors=tuple(errors),
    )


async def _collect_with_client(
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    client: ClientOrNone,
    request_interval_seconds: float,
    max_items_per_symbol: int,
    excluded_sources: Collection[str],
) -> _CollectedFeeds:
    if client is not None:
        return await _collect_feeds(
            market=market,
            symbols=symbols,
            client=client,
            request_interval_seconds=request_interval_seconds,
            max_items_per_symbol=max_items_per_symbol,
            excluded_sources=excluded_sources,
        )
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as owned_client:
        return await _collect_feeds(
            market=market,
            symbols=symbols,
            client=owned_client,
            request_interval_seconds=request_interval_seconds,
            max_items_per_symbol=max_items_per_symbol,
            excluded_sources=excluded_sources,
        )


def _run_status(*, symbol_count: int, collected: _CollectedFeeds) -> str:
    if not collected.errors:
        return "success"
    if collected.successful_symbols > 0 or symbol_count == 0:
        return "partial"
    return "failed"


def _error_message(errors: tuple[str, ...]) -> str | None:
    if not errors:
        return None
    return "; ".join(errors)[:2000]


async def _summarize_after_ingest(
    db: AsyncSession,
    article_urls: Collection[str],
) -> None:
    try:
        result = await summarize_ingested_news(db, tuple(article_urls))
    except Exception:
        await db.rollback()
        logger.exception("Google News RSS 자동 요약 batch 실패")
        return
    logger.info(
        "Google News RSS 자동 요약: status=%s selected=%d summarized=%d failed=%d",
        result.status,
        result.selected,
        result.summarized,
        result.failed,
    )


async def _record_failed_run(
    db: AsyncSession,
    *,
    run_uuid: str,
    market: str,
    started_at: datetime,
    error: BaseException,
) -> None:
    await db.rollback()
    raw_message = str(error).strip()
    message = (raw_message or type(error).__name__)[:2000]
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=market,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status="failed",
        finished_at=_utcnow(),
        counts=_ZERO_COUNTS,
        error_message=message,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await db.commit()


async def _persist_collected(
    db: AsyncSession,
    *,
    run_uuid: str,
    market: str,
    started_at: datetime,
    symbol_count: int,
    collected: _CollectedFeeds,
) -> FeedArticleUpsertCounts:
    unique_items = list(collected.articles_by_url.values())
    changed = await symbol_news_store.count_feed_article_changes(db, unique_items)
    counts = FeedArticleUpsertCounts(
        inserted=changed.inserted,
        updated=changed.updated,
        skipped=(
            changed.skipped
            + collected.duplicate_count
            + collected.truncated_count
            + collected.excluded_count
        ),
    )
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=market,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    for symbol, urls in collected.urls_by_symbol.items():
        items = [collected.articles_by_url[url] for url in urls]
        await symbol_news_store.upsert_feed_articles(
            db,
            market,
            symbol,
            items,
            feed_source=GOOGLE_NEWS_FEED_SOURCE,
            commit=False,
        )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status=_run_status(symbol_count=symbol_count, collected=collected),
        finished_at=_utcnow(),
        counts=counts,
        error_message=_error_message(collected.errors),
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await db.commit()
    return counts


async def ingest_google_news_rss(
    db: AsyncSession,
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    run_uuid: str | None = None,
    client: ClientOrNone = None,
    request_interval_seconds: float = REQUEST_INTERVAL_SECONDS,
    max_items_per_symbol: int = MAX_ITEMS_PER_SYMBOL,
    excluded_sources: Collection[str] = DEFAULT_EXCLUDED_SOURCES,
) -> tuple[int, int, int]:
    """한 시장의 종목 피드를 직렬 수집하고 ``(신규, 갱신, 스킵)``을 반환한다.

    검색 종목은 확정 연관 종목이 아니라 관련도 판정 전 후보로만 저장한다.
    HTTP·형식 오류는 종목 단위로 격리하며 성공 종목이 하나라도 있으면 회차는
    ``partial``, 전부 실패하면 ``failed``로 닫는다.
    """
    config = market_config(market)
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must not be negative")
    if max_items_per_symbol < 1:
        raise ValueError("max_items_per_symbol must be at least 1")

    current_run_uuid = run_uuid or str(uuid.uuid4())
    started_at = _utcnow()
    try:
        collected = await _collect_with_client(
            market=config.market,
            symbols=symbols,
            client=client,
            request_interval_seconds=request_interval_seconds,
            excluded_sources=excluded_sources,
            max_items_per_symbol=max_items_per_symbol,
        )
        counts = await _persist_collected(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            symbol_count=len(symbols),
            collected=collected,
        )
    except asyncio.CancelledError as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            error=exc,
        )
        raise
    except Exception as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            error=exc,
        )
        logger.exception(
            "Google News RSS 회차 실패: market=%s run_uuid=%s",
            config.market,
            current_run_uuid,
        )
        raise
    if collected.articles_by_url:
        await _summarize_after_ingest(db, collected.articles_by_url)
    return counts.inserted, counts.updated, counts.skipped
