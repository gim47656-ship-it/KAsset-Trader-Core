"""일반 뉴스 AI 요약의 검증, 영속화, 격리, 자동 수집 배선 테스트."""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.services.news_summary_service import (
    GeneratedNewsSummary,
    NewsSummaryInput,
    OpenAiNewsSummaryGenerator,
    summarize_pending_news,
)


class FakeResponsesClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def request_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.responses[len(self.calls) - 1]


class FakeSummaryGenerator:
    def __init__(self, outcomes: dict[str, str | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls: list[NewsSummaryInput] = []

    async def summarize(self, news: NewsSummaryInput) -> GeneratedNewsSummary:
        self.calls.append(news)
        outcome = self.outcomes[news.title]
        if isinstance(outcome, BaseException):
            raise outcome
        return GeneratedNewsSummary(
            summary=outcome,
            sentiment=Sentiment.NEUTRAL,
            confidence=84,
            model_name="test-news-summary",
            prompt="test prompt",
            raw_response="{}",
        )


def _article(
    *,
    url: str,
    title: str,
    summary: str | None,
    article_content: str | None = None,
    feed_source: str | None = "google_news",
    published_at: datetime,
) -> NewsArticle:
    stored_at = datetime(2026, 8, 29, 2, 0)
    return NewsArticle(
        url=url,
        title=title,
        source="테스트 뉴스",
        article_content=article_content,
        summary=summary,
        feed_source=feed_source,
        market="kr",
        keywords=None,
        is_analyzed=False,
        stock_symbol="005930",
        stock_name="테스트종목",
        article_published_at=published_at,
        scraped_at=stored_at,
        user_id=None,
        created_at=stored_at,
        updated_at=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_translates_with_strict_grounded_contract() -> None:
    client = FakeResponsesClient(
        [
            {
                "summary": (
                    "회사는 2026년 7월 26일 분기 실적을 발표했다. "
                    "매출은 96,221 million으로 집계됐다."
                ),
                "sentiment": "neutral",
                "confidence": 88,
            }
        ]
    )
    generator = OpenAiNewsSummaryGenerator(client, model="gpt-5.6-luna")
    news = NewsSummaryInput(
        title="Company reports quarterly results",
        source="Example Wire",
        article_content=(
            "The company reported quarterly results for the period ended July 26, 2026. "
            "Revenue was 96,221 million."
        ),
        raw_excerpt=None,
    )

    result = await generator.summarize(news)

    assert result.summary.startswith("회사는 2026년 7월 26일")
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.confidence == 88
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning_effort"] == "low"
    assert call["schema_name"] == "kasset_news_summary"
    assert call["input_payload"] == news.to_payload()
    assert call["schema"]["additionalProperties"] is False
    assert "2~4문장" in call["additional_instructions"]
    assert "투자 권유" in call["additional_instructions"]
    assert "핵심 사건과 주체" in call["additional_instructions"]
    assert "투자자 영향" in call["additional_instructions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_translates_foreign_title_when_body_is_missing() -> None:
    client = FakeResponsesClient(
        [
            {
                "summary": "엔비디아 실적 발표 이후 아시아 증시는 혼조세를 보였다.",
                "sentiment": "neutral",
                "confidence": 72,
            }
        ]
    )
    generator = OpenAiNewsSummaryGenerator(client, model="gpt-5.6-luna")
    news = NewsSummaryInput(
        title="Asian Stocks Mixed After Nvidia Earnings",
        source="Example Wire",
        article_content=None,
        raw_excerpt=None,
    )

    result = await generator.summarize(news)

    assert result.summary == "엔비디아 실적 발표 이후 아시아 증시는 혼조세를 보였다."
    assert "한국어 한 문장" in client.calls[0]["additional_instructions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_rejects_raw_copy_and_invented_number() -> None:
    raw_copy = (
        "회사는 신규 제품 출시 계획을 발표했다. "
        "출시 일정은 시장 상황에 따라 정해진다."
    )
    copy_generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [{"summary": raw_copy, "sentiment": "neutral", "confidence": 70}]
        ),
        model="gpt-5.6-luna",
    )
    news = NewsSummaryInput(
        title="신규 제품 출시",
        source="테스트 뉴스",
        article_content=None,
        raw_excerpt=raw_copy,
    )

    with pytest.raises(ValueError, match="duplicates raw input"):
        await copy_generator.summarize(news)

    number_generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": "회사는 신규 제품을 공개했다. 매출은 999억원으로 예상됐다.",
                    "sentiment": "positive",
                    "confidence": 70,
                },
                {
                    "summary": "회사는 신규 제품을 공개했다. 매출은 999억원으로 예상됐다.",
                    "sentiment": "positive",
                    "confidence": 70,
                },
            ]
        ),
        model="gpt-5.6-luna",
    )
    with pytest.raises(ValueError, match="numbers absent from source"):
        await number_generator.summarize(
            NewsSummaryInput(
                title="신규 제품 출시",
                source="테스트 뉴스",
                article_content=(
                    "회사는 신규 제품을 공개했으며 구체적인 매출 수치는 밝히지 않았다. "
                    "출시 일정도 추후 공개할 예정이다."
                ),
                raw_excerpt=None,
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_rejects_template_language_and_multi_sentence_title_fallback() -> None:
    body_news = NewsSummaryInput(
        title="삼성전자 공급 계약 체결",
        source="Example Wire",
        article_content=(
            "삼성전자는 데이터센터 운영사와 장기 공급 계약을 체결했다. "
            "계약 기간은 2026년 9월부터 2029년 8월까지다."
        ),
        raw_excerpt=None,
    )
    template_generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": (
                        "이 기사는 삼성전자의 공급 계약을 다룬다. "
                        "계약 기간은 2026년 9월부터 2029년 8월까지다."
                    ),
                    "sentiment": "neutral",
                    "confidence": 70,
                }
            ]
        ),
        model="gpt-5.6-luna",
    )
    with pytest.raises(ValueError, match="template language"):
        await template_generator.summarize(body_news)

    title_generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": (
                        "엔비디아가 분기 실적을 발표했다. "
                        "투자자들은 결과에 주목하고 있다."
                    ),
                    "sentiment": "neutral",
                    "confidence": 60,
                }
            ]
        ),
        model="gpt-5.6-luna",
    )
    with pytest.raises(ValueError, match="1 to 1 sentences"):
        await title_generator.summarize(
            NewsSummaryInput(
                title="Nvidia Reports Quarterly Results",
                source="Example Wire",
                article_content=None,
                raw_excerpt=None,
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_persists_analysis_isolates_failure_skips_thin_input_and_is_idempotent(
    db_session,
) -> None:
    suffix = uuid.uuid4().hex
    success_url = f"https://news.test.invalid/{suffix}/success"
    failed_url = f"https://news.test.invalid/{suffix}/failed"
    thin_url = f"https://news.test.invalid/{suffix}/thin"
    disclosure_url = f"https://news.test.invalid/{suffix}/disclosure"
    success = _article(
        url=success_url,
        title="해외 기업 실적 발표",
        summary=(
            "The company reported quarterly operating results and maintained its guidance. "
            "Management also described demand conditions in its primary market."
        ),
        published_at=datetime(2026, 8, 29, 12, 0),
    )
    failed = _article(
        url=failed_url,
        title="공급 계약 발표",
        summary=(
            "The company announced a supply agreement with an existing customer. "
            "Delivery timing remains subject to the contract terms."
        ),
        published_at=datetime(2026, 8, 29, 11, 0),
    )
    thin = _article(
        url=thin_url,
        title="제목뿐인 뉴스",
        summary="제목뿐인 뉴스",
        published_at=datetime(2026, 8, 29, 10, 0),
    )
    disclosure = _article(
        url=disclosure_url,
        title="공시 행",
        summary="공시의 검증된 요약",
        feed_source="dart",
        published_at=datetime(2026, 8, 29, 9, 0),
    )
    db_session.add_all([success, failed, thin, disclosure])
    await db_session.flush()
    success_id = success.id
    failed_id = failed.id
    await db_session.commit()
    generator = FakeSummaryGenerator(
        {
            "해외 기업 실적 발표": (
                "회사는 분기 영업 실적을 발표했다. 기존 가이던스도 유지했다."
            ),
            "공급 계약 발표": RuntimeError("fake provider failure"),
        }
    )

    first = await summarize_pending_news(
        db_session,
        batch_size=4,
        article_urls=[success_url, failed_url, thin_url, disclosure_url],
        generator=generator,
    )

    assert first.status == "partial"
    assert first.selected == 3
    assert first.summarized == 1
    assert first.skipped_insufficient == 1
    assert first.failed == 1
    stored = await db_session.scalar(
        select(NewsAnalysisResult).where(
            NewsAnalysisResult.article_id == success_id
        )
    )
    assert stored is not None
    assert stored.summary == (
        "회사는 분기 영업 실적을 발표했다. 기존 가이던스도 유지했다."
    )
    assert stored.model_name == "test-news-summary"
    await db_session.refresh(success)
    await db_session.refresh(failed)
    await db_session.refresh(thin)
    assert success.summary.startswith("The company reported")
    assert success.is_analyzed is True
    assert failed.is_analyzed is False
    assert thin.is_analyzed is False

    generator.outcomes["공급 계약 발표"] = (
        "회사는 기존 고객과 공급 계약을 발표했다. 납품 일정은 계약 조건을 따른다."
    )
    second = await summarize_pending_news(
        db_session,
        batch_size=4,
        article_urls=[success_url, failed_url, thin_url, disclosure_url],
        generator=generator,
    )

    assert second.summarized == 1
    assert second.skipped_insufficient == 1
    assert second.failed == 0
    assert [call.title for call in generator.calls].count("해외 기업 실적 발표") == 1
    assert [call.title for call in generator.calls].count("공급 계약 발표") == 2
    analysis_ids = list(
        (
            await db_session.scalars(
                select(NewsAnalysisResult.id).where(
                    NewsAnalysisResult.article_id.in_([success_id, failed_id])
                )
            )
        ).all()
    )
    assert len(analysis_ids) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_google_ingestion_invokes_summary_only_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import google_news_ingestion as ingestion
    from app.services.symbol_news_store import FeedArticleInput, FeedArticleUpsertCounts

    item = FeedArticleInput(
        url="https://news.test.invalid/auto-summary",
        title="자동 요약 대상",
        source="테스트 뉴스",
        published_at=datetime(2026, 8, 29, 12, 0),
        summary="자동 요약에 사용할 충분한 길이의 원문 발췌가 저장되어 있다.",
    )
    collected = ingestion._CollectedFeeds(
        articles_by_url={item.url: item},
        urls_by_symbol={"005930": {item.url}},
        duplicate_count=0,
        truncated_count=0,
        excluded_count=0,
        successful_symbols=1,
        errors=(),
    )
    events: list[str] = []

    async def fake_collect(**kwargs):
        return collected

    async def fake_enrich(collected, **kwargs):
        events.append("enrich")
        return collected

    async def fake_persist(db, **kwargs):
        events.append("persist")
        return FeedArticleUpsertCounts(inserted=1, updated=0, skipped=0)

    async def fake_summarize(db, article_urls):
        events.append(f"summarize:{','.join(article_urls)}")

    monkeypatch.setattr(ingestion, "_collect_with_client", fake_collect)
    monkeypatch.setattr(ingestion, "_enrich_with_client", fake_enrich)
    monkeypatch.setattr(ingestion, "_persist_collected", fake_persist)
    monkeypatch.setattr(ingestion, "_summarize_after_ingest", fake_summarize)

    result = await ingestion.ingest_google_news_rss(
        object(),
        market="kr",
        symbols=[],
    )

    assert result == (1, 0, 0)
    assert events == ["enrich", "persist", f"summarize:{item.url}"]


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_job_forwards_bounded_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import news_summary as job

    received: dict[str, object] = {}
    expected = {
        "status": "success",
        "selected": 2,
        "summarized": 2,
        "skipped_existing": 0,
        "skipped_insufficient": 0,
        "failed": 0,
    }

    async def fake_summarize(db, **kwargs):
        received.update(kwargs)
        return SimpleNamespace(to_dict=lambda: expected)

    monkeypatch.setattr(job, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(job, "summarize_pending_news", fake_summarize)

    result = await job.run_news_summary_backfill(
        batch_size=2,
        market="us",
        feed_source="google_news",
    )

    assert result == expected
    assert received == {
        "batch_size": 2,
        "market": "us",
        "feed_source": "google_news",
        "generator": None,
    }


@pytest.mark.unit
def test_news_summary_task_is_registered_without_schedule() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, news_summary_tasks

    assert news_summary_tasks in TASKIQ_TASK_MODULES
    task = news_summary_tasks.summarize_news_task
    assert task.task_name == "news.articles.summarize"
    assert "schedule" not in task.labels
