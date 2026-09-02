"""일반 뉴스의 한국어 AI 번역·요약 생성과 영속 저장 서비스."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Literal, Protocol

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.ai.base import StructuredJsonClient
from app.extensions.kasset.ai.factory import build_summary_json_client
from app.extensions.kasset.ai.runtime_config import AiRuntimeSnapshot
from app.models.ai_call_events import AiCallEvent
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment
from app.services.ai_runtime_config import get_ai_runtime_snapshot
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES
from app.services.google_news_rss import DEFAULT_EXCLUDED_SOURCES
from app.services.market_news_noise import classify_pre_summary_noise, noise_reason

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
NEWS_SUMMARY_ARTICLES_PER_CALL = 10
NEWS_SUMMARY_DAILY_CALL_LIMIT = settings.KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT
NEWS_SUMMARY_RETRY_BACKOFF = timedelta(hours=6)
_NEWS_SUMMARY_FEATURE = "kasset_news_summary"
_NEWS_SUMMARY_DAILY_LIMIT_LOCK_KEY = "kasset:news-summary:daily-call-limit"
MAX_TRANSLATION_SOURCE_CHARS = 4_000
MAX_TRANSLATED_TITLE_CHARS = 500
MAX_TRANSLATED_EXCERPT_CHARS = 6_000
MIN_SOURCE_BODY_CHARS = 40
MIN_SOURCE_BODY_WORDS = 6
CANDIDATE_SCAN_MULTIPLIER = 10
#: 제목 잡음(``classify_pre_summary_noise``)은 SQL 술어로 옮길 수 없어 gate 탈락이
#: 한 page를 통째로 채울 수 있다. 그때 스캔을 멈추지 않고 다음 page를 이어 읽되,
#: page 수를 고정 상한으로 묶어 무한 스캔과 무한 루프를 함께 막는다.
CANDIDATE_SCAN_MAX_PAGES = 5
_SUMMARY_MAX_CHARS = 1_200
#: 신뢰할 수 없는 출처 정책은 수집 시점(``google_news_rss``)과 하나를 공유한다.
#: 정책 도입 전에 저장된 행이나 ``excluded_sources``를 덮어쓴 수집 회차의 행이
#: 모델 호출까지 도달하지 않도록 AI 경계에서 같은 목록을 다시 적용한다.
_EXCLUDED_SOURCE_KEYS: frozenset[str] = frozenset(
    value.casefold() for value in DEFAULT_EXCLUDED_SOURCES
)
#: 같은 목록의 SQL 비교용 표현. PostgreSQL ``lower()`` 결과와 맞춘다.
_EXCLUDED_SOURCE_SQL_KEYS: tuple[str, ...] = tuple(
    sorted(value.lower() for value in DEFAULT_EXCLUDED_SOURCES)
)

_SUMMARY_ITEM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "index": {
            "type": "integer",
            "minimum": 0,
            "maximum": NEWS_SUMMARY_ARTICLES_PER_CALL - 1,
        },
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
        "index",
        "summary",
        "translated_title",
        "translated_excerpt",
        "sentiment",
        "confidence",
    ],
    "additionalProperties": False,
}
_SUMMARY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": NEWS_SUMMARY_ARTICLES_PER_CALL,
            "items": _SUMMARY_ITEM_SCHEMA,
        }
    },
    "required": ["items"],
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
_BATCH_SUMMARY_INSTRUCTIONS = (
    "입력 items의 각 index를 변경하지 말고, 입력마다 출력 items 항목을 정확히 하나씩 "
    "같은 index로 반환하라. 항목을 합치거나 생략하거나 중복하지 마라. "
    f"{_SUMMARY_INSTRUCTIONS} {_TITLE_ONLY_INSTRUCTIONS}"
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
    status: Literal["success", "partial", "failed", "unconfigured", "daily_limit"]
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
    async def summarize_batch(
        self,
        news_items: Sequence[NewsSummaryInput],
    ) -> dict[int, GeneratedNewsSummary]: ...


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


async def _daily_model_call_count(db: AsyncSession) -> int:
    """UTC 당일 일반 뉴스 요약 provider attempt 수를 원장에서 센다."""

    day_start = _utcnow().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=UTC,
    )
    count = await db.scalar(
        select(func.count())
        .select_from(AiCallEvent)
        .where(
            AiCallEvent.feature == _NEWS_SUMMARY_FEATURE,
            AiCallEvent.started_at >= day_start,
            AiCallEvent.started_at < day_start + timedelta(days=1),
        )
    )
    return int(count or 0)


async def _daily_call_limit_reached(db: AsyncSession) -> bool:
    return await _daily_model_call_count(db) >= NEWS_SUMMARY_DAILY_CALL_LIMIT


async def _lock_daily_call_budget(db: AsyncSession) -> bool:
    """동시 worker를 직렬화하고 transaction 안에서 남은 일일 예산을 확인한다."""

    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(_NEWS_SUMMARY_DAILY_LIMIT_LOCK_KEY, 0)
            )
        )
    )
    return not await _daily_call_limit_reached(db)


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


class OpenAiNewsSummaryGenerator:
    """공통 structured JSON transport로 일반 뉴스 요약 batch를 생성한다."""

    def __init__(self, client: StructuredJsonClient, *, model: str) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("news summary model is required")
        self._client = client
        self._model = normalized_model

    async def summarize_batch(
        self,
        news_items: Sequence[NewsSummaryInput],
    ) -> dict[int, GeneratedNewsSummary]:
        response = await self._request(news_items)
        raw_items = response["items"]
        if not isinstance(raw_items, list):
            raise ValueError("news summary response items must be an array")

        items_by_index: dict[int, list[dict[str, object]]] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            index = raw_item.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(news_items)
            ):
                continue
            items_by_index.setdefault(index, []).append(raw_item)

        expected_keys = {
            "index",
            "summary",
            "translated_title",
            "translated_excerpt",
            "sentiment",
            "confidence",
        }
        model_name = str(getattr(response, "model_name", self._model))
        generated: dict[int, GeneratedNewsSummary] = {}
        for index, news in enumerate(news_items):
            candidates = items_by_index.get(index, [])
            if len(candidates) != 1:
                if len(candidates) > 1:
                    logger.warning(
                        "일반 뉴스 요약 batch 응답 index 중복: index=%d count=%d",
                        index,
                        len(candidates),
                    )
                continue
            item = candidates[0]
            if set(item) != expected_keys:
                logger.warning(
                    "일반 뉴스 요약 batch 항목 shape 오류: index=%d",
                    index,
                )
                continue
            try:
                summary = _validated_summary(item.get("summary"), news)
                translated_title, translated_excerpt = _validated_translations(
                    translated_title=item.get("translated_title"),
                    translated_excerpt=item.get("translated_excerpt"),
                    news=news,
                )
                sentiment = _validated_sentiment(item.get("sentiment"))
                confidence = _validated_confidence(item.get("confidence"))
            except ValueError as exc:
                logger.warning(
                    "일반 뉴스 요약 batch 항목 검증 실패: index=%d error_type=%s",
                    index,
                    type(exc).__name__,
                )
                continue
            prompt = json.dumps(
                {
                    "instructions": _BATCH_SUMMARY_INSTRUCTIONS,
                    "input": {"items": [{"index": index, **news.to_payload()}]},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            generated[index] = GeneratedNewsSummary(
                summary=summary,
                translated_title=translated_title,
                translated_excerpt=translated_excerpt,
                sentiment=sentiment,
                confidence=confidence,
                model_name=model_name,
                prompt=prompt,
                raw_response=json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        return generated

    async def _request(
        self,
        news_items: Sequence[NewsSummaryInput],
    ) -> dict[str, object]:
        if not 1 <= len(news_items) <= NEWS_SUMMARY_ARTICLES_PER_CALL:
            raise ValueError(
                "news summary batch size must be between 1 and "
                f"{NEWS_SUMMARY_ARTICLES_PER_CALL}"
            )
        response = await self._client.request_json(
            model=self._model,
            input_payload={
                "items": [
                    {"index": index, **news.to_payload()}
                    for index, news in enumerate(news_items)
                ]
            },
            reasoning_effort="low",
            schema_name=_NEWS_SUMMARY_FEATURE,
            schema=_SUMMARY_SCHEMA,
            additional_instructions=_BATCH_SUMMARY_INSTRUCTIONS,
        )
        if set(response) != {"items"} or not isinstance(response.get("items"), list):
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


def complete_korean_analysis_conditions():
    return (
        NewsAnalysisResult.article_id == NewsArticle.id,
        NewsAnalysisResult.summary.op("~")(_HANGUL_RE.pattern),
        or_(
            NewsArticle.title.op("~")(_HANGUL_RE.pattern),
            NewsAnalysisResult.translated_title.op("~")(_HANGUL_RE.pattern),
        ),
    )


def complete_korean_analysis_exists():
    return exists().where(*complete_korean_analysis_conditions())


@dataclass(frozen=True, slots=True)
class _GatedCandidates:
    """결정론 gate가 나눈 후보. ``rejected``는 generator를 보지 않는다."""

    admitted: tuple[int, ...]
    rejected: tuple[int, ...]


def _ai_rejection_reason(*, title: str, source: str) -> str | None:
    """AI 호출을 거부할 결정론 사유. 통과하면 ``None``을 돌려준다."""

    if not title:
        return "content:missing_title"
    if source.casefold() in _EXCLUDED_SOURCE_KEYS:
        return "source:excluded"
    noise = classify_pre_summary_noise(title)
    if noise:
        return noise_reason(noise)
    return None


class _CandidateGate:
    """출처·품질·중복을 AI 호출 전에 판정해 후보를 둘로 나눈다.

    ``consume``이 받는 행의 순서는 후보 질의의 순서(본문 풍부함 → 최신 게시 →
    최신 id)를 그대로 따르므로, 같은 (제목, 출처) 중복 중에서는 본문이 가장
    풍부하고 최신인 한 건만 모델을 본다. 상태는 page를 가로질러 유지되므로 page
    경계가 중복 판정을 갈라놓지 않는다.
    """

    __slots__ = ("_admitted", "_rejected", "_reasons", "_seen")

    def __init__(self) -> None:
        self._admitted: list[int] = []
        self._rejected: list[int] = []
        self._reasons: Counter[str] = Counter()
        self._seen: set[tuple[str, str]] = set()

    @property
    def admitted_count(self) -> int:
        return len(self._admitted)

    def consume(self, rows: Iterable[tuple[int, str | None, str | None]]) -> None:
        for article_id, raw_title, raw_source in rows:
            title = _normalized_text(raw_title)
            source = _normalized_text(raw_source)
            reason = _ai_rejection_reason(title=title, source=source)
            if reason is None:
                key = (_comparison_key(title), _comparison_key(source))
                if key in self._seen:
                    reason = "duplicate:title_source"
                else:
                    self._seen.add(key)
            if reason is not None:
                self._rejected.append(article_id)
                self._reasons[reason] += 1
                continue
            self._admitted.append(article_id)

    def finish(self) -> _GatedCandidates:
        """스캔 1회의 최종 판정. 탈락 집계는 여기서 한 번만 남긴다."""

        if self._rejected:
            logger.info(
                "일반 뉴스 요약 AI 호출 전 배제: rejected=%d admitted=%d reasons=%s",
                len(self._rejected),
                len(self._admitted),
                dict(sorted(self._reasons.items())),
            )
        return _GatedCandidates(
            admitted=tuple(self._admitted),
            rejected=tuple(self._rejected),
        )


def _sql_expressible_gate_conditions():
    """결정론 gate 중 SQL로 표현되는 배제를 ``LIMIT`` 이전으로 끌어올린다.

    이 두 사유(``content:missing_title``·``source:excluded``)는 영속 흔적을 전혀
    남기지 않아 backoff에도, 완료 분석 조건에도 걸리지 않는다. 스캔 창 안에
    남겨두면 매 회차 같은 행이 창을 영구 점유해 신규 후보를 밀어낸다.

    ``_ai_rejection_reason``이 여전히 최종 판정자다. Python 정규화가 유니코드
    공백까지 접고 ``casefold``를 쓰므로 여기의 술어는 의도적으로 더 느슨하다 —
    Python gate가 반드시 탈락시킬 행만 지운다.
    """

    return (
        func.btrim(func.coalesce(NewsArticle.title, "")) != "",
        func.lower(func.btrim(func.coalesce(NewsArticle.source, ""))).not_in(
            _EXCLUDED_SOURCE_SQL_KEYS
        ),
    )


async def _scan_candidates(
    db: AsyncSession,
    *,
    batch_size: int,
    market: str | None,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
) -> _GatedCandidates:
    if article_urls is not None and not article_urls:
        return _GatedCandidates(admitted=(), rejected=())
    retry_cutoff = _utcnow() - NEWS_SUMMARY_RETRY_BACKOFF
    recent_incomplete_attempt = exists().where(
        NewsAnalysisResult.article_id == NewsArticle.id,
        func.coalesce(
            NewsAnalysisResult.updated_at,
            NewsAnalysisResult.created_at,
        )
        >= retry_cutoff,
    )
    statement = (
        select(NewsArticle.id, NewsArticle.title, NewsArticle.source)
        .where(
            or_(
                NewsArticle.feed_source.is_(None),
                NewsArticle.feed_source.not_in(DISCLOSURE_FEED_SOURCES),
            ),
            ~complete_korean_analysis_exists(),
            ~recent_incomplete_attempt,
            *_sql_expressible_gate_conditions(),
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
    )
    if market is not None:
        statement = statement.where(NewsArticle.market == market)
    if feed_source is not None:
        statement = statement.where(NewsArticle.feed_source == feed_source)
    if article_urls is not None:
        statement = statement.where(
            NewsArticle.url.in_(tuple(dict.fromkeys(article_urls)))
        )

    # SQL로 옮길 수 없는 제목 잡음은 여전히 창을 채울 수 있다. batch를 채울 만큼
    # 통과 후보가 모이거나 적격 모집단이 소진될 때까지만 page를 이어 읽는다.
    page_size = batch_size * CANDIDATE_SCAN_MULTIPLIER
    gate = _CandidateGate()
    for page in range(CANDIDATE_SCAN_MAX_PAGES):
        rows = (
            await db.execute(statement.limit(page_size).offset(page * page_size))
        ).all()
        gate.consume((row.id, row.title, row.source) for row in rows)
        if gate.admitted_count >= batch_size or len(rows) < page_size:
            break
    return gate.finish()


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


@dataclass(frozen=True, slots=True)
class _PreparedNewsSummary:
    article_id: int
    news: NewsSummaryInput


async def _has_complete_analysis(db: AsyncSession, article_id: int) -> bool:
    analysis_id = await db.scalar(
        select(NewsAnalysisResult.id)
        .join(NewsArticle, NewsArticle.id == NewsAnalysisResult.article_id)
        .where(
            NewsArticle.id == article_id,
            *complete_korean_analysis_conditions(),
        )
        .limit(1)
    )
    return analysis_id is not None


async def _latest_analysis(
    db: AsyncSession,
    article_id: int,
) -> NewsAnalysisResult | None:
    return await db.scalar(
        select(NewsAnalysisResult)
        .where(NewsAnalysisResult.article_id == article_id)
        .order_by(
            func.coalesce(
                NewsAnalysisResult.updated_at,
                NewsAnalysisResult.created_at,
            ).desc(),
            NewsAnalysisResult.id.desc(),
        )
        .limit(1)
    )


async def _persist_generated_summary(
    db: AsyncSession,
    *,
    prepared: _PreparedNewsSummary,
    generated: GeneratedNewsSummary,
    elapsed_ms: int,
) -> bool:
    article = await db.scalar(
        select(NewsArticle)
        .where(NewsArticle.id == prepared.article_id)
        .with_for_update(skip_locked=True)
    )
    if article is None or await _has_complete_analysis(db, prepared.article_id):
        await db.rollback()
        return False

    summary = _validated_summary(generated.summary, prepared.news)
    translated_title, translated_excerpt = _validated_translations(
        translated_title=generated.translated_title,
        translated_excerpt=generated.translated_excerpt,
        news=prepared.news,
    )
    sentiment = _validated_sentiment(generated.sentiment)
    confidence = _validated_confidence(generated.confidence)
    repair_target = await _latest_analysis(db, prepared.article_id)
    now = _utcnow()
    analysis = repair_target or NewsAnalysisResult(
        article_id=article.id,
        created_at=now,
    )
    analysis.model_name = generated.model_name
    analysis.sentiment = sentiment
    analysis.sentiment_score = None
    analysis.summary = summary
    analysis.translated_title = translated_title
    analysis.translated_excerpt = translated_excerpt
    analysis.key_points = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(summary)
        if sentence.strip()
    ]
    analysis.topics = None
    analysis.price_impact = None
    analysis.price_impact_score = None
    analysis.confidence = confidence
    analysis.analysis_quality = _analysis_quality(confidence)
    analysis.prompt = generated.prompt
    analysis.raw_response = generated.raw_response
    analysis.processing_time_ms = elapsed_ms
    analysis.updated_at = now if repair_target is not None else None
    if repair_target is None:
        db.add(analysis)
    article.is_analyzed = True
    article.updated_at = now
    await db.commit()
    return True


async def _persist_failure_backoff(
    db: AsyncSession,
    *,
    article_id: int,
    error_type: str,
    elapsed_ms: int,
) -> bool:
    """완료 데이터는 건드리지 않고 불완전 분석 행으로 6시간 backoff를 남긴다."""

    article = await db.scalar(
        select(NewsArticle)
        .where(NewsArticle.id == article_id)
        .with_for_update(skip_locked=True)
    )
    if article is None or await _has_complete_analysis(db, article_id):
        await db.rollback()
        return False

    repair_target = await _latest_analysis(db, article_id)
    now = _utcnow()
    if repair_target is None:
        db.add(
            NewsAnalysisResult(
                article_id=article_id,
                model_name="news-summary-failed",
                sentiment=Sentiment.NEUTRAL,
                sentiment_score=None,
                summary="",
                translated_title=None,
                translated_excerpt=None,
                key_points=[],
                topics=None,
                price_impact=None,
                price_impact_score=None,
                confidence=0,
                analysis_quality="low",
                prompt="",
                raw_response=json.dumps(
                    {"error_type": error_type},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                processing_time_ms=elapsed_ms,
                created_at=now,
                updated_at=None,
            )
        )
    else:
        repair_target.processing_time_ms = elapsed_ms
        repair_target.updated_at = now
    await db.commit()
    return True


async def _run_batch(
    db: AsyncSession,
    *,
    batch_size: int,
    market: str | None,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
    generator: NewsSummaryGenerator,
) -> NewsSummaryBatchResult:
    if await _daily_call_limit_reached(db):
        await db.rollback()
        return NewsSummaryBatchResult(
            status="daily_limit",
            selected=0,
            summarized=0,
            skipped_existing=0,
            skipped_insufficient=0,
            failed=0,
        )
    scan = await _scan_candidates(
        db,
        batch_size=batch_size,
        market=market,
        feed_source=feed_source,
        article_urls=article_urls,
    )
    await db.commit()

    summarized = 0
    skipped_existing = 0
    # gate 탈락 행은 AI 호출 없이 이미 확정된 스킵이다. 기존 계약의
    # ``skipped_insufficient``/``skipped_article_ids`` 증거를 그대로 쓴다.
    skipped_ids: list[int] = list(scan.rejected)
    failed_ids: list[int] = []
    processed = len(scan.rejected)
    attempted = 0
    candidate_offset = 0
    daily_limit_hit = False

    while candidate_offset < len(scan.admitted) and attempted < batch_size:
        prepared_batch: list[_PreparedNewsSummary] = []
        while (
            candidate_offset < len(scan.admitted)
            and attempted < batch_size
            and len(prepared_batch) < NEWS_SUMMARY_ARTICLES_PER_CALL
        ):
            article_id = scan.admitted[candidate_offset]
            candidate_offset += 1
            try:
                article = await db.scalar(
                    select(NewsArticle)
                    .where(NewsArticle.id == article_id)
                    .with_for_update(skip_locked=True)
                )
                if article is None or await _has_complete_analysis(db, article_id):
                    processed += 1
                    skipped_existing += 1
                    continue
                news_input = _summary_input_for(article)
                if news_input is None:
                    skipped_ids.append(article_id)
                    processed += 1
                    logger.info(
                        "일반 뉴스 요약 입력 부족으로 스킵: article_id=%d",
                        article_id,
                    )
                    continue
                processed += 1
                attempted += 1
                prepared_batch.append(
                    _PreparedNewsSummary(article_id=article_id, news=news_input)
                )
            finally:
                await db.rollback()

        if not prepared_batch:
            continue
        if not await _lock_daily_call_budget(db):
            await db.rollback()
            daily_limit_hit = True
            break

        started = time.monotonic()
        batch_error_type: str | None = None
        try:
            generated_by_index = await generator.summarize_batch(
                tuple(prepared.news for prepared in prepared_batch)
            )
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as exc:
            generated_by_index = {}
            batch_error_type = type(exc).__name__
            logger.warning(
                "일반 뉴스 요약 모델 batch 실패: articles=%d error_type=%s",
                len(prepared_batch),
                batch_error_type,
            )
        elapsed_ms = int((time.monotonic() - started) * 1_000)

        for index, prepared in enumerate(prepared_batch):
            generated = generated_by_index.get(index)
            if generated is not None:
                try:
                    if await _persist_generated_summary(
                        db,
                        prepared=prepared,
                        generated=generated,
                        elapsed_ms=elapsed_ms,
                    ):
                        summarized += 1
                    else:
                        skipped_existing += 1
                    continue
                except asyncio.CancelledError:
                    await db.rollback()
                    raise
                except Exception as exc:
                    await db.rollback()
                    error_type = type(exc).__name__
            else:
                error_type = batch_error_type or "MissingBatchItem"

            try:
                marked = await _persist_failure_backoff(
                    db,
                    article_id=prepared.article_id,
                    error_type=error_type,
                    elapsed_ms=elapsed_ms,
                )
            except asyncio.CancelledError:
                await db.rollback()
                raise
            except Exception as exc:
                await db.rollback()
                marked = True
                logger.warning(
                    "일반 뉴스 요약 실패 backoff 저장 실패: "
                    "article_id=%d error_type=%s",
                    prepared.article_id,
                    type(exc).__name__,
                )
            if marked:
                failed_ids.append(prepared.article_id)
                logger.warning(
                    "일반 뉴스 요약 행 실패: article_id=%d error_type=%s",
                    prepared.article_id,
                    error_type,
                )
            else:
                skipped_existing += 1

    failed = len(failed_ids)
    status: Literal["success", "partial", "failed", "daily_limit"]
    if daily_limit_hit:
        status = "daily_limit"
    else:
        status = _batch_status(
            summarized=summarized,
            skipped_existing=skipped_existing,
            skipped_insufficient=len(skipped_ids),
            failed=failed,
        )
    return NewsSummaryBatchResult(
        status=status,
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


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "NEWS_SUMMARY_ARTICLES_PER_CALL",
    "NEWS_SUMMARY_DAILY_CALL_LIMIT",
    "MAX_TRANSLATED_EXCERPT_CHARS",
    "MAX_TRANSLATED_TITLE_CHARS",
    "MAX_TRANSLATION_SOURCE_CHARS",
    "NEWS_SUMMARY_RETRY_BACKOFF",
    "GeneratedNewsSummary",
    "NewsSummaryBatchResult",
    "NewsSummaryGenerator",
    "NewsSummaryInput",
    "OpenAiNewsSummaryGenerator",
    "build_news_summary_generator",
    "complete_korean_analysis_conditions",
    "complete_korean_analysis_exists",
    "summarize_pending_news",
]
