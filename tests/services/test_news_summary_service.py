"""일반 뉴스 AI 요약의 검증, 영속화, 격리, 자동 수집 배선 테스트."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.schemas.news import NewsAnalysisResultResponse
from app.services import news_summary_service
from app.services.news_summary_service import (
    MAX_TRANSLATED_EXCERPT_CHARS,
    MAX_TRANSLATION_SOURCE_CHARS,
    GeneratedNewsSummary,
    NewsSummaryInput,
    OpenAiNewsSummaryGenerator,
    _summary_input_for,
    summarize_ingested_news,
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
    def __init__(
        self,
        outcomes: dict[str, str | BaseException],
        *,
        translations: dict[str, tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.translations = translations or {}
        self.calls: list[NewsSummaryInput] = []

    async def summarize(self, news: NewsSummaryInput) -> GeneratedNewsSummary:
        self.calls.append(news)
        outcome = self.outcomes[news.title]
        if isinstance(outcome, BaseException):
            raise outcome
        translated_title, translated_excerpt = self.translations.get(
            news.title, (None, None)
        )
        return GeneratedNewsSummary(
            summary=outcome,
            translated_title=translated_title,
            translated_excerpt=translated_excerpt,
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
                    "매출은 96,221백만으로 집계됐다."
                ),
                "translated_title": "회사의 분기 실적 발표",
                "translated_excerpt": (
                    "회사는 2026년 7월 26일에 끝난 기간의 분기 실적을 발표했다. "
                    "매출은 96,221백만이었다."
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
    assert result.translated_title == "회사의 분기 실적 발표"
    assert result.translated_excerpt is not None
    assert result.translated_excerpt.endswith("96,221백만이었다.")
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.confidence == 88
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning_effort"] == "low"
    assert call["schema_name"] == "kasset_news_summary"
    assert call["input_payload"] == news.to_payload()
    assert set(call["schema"]["required"]) == {
        "summary",
        "translated_title",
        "translated_excerpt",
        "sentiment",
        "confidence",
    }
    assert call["schema"]["additionalProperties"] is False
    assert (
        call["schema"]["properties"]["translated_excerpt"]["anyOf"][0]["maxLength"]
        == MAX_TRANSLATED_EXCERPT_CHARS
    )
    assert "2~4문장" in call["additional_instructions"]
    assert "투자 권유" in call["additional_instructions"]
    assert "핵심 사건과 주체" in call["additional_instructions"]
    assert "투자자 영향" in call["additional_instructions"]
    assert "translated_excerpt를 요약" in call["additional_instructions"]
    assert "숫자와 단위" in call["additional_instructions"]
    assert "범위 밖 사실" in call["additional_instructions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_accepts_equivalent_korean_scale_translation() -> None:
    response = {
        "summary": "회사가 실적을 발표했다. 매출은 50억 달러였다.",
        "translated_title": "회사 실적 발표",
        "translated_excerpt": "회사는 매출이 50억 달러라고 발표했다.",
        "sentiment": "neutral",
        "confidence": 85,
    }
    generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient([response]),
        model="gpt-5.6-luna",
    )

    result = await generator.summarize(
        NewsSummaryInput(
            title="Company reports results",
            source="Example Wire",
            article_content="The company reported results. Revenue was $5 billion.",
            raw_excerpt=None,
        )
    )

    assert result.summary == response["summary"]
    assert result.translated_excerpt == response["translated_excerpt"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_translates_foreign_title_when_body_is_missing() -> None:
    client = FakeResponsesClient(
        [
            {
                "summary": "엔비디아 실적 발표 이후 아시아 증시는 혼조세를 보였다.",
                "translated_title": "엔비디아 실적 발표 후 아시아 증시 혼조",
                "translated_excerpt": None,
                "sentiment": "neutral",
                "confidence": 72,
            },
            {
                "summary": "엔비디아 실적 발표 이후 아시아 증시는 혼조세를 보였다.",
                "translated_title": "엔비디아 실적 발표 후 아시아 증시 혼조",
                "translated_excerpt": "본문 없이 생성한 번역 발췌",
                "sentiment": "neutral",
                "confidence": 72,
            },
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
    assert result.translated_title == "엔비디아 실적 발표 후 아시아 증시 혼조"
    assert result.translated_excerpt is None
    assert "한국어 한 문장" in client.calls[0]["additional_instructions"]
    rejected = await generator.summarize(news)
    assert rejected.summary == result.summary
    assert rejected.translated_title == result.translated_title
    assert rejected.translated_excerpt is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_rejects_raw_copy_and_invented_number() -> None:
    raw_copy = (
        "회사는 신규 제품 출시 계획을 발표했다. 출시 일정은 시장 상황에 따라 정해진다."
    )
    copy_generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": raw_copy,
                    "translated_title": None,
                    "translated_excerpt": None,
                    "sentiment": "neutral",
                    "confidence": 70,
                }
            ]
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

    number_client = FakeResponsesClient(
        [
            {
                "summary": "회사는 신규 제품을 공개했다. 매출은 999억원으로 예상됐다.",
                "translated_title": None,
                "translated_excerpt": None,
                "sentiment": "positive",
                "confidence": 70,
            }
        ]
    )
    number_generator = OpenAiNewsSummaryGenerator(
        number_client,
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
    assert len(number_client.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_rejects_template_language_and_multi_sentence_title_fallback() -> (
    None
):
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
                    "translated_title": None,
                    "translated_excerpt": None,
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
                    "translated_title": "엔비디아 분기 실적 발표",
                    "translated_excerpt": None,
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


@pytest.mark.unit
def test_source_excerpt_caps_article_content_and_raw_excerpt_at_4000_characters() -> (
    None
):
    capped_source = ("word " * 799) + "endxy"
    source = capped_source + " FORBIDDEN TAIL"
    assert len(capped_source) == MAX_TRANSLATION_SOURCE_CHARS

    content_input = _summary_input_for(
        _article(
            url="https://news.test.invalid/source-cap/content",
            title="Company publishes a detailed market update",
            summary=None,
            article_content=source,
            published_at=datetime(2026, 8, 29, 12, 0),
        )
    )
    excerpt_input = _summary_input_for(
        _article(
            url="https://news.test.invalid/source-cap/excerpt",
            title="Company publishes another detailed market update",
            summary=source,
            published_at=datetime(2026, 8, 29, 12, 0),
        )
    )

    assert content_input is not None
    assert content_input.to_payload()["article_content"] == capped_source
    assert content_input.to_payload()["raw_excerpt"] is None
    assert excerpt_input is not None
    assert excerpt_input.to_payload()["article_content"] is None
    assert excerpt_input.to_payload()["raw_excerpt"] == capped_source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_enforces_6000_character_translation_boundary() -> None:
    client = FakeResponsesClient(
        [
            {
                "summary": (
                    "회사가 시장 전망을 발표했다. 경영진은 기존 계획을 유지했다."
                ),
                "translated_title": "회사의 시장 전망 발표",
                "translated_excerpt": "가" * MAX_TRANSLATED_EXCERPT_CHARS,
                "sentiment": "neutral",
                "confidence": 80,
            },
            {
                "summary": (
                    "회사가 시장 전망을 발표했다. 경영진은 기존 계획을 유지했다."
                ),
                "translated_title": "회사의 시장 전망 발표",
                "translated_excerpt": "가" * (MAX_TRANSLATED_EXCERPT_CHARS + 1),
                "sentiment": "neutral",
                "confidence": 80,
            },
        ]
    )
    generator = OpenAiNewsSummaryGenerator(client, model="gpt-5.6-luna")
    news = NewsSummaryInput(
        title="Company publishes its market outlook",
        source="Example Wire",
        article_content=(
            "The company published its market outlook and retained its existing plan "
            "after management reviewed current demand conditions."
        ),
        raw_excerpt=None,
    )

    accepted = await generator.summarize(news)

    assert accepted.translated_excerpt is not None
    assert len(accepted.translated_excerpt) == MAX_TRANSLATED_EXCERPT_CHARS
    rejected = await generator.summarize(news)
    assert rejected.summary
    assert rejected.translated_excerpt is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_discards_translation_for_korean_source() -> None:
    valid_response = {
        "summary": "회사가 시장 전망을 발표했다. 경영진은 기존 계획을 유지했다.",
        "translated_title": None,
        "translated_excerpt": None,
        "sentiment": "neutral",
        "confidence": 80,
    }
    invalid_response = {
        **valid_response,
        "translated_title": "회사의 시장 전망 발표",
    }
    client = FakeResponsesClient([valid_response, invalid_response])
    generator = OpenAiNewsSummaryGenerator(client, model="gpt-5.6-luna")
    news = NewsSummaryInput(
        title="회사의 시장 전망 발표",
        source="테스트 뉴스",
        article_content=(
            "회사는 시장 전망을 발표하고 기존 계획을 유지했다. "
            "경영진은 현재 수요 여건도 함께 설명했다."
        ),
        raw_excerpt=None,
    )

    accepted = await generator.summarize(news)

    assert accepted.translated_title is None
    assert accepted.translated_excerpt is None
    rejected = await generator.summarize(news)
    assert rejected.summary == valid_response["summary"]
    assert rejected.translated_title is None
    assert rejected.translated_excerpt is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_does_not_translate_korean_title_with_latin_brand_names() -> (
    None
):
    response = {
        "summary": "LG전자가 CES 참가 계획을 발표했다. 전시 제품군도 함께 소개했다.",
        "translated_title": None,
        "translated_excerpt": None,
        "sentiment": "neutral",
        "confidence": 80,
    }
    generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient([response]),
        model="gpt-5.6-luna",
    )

    result = await generator.summarize(
        NewsSummaryInput(
            title="LG전자 CES 참가",
            source="테스트 뉴스",
            article_content=(
                "LG전자는 CES 참가 계획을 발표했다. "
                "회사는 현장에서 공개할 전시 제품군도 함께 소개했다."
            ),
            raw_excerpt=None,
        )
    )

    assert result.summary == response["summary"]
    assert result.translated_title is None
    assert result.translated_excerpt is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_rejects_incomplete_structured_translation_shape() -> (
    None
):
    generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": (
                        "회사가 시장 전망을 발표했다. 경영진은 기존 계획을 유지했다."
                    ),
                    "translated_title": "회사의 시장 전망 발표",
                    "sentiment": "neutral",
                    "confidence": 80,
                }
            ]
        ),
        model="gpt-5.6-luna",
    )

    with pytest.raises(ValueError, match="response shape is invalid"):
        await generator.summarize(
            NewsSummaryInput(
                title="Company publishes its market outlook",
                source="Example Wire",
                article_content=(
                    "The company published its market outlook and retained its "
                    "existing plan after reviewing current demand conditions."
                ),
                raw_excerpt=None,
            )
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_body", "translated_excerpt"),
    [
        (
            "The company reported revenue of 96,221 million "
            "and maintained its outlook.",
            "회사는 매출을 보고하고 기존 전망을 유지했다.",
        ),
        (
            "The company reported revenue of 96,221 and maintained its outlook.",
            "회사는 매출 96,221억원을 보고하고 기존 전망을 유지했다.",
        ),
    ],
)
async def test_openai_generator_discards_translation_number_or_unit_drift(
    source_body: str,
    translated_excerpt: str,
) -> None:
    generator = OpenAiNewsSummaryGenerator(
        FakeResponsesClient(
            [
                {
                    "summary": (
                        "회사가 분기 매출을 발표했다. 경영진은 기존 전망을 유지했다."
                    ),
                    "translated_title": "회사 매출 보고서",
                    "translated_excerpt": translated_excerpt,
                    "sentiment": "neutral",
                    "confidence": 80,
                }
            ]
        ),
        model="gpt-5.6-luna",
    )

    result = await generator.summarize(
        NewsSummaryInput(
            title="Company revenue report",
            source="Example Wire",
            article_content=source_body,
            raw_excerpt=None,
        )
    )

    assert result.summary
    assert result.translated_excerpt is None


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
        title="Overseas company reports results",
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
            "Overseas company reports results": (
                "회사는 분기 영업 실적을 발표했다. 기존 가이던스도 유지했다."
            ),
            "공급 계약 발표": RuntimeError("fake provider failure"),
        },
        translations={
            "Overseas company reports results": (
                "해외 기업의 실적 발표",
                "회사는 분기 영업 실적을 보고하고 가이던스를 유지했다. "
                "경영진은 주력 시장의 수요 여건도 설명했다.",
            ),
            "공급 계약 발표": (
                None,
                "회사는 기존 고객과 공급 계약을 발표했다. "
                "납품 시기는 계약 조건의 적용을 받는다.",
            ),
        },
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
        select(NewsAnalysisResult).where(NewsAnalysisResult.article_id == success_id)
    )
    assert stored is not None
    assert stored.summary == (
        "회사는 분기 영업 실적을 발표했다. 기존 가이던스도 유지했다."
    )
    assert stored.model_name == "test-news-summary"
    assert stored.translated_title == "해외 기업의 실적 발표"
    assert stored.translated_excerpt == (
        "회사는 분기 영업 실적을 보고하고 가이던스를 유지했다. "
        "경영진은 주력 시장의 수요 여건도 설명했다."
    )
    analysis_wire = NewsAnalysisResultResponse.model_validate(stored).model_dump()
    assert analysis_wire["translated_title"] == stored.translated_title
    assert analysis_wire["translated_excerpt"] == stored.translated_excerpt
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
    assert [call.title for call in generator.calls].count(
        "Overseas company reports results"
    ) == 1
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



@pytest.mark.integration
@pytest.mark.asyncio
async def test_incomplete_foreign_analysis_is_reprocessed_until_translation_exists(
    db_session,
) -> None:
    suffix = uuid.uuid4().hex
    url = f"https://news.test.invalid/{suffix}/repair-translation"
    article = _article(
        url=url,
        title="Company announces semiconductor investment",
        summary="The company announced a new semiconductor investment plan.",
        published_at=datetime(2026, 8, 29, 12, 0),
    )
    db_session.add(article)
    await db_session.flush()
    db_session.add(
        NewsAnalysisResult(
            article_id=article.id,
            model_name="old-incomplete-analysis",
            sentiment=Sentiment.NEUTRAL,
            sentiment_score=None,
            summary="회사가 신규 반도체 투자 계획을 발표했다.",
            translated_title=None,
            translated_excerpt=None,
            key_points=[],
            topics=None,
            price_impact=None,
            price_impact_score=None,
            confidence=70,
            analysis_quality="high",
            prompt="old prompt",
            raw_response="{}",
            processing_time_ms=1,
            created_at=datetime(2026, 8, 29, 12, 1),
            updated_at=None,
        )
    )
    await db_session.commit()
    generator = FakeSummaryGenerator(
        {
            article.title: "회사가 신규 반도체 투자 계획을 발표했다.",
        },
        translations={
            article.title: (
                "회사의 반도체 투자 계획 발표",
                "회사는 신규 반도체 투자 계획을 발표했다.",
            )
        },
    )

    first = await summarize_pending_news(
        db_session,
        batch_size=1,
        article_urls=[url],
        generator=generator,
    )
    second = await summarize_pending_news(
        db_session,
        batch_size=1,
        article_urls=[url],
        generator=generator,
    )

    analyses = list(
        (
            await db_session.scalars(
                select(NewsAnalysisResult)
                .where(NewsAnalysisResult.article_id == article.id)
                .order_by(NewsAnalysisResult.created_at.asc())
            )
        ).all()
    )
    assert first.summarized == 1
    assert second.skipped_existing == 1
    assert len(generator.calls) == 1
    assert len(analyses) == 2
    assert analyses[-1].translated_title == "회사의 반도체 투자 계획 발표"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingested_news_summary_chunks_every_persisted_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    async def fake_pending(_db, *, article_urls, **_kwargs):
        urls = list(article_urls)
        calls.append(urls)
        return SimpleNamespace(
            selected=len(urls),
            summarized=len(urls),
            skipped_existing=0,
            skipped_insufficient=0,
            failed=0,
            status="success",
            failed_article_ids=(),
            skipped_article_ids=(),
        )

    monkeypatch.setattr(
        news_summary_service,
        "summarize_pending_news",
        fake_pending,
    )
    urls = [f"https://news.test.invalid/chunk/{index}" for index in range(45)]

    result = await summarize_ingested_news(object(), urls)

    assert [len(chunk) for chunk in calls] == [20, 20, 5]
    assert result.selected == 45
    assert result.summarized == 45
    assert result.status == "success"


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
def test_news_summary_task_is_registered_with_recurring_schedule() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, news_summary_tasks

    assert news_summary_tasks in TASKIQ_TASK_MODULES
    task = news_summary_tasks.summarize_news_task
    assert task.task_name == "news.articles.summarize"
    assert task.labels.get("schedule") == [
        {"cron": "*/5 * * * *", "cron_offset": "UTC"}
    ]


@pytest.mark.unit
def test_news_translation_migration_is_nullable_additive_and_reversible() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic/versions/20260830_news_translation.py"
    text = migration.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    downgrade = text.split("def downgrade()", 1)[1]

    assert 'revision = "20260830_news_translation"' in text
    assert 'down_revision = "20260830_kr_lifecycle_ca"' in text
    assert upgrade.count("op.add_column(") == 2
    assert 'sa.Column("translated_title", sa.Text(), nullable=True)' in upgrade
    assert 'sa.Column("translated_excerpt", sa.Text(), nullable=True)' in upgrade
    assert "server_default" not in upgrade
    assert 'op.drop_column("news_analysis_results", "translated_excerpt")' in downgrade
    assert 'op.drop_column("news_analysis_results", "translated_title")' in downgrade
