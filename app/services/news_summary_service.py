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
from app.extensions.kasset.ai.runtime_config import AiRuntimeSnapshot
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.services.ai_runtime_config import get_ai_runtime_snapshot
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
AUTO_SUMMARY_BATCH_SIZE = 20
MAX_TRANSLATION_SOURCE_CHARS = 4_000
MAX_TRANSLATED_TITLE_CHARS = 500
MAX_TRANSLATED_EXCERPT_CHARS = 6_000
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
        "translated_title": {
            "anyOf": [
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TRANSLATED_TITLE_CHARS,
                },
                {"type": "null"},
            ],
        },
        "translated_excerpt": {
            "anyOf": [
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TRANSLATED_EXCERPT_CHARS,
                },
                {"type": "null"},
            ],
        },
        "sentiment": {
            "type": "string",
            "enum": [member.value for member in Sentiment],
        },
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "summary",
        "translated_title",
        "translated_excerpt",
        "sentiment",
        "confidence",
    ],
    "additionalProperties": False,
}
_SUMMARY_INSTRUCTIONS = (
    "입력의 title, source, article_content 또는 raw_excerpt에 명시된 사실만 사용하라. "
    "summary는 번역문과 별도로 자연스러운 한국어 2~4문장으로 요약하라. 첫 문장에는 "
    "원문에 확인되는 핵심 사건과 주체를 쓰고, 수치·날짜·시장 반응 또는 투자자 영향은 "
    "원문에 명시된 항목만 후속 문장에 포함하라. translated_title은 title이 영문 우세일 "
    "때만 자연스러운 한국어 제목으로 번역하고, 아니면 null로 반환하라. "
    "translated_excerpt는 article_content 또는 raw_excerpt가 있고 그 본문이 영문 "
    "우세일 때만 제공된 최대 4,000자의 전체 범위를 문장 순서대로 한국어로 번역하라. "
    "translated_excerpt를 요약하거나 범위 밖 사실을 보완하지 말고, 본문이 없거나 영문 "
    "우세가 아니면 null로 반환하라. 모든 출력에서 원문의 숫자와 단위를 그대로 보존하고 "
    "계산, 환산, 추측, 사실 보완을 하지 마라. 원문에 없는 배경, 전망, 인과관계, "
    "투자 권유, 매수·매도 추천, 목표주가를 summary에 쓰지 마라. summary에 '이 기사는', "
    "'이번 소식은'처럼 내용 없는 서두나 항목 나열을 쓰지 말고, title이나 본문을 복제하지 "
    "말고 핵심 사실을 구체적으로 재서술하라. sentiment는 기사 서술의 정서만 positive, "
    "negative, neutral 중 하나로 분류하라."
)
_TITLE_ONLY_INSTRUCTIONS = (
    "article_content와 raw_excerpt가 모두 없으면 summary는 title에 명시된 주체와 사건만 "
    "자연스러운 한국어 한 문장으로 번역·재서술하라. 제목을 그대로 복사하지 말고 배경 설명, "
    "원인, 수치, 영향 또는 전망을 추가하지 마라. translated_excerpt는 null로 반환하라."
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(
    r"(?<!\d)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s*(?:%|percent(?:age)?s?|퍼센트))?",
    re.IGNORECASE,
)
_PERCENT_SUFFIX_RE = re.compile(
    r"(?:%|percent(?:age)?s?|퍼센트)$",
    re.IGNORECASE,
)
_SCALED_QUANTITY_RE = re.compile(
    r"(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<scale>thousands?(?![A-Za-z])|millions?(?![A-Za-z])|"
    r"billions?(?![A-Za-z])|trillions?(?![A-Za-z])|백만|십억|천|억|조)",
    re.IGNORECASE,
)
_SCALE_FACTORS: dict[str, Decimal] = {
    "thousand": Decimal("1000"),
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "trillion": Decimal("1000000000000"),
    "천": Decimal("1000"),
    "백만": Decimal("1000000"),
    "억": Decimal("100000000"),
    "십억": Decimal("1000000000"),
    "조": Decimal("1000000000000"),
}
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
_LATIN_HANGUL_BOUNDARY_RE = re.compile(
    r"(?<=[A-Za-z])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z])"
)
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
_UNIT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "percentage-point",
        re.compile(
            r"\bpercentage(?:-| )points?\b|퍼센트\s*포인트|%\s*(?:p|포인트)",
            re.IGNORECASE,
        ),
    ),
    (
        "basis-point",
        re.compile(
            r"\bbasis(?:-| )points?\b|\bbps?\b|베이시스\s*포인트",
            re.IGNORECASE,
        ),
    ),
    (
        "percent",
        re.compile(r"%|\bpercent(?:age)?s?\b|퍼센트", re.IGNORECASE),
    ),
    (
        "thousand",
        re.compile(
            r"\bthousands?\b|천(?=\s*(?:원|달러|유로|엔))",
            re.IGNORECASE,
        ),
    ),
    ("million", re.compile(r"\bmillions?\b|백만", re.IGNORECASE)),
    ("billion", re.compile(r"\bbillions?\b|십억", re.IGNORECASE)),
    (
        "trillion",
        re.compile(
            r"\btrillions?\b|조(?=\s*(?:원|달러|유로|엔))",
            re.IGNORECASE,
        ),
    ),
    ("hundred-million", re.compile(r"억(?=\s*(?:원|달러|유로|엔))")),
    (
        "usd",
        re.compile(
            r"US\$|\$|\bUSD\b|\bU\.S\.\s*dollars?\b|\bdollars?\b|달러",
            re.IGNORECASE,
        ),
    ),
    (
        "krw",
        re.compile(
            r"₩|\bKRW\b|\bwon\b|(?<![가-힣])(?:천|만|백만|억|십억|조)?\s*원(?![가-힣])",
            re.IGNORECASE,
        ),
    ),
    ("eur", re.compile(r"€|\bEUR\b|\beuros?\b|유로", re.IGNORECASE)),
    (
        "jpy",
        re.compile(
            r"¥|\bJPY\b|\byen\b|(?<![가-힣])엔(?![가-힣])",
            re.IGNORECASE,
        ),
    ),
    ("gbp", re.compile(r"£|\bGBP\b|\bpounds?\b|파운드", re.IGNORECASE)),
    ("barrel", re.compile(r"\bbarrels?\b|배럴", re.IGNORECASE)),
    (
        "metric-ton",
        re.compile(r"\bmetric(?:-| )tons?\b|메트릭\s*톤", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class NewsSummaryInput:
    title: str
    source: str | None
    article_content: str | None
    raw_excerpt: str | None

    @property
    def body(self) -> str:
        article_content = _bounded_source_text(self.article_content)
        if article_content:
            return article_content
        return _bounded_source_text(self.raw_excerpt)

    @property
    def source_text(self) -> str:
        return "\n".join(
            value for value in (self.title, self.source, self.body) if value
        )

    def to_payload(self) -> dict[str, object]:
        article_content = _bounded_source_text(self.article_content) or None
        raw_excerpt = (
            None
            if article_content is not None
            else _bounded_source_text(self.raw_excerpt) or None
        )
        return {
            "title": self.title,
            "source": self.source,
            "article_content": article_content,
            "raw_excerpt": raw_excerpt,
        }


@dataclass(frozen=True, slots=True)
class GeneratedNewsSummary:
    summary: str
    translated_title: str | None
    translated_excerpt: str | None
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


def _bounded_source_text(value: str | None) -> str:
    return _normalized_text((value or "")[:MAX_TRANSLATION_SOURCE_CHARS])


def _is_english_dominant(value: str | None) -> bool:
    normalized = value or ""
    return (
        _LATIN_RE.search(normalized) is not None
        and _HANGUL_RE.search(normalized) is None
    )


def _usable_body(value: str | None, *, title: str, source: str | None) -> str | None:
    normalized = _bounded_source_text(value)
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
    return normalized


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
    if not title or not _is_english_dominant(title):
        return None
    return NewsSummaryInput(
        title=title,
        source=source,
        article_content=None,
        raw_excerpt=None,
    )


def _normalized_number(raw: str) -> tuple[Decimal, bool] | None:
    compact = raw.replace(",", "").strip()
    is_percent = _PERCENT_SUFFIX_RE.search(compact) is not None
    if is_percent:
        compact = _PERCENT_SUFFIX_RE.sub("", compact)
    compact = compact.replace(" ", "")
    if compact.startswith("+"):
        compact = compact[1:]
    try:
        return Decimal(compact), is_percent
    except InvalidOperation:
        return None


def _normalize_scaled_quantities(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_scale = match.group("scale").casefold()
        scale = raw_scale[:-1] if raw_scale.endswith("s") else raw_scale
        number = Decimal(match.group("number").replace(",", ""))
        return format(number * _SCALE_FACTORS[scale], "f")

    return _SCALED_QUANTITY_RE.sub(replace, text)


def _number_set(text: str) -> set[tuple[Decimal, bool]]:
    numbers: set[tuple[Decimal, bool]] = set()
    for match in _NUMBER_RE.finditer(_normalize_scaled_quantities(text)):
        normalized = _normalized_number(match.group(0))
        if normalized is not None:
            numbers.add(normalized)
    return numbers


def _unit_set(text: str) -> set[str]:
    quantity_normalized = _normalize_scaled_quantities(text)
    boundary_normalized = _LATIN_HANGUL_BOUNDARY_RE.sub(" ", quantity_normalized)
    return {
        unit
        for unit, pattern in _UNIT_PATTERNS
        if pattern.search(boundary_normalized) is not None
    }


def _numbers_absent_from_source(
    generated_text: str,
    *,
    source_text: str,
) -> set[tuple[Decimal, bool]]:
    source_numbers = _number_set(source_text)
    return {
        number
        for number in _number_set(generated_text) - source_numbers
        if not _is_calendar_month_translation(
            number,
            summary=generated_text,
            source_text=source_text,
        )
    }


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

    if _numbers_absent_from_source(normalized, source_text=news.source_text):
        raise ValueError("news summary contains numbers absent from source")
    if _unit_set(normalized) - _unit_set(news.source_text):
        raise ValueError("news summary contains units absent from source")
    return normalized


def _validated_translation(
    value: object,
    *,
    source_text: str,
    field_name: str,
    max_chars: int,
) -> str | None:
    expects_translation = bool(source_text) and _is_english_dominant(source_text)
    if not expects_translation:
        if value is not None:
            raise ValueError(f"{field_name} must be null for non-English source")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string for English source")
    normalized = _normalized_text(value)
    if not normalized or len(normalized) > max_chars:
        raise ValueError(f"{field_name} length is invalid")
    if _HANGUL_RE.search(normalized) is None:
        raise ValueError(f"{field_name} must be written in Korean")
    if _numbers_absent_from_source(normalized, source_text=source_text):
        raise ValueError(f"{field_name} contains numbers absent from source")
    if _number_set(source_text) - _number_set(normalized):
        raise ValueError(f"{field_name} does not preserve source numbers")
    source_units = _unit_set(source_text)
    translated_units = _unit_set(normalized)
    if translated_units - source_units:
        raise ValueError(f"{field_name} contains units absent from source")
    if source_units - translated_units:
        raise ValueError(f"{field_name} does not preserve source units")
    return normalized


def _optional_validated_translation(
    value: object,
    *,
    source_text: str,
    field_name: str,
    max_chars: int,
) -> str | None:
    try:
        return _validated_translation(
            value,
            source_text=source_text,
            field_name=field_name,
            max_chars=max_chars,
        )
    except ValueError as exc:
        logger.warning(
            "Discarding invalid %s while preserving news summary: %s", field_name, exc
        )
        return None


def _validated_translations(
    *,
    translated_title: object,
    translated_excerpt: object,
    news: NewsSummaryInput,
) -> tuple[str | None, str | None]:
    return (
        _optional_validated_translation(
            translated_title,
            source_text=news.title,
            field_name="translated_title",
            max_chars=MAX_TRANSLATED_TITLE_CHARS,
        ),
        _optional_validated_translation(
            translated_excerpt,
            source_text=news.body,
            field_name="translated_excerpt",
            max_chars=MAX_TRANSLATED_EXCERPT_CHARS,
        ),
    )


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
        translated_title, translated_excerpt = _validated_translations(
            translated_title=response.get("translated_title"),
            translated_excerpt=response.get("translated_excerpt"),
            news=news,
        )
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
            translated_title=translated_title,
            translated_excerpt=translated_excerpt,
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
        if set(response) != {
            "summary",
            "translated_title",
            "translated_excerpt",
            "sentiment",
            "confidence",
        }:
            raise ValueError("news summary response shape is invalid")
        return response


def build_news_summary_generator(
    *,
    snapshot: AiRuntimeSnapshot | None = None,
) -> NewsSummaryGenerator | None:
    """``summary_luna`` 정책 순서로 일반 뉴스 요약 route를 만든다."""

    direct_model = settings.KASSET_AI_MODEL_LUNA.strip()
    fallback_model = settings.KASSET_AI_OPENROUTER_MODEL_FLASH.strip()
    client = build_summary_json_client(
        name="news-summary",
        direct_model=direct_model,
        fallback_model=fallback_model,
        snapshot=snapshot,
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
            translated_title, translated_excerpt = _validated_translations(
                translated_title=generated.translated_title,
                translated_excerpt=generated.translated_excerpt,
                news=news_input,
            )
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
                    translated_title=translated_title,
                    translated_excerpt=translated_excerpt,
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
    effective_generator = generator
    if effective_generator is None:
        # batch 시작 시 정책을 한 번만 읽는다. 기사마다 DB를 다시 읽지 않으므로
        # 이 batch 안에서는 route가 섞이지 않고, 다음 batch부터 새 정책이
        # 재시작 없이 적용된다.
        effective_generator = build_news_summary_generator(
            snapshot=await get_ai_runtime_snapshot(db),
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
    "MAX_TRANSLATED_EXCERPT_CHARS",
    "MAX_TRANSLATED_TITLE_CHARS",
    "MAX_TRANSLATION_SOURCE_CHARS",
    "GeneratedNewsSummary",
    "NewsSummaryBatchResult",
    "NewsSummaryGenerator",
    "NewsSummaryInput",
    "OpenAiNewsSummaryGenerator",
    "build_news_summary_generator",
    "summarize_ingested_news",
    "summarize_pending_news",
]
