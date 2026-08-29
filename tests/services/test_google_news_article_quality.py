"""Google News 원문 확보와 article_content 저장 품질 회귀 테스트."""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.models.news import NewsArticle
from app.services.google_news_ingestion import (
    GoogleNewsArticleFetcher,
    NewsArticleFetchError,
)
from app.services.symbol_news_store import (
    FeedArticleInput,
    count_feed_article_changes,
    upsert_feed_articles,
)


async def _public_resolver(host: str) -> tuple[str, ...]:
    assert host == "publisher.example"
    return ("8.8.8.8",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_redirect_fetches_real_provider_article_body() -> None:
    calls: list[str] = []
    google_url = "https://news.google.com/rss/articles/quality-fixture"
    provider_url = "https://publisher.example/markets/semiconductor-contract"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "news.google.com":
            return httpx.Response(
                302,
                headers={"Location": provider_url},
                request=request,
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nAllow: /",
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><body><nav>구독 로그인 인기 기사</nav><article>"
                "<h1>반도체 장기 공급 계약</h1>"
                "<p>삼성전자는 북미 데이터센터 운영사와 고대역폭 메모리 장기 공급 "
                "계약을 체결했다고 29일 밝혔다.</p>"
                "<p>계약 기간은 2026년 9월부터 2029년 8월까지이며, 회사는 신규 "
                "생산라인에서 제품을 공급할 계획이라고 설명했다.</p>"
                "<p>계약 금액은 고객사와의 비밀유지 조항에 따라 공개하지 않았다.</p>"
                "</article><footer>회사 소개 개인정보 처리방침</footer></body></html>"
            ),
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await GoogleNewsArticleFetcher(
            client,
            resolver=_public_resolver,
        ).fetch(google_url)

    assert calls == [
        google_url,
        "https://publisher.example/robots.txt",
        provider_url,
    ]
    assert "2026년 9월부터 2029년 8월" in text
    assert "계약 금액은" in text
    assert "구독 로그인" not in text
    assert "개인정보 처리방침" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_robots_rule_keeps_title_only_fallback() -> None:
    calls: list[str] = []
    provider_url = "https://publisher.example/private/paid-analysis"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            text="User-agent: *\nDisallow: /private/",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NewsArticleFetchError, match="disallowed by robots"):
            await GoogleNewsArticleFetcher(
                client,
                resolver=_public_resolver,
            ).fetch(provider_url)

    assert calls == ["https://publisher.example/robots.txt"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_redirect_revalidates_private_target_before_following() -> None:
    calls: list[str] = []
    google_url = "https://news.google.com/rss/articles/private-target"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://127.0.0.1/internal"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NewsArticleFetchError, match="non-public address"):
            await GoogleNewsArticleFetcher(client).fetch(google_url)

    assert calls == [google_url]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_feed_upsert_fills_previously_missing_article_content(db_session) -> None:
    url = f"https://publisher.example/article/{uuid.uuid4().hex}"
    original = FeedArticleInput(
        url=url,
        title="장기 공급 계약 체결",
        source="Example Wire",
        published_at=datetime(2026, 8, 29, 12, 0),
        summary="장기 공급 계약 체결 - Example Wire",
    )
    await upsert_feed_articles(
        db_session,
        "kr",
        "005930",
        [original],
        feed_source="google_news",
    )

    enriched = FeedArticleInput(
        url=url,
        title=original.title,
        source=original.source,
        published_at=original.published_at,
        summary=original.summary,
        article_content=(
            "삼성전자는 북미 데이터센터 운영사와 고대역폭 메모리 장기 공급 계약을 "
            "체결했다. 계약 기간은 2026년 9월부터 2029년 8월까지다."
        ),
    )
    changes = await count_feed_article_changes(db_session, [enriched])
    assert (changes.inserted, changes.updated, changes.skipped) == (0, 1, 0)

    await upsert_feed_articles(
        db_session,
        "kr",
        "005930",
        [enriched],
        feed_source="google_news",
    )
    stored = await db_session.scalar(select(NewsArticle).where(NewsArticle.url == url))
    assert stored is not None
    assert stored.article_content == enriched.article_content
    assert stored.summary == original.summary
