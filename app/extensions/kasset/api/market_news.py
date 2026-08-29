"""Android 종목 뉴스·공시 목록 조회."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import datetime, timedelta

from sqlalchemy import and_, desc, or_, select
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
from app.models.news import NewsArticle
from app.services.disclosures.feed_sources import DISCLOSURE_FEED_SOURCES

DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 50

_CURSOR_VERSION = 1
_CURSOR_CONTEXT = b"kasset-market-news-cursor:v1\0"
_CURSOR_SIGNATURE_BYTES = 16
_CURSOR_STRUCT = struct.Struct("!BBqq")
_NAIVE_EPOCH = datetime(1970, 1, 1)
_MAX_BIGINT = 2**63 - 1


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


def _encode_cursor(published_at: datetime | None, article_id: int) -> str:
    """게시시각과 행 식별자를 내부 필드명 없는 서명 토큰으로 인코딩한다."""

    if article_id < 1 or article_id > _MAX_BIGINT:
        raise ValueError("article_id is outside signed bigint range")
    is_null = int(published_at is None)
    published_microseconds = (
        0 if published_at is None else _datetime_to_microseconds(published_at)
    )
    payload = _CURSOR_STRUCT.pack(
        _CURSOR_VERSION,
        is_null,
        published_microseconds,
        article_id,
    )
    token = payload + _cursor_signature(payload)
    return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime | None, int]:
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

        version, is_null, published_microseconds, article_id = _CURSOR_STRUCT.unpack(
            payload
        )
        if version != _CURSOR_VERSION or is_null not in {0, 1}:
            raise ValueError("cursor version or null marker is invalid")
        if article_id < 1:
            raise ValueError("cursor article id is invalid")
        if is_null:
            if published_microseconds != 0:
                raise ValueError("null cursor contains a timestamp")
            return None, article_id
        return _NAIVE_EPOCH + timedelta(microseconds=published_microseconds), article_id
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


async def list_market_news(
    db: AsyncSession,
    *,
    market: str | None,
    symbol: str | None,
    kind: MarketNewsFilterKind,
    limit: int,
    cursor: str | None,
) -> MarketNewsResponse:
    """필터와 `(게시시각, id)` keyset으로 뉴스·공시 한 페이지를 읽는다."""

    normalized_market = _market_filter(market)
    normalized_symbol = _symbol_filter(symbol)

    statement = select(NewsArticle)
    if normalized_market is not None:
        statement = statement.where(NewsArticle.market == normalized_market)
    if normalized_symbol is not None:
        statement = statement.where(NewsArticle.stock_symbol == normalized_symbol)
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

    if cursor is not None:
        cursor_published_at, cursor_id = _decode_cursor(cursor)
        if cursor_published_at is None:
            statement = statement.where(
                and_(
                    NewsArticle.article_published_at.is_(None),
                    NewsArticle.id < cursor_id,
                )
            )
        else:
            statement = statement.where(
                or_(
                    NewsArticle.article_published_at < cursor_published_at,
                    and_(
                        NewsArticle.article_published_at == cursor_published_at,
                        NewsArticle.id < cursor_id,
                    ),
                    NewsArticle.article_published_at.is_(None),
                )
            )

    statement = statement.order_by(
        NewsArticle.article_published_at.desc().nulls_last(),
        desc(NewsArticle.id),
    ).limit(limit + 1)
    rows = list((await db.execute(statement)).scalars().all())
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = _encode_cursor(last.article_published_at, last.id)

    return MarketNewsResponse(
        items=[
            MarketNewsItem(
                kind=(
                    "disclosure"
                    if article.feed_source in DISCLOSURE_FEED_SOURCES
                    else "news"
                ),
                title=article.title,
                summary=article.summary,
                source=article.source,
                url=article.url,
                published_at=_published_at_wire(article.article_published_at),
                symbol=article.stock_symbol,
                stock_name=article.stock_name,
            )
            for article in page
        ],
        next_cursor=next_cursor,
    )
