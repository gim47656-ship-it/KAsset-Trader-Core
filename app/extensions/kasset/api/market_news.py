"""Android 종목 뉴스·공시 목록 조회."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, desc, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import KST
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import iso_z
from app.extensions.kasset.api.schemas import (
    MarketNewsFilterKind,
    MarketNewsItem,
    MarketNewsResponse,
)
from app.models.news import NewsAnalysisResult, NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.disclosures.feed_sources import (
    DISCLOSURE_FEED_SOURCES,
    SEC_FEED_SOURCE,
)
from app.services.disclosures.quality import (
    DART_HIGH_VALUE_TITLE_TERMS,
    DART_LOW_INFORMATION_TITLE_TERMS,
    title_matches_any,
)
from app.services.news_summary_service import (
    complete_korean_analysis_conditions,
    complete_korean_analysis_exists,
)

DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 50

_CURSOR_VERSION = 2
_CURSOR_CONTEXT = b"kasset-market-news-cursor:v2\0"
_CURSOR_SIGNATURE_BYTES = 16
_CURSOR_STRUCT = struct.Struct("!BBBqq")
_NAIVE_EPOCH = datetime(1970, 1, 1)
_MAX_BIGINT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class _LatestNewsAnalysis:
    summary: str
    translated_title: str | None
    translated_excerpt: str | None


def _cursor_signature(payload: bytes) -> bytes:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        _CURSOR_CONTEXT + payload,
        hashlib.sha256,
    ).digest()[:_CURSOR_SIGNATURE_BYTES]


def _naive_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(KST).replace(tzinfo=None)


def _datetime_to_microseconds(value: datetime) -> int:
    delta = _naive_kst(value) - _NAIVE_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _encode_cursor(
    published_at: datetime | None,
    curation_rank: int,
    article_id: int,
) -> str:
    """게시시각·큐레이션 순위·행 식별자를 서명 토큰으로 인코딩한다."""

    if article_id < 1 or article_id > _MAX_BIGINT:
        raise ValueError("article_id is outside signed bigint range")
    if not 0 <= curation_rank <= 255:
        raise ValueError("curation_rank is outside unsigned byte range")
    is_null = int(published_at is None)
    published_microseconds = (
        0 if published_at is None else _datetime_to_microseconds(published_at)
    )
    payload = _CURSOR_STRUCT.pack(
        _CURSOR_VERSION,
        is_null,
        curation_rank,
        published_microseconds,
        article_id,
    )
    token = payload + _cursor_signature(payload)
    return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime | None, int, int]:
    """서명과 정규 인코딩을 검증하고 keyset 경계를 복원한다."""

    try:
        encoded = cursor.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        token = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(token).rstrip(b"=")
        if not hmac.compare_digest(encoded, canonical):
            raise ValueError("cursor is not canonically encoded")
        if len(token) != _CURSOR_STRUCT.size + _CURSOR_SIGNATURE_BYTES:
            raise ValueError("cursor has an invalid length")

        payload = token[: _CURSOR_STRUCT.size]
        signature = token[_CURSOR_STRUCT.size :]
        if not hmac.compare_digest(signature, _cursor_signature(payload)):
            raise ValueError("cursor signature does not match")

        (
            version,
            is_null,
            curation_rank,
            published_microseconds,
            article_id,
        ) = _CURSOR_STRUCT.unpack(payload)
        if version != _CURSOR_VERSION or is_null not in {0, 1}:
            raise ValueError("cursor version or null marker is invalid")
        if article_id < 1:
            raise ValueError("cursor article id is invalid")
        if is_null:
            if published_microseconds != 0:
                raise ValueError("null cursor contains a timestamp")
            return None, curation_rank, article_id
        return (
            _NAIVE_EPOCH + timedelta(microseconds=published_microseconds),
            curation_rank,
            article_id,
        )
    except (
        UnicodeEncodeError,
        binascii.Error,
        OverflowError,
        struct.error,
        ValueError,
    ) as err:
        raise MobileApiError(
            422,
            "VALIDATION_ERROR",
            "유효하지 않은 커서입니다.",
        ) from err


def _market_filter(market: str | None) -> str | None:
    if market is None:
        return None
    normalized = krx_quotes.normalize_market(market)
    return "kr" if normalized == "KRX" else "us"


def _symbol_filter(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if not normalized:
        raise MobileApiError(422, "VALIDATION_ERROR", "종목 코드를 입력해 주세요.")
    return normalized


def _published_at_wire(value: datetime | None) -> str | None:
    if value is None:
        return None
    # 이 열은 TIMESTAMP WITHOUT TIME ZONE이며 수집기가 KST 벽시각을 쓴다.
    # UTC로 오인하지 않고 먼저 KST를 부여한 뒤 기존 UTC Z 직렬화기를 사용한다.
    return iso_z(_naive_kst(value).replace(tzinfo=KST))


def _localized_disclosure_title(article: NewsArticle) -> str | None:
    if article.feed_source != SEC_FEED_SOURCE or any(
        "가" <= character <= "힣" for character in article.title
    ):
        return None
    form = article.title.partition("—")[0].strip()
    subject = article.stock_name or article.stock_symbol or "미국 기업"
    return f"{subject} {form} 공시"


async def _latest_news_analyses(
    db: AsyncSession,
    article_ids: list[int],
) -> dict[int, _LatestNewsAnalysis]:
    """일반 뉴스의 최신 영속 요약과 번역 필드를 한 번에 읽는다."""

    if not article_ids:
        return {}
    ranked = (
        select(
            NewsAnalysisResult.article_id.label("article_id"),
            NewsAnalysisResult.summary.label("summary"),
            NewsAnalysisResult.translated_title.label("translated_title"),
            NewsAnalysisResult.translated_excerpt.label("translated_excerpt"),
            func.row_number()
            .over(
                partition_by=NewsAnalysisResult.article_id,
                order_by=[
                    NewsAnalysisResult.created_at.desc(),
                    NewsAnalysisResult.id.desc(),
                ],
            )
            .label("analysis_rank"),
        )
        .join(NewsArticle, NewsArticle.id == NewsAnalysisResult.article_id)
        .where(
            NewsAnalysisResult.article_id.in_(article_ids),
            *complete_korean_analysis_conditions(),
        )
        .subquery()
    )
    statement = select(
        ranked.c.article_id,
        ranked.c.summary,
        ranked.c.translated_title,
        ranked.c.translated_excerpt,
    ).where(ranked.c.analysis_rank == 1)
    return {
        int(article_id): _LatestNewsAnalysis(
            summary=summary,
            translated_title=translated_title,
            translated_excerpt=translated_excerpt,
        )
        for article_id, summary, translated_title, translated_excerpt in (
            await db.execute(statement)
        ).all()
    }


async def list_market_news(
    db: AsyncSession,
    *,
    market: str | None,
    symbol: str | None,
    kind: MarketNewsFilterKind,
    limit: int,
    cursor: str | None,
) -> MarketNewsResponse:
    """필터와 `(게시시각, 큐레이션 순위, id)` keyset으로 한 페이지를 읽는다."""

    normalized_market = _market_filter(market)
    normalized_symbol = _symbol_filter(symbol)

    curation_rank = literal(0)
    statement = select(NewsArticle)
    if normalized_market is not None:
        statement = statement.where(NewsArticle.market == normalized_market)
    if normalized_symbol is not None:
        relevance_conditions = [
            SymbolNewsRelevance.article_id == NewsArticle.id,
            SymbolNewsRelevance.symbol == normalized_symbol,
            SymbolNewsRelevance.status != "excluded",
        ]
        relevance_conditions.append(
            SymbolNewsRelevance.market
            == (
                normalized_market
                if normalized_market is not None
                else NewsArticle.market
            )
        )
        statement = statement.where(
            or_(
                NewsArticle.stock_symbol == normalized_symbol,
                exists().where(*relevance_conditions),
            )
        )
    else:
        is_dart = NewsArticle.feed_source == "dart"
        not_dart = or_(
            NewsArticle.feed_source.is_(None),
            NewsArticle.feed_source != "dart",
        )
        important_dart = title_matches_any(
            NewsArticle.title,
            DART_HIGH_VALUE_TITLE_TERMS,
        )
        low_information_dart = title_matches_any(
            NewsArticle.title,
            DART_LOW_INFORMATION_TITLE_TERMS,
        )
        statement = statement.where(
            or_(
                not_dart,
                and_(
                    is_dart,
                    NewsArticle.stock_symbol.is_not(None),
                    or_(
                        ~low_information_dart,
                        NewsArticle.summary.is_not(None),
                    ),
                ),
            )
        )
        is_disclosure_expr = NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES)
        is_ordinary_news_expr = or_(
            NewsArticle.feed_source.is_(None),
            NewsArticle.feed_source.not_in(DISCLOSURE_FEED_SOURCES),
        )
        has_analysis = exists().where(NewsAnalysisResult.article_id == NewsArticle.id)
        curation_rank = case(
            (and_(is_dart, important_dart), 0),
            (
                and_(
                    is_disclosure_expr,
                    not_dart,
                    NewsArticle.summary.is_not(None),
                ),
                0,
            ),
            (and_(is_ordinary_news_expr, has_analysis), 0),
            (and_(is_dart, ~low_information_dart), 1),
            (and_(is_dart, low_information_dart), 2),
            (is_disclosure_expr, 1),
            (
                and_(
                    is_ordinary_news_expr,
                    NewsArticle.article_content.is_not(None),
                ),
                1,
            ),
            else_=2,
        )

    if kind == "disclosure":
        statement = statement.where(
            NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES)
        )
    elif kind == "news":
        statement = statement.where(
            or_(
                NewsArticle.feed_source.is_(None),
                NewsArticle.feed_source.not_in(DISCLOSURE_FEED_SOURCES),
            )
        )
    is_disclosure_expr = NewsArticle.feed_source.in_(DISCLOSURE_FEED_SOURCES)
    is_ordinary_news_expr = or_(
        NewsArticle.feed_source.is_(None),
        NewsArticle.feed_source.not_in(DISCLOSURE_FEED_SOURCES),
    )
    statement = statement.where(
        or_(
            and_(
                is_disclosure_expr,
                NewsArticle.summary.op("~")("[가-힣]"),
            ),
            and_(
                is_ordinary_news_expr,
                complete_korean_analysis_exists(),
            ),
        )
    )

    day_bucket = func.date_trunc("day", NewsArticle.article_published_at)
    if cursor is not None:
        cursor_published_at, cursor_rank, cursor_id = _decode_cursor(cursor)
        if cursor_published_at is None:
            statement = statement.where(
                and_(
                    NewsArticle.article_published_at.is_(None),
                    or_(
                        curation_rank > cursor_rank,
                        and_(
                            curation_rank == cursor_rank,
                            NewsArticle.id < cursor_id,
                        ),
                    ),
                )
            )
        else:
            cursor_day = cursor_published_at.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            same_day_tail = or_(
                curation_rank > cursor_rank,
                and_(
                    curation_rank == cursor_rank,
                    or_(
                        NewsArticle.article_published_at < cursor_published_at,
                        and_(
                            NewsArticle.article_published_at == cursor_published_at,
                            NewsArticle.id < cursor_id,
                        ),
                    ),
                ),
            )
            statement = statement.where(
                or_(
                    day_bucket < cursor_day,
                    and_(day_bucket == cursor_day, same_day_tail),
                    NewsArticle.article_published_at.is_(None),
                )
            )

    statement = (
        statement.add_columns(curation_rank.label("curation_rank"))
        .order_by(
            day_bucket.desc().nulls_last(),
            curation_rank.asc(),
            NewsArticle.article_published_at.desc().nulls_last(),
            desc(NewsArticle.id),
        )
        .limit(limit + 1)
    )
    rows = list((await db.execute(statement)).all())
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last_article, last_rank = page[-1]
        next_cursor = _encode_cursor(
            last_article.article_published_at,
            int(last_rank),
            last_article.id,
        )

    news_analyses = await _latest_news_analyses(
        db,
        [
            article.id
            for article, _rank in page
            if article.feed_source not in DISCLOSURE_FEED_SOURCES
        ],
    )
    items: list[MarketNewsItem] = []
    for article, _rank in page:
        article_is_disclosure = article.feed_source in DISCLOSURE_FEED_SOURCES
        analysis = None if article_is_disclosure else news_analyses.get(article.id)
        items.append(
            MarketNewsItem(
                kind="disclosure" if article_is_disclosure else "news",
                title=article.title,
                summary=(
                    article.summary
                    if article_is_disclosure
                    else analysis.summary
                    if analysis is not None
                    else None
                ),
                translated_title=(
                    _localized_disclosure_title(article)
                    if article_is_disclosure
                    else analysis.translated_title
                    if analysis is not None
                    else None
                ),
                translated_excerpt=(
                    analysis.translated_excerpt if analysis is not None else None
                ),
                source=article.source,
                url=article.url,
                published_at=_published_at_wire(article.article_published_at),
                symbol=article.stock_symbol,
                stock_name=article.stock_name,
            )
        )
    return MarketNewsResponse(items=items, next_cursor=next_cursor)
