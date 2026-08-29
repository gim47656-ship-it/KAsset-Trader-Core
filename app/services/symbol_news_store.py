"""Persistence seam for the symbol-news relevance lifecycle (ROB-491).

All DB writes for the get_news cache go through here: ① article/link upsert at
fetch time (set-difference by unique url — feed order is never trusted), and
② judgment apply via the token-authed ingest route (PR2). No MCP imports, no
LLM, no broker/order surface. Callers own session lifecycle and commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle, NewsArticleRelatedSymbol, NewsIngestionRun
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.symbol_news_relevance import build_relevance_hints

logger = logging.getLogger(__name__)

KR_FEED_SOURCE = "naver_item_news"
FINNHUB_COMPANY_FEED_SOURCE = "finnhub_company_news"  # us
FINNHUB_GENERAL_FEED_SOURCE = "finnhub_general_news"  # crypto (심볼 키 아님)

_DISCLOSURE_URL_QUERY_CHUNK_SIZE = 500
_DISCLOSURE_UPSERT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    summary: str | None = None
    article_content: str | None = None


@dataclass(frozen=True)
class DisclosureArticleInput:
    url: str
    title: str
    source: str
    feed_source: str
    market: str
    stock_symbol: str | None
    stock_name: str | None
    published_at: datetime | None
    keywords: list[str] | None = None


@dataclass(frozen=True)
class DisclosureUpsertCounts:
    inserted: int
    updated: int
    skipped: int


@dataclass(frozen=True)
class FeedArticleUpsertCounts:
    inserted: int
    updated: int
    skipped: int


@dataclass(frozen=True)
class StoredSymbolNews:
    article_id: int
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    relevance: dict[str, Any]
    summary: str | None = None
    # Original upstream acquisition time. ``news_articles.scraped_at`` is
    # insert-only for the URL-conflict path, so it preserves the first
    # trustworthy fetch instant instead of the time of a later failed retry.
    fetched_at: datetime | None = None


def _utcnow() -> datetime:
    # Convention in this repo: naive UTC for DB storage to avoid asyncpg DataError
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def derive_status(relationship: str, relevance: str) -> str:
    """Server-owned status rule — the judgment job never writes status itself."""
    if relationship == "unrelated" or relevance == "low":
        return "excluded"
    return "confirmed"


def _relevance_block(link: SymbolNewsRelevance) -> dict[str, Any]:
    return {
        "status": link.status,
        "relationship": link.relationship,
        "relevance": link.relevance,
        "price_relevance": link.price_relevance,
        "score": link.score,
        "reason": link.reason,
        "judged_by": link.judged_by,
        "judged_at": link.judged_at.isoformat() if link.judged_at else None,
        "hints": link.hints,
    }


async def count_feed_article_changes(
    db: AsyncSession,
    items: list[FeedArticleInput],
) -> FeedArticleUpsertCounts:
    """URL별 현재 제목·출처와 새로 확보한 원문 유무를 비교한다."""
    deduplicated = {item.url: item for item in items}
    if not deduplicated:
        return FeedArticleUpsertCounts(inserted=0, updated=0, skipped=0)

    existing_rows = await db.execute(
        select(
            NewsArticle.url,
            NewsArticle.title,
            NewsArticle.source,
            NewsArticle.article_content.is_not(None).label("has_article_content"),
        ).where(NewsArticle.url.in_(deduplicated))
    )
    existing_by_url = {
        url: (title, source, has_article_content)
        for url, title, source, has_article_content in existing_rows.all()
    }
    inserted = sum(url not in existing_by_url for url in deduplicated)
    updated = sum(
        url in existing_by_url
        and (
            existing_by_url[url][:2] != (item.title[:500], item.source)
            or (
                not existing_by_url[url][2]
                and bool((item.article_content or "").strip())
            )
        )
        for url, item in deduplicated.items()
    )
    return FeedArticleUpsertCounts(
        inserted=inserted,
        updated=updated,
        skipped=len(deduplicated) - inserted - updated,
    )


async def upsert_feed_articles(
    db: AsyncSession,
    market: str,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str,
    commit: bool = True,
) -> int:
    """URL 기준 기사 upsert와 후보 종목의 pending 관련도 링크를 함께 쓴다.

    충돌 기사는 ID와 최초 RSS 발췌를 유지하고 제목·출처를 갱신한다. 새 원문은
    기존 ``article_content``가 비어 있을 때만 채운다. 반환값은 새로 만든
    ``symbol_news_relevance`` pending 링크 수다.
    """
    if not items:
        return 0
    deduplicated = {item.url: item for item in items}
    now = _utcnow()
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "summary": item.summary,
            "article_content": item.article_content,
            "market": market,
            "feed_source": feed_source,
            "article_published_at": _naive_utc(item.published_at),
            "is_analyzed": False,
            "stock_symbol": None,
            "stock_name": None,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for item in deduplicated.values()
    ]
    insert_stmt = pg_insert(NewsArticle).values(article_values)
    excluded = insert_stmt.excluded
    await db.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=[NewsArticle.url],
            set_={
                "title": excluded.title,
                "source": excluded.source,
                "article_content": func.coalesce(
                    NewsArticle.article_content,
                    excluded.article_content,
                ),
            },
            where=(
                NewsArticle.title.is_distinct_from(excluded.title)
                | NewsArticle.source.is_distinct_from(excluded.source)
                | (
                    NewsArticle.article_content.is_(None)
                    & excluded.article_content.is_not(None)
                )
            ),
        )
    )
    urls = list(deduplicated)
    id_rows = await db.execute(
        select(NewsArticle.id, NewsArticle.url).where(NewsArticle.url.in_(urls))
    )
    url_to_id = {url: article_id for article_id, url in id_rows.all()}

    link_values = []
    for item in deduplicated.values():
        article_id = url_to_id.get(item.url)
        if (
            article_id is None
        ):  # insert race lost and url missing — skip, next call heals
            continue
        link_values.append(
            {
                "article_id": article_id,
                "market": market,
                "symbol": symbol,
                "feed_source": feed_source,
                "first_seen_at": now,
                "status": "pending",
                "hints": build_relevance_hints(
                    symbol=symbol, market=market, title=item.title
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
    new_links = 0
    if link_values:
        result = await db.execute(
            pg_insert(SymbolNewsRelevance)
            .values(link_values)
            .on_conflict_do_nothing(
                index_elements=[
                    SymbolNewsRelevance.article_id,
                    SymbolNewsRelevance.market,
                    SymbolNewsRelevance.symbol,
                ]
            )
        )
        new_links = int(result.rowcount or 0)
    if commit:
        await db.commit()
    return new_links


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> int:
    """KR 호환 래퍼 — 기존 호출부 보존용 (ROB-491)."""
    return await upsert_feed_articles(db, "kr", symbol, items, feed_source=feed_source)


async def upsert_related_symbols(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    commit: bool = True,
) -> int:
    """Single write seam for `news_article_related_symbols` (ROB-916).

    ``rows`` are pre-built dicts matching the ORM column set (see
    ``app.services.news_payload_normalizer`` row builders). Idempotent by the
    ``(article_id, market, symbol, source)`` unique constraint — re-running
    over the same articles/matcher is always a no-op for existing rows.
    Callers that batch multiple writes in one transaction (e.g. the
    news-ingestor bulk endpoint) should pass ``commit=False`` and commit once
    at the end themselves.
    """
    if not rows:
        return 0
    result = await db.execute(
        pg_insert(NewsArticleRelatedSymbol)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[
                NewsArticleRelatedSymbol.article_id,
                NewsArticleRelatedSymbol.market,
                NewsArticleRelatedSymbol.symbol,
                NewsArticleRelatedSymbol.source,
            ]
        )
    )
    if commit:
        await db.commit()
    return int(result.rowcount or 0)


async def list_pending(
    db: AsyncSession,
    market: str,
    limit: int,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Pending links oldest-first with the article fields a judge needs."""
    conditions = [
        SymbolNewsRelevance.market == market,
        SymbolNewsRelevance.status == "pending",
    ]
    if symbol:
        conditions.append(SymbolNewsRelevance.symbol == symbol)
    stmt = (
        select(NewsArticle, SymbolNewsRelevance)
        .join(SymbolNewsRelevance, SymbolNewsRelevance.article_id == NewsArticle.id)
        .where(*conditions)
        .order_by(
            SymbolNewsRelevance.first_seen_at.asc(),
            SymbolNewsRelevance.id.asc(),
        )
        .limit(limit)
    )
    rows = await db.execute(stmt)
    return [
        {
            "article_id": article.id,
            "market": link.market,
            "symbol": link.symbol,
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "published_at": (
                article.article_published_at.isoformat()
                if article.article_published_at
                else None
            ),
            "first_seen_at": link.first_seen_at.isoformat(),
            "hints": link.hints,
        }
        for article, link in rows.all()
    ]


async def apply_judgment(
    db: AsyncSession,
    *,
    article_id: int,
    market: str,
    symbol: str,
    relationship: str,
    relevance: str,
    price_relevance: str,
    score: float | None,
    reason: str,
    judged_by: str,
) -> str | None:
    """Idempotent judgment write-back. Returns new status, None if link missing.

    Status is derived server-side (``derive_status``) — the job never sets it.
    """
    link = (
        await db.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article_id,
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        return None
    now = _utcnow()
    link.relationship = relationship
    link.relevance = relevance
    link.price_relevance = price_relevance
    link.score = score
    link.reason = reason
    link.judged_by = judged_by
    link.judged_at = now
    link.updated_at = now
    link.status = derive_status(relationship, relevance)
    await db.flush()
    return link.status


async def load_symbol_news(
    db: AsyncSession,
    symbol: str,
    market: str,
    limit: int,
) -> tuple[list[StoredSymbolNews], int]:
    """Canonical read: non-excluded rows newest-first + excluded count."""
    rows = await db.execute(
        select(NewsArticle, SymbolNewsRelevance)
        .join(
            SymbolNewsRelevance,
            SymbolNewsRelevance.article_id == NewsArticle.id,
        )
        .where(
            SymbolNewsRelevance.market == market,
            SymbolNewsRelevance.symbol == symbol,
            SymbolNewsRelevance.status != "excluded",
        )
        .order_by(
            NewsArticle.article_published_at.desc().nullslast(),
            NewsArticle.id.desc(),
        )
        .limit(limit)
    )
    stored = [
        StoredSymbolNews(
            article_id=article.id,
            url=article.url,
            title=article.title,
            source=article.source,
            published_at=article.article_published_at,
            relevance=_relevance_block(link),
            summary=article.summary,
            fetched_at=article.scraped_at,
        )
        for article, link in rows.all()
    ]
    excluded_count = (
        await db.execute(
            select(func.count())
            .select_from(SymbolNewsRelevance)
            .where(
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
                SymbolNewsRelevance.status == "excluded",
            )
        )
    ).scalar_one()
    return stored, int(excluded_count)


async def upsert_disclosures(
    db: AsyncSession,
    items: list[DisclosureArticleInput],
) -> DisclosureUpsertCounts:
    """발행 법인이 특정된 공시를 URL 기준으로 멱등 저장한다.

    충돌 행은 제목·법인명을 갱신하고 최초에 비었던 상장 종목코드만 채운다.
    원천 식별용 키워드는 최초 삽입값을 보존해 후속 분석이 추가한 키워드를
    재수집으로 덮어쓰지 않는다. 같은 회차에 같은 URL이 반복되면 마지막 값을
    한 번만 쓰고 나머지는 스킵한다.
    """
    if not items:
        return DisclosureUpsertCounts(inserted=0, updated=0, skipped=0)

    deduplicated: dict[str, DisclosureArticleInput] = {}
    duplicate_count = 0
    for item in items:
        if item.url in deduplicated:
            duplicate_count += 1
        deduplicated[item.url] = item

    now = _utcnow()
    article_values = []
    normalized_by_url: dict[str, tuple[str, str | None, str | None]] = {}
    for item in deduplicated.values():
        title = item.title[:500]
        stock_name = item.stock_name[:100] if item.stock_name else None
        stock_symbol = item.stock_symbol[:20] if item.stock_symbol else None
        keywords = list(item.keywords) if item.keywords is not None else None
        article_values.append(
            {
                "url": item.url,
                "title": title,
                "source": item.source[:100],
                "article_content": None,
                "summary": None,
                "feed_source": item.feed_source[:50],
                "market": item.market[:20],
                "keywords": keywords,
                "is_analyzed": False,
                "stock_symbol": stock_symbol,
                "stock_name": stock_name,
                "article_published_at": _naive_utc(item.published_at),
                "scraped_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        normalized_by_url[item.url] = (title, stock_name, stock_symbol)

    urls = list(normalized_by_url)
    existing_by_url: dict[str, tuple[str, str | None, str | None]] = {}
    for offset in range(0, len(urls), _DISCLOSURE_URL_QUERY_CHUNK_SIZE):
        url_chunk = urls[offset : offset + _DISCLOSURE_URL_QUERY_CHUNK_SIZE]
        existing_rows = await db.execute(
            select(
                NewsArticle.url,
                NewsArticle.title,
                NewsArticle.stock_name,
                NewsArticle.stock_symbol,
            ).where(NewsArticle.url.in_(url_chunk))
        )
        existing_by_url.update(
            (url, (title, stock_name, stock_symbol))
            for url, title, stock_name, stock_symbol in existing_rows.all()
        )

    inserted = sum(url not in existing_by_url for url in urls)
    updated = sum(
        url in existing_by_url
        and (
            existing_by_url[url][:2] != normalized[:2]
            or (existing_by_url[url][2] is None and normalized[2] is not None)
        )
        for url, normalized in normalized_by_url.items()
    )
    unchanged = len(urls) - inserted - updated

    for offset in range(0, len(article_values), _DISCLOSURE_UPSERT_CHUNK_SIZE):
        article_chunk = article_values[offset : offset + _DISCLOSURE_UPSERT_CHUNK_SIZE]
        insert_stmt = pg_insert(NewsArticle).values(article_chunk)
        excluded = insert_stmt.excluded
        await db.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=[NewsArticle.url],
                set_={
                    "title": excluded.title,
                    "stock_name": excluded.stock_name,
                    "stock_symbol": func.coalesce(
                        NewsArticle.stock_symbol,
                        excluded.stock_symbol,
                    ),
                },
                where=(
                    NewsArticle.title.is_distinct_from(excluded.title)
                    | NewsArticle.stock_name.is_distinct_from(excluded.stock_name)
                    | (
                        NewsArticle.stock_symbol.is_(None)
                        & excluded.stock_symbol.is_not(None)
                    )
                ),
            )
        )
    return DisclosureUpsertCounts(
        inserted=inserted,
        updated=updated,
        skipped=duplicate_count + unchanged,
    )


async def create_news_ingestion_run(
    db: AsyncSession,
    *,
    run_uuid: str,
    started_at: datetime,
    market: str,
    feed_source: str,
) -> None:
    """수집 회차의 시작값을 현재 트랜잭션에 추가한다.

    모델에 ``running`` 상태가 없으므로 허용값인 ``success``를 임시 지정하지만,
    호출자는 같은 트랜잭션에서 :func:`finish_news_ingestion_run`으로 닫은 뒤에만
    커밋한다. 따라서 미종료 ``success`` 행은 외부에 노출되지 않는다.
    """
    db.add(
        NewsIngestionRun(
            run_uuid=run_uuid,
            market=market,
            feed_set=feed_source,
            started_at=started_at,
            finished_at=None,
            status="success",
            source_counts={feed_source: {}},
            inserted_count=0,
            skipped_count=0,
            error_message=None,
            created_at=started_at,
        )
    )
    await db.flush()


async def finish_news_ingestion_run(
    db: AsyncSession,
    *,
    run_uuid: str,
    status: str,
    finished_at: datetime,
    counts: DisclosureUpsertCounts | FeedArticleUpsertCounts,
    error_message: str | None,
    feed_source: str,
) -> None:
    """수집 회차를 허용 상태로 닫고 갱신 건수는 JSON 건수 필드에 보존한다."""
    run = (
        await db.execute(
            select(NewsIngestionRun).where(NewsIngestionRun.run_uuid == run_uuid)
        )
    ).scalar_one()
    run.finished_at = finished_at
    run.status = status
    run.source_counts = {
        feed_source: {
            "inserted": counts.inserted,
            "updated": counts.updated,
            "skipped": counts.skipped,
        }
    }
    run.inserted_count = counts.inserted
    run.skipped_count = counts.skipped
    run.error_message = error_message
