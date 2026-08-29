"""Google News RSS 응답을 공통 뉴스 저장 입력으로 정규화한다."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Protocol

import httpx

from app.services.symbol_news_store import FeedArticleInput

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_FEED_SOURCE = "google_news"
MAX_ITEMS_PER_SYMBOL = 100
# 2026-08-29 NAVER 검색 피드 실측에서 기업 IR·블로그·유료 콘텐츠 소스로 관측됐다.
DEFAULT_EXCLUDED_SOURCES = frozenset(
    {"NAVERCorp.", "Naver Blog", "네이버 프리미엄콘텐츠"}
)
_REQUEST_TIMEOUT_SECONDS = 15.0
_KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class GoogleNewsMarketConfig:
    market: str
    hl: str
    gl: str
    ceid: str


@dataclass(frozen=True)
class GoogleNewsRssFeed:
    items: tuple[FeedArticleInput, ...]
    truncated_count: int
    excluded_count: int


class GoogleNewsRssError(RuntimeError):
    """HTTP 또는 RSS 형식 오류로 종목 피드를 신뢰할 수 없음을 나타낸다."""


class GoogleNewsHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response: ...


_MARKET_CONFIGS = {
    "kr": GoogleNewsMarketConfig(market="kr", hl="ko", gl="KR", ceid="KR:ko"),
    "us": GoogleNewsMarketConfig(market="us", hl="en-US", gl="US", ceid="US:en"),
}
_US_TRAILING_TOKENS = frozenset({"NEW", "CP", "CORP", "ORD", "ETF", "INC", "CO", "LTD"})


def market_config(market: str) -> GoogleNewsMarketConfig:
    """시장 코드에 대응하는 Google News 로케일을 반환한다."""
    normalized = market.strip().lower()
    try:
        return _MARKET_CONFIGS[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported market: {market!r}") from exc


def normalize_us_company_name(value: str) -> str:
    """관측된 잡음 접미를 토큰 단위로만 영문 회사명 끝에서 제거한다."""
    words = value.strip().split()
    while words:
        raw_token = words[-1]
        token = re.sub(r"[^A-Za-z0-9]", "", raw_token).upper()
        if token in _US_TRAILING_TOKENS or (
            len(raw_token) == 1 and raw_token.isascii() and raw_token.isupper()
        ):
            words.pop()
            continue
        break
    normalized = " ".join(words).strip(" ,.-")
    if not normalized:
        raise ValueError("US company name is empty after normalization")
    return normalized


def build_symbol_query(*, market: str, name: str) -> str:
    """KR은 종목명, US는 정규화한 영문 회사명과 stock으로 검색어를 만든다."""
    config = market_config(market)
    if config.market == "us":
        return f"{normalize_us_company_name(name)} stock"
    normalized = " ".join(name.split())
    if not normalized:
        raise ValueError("KR company name is required")
    return normalized


class _DescriptionTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {"br", "div", "li", "ol", "p", "table", "td", "th", "tr", "ul"}
    )
    _IGNORED_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def html_to_text(value: str | None) -> str | None:
    """description HTML에서 태그와 엔티티를 제거하고 공백을 정규화한다."""
    if not value:
        return None
    parser = _DescriptionTextExtractor()
    parser.feed(value)
    parser.close()
    normalized = " ".join("".join(parser.parts).split())
    return normalized or None


def _published_at(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise GoogleNewsRssError(f"invalid pubDate: {value!r}") from exc
    if parsed.tzinfo is None:
        raise GoogleNewsRssError(f"pubDate has no timezone: {value!r}")
    # DB 열은 공시와 같은 규약인 tz 없는 KST로 저장해 UTC 대비 9시간 어긋남을 막는다.
    return parsed.astimezone(_KST).replace(tzinfo=None)


def _normalized_item(item: ET.Element, *, index: int) -> FeedArticleInput:
    raw_title = item.findtext("title")
    raw_url = item.findtext("link")
    if raw_title is None or not raw_title.strip():
        raise GoogleNewsRssError(f"item[{index}] missing required title")
    if raw_url is None or not raw_url.strip():
        raise GoogleNewsRssError(f"item[{index}] missing required link")

    url = raw_url.strip()
    if len(url) > 2048:
        raise GoogleNewsRssError(f"item[{index}] link exceeds 2048 characters")
    source_text = item.findtext("source")
    source = " ".join(source_text.split()) if source_text else None
    title = " ".join(raw_title.split())
    if source:
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
    if not title:
        raise GoogleNewsRssError(f"item[{index}] title is empty after normalization")

    return FeedArticleInput(
        url=url,
        title=title[:500],
        source=source[:100] if source else None,
        published_at=_published_at(item.findtext("pubDate")),
        summary=html_to_text(item.findtext("description")),
    )


def parse_google_news_rss(
    payload: bytes,
    *,
    max_items: int = MAX_ITEMS_PER_SYMBOL,
    excluded_sources: Collection[str] = DEFAULT_EXCLUDED_SOURCES,
) -> GoogleNewsRssFeed:
    """RSS 2.0 바이트를 소스 제외 규칙과 기사 상한에 맞춰 파싱한다."""
    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise GoogleNewsRssError(f"invalid RSS XML: {exc}") from exc
    if root.tag != "rss":
        raise GoogleNewsRssError(f"unexpected RSS root: {root.tag!r}")
    channel = root.find("channel")
    if channel is None:
        raise GoogleNewsRssError("RSS channel is missing")

    excluded_keys = {
        source.strip().casefold() for source in excluded_sources if source.strip()
    }
    item_nodes = channel.findall("item")
    items: list[FeedArticleInput] = []
    excluded_count = 0
    processed_count = 0
    for index, item_node in enumerate(item_nodes):
        if len(items) >= max_items:
            break
        item = _normalized_item(item_node, index=index)
        processed_count += 1
        if item.source and item.source.casefold() in excluded_keys:
            excluded_count += 1
            continue
        items.append(item)
    return GoogleNewsRssFeed(
        items=tuple(items),
        truncated_count=max(0, len(item_nodes) - processed_count),
        excluded_count=excluded_count,
    )


async def fetch_google_news_rss(
    *,
    market: str,
    query: str,
    client: GoogleNewsHttpClient | None = None,
    max_items: int = MAX_ITEMS_PER_SYMBOL,
    excluded_sources: Collection[str] = DEFAULT_EXCLUDED_SOURCES,
) -> GoogleNewsRssFeed:
    """한 시장·검색어의 Google News RSS를 받아 정규화한다."""
    config = market_config(market)
    params = {"q": query, "hl": config.hl, "gl": config.gl, "ceid": config.ceid}
    if client is None:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as owned_client:
            response = await owned_client.get(GOOGLE_NEWS_RSS_URL, params=params)
    else:
        response = await client.get(GOOGLE_NEWS_RSS_URL, params=params)
    if response.status_code != 200:
        raise GoogleNewsRssError(
            f"Google News RSS HTTP {response.status_code} for market={config.market}"
        )
    return parse_google_news_rss(
        response.content,
        max_items=max_items,
        excluded_sources=excluded_sources,
    )
