"""Google News 원문 확보와 article_content 저장 품질 회귀 테스트."""

from __future__ import annotations

import json
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
async def test_signed_google_article_page_decodes_provider_before_fetch() -> None:
    calls: list[tuple[str, str]] = []
    google_url = "https://news.google.com/rss/articles/AU_yqLsigned-token"
    provider_url = "https://publisher.example/markets/decoded-story"
    decoded_payload = json.dumps(["garturlres", provider_url])
    batch_body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", decoded_payload]]) + "\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.path.startswith("/rss/articles/"):
            return httpx.Response(
                200,
                text=(
                    '<html><body><c-wiz data-n-a-sg="signed-value" '
                    'data-n-a-ts="1787980800"></c-wiz></body></html>'
                ),
                headers={"Content-Type": "text/html; charset=utf-8"},
                request=request,
            )
        if request.url.path == "/_/DotsSplashUi/data/batchexecute":
            form_body = (await request.aread()).decode()
            assert "f.req=" in form_body
            assert "AU_yqLsigned-token" in form_body
            return httpx.Response(
                200,
                text=batch_body,
                headers={"Content-Type": "application/json; charset=utf-8"},
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
                "<html><article><h1>해제된 공급자 기사</h1>"
                "<p>이 기사는 Google News의 서명된 식별자를 동적 RPC로 해제한 뒤 "
                "원문 공급자 사이트에서 직접 확보한 본문입니다.</p>"
                "<p>두 번째 문단은 제목 카드가 아니라 실제 기사 본문이 저장되고 "
                "요약 입력으로 사용되는지를 검증하기 위한 충분한 길이를 제공합니다.</p>"
                "<p>수집기는 공급자 도메인의 robots 정책과 공개 주소를 다시 검사하고 "
                "본문 길이 제한 안에서만 후속 AI 요약 단계로 전달합니다.</p>"
                "</article></html>"
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
        ("GET", google_url),
        ("POST", "https://news.google.com/_/DotsSplashUi/data/batchexecute"),
        ("GET", "https://publisher.example/robots.txt"),
        ("GET", provider_url),
    ]
    assert "제목 카드가 아니라 실제 기사 본문" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_page_does_not_follow_untrusted_body_anchor() -> None:
    google_url = "https://news.google.com/rss/articles/untrusted-anchor"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            text=(
                "<html><body><a href='https://publisher.example/unrelated-ad'>"
                "광고 링크</a></body></html>"
            ),
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NewsArticleFetchError, match="no decodable"):
            await GoogleNewsArticleFetcher(
                client,
                resolver=_public_resolver,
            ).fetch(google_url)

    assert calls == [google_url]


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
