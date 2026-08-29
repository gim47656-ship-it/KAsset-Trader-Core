"""일반 뉴스의 한국어 AI 번역·요약 생성과 영속 저장 서비스."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Literal, Protocol

from sqlalchemy import case, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.ai.base import StructuredJsonClient
from app.extensions.kasset.ai.factory import build_summary_json_client
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
AUTO_SUMMARY_BATCH_SIZE = 20
MAX_SOURCE_BODY_CHARS = 8_000
MIN_SOURCE_BODY_CHARS = 40
MIN_SOURCE_BODY_WORDS = 6
CANDIDATE_SCAN_MULTIPLIER = 10
AUTO_SUMMARY_CANDIDATE_LIMIT = 200
_SUMMARY_MAX_CHARS = 1_200

_SUMMARY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": _SUMMARY_MAX_CHARS,
        },
        "sentiment": {
            "type": "string",
            "enum": [member.value for member in Sentiment],
        },
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["summary", "sentiment", "confidence"],
    "additionalProperties": False,
}
_SUMMARY_INSTRUCTIONS = (
    "입력의 title, source, article_content 또는 raw_excerpt에 명시된 사실만 사용하라. "
    "외국어 기사는 자연스러운 한국어로 번역해 2~4문장으로 요약하라. 첫 문장에는 "
    "원문에 확인되는 핵심 사건과 주체를 쓰고, 수치·날짜·시장 반응 또는 투자자 영향은 "
    "원문에 명시된 항목만 후속 문장에 포함하라. 원문의 숫자와 단위를 그대로 보존하고 "
    "계산, 환산, 추측, 사실 보완을 하지 마라. 원문에 없는 배경, 전망, 인과관계, "
    "투자 권유, 매수·매도 추천, 목표주가를 쓰지 마라. '이 기사는', '이번 소식은'처럼 "
    "내용 없는 서두나 항목 나열을 쓰지 말고, title이나 raw_excerpt를 복제하지 말고 "
    "핵심 사실을 구체적으로 재서술하라. sentiment는 기사 서술의 정서만 positive, "
    "negative, neutral 중 하나로 분류하라."
)
_TITLE_ONLY_INSTRUCTIONS = (
    "article_content와 raw_excerpt가 모두 없으면 title에 명시된 주체와 사건만 자연스러운 "
    "한국어 한 문장으로 번역·재서술하라. 제목을 그대로 복사하지 말고 배경 설명, 원인, "
    "수치, 영향 또는 전망을 추가하지 마라."
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"(?<!\d)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*%)?")
_INVESTMENT_ADVICE_RE = re.compile(
    r"(?:매수|매도|투자)(?:를|가|는)?\s*(?:권고|권유|추천|해야)|"
    r"목표\s*주가|목표가"
)
_TEMPLATE_LANGUAGE_RE = re.compile(
    r"(?:이|본|해당)\s*(?:기사|뉴스)는|이번\s*소식은|"
    r"투자자(?:들은?|에게)는?\s*(?:주목|유의|관심)"
)
_HANGUL_RE = re.compile(r"[가-힣]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_ENGLISH_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


@dataclass(frozen=True, slots=True)
class NewsSummaryInput:
    title: str
    source: str | None
    article_content: str | None
    raw_excerpt: str | None

    @property
    def body(self) -> str:
        return self.article_content or self.raw_excerpt or ""

    @property
    def source_text(self) -> str:
        return "\n".join(
            value for value in (self.title, self.source, self.body) if value
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "source": self.source,
            "article_content": self.article_content,
            "raw_excerpt": self.raw_excerpt,
        }


@dataclass(frozen=True, slots=True)
class GeneratedNewsSummary:
    summary: str
    sentiment: Sentiment
    confidence: int
    model_name: str
    prompt: str
    raw_response: str


@dataclass(frozen=True, slots=True)
class NewsSummaryBatchResult:
    status: Literal["success", "partial", "failed", "unconfigured"]
    selected: int
    summarized: int
    skipped_existing: int
    skipped_insufficient: int
    failed: int
    failed_article_ids: tuple[int, ...] = ()
    skipped_article_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class NewsSummaryGenerator(Protocol):
    async def summarize(self, news: NewsSummaryInput) -> GeneratedNewsSummary: ...


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _usable_body(value: str | None, *, title: str, source: str | None) -> str | None:
    normalized = _normalized_text(value)
    if not normalized:
        return None
    comparable = normalized.casefold()
    title_key = _normalized_text(title).casefold()
    source_key = _normalized_text(source).casefold()
    duplicates = {title_key}
    if source_key:
        duplicates.update(
            {
                f"{title_key} - {source_key}",
                f"{title_key} {source_key}",
            }
        )
    if comparable in duplicates:
        return None
    if len(normalized) < MIN_SOURCE_BODY_CHARS:
        return None
    if len(normalized.split()) < MIN_SOURCE_BODY_WORDS:
        return None
    return normalized[:MAX_SOURCE_BODY_CHARS]


def _summary_input_for(article: NewsArticle) -> NewsSummaryInput | None:
    title = _normalized_text(article.title)
    source = _normalized_text(article.source) or None
    content = _usable_body(
        article.article_content,
        title=title,
        source=source,
    )
    if content is not None:
        return NewsSummaryInput(
            title=title,
            source=source,
            article_content=content,
            raw_excerpt=None,
        )
    excerpt = _usable_body(
        article.summary,
        title=title,
        source=source,
    )
    if excerpt is not None:
        return NewsSummaryInput(
            title=title,
            source=source,
            article_content=None,
            raw_excerpt=excerpt,
        )
    if (
        not title
        or _HANGUL_RE.search(title) is not None
        or _LATIN_RE.search(title) is None
    ):
        return None
    return NewsSummaryInput(
        title=title,
        source=source,
        article_content=None,
        raw_excerpt=None,
    )


def _normalized_number(raw: str) -> tuple[Decimal, bool] | None:
    compact = raw.replace(",", "").replace(" ", "")
    is_percent = compact.endswith("%")
    if is_percent:
        compact = compact[:-1]
    if compact.startswith("+"):
        compact = compact[1:]
    try:
        return Decimal(compact), is_percent
    except InvalidOperation:
        return None


def _number_set(text: str) -> set[tuple[Decimal, bool]]:
    numbers: set[tuple[Decimal, bool]] = set()
    for match in _NUMBER_RE.finditer(text):
        normalized = _normalized_number(match.group(0))
        if normalized is not None:
            numbers.add(normalized)
    return numbers


def _is_calendar_month_translation(
    number: tuple[Decimal, bool],
    *,
    summary: str,
    source_text: str,
) -> bool:
    value, is_percent = number
    if is_percent or value != value.to_integral_value():
        return False
    month_number = int(value)
    if not 1 <= month_number <= len(_ENGLISH_MONTHS):
        return False
    if re.search(rf"(?<!\d){month_number}\s*월", summary) is None:
        return False
    month_name = _ENGLISH_MONTHS[month_number - 1]
    return re.search(rf"\b{month_name}\b", source_text, re.IGNORECASE) is not None


def _comparison_key(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


def _duplicates_title(sentence: str, title: str) -> bool:
    sentence_key = _comparison_key(sentence)
    title_key = _comparison_key(title)
    if len(title_key) < 8:
        return sentence_key == title_key
    return SequenceMatcher(None, sentence_key, title_key).ratio() >= 0.94


def _validated_summary(summary: object, news: NewsSummaryInput) -> str:
    if not isinstance(summary, str):
        raise ValueError("news summary must be a string")
    normalized = _normalized_text(summary)
    if not normalized or len(normalized) > _SUMMARY_MAX_CHARS:
        raise ValueError("news summary length is invalid")
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(normalized)
        if sentence.strip()
    ]
    minimum_sentences = 2 if news.body else 1
    maximum_sentences = 4 if news.body else 1
    if not minimum_sentences <= len(sentences) <= maximum_sentences:
        raise ValueError(
            "news summary must contain "
            f"{minimum_sentences} to {maximum_sentences} sentences"
        )
    if _HANGUL_RE.search(normalized) is None:
        raise ValueError("news summary must be written in Korean")
    if _INVESTMENT_ADVICE_RE.search(normalized):
        raise ValueError("news summary contains investment advice")
    if _TEMPLATE_LANGUAGE_RE.search(normalized):
        raise ValueError("news summary contains template language")
    if normalized.casefold() in {
        _normalized_text(news.title).casefold(),
        _normalized_text(news.body).casefold(),
    } or any(_duplicates_title(sentence, news.title) for sentence in sentences):
        raise ValueError("news summary duplicates raw input")

    source_numbers = _number_set(news.source_text)
    generated_numbers = _number_set(normalized)
    invented_numbers = {
        number
        for number in generated_numbers - source_numbers
        if not _is_calendar_month_translation(
            number,
            summary=normalized,
            source_text=news.source_text,
        )
    }
    if invented_numbers:
        raise ValueError("news summary contains numbers absent from source")
    return normalized


def _validated_sentiment(value: object) -> Sentiment:
    try:
        return Sentiment(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("news summary sentiment is invalid") from exc


def _validated_confidence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("news summary confidence is invalid")
    return value


def _instructions_for(news: NewsSummaryInput) -> str:
    if news.body:
        return _SUMMARY_INSTRUCTIONS
    return f"{_SUMMARY_INSTRUCTIONS} {_TITLE_ONLY_INSTRUCTIONS}"


class OpenAiNewsSummaryGenerator:
    """Generate news summaries through the common structured JSON transport."""

    def __init__(self, client: StructuredJsonClient, *, model: str) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("news summary model is required")
        self._client = client
        self._model = normalized_model

    async def summarize(self, news: NewsSummaryInput) -> GeneratedNewsSummary:
        response = await self._request(news)
        summary = _validated_summary(response.get("summary"), news)
        sentiment = _validated_sentiment(response.get("sentiment"))
        confidence = _validated_confidence(response.get("confidence"))
        prompt = json.dumps(
            {
                "instructions": _instructions_for(news),
                "input": news.to_payload(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return GeneratedNewsSummary(
            summary=summary,
            sentiment=sentiment,
            confidence=confidence,
            model_name=str(getattr(response, "model_name", self._model)),
            prompt=prompt,
            raw_response=json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    async def _request(self, news: NewsSummaryInput) -> dict[str, object]:
        response = await self._client.request_json(
            model=self._model,
            input_payload=news.to_payload(),
            reasoning_effort="low",
            schema_name="kasset_news_summary",
            schema=_SUMMARY_SCHEMA,
            additional_instructions=_instructions_for(news),
        )
        if set(response) != {"summary", "sentiment", "confidence"}:
            raise ValueError("news summary response shape is invalid")
        return response


def build_news_summary_generator() -> NewsSummaryGenerator | None:
    """direct API -> OpenRouter 일반 뉴스 요약 route를 만든다."""

    direct_model = settings.KASSET_AI_MODEL_LUNA.strip()
    fallback_model = settings.KASSET_AI_OPENROUTER_MODEL_FLASH.strip()
    client = build_summary_json_client(
        name="news-summary",
        direct_model=direct_model,
        fallback_model=fallback_model,
    )
    if client is None:
        return None
    return OpenAiNewsSummaryGenerator(
        client,
        model=direct_model or fallback_model,
    )


def _validate_scope(
    batch_size: int,
    market: str | None,
    feed_source: str | None,
) -> None:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if market is not None and market not in {"kr", "us", "crypto"}:
        raise ValueError("market must be one of: kr, us, crypto")
    if feed_source is not None:
        if not feed_source.strip():
            raise ValueError("feed_source must not be blank")
        if feed_source in DISCLOSURE_FEED_SOURCES:
            raise ValueError("feed_source must select ordinary news, not disclosures")


async def _candidate_ids(
    db: AsyncSession,
    *,
    batch_size: int,
    market: str | None,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
) -> list[int]:
    if article_urls is not None and not article_urls:
        return []
    statement = (
        select(NewsArticle.id)
        .where(
            or_(
                NewsArticle.feed_source.is_(None),
                NewsArticle.feed_source.not_in(DISCLOSURE_FEED_SOURCES),
            ),
            ~exists().where(NewsAnalysisResult.article_id == NewsArticle.id),
        )
        .order_by(
            case(
                (NewsArticle.article_content.is_not(None), 0),
                (NewsArticle.summary.is_not(None), 1),
                else_=2,
            ),
            NewsArticle.article_published_at.desc().nullslast(),
            NewsArticle.id.desc(),
        )
        .limit(batch_size * CANDIDATE_SCAN_MULTIPLIER)
    )
    if market is not None:
        statement = statement.where(NewsArticle.market == market)
    if feed_source is not None:
        statement = statement.where(NewsArticle.feed_source == feed_source)
    if article_urls is not None:
        statement = statement.where(
            NewsArticle.url.in_(tuple(dict.fromkeys(article_urls)))
        )
    return list((await db.scalars(statement)).all())


def _batch_status(
    *,
    summarized: int,
    skipped_existing: int,
    skipped_insufficient: int,
    failed: int,
) -> Literal["success", "partial", "failed"]:
    if failed == 0:
        return "success"
    if summarized > 0 or skipped_existing > 0 or skipped_insufficient > 0:
        return "partial"
    return "failed"


def _analysis_quality(confidence: int) -> str:
    if confidence >= 80:
        return "high"
    if confidence >= 50:
        return "medium"
    return "low"


async def _run_batch(
    db: AsyncSession,
    *,
    batch_size: int,
    market: str | None,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
    generator: NewsSummaryGenerator,
) -> NewsSummaryBatchResult:
    candidate_ids = await _candidate_ids(
        db,
        batch_size=batch_size,
        market=market,
        feed_source=feed_source,
        article_urls=article_urls,
    )
    await db.commit()

    summarized = 0
    skipped_existing = 0
    skipped_ids: list[int] = []
    failed_ids: list[int] = []
    processed = 0
    attempted = 0
    for article_id in candidate_ids:
        try:
            article = await db.scalar(
                select(NewsArticle)
                .where(NewsArticle.id == article_id)
                .with_for_update(skip_locked=True)
            )
            if article is None:
                processed += 1
                skipped_existing += 1
                await db.rollback()
                continue
            existing_id = await db.scalar(
                select(NewsAnalysisResult.id)
                .where(NewsAnalysisResult.article_id == article_id)
                .limit(1)
            )
            if existing_id is not None:
                processed += 1
                skipped_existing += 1
                await db.rollback()
                continue
            news_input = _summary_input_for(article)
            if news_input is None:
                skipped_ids.append(article_id)
                processed += 1
                logger.info(
                    "일반 뉴스 요약 입력 부족으로 스킵: article_id=%d", article_id
                )
                await db.rollback()
                continue
            if attempted >= batch_size:
                await db.rollback()
                break
            processed += 1
            attempted += 1

            started = time.monotonic()
            generated = await generator.summarize(news_input)
            summary = _validated_summary(generated.summary, news_input)
            sentiment = _validated_sentiment(generated.sentiment)
            confidence = _validated_confidence(generated.confidence)
            elapsed_ms = int((time.monotonic() - started) * 1_000)
            now = _utcnow()
            db.add(
                NewsAnalysisResult(
                    article_id=article.id,
                    model_name=generated.model_name,
                    sentiment=sentiment,
                    sentiment_score=None,
                    summary=summary,
                    key_points=[
                        sentence.strip()
                        for sentence in _SENTENCE_SPLIT_RE.split(summary)
                        if sentence.strip()
                    ],
                    topics=None,
                    price_impact=None,
                    price_impact_score=None,
                    confidence=confidence,
                    analysis_quality=_analysis_quality(confidence),
                    prompt=generated.prompt,
                    raw_response=generated.raw_response,
                    processing_time_ms=elapsed_ms,
                    created_at=now,
                    updated_at=None,
                )
            )
            article.is_analyzed = True
            article.updated_at = now
            await db.commit()
            summarized += 1
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            failed_ids.append(article_id)
            logger.warning(
                "일반 뉴스 요약 행 실패: article_id=%d error_type=%s",
                article_id,
                type(exc).__name__,
            )

    failed = len(failed_ids)
    return NewsSummaryBatchResult(
        status=_batch_status(
            summarized=summarized,
            skipped_existing=skipped_existing,
            skipped_insufficient=len(skipped_ids),
            failed=failed,
        ),
        selected=processed,
        summarized=summarized,
        skipped_existing=skipped_existing,
        skipped_insufficient=len(skipped_ids),
        failed=failed,
        failed_article_ids=tuple(failed_ids),
        skipped_article_ids=tuple(skipped_ids),
    )


async def summarize_pending_news(
    db: AsyncSession,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    market: str | None = None,
    feed_source: str | None = None,
    article_urls: Sequence[str] | None = None,
    generator: NewsSummaryGenerator | None = None,
) -> NewsSummaryBatchResult:
    """미요약 일반 뉴스를 제한 batch로 처리하며 각 행을 독립 커밋한다."""

    _validate_scope(batch_size, market, feed_source)
    effective_generator = (
        generator if generator is not None else build_news_summary_generator()
    )
    if effective_generator is None:
        return NewsSummaryBatchResult(
            status="unconfigured",
            selected=0,
            summarized=0,
            skipped_existing=0,
            skipped_insufficient=0,
            failed=0,
        )
    return await _run_batch(
        db,
        batch_size=batch_size,
        market=market,
        feed_source=feed_source,
        article_urls=article_urls,
        generator=effective_generator,
    )


async def summarize_ingested_news(
    db: AsyncSession,
    article_urls: Sequence[str],
) -> NewsSummaryBatchResult:
    """한 수집 회차의 최신 일반 뉴스만 비용 상한 안에서 자동 요약한다."""

    bounded_urls = tuple(dict.fromkeys(article_urls))[:AUTO_SUMMARY_CANDIDATE_LIMIT]
    return await summarize_pending_news(
        db,
        batch_size=AUTO_SUMMARY_BATCH_SIZE,
        article_urls=bounded_urls,
    )


__all__ = [
    "AUTO_SUMMARY_CANDIDATE_LIMIT",
    "AUTO_SUMMARY_BATCH_SIZE",
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "GeneratedNewsSummary",
    "NewsSummaryBatchResult",
    "NewsSummaryGenerator",
    "NewsSummaryInput",
    "OpenAiNewsSummaryGenerator",
    "build_news_summary_generator",
    "summarize_ingested_news",
    "summarize_pending_news",
]
