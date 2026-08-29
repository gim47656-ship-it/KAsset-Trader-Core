"""공시 본문 기반 한국어 요약 생성과 멱등 저장 서비스."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.extensions.kasset.ai.api_provider import OpenAiResponsesClient
from app.models.news import NewsArticle
from app.services.disclosures.content_fetcher import DisclosureTextFetcher
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
AUTO_SUMMARY_BATCH_SIZE = 20
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
    "공시 원문에 명시된 사실만 사용해 한국어 2~4문장으로 요약하라. "
    "매출, 영업이익 또는 순이익, 가이던스, 핵심 사건은 body_excerpt에 실제로 "
    "있는 항목만 포함하라. 원문의 숫자와 단위를 그대로 보존하고 계산, 환산, 추측, "
    "보완을 하지 마라. 투자 권유, 매수·매도 추천, 목표주가를 쓰지 마라. "
    "제목이나 form을 본문 사실처럼 확대 해석하지 마라."
)
_NUMBER_RETRY_INSTRUCTIONS = (
    "직전 시도는 원문에 없는 수치 형식 또는 단위 변환 때문에 폐기됐다. "
    "수치를 쓸 때는 body_excerpt에 보이는 숫자 토큰과 단위를 문자 그대로 복사하라. "
    "반올림, 자릿수 축약, million/billion 환산을 하지 마라. 그대로 복사할 수 없으면 "
    "그 수치를 생략하고 원문에 명시된 비수치 사실만 2~4문장으로 요약하라."
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"(?<!\d)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*%)?")
_INVESTMENT_ADVICE_RE = re.compile(
    r"(?:매수|매도|투자)(?:를|가|는)?\s*(?:권고|권유|추천|해야)|"
    r"목표\s*주가|목표가"
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


def _validated_summary(summary: object, source_text: str) -> str:
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
    if _INVESTMENT_ADVICE_RE.search(normalized):
        raise ValueError("disclosure summary contains investment advice")

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
    return normalized


class OpenAiDisclosureSummaryGenerator:
    """기존 Responses API 클라이언트로 저비용 Luna 단일 호출을 수행한다."""

    def __init__(self, client: OpenAiResponsesClient, *, model: str) -> None:
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
        response = await self._request(payload, retry=False)
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
        try:
            return _validated_summary(response["summary"], source_text)
        except ValueError as exc:
            if str(exc) != "disclosure summary contains numbers absent from source":
                raise
        retry_response = await self._request(payload, retry=True)
        return _validated_summary(retry_response["summary"], source_text)

    async def _request(
        self,
        payload: dict[str, object],
        *,
        retry: bool,
    ) -> dict[str, object]:
        response = await self._client.request_json(
            model=self._model,
            input_payload=payload,
            reasoning_effort="low",
            schema_name=(
                "kasset_disclosure_summary_retry"
                if retry
                else "kasset_disclosure_summary"
            ),
            schema=_SUMMARY_SCHEMA,
            additional_instructions=(
                f"{_SUMMARY_INSTRUCTIONS} {_NUMBER_RETRY_INSTRUCTIONS}"
                if retry
                else _SUMMARY_INSTRUCTIONS
            ),
        )
        if set(response) != {"summary"}:
            raise ValueError("disclosure summary response shape is invalid")
        return response


def build_disclosure_summary_generator() -> DisclosureSummaryGenerator | None:
    """가장 낮은 기존 분석 tier인 Luna가 설정됐을 때만 API 생성기를 만든다."""
    api_key = (
        settings.KASSET_AI_API_KEY.get_secret_value().strip()
        if settings.KASSET_AI_API_KEY is not None
        else ""
    )
    model = (
        settings.KASSET_AI_MODEL_LUNA.strip() or settings.KASSET_AI_API_MODEL.strip()
    )
    if not api_key or not model:
        return None
    return OpenAiDisclosureSummaryGenerator(
        OpenAiResponsesClient(
            name="disclosure-summary",
            base_url=settings.KASSET_AI_API_BASE_URL,
            api_key=api_key,
            timeout_seconds=60.0,
        ),
        model=model,
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
    stmt = (
        select(NewsArticle.id)
        .where(
            NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES),
            NewsArticle.summary.is_(None),
        )
        .order_by(
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

            body_excerpt = await fetcher.fetch(article.url)
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
    bounded_urls = tuple(dict.fromkeys(article_urls))[:AUTO_SUMMARY_BATCH_SIZE]
    return await summarize_pending_disclosures(
        db,
        batch_size=AUTO_SUMMARY_BATCH_SIZE,
        article_urls=bounded_urls,
    )


__all__ = [
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
