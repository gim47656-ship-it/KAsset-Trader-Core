"""공시 본문 기반 한국어 요약 생성과 멱등 저장 서비스."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Literal, Protocol

import httpx
from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.ai.base import StructuredJsonClient
from app.extensions.kasset.ai.factory import build_summary_json_client
from app.models.news import NewsArticle
from app.services.disclosures.content_fetcher import (
    MAX_TEXT_CHARS,
    DisclosureTextFetcher,
)
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES
from app.services.disclosures.quality import (
    DART_HIGH_VALUE_TITLE_TERMS,
    DART_LOW_INFORMATION_TITLE_TERMS,
    title_matches_any,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
AUTO_SUMMARY_BATCH_SIZE = 20
AUTO_SUMMARY_CANDIDATE_LIMIT = 200
_MIN_BODY_CHARS = 40
_HTTP_TIMEOUT_SECONDS = 20.0
_SUMMARY_MAX_CHARS = 1_200
_SEC_FORM_PREFIX = "sec_form:"

_SUMMARY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": _SUMMARY_MAX_CHARS,
        }
    },
    "required": ["summary"],
    "additionalProperties": False,
}
_SUMMARY_INSTRUCTIONS = (
    "공시 원문에 명시된 사실만 사용해 한국어 2~4문장으로 요약하라. 첫 문장에는 "
    "공시 주체와 핵심 사건을 쓰고, 수치·날짜·상대방·기간·투자자 영향은 "
    "body_excerpt에 명시된 항목만 포함하라. 표에서는 항목명과 값을 함께 읽어 "
    "매출, 영업이익, 순이익, 계약금액, 발행조건 등 사건을 설명하는 핵심 행을 "
    "노이즈보다 우선하라. 정정공시는 정정 사유와 정정 전·후 값, 달라진 숫자와 "
    "단위를 원문에 있는 그대로 보존하라. 계산, 환산, 추측, 사실 보완을 하지 말고 "
    "원문에 없는 시장 영향이나 전망을 만들지 마라. 투자 권유, 매수·매도 추천, "
    "목표주가를 쓰지 마라. '본 공시는', '공시 내용에 따르면' 같은 상투적 서두, "
    "제목 복제, form/표제 나열 대신 실제 사건과 변경 내용을 직접 서술하라."
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"(?<!\d)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*%)?")
_INVESTMENT_ADVICE_RE = re.compile(
    r"(?:매수|매도|투자)(?:를|가|는)?\s*(?:권고|권유|추천|해야)|"
    r"목표\s*주가|목표가"
)
_TEMPLATE_LANGUAGE_RE = re.compile(
    r"(?:본|해당|이번)\s*공시는|공시\s*내용에\s*따르면|"
    r"투자자(?:들은?|에게)는?\s*(?:주목|유의|관심)"
)
_HANGUL_RE = re.compile(r"[가-힣]")
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
class DisclosureSummaryInput:
    title: str
    company: str | None
    form: str | None
    body_excerpt: str


@dataclass(frozen=True, slots=True)
class DisclosureSummaryBatchResult:
    status: Literal["success", "partial", "failed", "unconfigured"]
    selected: int
    summarized: int
    skipped_existing: int
    failed: int
    failed_article_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DisclosureSummaryGenerator(Protocol):
    async def summarize(self, disclosure: DisclosureSummaryInput) -> str: ...


class DisclosureBodyFetcher(Protocol):
    async def fetch(self, url: str) -> str: ...


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


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


def _correction_comparison_required(body: str) -> bool:
    body_key = re.sub(r"\s+", "", body)
    return (
        ("정정전" in body_key or "변경전" in body_key)
        and ("정정후" in body_key or "변경후" in body_key)
    )


def _validated_summary(
    summary: object,
    disclosure: DisclosureSummaryInput,
) -> str:
    if not isinstance(summary, str):
        raise ValueError("disclosure summary must be a string")
    normalized = " ".join(summary.split())
    if not normalized or len(normalized) > _SUMMARY_MAX_CHARS:
        raise ValueError("disclosure summary length is invalid")

    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(normalized)
        if sentence.strip()
    ]
    if len(sentences) < 2 or len(sentences) > 4:
        raise ValueError("disclosure summary must contain 2 to 4 sentences")
    if _HANGUL_RE.search(normalized) is None:
        raise ValueError("disclosure summary must be written in Korean")
    if _INVESTMENT_ADVICE_RE.search(normalized):
        raise ValueError("disclosure summary contains investment advice")
    if _TEMPLATE_LANGUAGE_RE.search(normalized):
        raise ValueError("disclosure summary contains template language")
    if any(_duplicates_title(sentence, disclosure.title) for sentence in sentences):
        raise ValueError("disclosure summary duplicates title")

    source_text = "\n".join(
        value
        for value in (
            disclosure.title,
            disclosure.company,
            disclosure.form,
            disclosure.body_excerpt,
        )
        if value
    )
    source_numbers = _number_set(source_text)
    generated_numbers = _number_set(normalized)
    invented_numbers = {
        number
        for number in generated_numbers - source_numbers
        if not _is_calendar_month_translation(
            number,
            summary=normalized,
            source_text=source_text,
        )
    }
    if invented_numbers:
        raise ValueError("disclosure summary contains numbers absent from source")
    if _correction_comparison_required(disclosure.body_excerpt):
        if "정정" not in normalized and "변경" not in normalized:
            raise ValueError("disclosure summary omits correction comparison")
        body_numbers = _number_set(disclosure.body_excerpt)
        if len(body_numbers) >= 2 and len(generated_numbers & body_numbers) < 2:
            raise ValueError("disclosure summary omits correction values")
    return normalized


class OpenAiDisclosureSummaryGenerator:
    """Generate disclosure summaries through the common JSON transport."""

    def __init__(self, client: StructuredJsonClient, *, model: str) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("disclosure summary model is required")
        self._client = client
        self._model = normalized_model

    async def summarize(self, disclosure: DisclosureSummaryInput) -> str:
        payload: dict[str, object] = {
            "title": disclosure.title,
            "company": disclosure.company,
            "form": disclosure.form,
            "body_excerpt": disclosure.body_excerpt,
        }
        response = await self._request(payload)
        return _validated_summary(response["summary"], disclosure)

    async def _request(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        response = await self._client.request_json(
            model=self._model,
            input_payload=payload,
            reasoning_effort="low",
            schema_name="kasset_disclosure_summary",
            schema=_SUMMARY_SCHEMA,
            additional_instructions=_SUMMARY_INSTRUCTIONS,
        )
        if set(response) != {"summary"}:
            raise ValueError("disclosure summary response shape is invalid")
        return response


def build_disclosure_summary_generator() -> DisclosureSummaryGenerator | None:
    """direct API -> OpenRouter 공시 요약 route를 만든다."""

    direct_model = settings.KASSET_AI_MODEL_LUNA.strip()
    fallback_model = settings.KASSET_AI_OPENROUTER_MODEL_FLASH.strip()
    client = build_summary_json_client(
        name="disclosure-summary",
        direct_model=direct_model,
        fallback_model=fallback_model,
    )
    if client is None:
        return None
    return OpenAiDisclosureSummaryGenerator(
        client,
        model=direct_model or fallback_model,
    )


def _form_for(article: NewsArticle) -> str | None:
    if article.feed_source == "sec" and isinstance(article.keywords, list):
        for value in article.keywords:
            if isinstance(value, str) and value.startswith(_SEC_FORM_PREFIX):
                form = value.removeprefix(_SEC_FORM_PREFIX).strip()
                if form:
                    return form
    if article.feed_source == "dart":
        return article.title
    return None


def _validate_scope(batch_size: int, feed_source: str | None) -> None:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if feed_source is not None and feed_source not in DISCLOSURE_FEED_SOURCES:
        raise ValueError(
            f"feed_source must be one of: {', '.join(DISCLOSURE_FEED_SOURCES)}"
        )


async def _candidate_ids(
    db: AsyncSession,
    *,
    batch_size: int,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
) -> list[int]:
    if article_urls is not None and not article_urls:
        return []
    is_dart = NewsArticle.feed_source == "dart"
    important_title = title_matches_any(
        NewsArticle.title,
        DART_HIGH_VALUE_TITLE_TERMS,
    )
    low_information_title = title_matches_any(
        NewsArticle.title,
        DART_LOW_INFORMATION_TITLE_TERMS,
    )
    stmt = (
        select(NewsArticle.id)
        .where(
            NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES),
            NewsArticle.summary.is_(None),
            or_(
                ~is_dart,
                and_(
                    NewsArticle.stock_symbol.is_not(None),
                    ~low_information_title,
                ),
            ),
        )
        .order_by(
            case(
                (and_(is_dart, important_title), 0),
                (~is_dart, 0),
                else_=1,
            ),
            case((NewsArticle.article_content.is_not(None), 0), else_=1),
            NewsArticle.article_published_at.desc().nullslast(),
            NewsArticle.id.desc(),
        )
        .limit(batch_size)
    )
    if feed_source is not None:
        stmt = stmt.where(NewsArticle.feed_source == feed_source)
    if article_urls is not None:
        stmt = stmt.where(NewsArticle.url.in_(tuple(dict.fromkeys(article_urls))))
    return list((await db.scalars(stmt)).all())


def _batch_status(
    *,
    summarized: int,
    skipped_existing: int,
    failed: int,
) -> Literal["success", "partial", "failed"]:
    if failed == 0:
        return "success"
    if summarized > 0 or skipped_existing > 0:
        return "partial"
    return "failed"


def _body_excerpt(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if len(normalized) < _MIN_BODY_CHARS:
        return None
    return normalized[:MAX_TEXT_CHARS].rstrip()


async def _run_batch(
    db: AsyncSession,
    *,
    batch_size: int,
    feed_source: str | None,
    article_urls: Sequence[str] | None,
    fetcher: DisclosureBodyFetcher,
    generator: DisclosureSummaryGenerator,
) -> DisclosureSummaryBatchResult:
    candidate_ids = await _candidate_ids(
        db,
        batch_size=batch_size,
        feed_source=feed_source,
        article_urls=article_urls,
    )
    await db.commit()

    summarized = 0
    skipped_existing = 0
    failed_ids: list[int] = []
    for article_id in candidate_ids:
        try:
            article = await db.scalar(
                select(NewsArticle)
                .where(NewsArticle.id == article_id)
                .with_for_update(skip_locked=True)
            )
            if article is None:
                skipped_existing += 1
                await db.rollback()
                continue
            if article.summary is not None:
                if not article.is_analyzed:
                    article.is_analyzed = True
                    article.updated_at = _utcnow()
                    await db.commit()
                else:
                    await db.rollback()
                skipped_existing += 1
                continue

            body_excerpt = _body_excerpt(article.article_content)
            if body_excerpt is None:
                fetched_body = _body_excerpt(await fetcher.fetch(article.url))
                if fetched_body is None:
                    raise ValueError("disclosure body is missing or too short")
                article.article_content = fetched_body
                article.updated_at = _utcnow()
                await db.commit()
                article = await db.scalar(
                    select(NewsArticle)
                    .where(NewsArticle.id == article_id)
                    .with_for_update(skip_locked=True)
                )
                if article is None or article.summary is not None:
                    skipped_existing += 1
                    await db.rollback()
                    continue
                body_excerpt = _body_excerpt(article.article_content) or fetched_body

            summary = await generator.summarize(
                DisclosureSummaryInput(
                    title=article.title,
                    company=article.stock_name,
                    form=_form_for(article),
                    body_excerpt=body_excerpt,
                )
            )
            article.summary = summary
            article.is_analyzed = True
            article.updated_at = _utcnow()
            await db.commit()
            summarized += 1
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            failed_ids.append(article_id)
            logger.warning(
                "공시 요약 행 실패: article_id=%d error_type=%s",
                article_id,
                type(exc).__name__,
            )

    failed = len(failed_ids)
    return DisclosureSummaryBatchResult(
        status=_batch_status(
            summarized=summarized,
            skipped_existing=skipped_existing,
            failed=failed,
        ),
        selected=len(candidate_ids),
        summarized=summarized,
        skipped_existing=skipped_existing,
        failed=failed,
        failed_article_ids=tuple(failed_ids),
    )


async def summarize_pending_disclosures(
    db: AsyncSession,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    feed_source: str | None = None,
    article_urls: Sequence[str] | None = None,
    fetcher: DisclosureBodyFetcher | None = None,
    generator: DisclosureSummaryGenerator | None = None,
) -> DisclosureSummaryBatchResult:
    """미요약 공시를 제한 batch로 처리하며 각 행을 독립 커밋한다."""
    _validate_scope(batch_size, feed_source)
    effective_generator = (
        generator if generator is not None else build_disclosure_summary_generator()
    )
    if effective_generator is None:
        return DisclosureSummaryBatchResult(
            status="unconfigured",
            selected=0,
            summarized=0,
            skipped_existing=0,
            failed=0,
        )

    if fetcher is not None:
        return await _run_batch(
            db,
            batch_size=batch_size,
            feed_source=feed_source,
            article_urls=article_urls,
            fetcher=fetcher,
            generator=effective_generator,
        )

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        return await _run_batch(
            db,
            batch_size=batch_size,
            feed_source=feed_source,
            article_urls=article_urls,
            fetcher=DisclosureTextFetcher(client),
            generator=effective_generator,
        )


async def summarize_ingested_disclosures(
    db: AsyncSession,
    article_urls: Sequence[str],
) -> DisclosureSummaryBatchResult:
    """한 수집 회차의 최신 공시만 비용 상한 안에서 자동 요약한다."""
    bounded_urls = tuple(dict.fromkeys(article_urls))[
        :AUTO_SUMMARY_CANDIDATE_LIMIT
    ]
    return await summarize_pending_disclosures(
        db,
        batch_size=AUTO_SUMMARY_BATCH_SIZE,
        article_urls=bounded_urls,
    )


__all__ = [
    "AUTO_SUMMARY_CANDIDATE_LIMIT",
    "AUTO_SUMMARY_BATCH_SIZE",
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "DisclosureSummaryBatchResult",
    "DisclosureBodyFetcher",
    "DisclosureSummaryGenerator",
    "DisclosureSummaryInput",
    "OpenAiDisclosureSummaryGenerator",
    "build_disclosure_summary_generator",
    "summarize_ingested_disclosures",
    "summarize_pending_disclosures",
]
