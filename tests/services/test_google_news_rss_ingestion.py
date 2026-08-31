"""Google News RSS 수집·중복제거·후보 종목 매핑 계약 테스트."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from html import escape
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.jobs.google_news_rss_ingestion import load_google_news_symbols
from app.models.news import NewsArticle, NewsIngestionRun
from app.models.symbol_master import SymbolMaster
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services import truth_social_ingestion
from app.services.google_news_ingestion import GoogleNewsSymbol, ingest_google_news_rss
from app.services.google_news_rss import (
    GOOGLE_NEWS_FEED_SOURCE,
    GoogleNewsRssError,
    build_symbol_query,
    market_config,
    normalize_us_company_name,
    parse_google_news_rss,
)
from app.services.truth_social_ingestion import (
    TRUTH_SOCIAL_ACCOUNT,
    TRUTH_SOCIAL_ACCOUNT_ID,
    TRUTH_SOCIAL_FEED_SOURCE,
    TRUTH_SOCIAL_PROFILE_URL,
    TruthSocialError,
    ingest_truth_social,
)
from app.tasks import (
    TASKIQ_TASK_MODULES,
    google_news_rss_ingestion_tasks,
    truth_social_tasks,
)


@dataclass(frozen=True)
class _ResponseSpec:
    status_code: int
    body: bytes


class FakeGoogleNewsClient:
    def __init__(self, *responses: _ResponseSpec) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response:
        self.calls.append({"url": url, "params": dict(params)})
        response = self.responses.pop(0)
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            response.status_code,
            content=response.body,
            request=request,
        )


def _article_url(label: str) -> str:
    return f"https://news.google.com/rss/articles/{label}-{uuid.uuid4().hex}"


def _item(
    *,
    url: str,
    title: str,
    source: str | None = "테스트-뉴스",
    pub_date: str | None = "Fri, 29 Aug 2026 05:00:00 GMT",
    description: str | None = None,
) -> str:
    parts = ["<item>", f"<title>{escape(title)}</title>", f"<link>{escape(url)}</link>"]
    if source is not None:
        parts.append(f"<source>{escape(source)}</source>")
    if pub_date is not None:
        parts.append(f"<pubDate>{escape(pub_date)}</pubDate>")
    if description is not None:
        parts.append(f"<description>{escape(description)}</description>")
    parts.append("</item>")
    return "".join(parts)


def _rss(*items: str) -> bytes:
    return f"<rss version='2.0'><channel>{''.join(items)}</channel></rss>".encode()


def _ok(body: bytes) -> _ResponseSpec:
    return _ResponseSpec(200, body)


async def _run(db_session, run_uuid: str) -> NewsIngestionRun:
    return (
        await db_session.execute(
            select(NewsIngestionRun).where(NewsIngestionRun.run_uuid == run_uuid)
        )
    ).scalar_one()


class FakeTruthSocialClient:
    def __init__(self, *payloads: object) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response:
        self.calls.append({"url": url, "params": dict(params)})
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json=self.payloads.pop(0), request=request)


def _truth_account() -> dict[str, object]:
    return {
        "id": TRUTH_SOCIAL_ACCOUNT_ID,
        "acct": TRUTH_SOCIAL_ACCOUNT,
        "url": TRUTH_SOCIAL_PROFILE_URL,
        "verified": True,
    }


def _truth_status(
    status_id: str,
    content: str,
    *,
    reblog: object | None = None,
    in_reply_to_id: object | None = None,
    card: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": status_id,
        "url": f"{TRUTH_SOCIAL_PROFILE_URL}/{status_id}",
        "created_at": "2026-08-31T10:00:00.000Z",
        "visibility": "public",
        "reblog": reblog,
        "in_reply_to_id": in_reply_to_id,
        "content": f"<p>{escape(content)}</p>",
        "account": _truth_account(),
        "card": card,
    }


def test_market_locale_and_market_specific_query_are_pure() -> None:
    kr = market_config("KR")
    us = market_config("us")

    assert (kr.market, kr.hl, kr.gl, kr.ceid) == ("kr", "ko", "KR", "KR:ko")
    assert (us.market, us.hl, us.gl, us.ceid) == (
        "us",
        "en-US",
        "US",
        "US:en",
    )
    assert build_symbol_query(market="kr", name="  삼성전자  ") == "삼성전자"
    assert (
        build_symbol_query(market="us", name="PROSHARES TRUST ULTRAPRO QQQ ETF")
        == "PROSHARES TRUST ULTRAPRO QQQ stock"
    )
    with pytest.raises(ValueError, match="unsupported market"):
        market_config("crypto")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NVIDIA CORP", "NVIDIA"),
        ("ADV MICRO DEVICE", "ADV MICRO DEVICE"),
        ("ALPHABET INC A", "ALPHABET"),
        ("SANDISK CORP ORD", "SANDISK"),
        ("KEYCORP NEW", "KEYCORP"),
        ("AT&T", "AT&T"),
        ("ALLSTATE CP", "ALLSTATE"),
        ("PROSHARES ULTRAPRO QQQ ETF", "PROSHARES ULTRAPRO QQQ"),
        ("NEWMONT", "NEWMONT"),
    ],
)
def test_us_company_name_normalization(raw: str, expected: str) -> None:
    assert normalize_us_company_name(raw) == expected


def test_us_company_name_rejects_empty_normalized_value() -> None:
    with pytest.raises(ValueError, match="empty after normalization"):
        normalize_us_company_name("INC CO")


def test_parser_strips_exact_source_suffix_html_and_converts_gmt_to_naive_kst() -> None:
    first_url = _article_url("normalize-one")
    second_url = _article_url("normalize-two")
    feed = parse_google_news_rss(
        _rss(
            _item(
                url=first_url,
                title="기업 - 인수 전망 - 서울-경제",
                source="서울-경제",
                description="<p>첫 문장 &amp; 다음</p><div>둘째&nbsp;문장</div>",
            ),
            _item(
                url=second_url,
                title="제목 자체에 - 하이픈이 있음",
                source=None,
                pub_date=None,
            ),
        )
    )

    first, second = feed.items
    assert first.title == "기업 - 인수 전망"
    assert first.source == "서울-경제"
    assert first.summary is None
    assert first.published_at == datetime(2026, 8, 29, 14, 0)
    assert first.published_at.tzinfo is None
    assert second.title == "제목 자체에 - 하이픈이 있음"
    assert second.source is None
    assert second.published_at is None


def test_parser_excludes_observed_sources_and_allows_configuration() -> None:
    observed = parse_google_news_rss(
        _rss(
            _item(
                url=_article_url("ir"),
                title="IR 게시물 - NAVERCorp.",
                source="NAVERCorp.",
            ),
            _item(
                url=_article_url("blog"),
                title="블로그 게시물 - Naver Blog",
                source="Naver Blog",
            ),
            _item(
                url=_article_url("premium"),
                title="유료 콘텐츠 - 네이버 프리미엄콘텐츠",
                source="네이버 프리미엄콘텐츠",
            ),
            _item(
                url=_article_url("press"),
                title="정상 기사 - 아시아투데이",
                source="아시아투데이",
            ),
        )
    )
    configured = parse_google_news_rss(
        _rss(
            _item(
                url=_article_url("custom"),
                title="설정 제외 - Custom Source",
                source="Custom Source",
            )
        ),
        excluded_sources={"custom source"},
    )

    assert [item.source for item in observed.items] == ["아시아투데이"]
    assert observed.excluded_count == 3
    assert observed.truncated_count == 0
    assert configured.items == ()
    assert configured.excluded_count == 1


def test_parser_rejects_missing_required_item_element_and_malformed_xml() -> None:
    missing_link = (
        "<rss><channel><item><title>제목</title></item></channel></rss>".encode()
    )
    with pytest.raises(GoogleNewsRssError, match="missing required link"):
        parse_google_news_rss(missing_link)
    with pytest.raises(GoogleNewsRssError, match="invalid RSS XML"):
        parse_google_news_rss(b"<rss><channel>")
    with pytest.raises(GoogleNewsRssError, match="unexpected RSS root"):
        parse_google_news_rss(b"<feed></feed>")


@pytest.mark.parametrize(
    "url",
    [
        "http://news.google.com/rss/articles/insecure",
        "https://publisher.example/story",
        "https://user:secret@news.google.com/rss/articles/credential",
        "https://news.google.com/rss/articles/has space",
    ],
)
def test_parser_rejects_non_google_or_unsafe_article_url(url: str) -> None:
    with pytest.raises(GoogleNewsRssError, match="Google News HTTPS"):
        parse_google_news_rss(_rss(_item(url=url, title="검증 대상")))


def test_parser_caps_each_symbol_and_truncates_title_after_suffix_removal() -> None:
    long_title = "가" * 510
    items = [
        _item(
            url=_article_url(f"cap-{index}"),
            title=f"{long_title} - 매체",
            source="매체",
        )
        for index in range(101)
    ]

    feed = parse_google_news_rss(_rss(*items), max_items=100)

    assert len(feed.items) == 100
    assert feed.truncated_count == 1
    assert feed.excluded_count == 0
    assert feed.items[0].title == "가" * 500


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_and_reingest_preserve_id_and_only_refresh_title_source(
    db_session,
) -> None:
    url = _article_url("reingest")
    symbol = GoogleNewsSymbol(symbol="005930", name="삼성전자")
    first_run = str(uuid.uuid4())
    first = FakeGoogleNewsClient(
        _ok(
            _rss(
                _item(
                    url=url,
                    title="첫 제목 - 첫 매체",
                    source="첫 매체",
                    description="<b>첫 요약</b>",
                )
            )
        )
    )

    first_counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[symbol],
        run_uuid=first_run,
        client=first,
        request_interval_seconds=0,
    )
    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    article_id = article.id

    second_counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[symbol],
        run_uuid=str(uuid.uuid4()),
        client=FakeGoogleNewsClient(
            _ok(
                _rss(
                    _item(
                        url=url,
                        title="정정 제목 - 새 매체",
                        source="새 매체",
                        description="<b>바뀐 요약</b>",
                    )
                )
            )
        ),
        request_interval_seconds=0,
    )
    unchanged_counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[symbol],
        run_uuid=str(uuid.uuid4()),
        client=FakeGoogleNewsClient(
            _ok(
                _rss(
                    _item(
                        url=url,
                        title="정정 제목 - 새 매체",
                        source="새 매체",
                        description="<b>또 다른 요약</b>",
                    )
                )
            )
        ),
        request_interval_seconds=0,
    )

    await db_session.refresh(article)
    assert first_counts == (1, 0, 0)
    assert second_counts == (0, 1, 0)
    assert unchanged_counts == (0, 0, 1)
    assert article.id == article_id
    assert article.title == "정정 제목"
    assert article.source == "새 매체"
    assert article.summary is None
    assert article.article_content is None
    assert article.stock_symbol is None
    assert article.stock_name is None
    assert article.feed_source == GOOGLE_NEWS_FEED_SOURCE
    assert article.market == "kr"
    assert article.is_analyzed is False
    assert (
        await db_session.scalar(
            select(func.count()).select_from(NewsArticle).where(NewsArticle.url == url)
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_link_from_multiple_queries_writes_one_article_and_two_candidates(
    db_session,
) -> None:
    url = _article_url("shared")
    body = _rss(_item(url=url, title="공동 기사 - 테스트-뉴스"))
    run_uuid = str(uuid.uuid4())

    counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[
            GoogleNewsSymbol(symbol="005930", name="삼성전자"),
            GoogleNewsSymbol(symbol="000660", name="SK하이닉스"),
        ],
        run_uuid=run_uuid,
        client=FakeGoogleNewsClient(_ok(body), _ok(body)),
        request_interval_seconds=0,
    )

    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    links = (
        (
            await db_session.execute(
                select(SymbolNewsRelevance).where(
                    SymbolNewsRelevance.article_id == article.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert counts == (1, 0, 1)
    assert article.stock_symbol is None
    assert {(link.symbol, link.status) for link in links} == {
        ("005930", "pending"),
        ("000660", "pending"),
    }
    run = await _run(db_session, run_uuid)
    assert run.status == "success"
    assert run.source_counts[GOOGLE_NEWS_FEED_SOURCE] == {
        "inserted": 1,
        "updated": 0,
        "skipped": 1,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_excluded_sources_are_counted_as_skipped(db_session) -> None:
    valid_url = _article_url("allowed-source")
    excluded_items = [
        _item(
            url=_article_url(f"excluded-{index}"),
            title=f"제외 기사 {index} - {source}",
            source=source,
        )
        for index, source in enumerate(
            ("NAVERCorp.", "Naver Blog", "네이버 프리미엄콘텐츠")
        )
    ]
    run_uuid = str(uuid.uuid4())

    counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[GoogleNewsSymbol(symbol="035420", name="NAVER")],
        run_uuid=run_uuid,
        client=FakeGoogleNewsClient(
            _ok(
                _rss(
                    *excluded_items,
                    _item(
                        url=valid_url,
                        title="정상 기사 - 아시아투데이",
                        source="아시아투데이",
                    ),
                )
            )
        ),
        request_interval_seconds=0,
    )

    run = await _run(db_session, run_uuid)
    assert counts == (1, 0, 3)
    assert run.skipped_count == 3
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(NewsArticle)
            .where(NewsArticle.url == valid_url)
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_and_parse_errors_make_partial_but_empty_feed_is_success(
    db_session,
) -> None:
    url = _article_url("partial")
    partial_run = str(uuid.uuid4())
    partial_counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[
            GoogleNewsSymbol(symbol="005930", name="삼성전자"),
            GoogleNewsSymbol(symbol="000660", name="SK하이닉스"),
            GoogleNewsSymbol(symbol="035420", name="NAVER"),
        ],
        run_uuid=partial_run,
        client=FakeGoogleNewsClient(
            _ok(_rss(_item(url=url, title="정상 기사 - 테스트-뉴스"))),
            _ResponseSpec(503, b"blocked"),
            _ok(b"<rss><channel>"),
        ),
        request_interval_seconds=0,
    )
    partial = await _run(db_session, partial_run)

    empty_run = str(uuid.uuid4())
    empty_counts = await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[GoogleNewsSymbol(symbol="051910", name="LG화학")],
        run_uuid=empty_run,
        client=FakeGoogleNewsClient(_ok(_rss())),
        request_interval_seconds=0,
    )
    empty = await _run(db_session, empty_run)

    assert partial_counts == (1, 0, 0)
    assert partial.status == "partial"
    assert partial.finished_at is not None
    assert "000660: Google News RSS HTTP 503" in partial.error_message
    assert "035420: invalid RSS XML" in partial.error_message
    assert empty_counts == (0, 0, 0)
    assert empty.status == "success"
    assert empty.error_message is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_failed_symbols_close_run_as_failed(db_session) -> None:
    run_uuid = str(uuid.uuid4())

    counts = await ingest_google_news_rss(
        db_session,
        market="us",
        symbols=[
            GoogleNewsSymbol(symbol="AMD", name="ADV MICRO DEVICE"),
            GoogleNewsSymbol(symbol="EMPTY", name="INC CO"),
        ],
        run_uuid=run_uuid,
        client=FakeGoogleNewsClient(_ResponseSpec(429, b"rate limited")),
        request_interval_seconds=0,
    )

    run = await _run(db_session, run_uuid)
    assert counts == (0, 0, 0)
    assert run.market == "us"
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "AMD: Google News RSS HTTP 429" in run.error_message
    assert "EMPTY: US company name is empty after normalization" in run.error_message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_is_stored_per_separate_run_with_shared_feed_source(
    db_session,
) -> None:
    kr_url = _article_url("kr-market")
    us_url = _article_url("us-market")
    kr_client = FakeGoogleNewsClient(
        _ok(_rss(_item(url=kr_url, title="국내 기사 - 테스트-뉴스")))
    )
    us_client = FakeGoogleNewsClient(
        _ok(
            _rss(
                _item(
                    url=us_url,
                    title="US story - Example News",
                    source="Example News",
                )
            )
        )
    )

    await ingest_google_news_rss(
        db_session,
        market="kr",
        symbols=[GoogleNewsSymbol(symbol="005930", name="삼성전자")],
        run_uuid=str(uuid.uuid4()),
        client=kr_client,
        request_interval_seconds=0,
    )
    await ingest_google_news_rss(
        db_session,
        market="us",
        symbols=[GoogleNewsSymbol(symbol="AAPL", name="APPLE INC")],
        run_uuid=str(uuid.uuid4()),
        client=us_client,
        request_interval_seconds=0,
    )

    rows = (
        await db_session.execute(
            select(NewsArticle.url, NewsArticle.market, NewsArticle.feed_source).where(
                NewsArticle.url.in_([kr_url, us_url])
            )
        )
    ).all()
    assert set(rows) == {
        (kr_url, "kr", GOOGLE_NEWS_FEED_SOURCE),
        (us_url, "us", GOOGLE_NEWS_FEED_SOURCE),
    }
    assert kr_client.calls[0]["params"] == {
        "q": "삼성전자",
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    assert us_client.calls[0]["params"] == {
        "q": "APPLE stock",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_loader_uses_active_symbol_master_names(db_session) -> None:
    kr_symbol = f"{uuid.uuid4().int % 1_000_000:06d}"
    us_symbol = f"Z{uuid.uuid4().hex[:9].upper()}"
    db_session.add_all(
        [
            SymbolMaster(
                market="KRX",
                symbol=kr_symbol,
                name="국내테스트",
                name_en="*TestKR",
                security_type="COMMON_STOCK",
                is_active=True,
            ),
            SymbolMaster(
                market="US",
                symbol=us_symbol,
                name="미국테스트",
                name_en="TEST GLOBAL CORP",
                security_type="COMMON_STOCK",
                is_active=True,
            ),
        ]
    )
    await db_session.flush()

    kr = await load_google_news_symbols(
        db_session, market="kr", stock_symbols=[kr_symbol]
    )
    us = await load_google_news_symbols(
        db_session, market="us", stock_symbols=[us_symbol]
    )

    assert kr == [GoogleNewsSymbol(symbol=kr_symbol, name="국내테스트")]
    assert us == [GoogleNewsSymbol(symbol=us_symbol, name="TEST GLOBAL CORP")]


def test_google_news_task_is_registered_without_recurring_schedule() -> None:
    assert google_news_rss_ingestion_tasks in TASKIQ_TASK_MODULES
    task = google_news_rss_ingestion_tasks.ingest_google_news_rss_task
    assert task.task_name == "news.google_news.ingest"
    assert "schedule" not in task.labels


@pytest.mark.integration
@pytest.mark.asyncio
async def test_truth_social_default_transport_uses_browser_tls_impersonation(
    db_session,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}
    client = FakeTruthSocialClient(_truth_account(), [])

    class FakeCurlSession:
        def __init__(self, **kwargs) -> None:
            observed.update(kwargs)

        async def __aenter__(self):
            return client

        async def __aexit__(self, *_args) -> None:
            return None

    async def fake_summary(_db, urls):
        assert list(urls) == []
        return SimpleNamespace(status="success", summarized=0, failed=0)

    monkeypatch.setattr(truth_social_ingestion, "CurlAsyncSession", FakeCurlSession)
    monkeypatch.setattr(
        truth_social_ingestion,
        "summarize_ingested_news",
        fake_summary,
    )

    result = await ingest_truth_social(db_session)

    assert result.fetched == 0
    assert observed == {"impersonate": "chrome146", "timeout": 15.0}


@pytest.mark.asyncio
async def test_truth_social_rejects_official_account_identity_mismatch(
    db_session,
) -> None:
    mismatched = _truth_account()
    mismatched["id"] = "1"

    with pytest.raises(TruthSocialError, match="identity mismatch"):
        await ingest_truth_social(
            db_session,
            http_client=FakeTruthSocialClient(mismatched),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_truth_social_isolates_bad_rows_and_filters_non_original_posts(
    db_session,
    monkeypatch,
) -> None:
    relevant_id = str(uuid.uuid4().int)
    irrelevant_id = str(uuid.uuid4().int)
    boosted_id = str(uuid.uuid4().int)
    card_only_id = str(uuid.uuid4().int)
    reply_id = str(uuid.uuid4().int)
    statuses = [
        _truth_status(
            relevant_id,
            (
                "I will impose a 25% tariff on imported semiconductor chips. "
                "https://example.com/2026/08/31/policy"
            ),
        ),
        _truth_status(irrelevant_id, "Happy birthday to a great friend."),
        _truth_status(
            boosted_id,
            "Federal Reserve rate decision.",
            reblog={"id": "someone-else"},
        ),
        _truth_status(
            card_only_id,
            "Read this report.",
            card={"title": "New semiconductor tariff policy", "description": ""},
        ),
        _truth_status(
            reply_id,
            "The stock market will rise.",
            in_reply_to_id="parent-status",
        ),
        _truth_status("not-a-decimal-id", "Federal Reserve rate decision."),
    ]
    summary_calls: list[list[str]] = []

    async def fake_summary(_db, urls):
        captured = list(urls)
        summary_calls.append(captured)
        return SimpleNamespace(
            status="success",
            summarized=len(captured),
            failed=0,
        )

    monkeypatch.setattr(
        truth_social_ingestion,
        "summarize_ingested_news",
        fake_summary,
    )
    relevant_url = f"{TRUTH_SOCIAL_PROFILE_URL}/{relevant_id}"
    card_only_url = f"{TRUTH_SOCIAL_PROFILE_URL}/{card_only_id}"
    inserted_urls = [relevant_url, card_only_url]
    try:
        first = await ingest_truth_social(
            db_session,
            http_client=FakeTruthSocialClient(_truth_account(), statuses),
        )
        second = await ingest_truth_social(
            db_session,
            http_client=FakeTruthSocialClient(_truth_account(), statuses),
        )

        article = (
            await db_session.execute(
                select(NewsArticle).where(NewsArticle.url == relevant_url)
            )
        ).scalar_one()
        link_count = await db_session.scalar(
            select(func.count())
            .select_from(SymbolNewsRelevance)
            .where(SymbolNewsRelevance.article_id == article.id)
        )
        assert first.fetched == 6
        assert first.relevant == 2
        assert (first.inserted, first.updated, first.skipped) == (2, 0, 4)
        assert (second.inserted, second.updated, second.skipped) == (0, 0, 6)
        assert first.summary_status == "success"
        assert summary_calls == [
            [relevant_url, card_only_url],
            [relevant_url, card_only_url],
        ]
        assert article.market == "us"
        assert article.feed_source == TRUTH_SOCIAL_FEED_SOURCE
        assert article.source == "Donald J. Trump · Truth Social"
        assert "tariff" in (article.article_content or "")
        assert article.title == (
            "I will impose a 25% tariff on imported semiconductor chips."
        )
        assert link_count == 0
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(NewsArticle)
                .where(NewsArticle.feed_source == TRUTH_SOCIAL_FEED_SOURCE)
            )
            == 2
        )
    finally:
        await db_session.execute(
            delete(NewsArticle).where(NewsArticle.url.in_(inserted_urls))
        )
        await db_session.commit()


def test_truth_social_task_is_registered_with_ten_minute_schedule() -> None:
    assert truth_social_tasks in TASKIQ_TASK_MODULES
    task = truth_social_tasks.ingest_truth_social_task
    assert task.task_name == "news.truth_social.ingest"
    assert task.labels.get("schedule") == [
        {"cron": "2,12,22,32,42,52 * * * *", "cron_offset": "UTC"}
    ]
