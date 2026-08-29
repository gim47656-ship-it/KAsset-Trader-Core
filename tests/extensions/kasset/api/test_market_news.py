"""Android 뉴스·공시 목록의 필터, 시간, keyset 와이어 계약."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.models.symbol_news_relevance import SymbolNewsRelevance


@dataclass(frozen=True, slots=True)
class _NewsSeed:
    symbol: str
    same_time_titles: frozenset[str]
    news_titles: frozenset[str]
    disclosure_titles: frozenset[str]
    null_title: str
    kst_title: str

    @property
    def all_titles(self) -> frozenset[str]:
        return self.news_titles | self.disclosure_titles


def _article(
    *,
    prefix: str,
    slug: str,
    title: str,
    symbol: str | None,
    market: str = "kr",
    feed_source: str | None,
    published_at: datetime | None,
    source: str | None,
    summary: str | None = None,
) -> NewsArticle:
    stored_at = datetime(2026, 8, 29, 2, 0)
    return NewsArticle(
        url=f"https://news.test.invalid/{prefix}/{slug}",
        title=title,
        source=source,
        article_content=None,
        summary=summary,
        feed_source=feed_source,
        market=market,
        keywords=None,
        is_analyzed=False,
        stock_symbol=symbol,
        stock_name="테스트종목",
        article_published_at=published_at,
        scraped_at=stored_at,
        user_id=None,
        created_at=stored_at,
        updated_at=None,
    )


def _analysis(
    article_id: int,
    *,
    summary: str,
    created_at: datetime,
) -> NewsAnalysisResult:
    return NewsAnalysisResult(
        article_id=article_id,
        model_name="test-news-summary",
        sentiment=Sentiment.NEUTRAL,
        sentiment_score=None,
        summary=summary,
        key_points=[],
        topics=None,
        price_impact=None,
        price_impact_score=None,
        confidence=90,
        analysis_quality="high",
        prompt="test prompt",
        raw_response="{}",
        processing_time_ms=1,
        created_at=created_at,
        updated_at=None,
    )


@pytest_asyncio.fixture
async def market_news_client(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[httpx.AsyncClient, _NewsSeed]]:
    prefix = uuid4().hex
    symbol = f"{int(prefix[:8], 16) % 1_000_000:06d}"
    other_symbol = f"{(int(symbol) + 1) % 1_000_000:06d}"
    published_at = datetime(2026, 8, 29, 0, 0)
    pending_candidate = _article(
        prefix=prefix,
        slug="pending-candidate",
        title="후보 뉴스",
        symbol=None,
        feed_source="google_news",
        published_at=datetime(2026, 8, 28, 10, 0),
        source="Google News",
    )
    excluded_candidate = _article(
        prefix=prefix,
        slug="excluded-candidate",
        title="제외 후보 뉴스",
        symbol=None,
        feed_source="google_news",
        published_at=datetime(2026, 8, 28, 11, 0),
        source="Google News",
    )
    rows = [
        _article(
            prefix=prefix,
            slug="same-disclosure",
            title="동시각 공시",
            symbol=symbol,
            feed_source="dart",
            published_at=published_at,
            source="DART",
            summary="검증된 공시 요약",
        ),
        _article(
            prefix=prefix,
            slug="same-news",
            title="동시각 뉴스",
            symbol=symbol,
            feed_source="google_news_rss",
            published_at=published_at,
            source="연합뉴스",
            summary="뉴스 요약",
        ),
        _article(
            prefix=prefix,
            slug="older-news",
            title="이전 뉴스",
            symbol=symbol,
            feed_source=None,
            published_at=datetime(2026, 8, 28, 9, 30),
            source=None,
        ),
        _article(
            prefix=prefix,
            slug="null-disclosure",
            title="시각 없는 공시",
            symbol=symbol,
            feed_source="dart",
            published_at=None,
            source="DART",
        ),
        _article(
            prefix=prefix,
            slug="other-symbol",
            title="다른 종목 뉴스",
            symbol=other_symbol,
            feed_source="google_news_rss",
            published_at=published_at,
            source="연합뉴스",
        ),
        _article(
            prefix=prefix,
            slug="other-market",
            title="다른 시장 뉴스",
            symbol=symbol,
            market="us",
            feed_source="finnhub_company_news",
            published_at=published_at,
            source="Finnhub",
        ),
        pending_candidate,
        excluded_candidate,
    ]
    db_session.add_all(rows)
    await db_session.flush()
    same_news = next(row for row in rows if row.title == "동시각 뉴스")
    db_session.add_all(
        [
            _analysis(
                same_news.id,
                summary="오래된 한국어 AI 요약이다. 이전 분석 결과다.",
                created_at=datetime(2026, 8, 29, 2, 1),
            ),
            _analysis(
                same_news.id,
                summary="최신 한국어 AI 요약이다. 영속 분석 결과를 사용한다.",
                created_at=datetime(2026, 8, 29, 2, 2),
            ),
        ]
    )
    link_time = datetime(2026, 8, 29, 2, 0)
    db_session.add_all(
        [
            SymbolNewsRelevance(
                article_id=pending_candidate.id,
                market="kr",
                symbol=symbol,
                feed_source="google_news",
                first_seen_at=link_time,
                status="pending",
                created_at=link_time,
                updated_at=link_time,
            ),
            SymbolNewsRelevance(
                article_id=excluded_candidate.id,
                market="kr",
                symbol=symbol,
                feed_source="google_news",
                first_seen_at=link_time,
                status="excluded",
                created_at=link_time,
                updated_at=link_time,
            ),
        ]
    )
    await db_session.commit()

    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def session_override() -> object:
        return SimpleNamespace(user=SimpleNamespace(id=101, role="trader"))

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    seed = _NewsSeed(
        symbol=symbol,
        same_time_titles=frozenset({"동시각 공시", "동시각 뉴스"}),
        news_titles=frozenset({"동시각 뉴스", "이전 뉴스", "후보 뉴스"}),
        disclosure_titles=frozenset({"동시각 공시", "시각 없는 공시"}),
        null_title="시각 없는 공시",
        kst_title="동시각 공시",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://kasset.test",
        ) as client:
            yield client, seed
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(NewsArticle).where(NewsArticle.url.like(f"%/{prefix}/%"))
        )
        await db_session.commit()


async def _page(
    client: httpx.AsyncClient,
    seed: _NewsSeed,
    *,
    kind: str = "all",
    limit: int = 20,
    cursor: str | None = None,
) -> httpx.Response:
    params: dict[str, str | int] = {
        "market": "KRX",
        "symbol": seed.symbol,
        "kind": kind,
        "limit": limit,
    }
    if cursor is not None:
        params["cursor"] = cursor
    return await client.get("/api/v1/market/news", params=params)


@pytest.mark.asyncio
async def test_kind_filters_split_actual_rows_and_never_expose_feed_source(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client

    for kind, expected in (
        ("all", seed.all_titles),
        ("news", seed.news_titles),
        ("disclosure", seed.disclosure_titles),
    ):
        response = await _page(client, seed, kind=kind)
        assert response.status_code == 200
        payload = response.json()
        assert {item["title"] for item in payload["items"]} == expected
        serialized = json.dumps(payload)
        assert "feed_source" not in serialized
        assert "feedSource" not in serialized
        assert {item["kind"] for item in payload["items"]} <= {
            "news",
            "disclosure",
        }


@pytest.mark.asyncio
async def test_summary_uses_latest_bulk_analysis_for_news_and_verified_article_for_disclosure(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client

    response = await _page(client, seed)

    assert response.status_code == 200
    by_title = {item["title"]: item for item in response.json()["items"]}
    assert by_title["동시각 뉴스"]["summary"] == (
        "최신 한국어 AI 요약이다. 영속 분석 결과를 사용한다."
    )
    assert by_title["동시각 뉴스"]["summary"] != "뉴스 요약"
    assert by_title["이전 뉴스"]["summary"] is None
    assert by_title["후보 뉴스"]["summary"] is None
    assert by_title["동시각 공시"]["summary"] == "검증된 공시 요약"


@pytest.mark.asyncio
async def test_keyset_keeps_same_timestamp_rows_without_duplicates_or_omissions(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client
    cursor: str | None = None
    titles: list[str] = []
    published_values: list[str | None] = []

    while True:
        response = await _page(client, seed, limit=1, cursor=cursor)
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["items"]) == 1
        titles.append(payload["items"][0]["title"])
        published_values.append(payload["items"][0]["publishedAt"])
        cursor = payload["nextCursor"]
        if cursor is None:
            break
        assert len(titles) <= len(seed.all_titles)

    assert frozenset(titles) == seed.all_titles
    assert len(titles) == len(set(titles)) == len(seed.all_titles)
    assert frozenset(titles[:2]) == seed.same_time_titles
    assert titles[-1] == seed.null_title
    assert published_values[-1] is None


@pytest.mark.asyncio
async def test_published_at_interprets_naive_storage_as_kst_before_utc_z(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client
    response = await _page(client, seed)

    assert response.status_code == 200
    item = next(
        item for item in response.json()["items"] if item["title"] == seed.kst_title
    )
    assert item["publishedAt"] == "2026-08-28T15:00:00Z"


@pytest.mark.asyncio
async def test_empty_result_is_200_with_empty_items(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client
    missing = f"{(int(seed.symbol) + 500_000) % 1_000_000:06d}"
    response = await client.get(
        "/api/v1/market/news",
        params={"market": "KRX", "symbol": missing},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}


@pytest.mark.asyncio
async def test_limit_accepts_boundaries_and_rejects_outside_range(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client

    assert (await _page(client, seed, limit=1)).status_code == 200
    assert (await _page(client, seed, limit=50)).status_code == 200
    assert (await _page(client, seed, limit=0)).status_code == 422
    assert (await _page(client, seed, limit=51)).status_code == 422


@pytest.mark.asyncio
async def test_malformed_and_tampered_cursors_are_rejected(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
) -> None:
    client, seed = market_news_client
    first = await _page(client, seed, limit=1)
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert isinstance(cursor, str)

    index = len(cursor) // 2
    replacement = "A" if cursor[index] != "A" else "B"
    tampered = cursor[:index] + replacement + cursor[index + 1 :]

    malformed_response = await _page(client, seed, cursor="not-a-cursor")
    tampered_response = await _page(client, seed, cursor=tampered)
    assert malformed_response.status_code == 422
    assert tampered_response.status_code == 422
    assert malformed_response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert tampered_response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_every_disclosure_provider_maps_to_disclosure_kind(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
    db_session: AsyncSession,
) -> None:
    """공시 provider 가 늘어도 `kind` 매핑이 한 곳에서 따라온다.

    미국 공시(`sec`)가 뉴스로 새는 것을 막는다. fixture 의 미국 행은
    `finnhub_company_news` 뉴스 하나뿐이므로 여기에 `sec` 공시를 더해
    같은 시장·종목에서 둘이 갈리는지 본다.
    """

    client, seed = market_news_client
    db_session.add(
        _article(
            prefix=uuid4().hex,
            slug="us-disclosure",
            title="미국 공시",
            symbol=seed.symbol,
            market="us",
            feed_source="sec",
            published_at=datetime(2026, 8, 29, 1, 0),
            source="SEC EDGAR",
        )
    )
    await db_session.commit()

    async def us_page(kind: str) -> dict[str, object]:
        response = await client.get(
            "/api/v1/market/news",
            params={"market": "US", "symbol": seed.symbol, "kind": kind},
        )
        assert response.status_code == 200
        return response.json()

    all_items = (await us_page("all"))["items"]
    by_title = {item["title"]: item["kind"] for item in all_items}
    assert by_title["미국 공시"] == "disclosure"
    assert by_title["다른 시장 뉴스"] == "news"

    disclosure_items = (await us_page("disclosure"))["items"]
    news_items = (await us_page("news"))["items"]
    disclosure_titles = {item["title"] for item in disclosure_items}
    news_titles = {item["title"] for item in news_items}
    assert disclosure_titles == {"미국 공시"}
    assert news_titles == {"다른 시장 뉴스"}

@pytest.mark.asyncio
async def test_global_feed_curates_dart_but_symbol_feed_keeps_symbol_rows(
    market_news_client: tuple[httpx.AsyncClient, _NewsSeed],
    db_session: AsyncSession,
) -> None:
    client, seed = market_news_client
    prefix = uuid4().hex
    published_at = datetime(2099, 8, 29, 0, 0)
    important = _article(
        prefix=prefix,
        slug="important-listed",
        title="단일판매ㆍ공급계약체결",
        symbol=seed.symbol,
        feed_source="dart",
        published_at=published_at,
        source="DART",
    )
    routine = _article(
        prefix=prefix,
        slug="routine-listed",
        title="주주총회소집결의",
        symbol=seed.symbol,
        feed_source="dart",
        published_at=published_at,
        source="DART",
    )
    low_information = _article(
        prefix=prefix,
        slug="low-information-listed",
        title="대규모기업집단현황공시[개별회사]",
        symbol=seed.symbol,
        feed_source="dart",
        published_at=published_at,
        source="DART",
    )
    unlisted = _article(
        prefix=prefix,
        slug="important-unlisted",
        title="유상증자결정",
        symbol=None,
        feed_source="dart",
        published_at=published_at,
        source="DART",
    )
    analyzed_news = _article(
        prefix=prefix,
        slug="analyzed-news",
        title="원문 기반 분석 뉴스",
        symbol=None,
        feed_source="google_news",
        published_at=datetime(2099, 8, 29, 10, 0),
        source="Example Wire",
    )
    title_only_news = _article(
        prefix=prefix,
        slug="title-only-news",
        title="제목만 있는 최신 뉴스",
        symbol=None,
        feed_source="google_news",
        published_at=datetime(2099, 8, 29, 11, 0),
        source="Example Wire",
    )
    db_session.add_all(
        [
            important,
            routine,
            low_information,
            unlisted,
            analyzed_news,
            title_only_news,
        ]
    )
    await db_session.flush()
    db_session.add(
        _analysis(
            analyzed_news.id,
            summary="상장사가 실제 계약 조건을 발표했다. 원문 수치를 확인한 요약이다.",
            created_at=datetime(2099, 8, 29, 10, 1),
        )
    )
    await db_session.commit()

    try:
        first = await client.get(
            "/api/v1/market/news",
            params={"market": "KRX", "kind": "disclosure", "limit": 1},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["items"][0]["title"] == important.title

        second = await client.get(
            "/api/v1/market/news",
            params={
                "market": "KRX",
                "kind": "disclosure",
                "limit": 1,
                "cursor": first_payload["nextCursor"],
            },
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["title"] == routine.title

        global_response = await client.get(
            "/api/v1/market/news",
            params={"market": "KRX", "kind": "disclosure", "limit": 50},
        )
        global_titles = {
            item["title"] for item in global_response.json()["items"]
        }
        assert important.title in global_titles
        assert routine.title in global_titles
        assert low_information.title not in global_titles
        assert unlisted.title not in global_titles

        global_news = await client.get(
            "/api/v1/market/news",
            params={"market": "KRX", "kind": "news", "limit": 2},
        )
        assert global_news.status_code == 200
        assert [
            item["title"] for item in global_news.json()["items"]
        ] == [analyzed_news.title, title_only_news.title]

        symbol_response = await _page(client, seed, kind="disclosure", limit=20)
        symbol_titles = {
            item["title"] for item in symbol_response.json()["items"]
        }
        assert low_information.title in symbol_titles
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(NewsArticle).where(NewsArticle.url.like(f"%/{prefix}/%"))
        )
        await db_session.commit()
