"""Google News RSS를 시장별 후보 종목과 함께 통합 뉴스 저장소에 적재한다."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from urllib import robotparser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Comment
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import symbol_news_store
from app.services.google_news_rss import (
    DEFAULT_EXCLUDED_SOURCES,
    GOOGLE_NEWS_FEED_SOURCE,
    MAX_ITEMS_PER_SYMBOL,
    GoogleNewsHttpClient,
    GoogleNewsRssError,
    build_symbol_query,
    fetch_google_news_rss,
    market_config,
)
from app.services.news_summary_service import summarize_ingested_news
from app.services.symbol_news_store import (
    FeedArticleInput,
    FeedArticleUpsertCounts,
)

logger = logging.getLogger(__name__)

REQUEST_INTERVAL_SECONDS = 1.0
_HTTP_TIMEOUT_SECONDS = 15.0
ARTICLE_FETCH_TIMEOUT_SECONDS = 8.0
ARTICLE_ENRICHMENT_BATCH_SIZE = 20
ARTICLE_ENRICHMENT_CONCURRENCY = 4
ARTICLE_MAX_REDIRECTS = 3
ARTICLE_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ARTICLE_MAX_TEXT_CHARS = 12_000
ARTICLE_ROBOTS_MAX_RESPONSE_BYTES = 256 * 1024
ARTICLE_MIN_TEXT_CHARS = 160
_ARTICLE_USER_AGENT = "KAsset-Trader-Core news-ingestion/1.0"
_ARTICLE_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_GOOGLE_NEWS_HOST = "news.google.com"
_GOOGLE_NEWS_BATCH_URL = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute"
)
_GOOGLE_NEWS_BATCH_RPC = "Fbv4je"
_PAYWALL_RE = re.compile(
    r"(?:subscribe|sign in|register)\s+to\s+continue|"
    r"(?:유료|구독)\s*(?:회원|서비스).{0,20}(?:전용|가입|로그인)|"
    r"기사\s*전문은\s*(?:구독|로그인)",
    re.IGNORECASE,
)
_ZERO_COUNTS = FeedArticleUpsertCounts(inserted=0, updated=0, skipped=0)


@dataclass(frozen=True)
class GoogleNewsSymbol:
    symbol: str
    name: str


@dataclass(frozen=True)
class _CollectedFeeds:
    articles_by_url: dict[str, FeedArticleInput]
    urls_by_symbol: dict[str, set[str]]
    duplicate_count: int
    truncated_count: int
    excluded_count: int
    successful_symbols: int
    errors: tuple[str, ...]


type ClientOrNone = GoogleNewsHttpClient | None

class NewsArticleFetchError(RuntimeError):
    """기사 원문을 안전 경계 안에서 확보하지 못했음을 나타낸다."""


class NewsArticleBodyFetcher(Protocol):
    async def fetch(self, url: str) -> str: ...


type HostResolver = Callable[[str], Awaitable[Sequence[str]]]


async def _resolve_host_addresses(host: str) -> Sequence[str]:
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise NewsArticleFetchError("article host DNS resolution failed") from exc
    return tuple({str(result[4][0]) for result in results})


def _validated_article_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise NewsArticleFetchError("invalid article URL") from exc
    if parsed.scheme.lower() != "https":
        raise NewsArticleFetchError("article URL scheme must be https")
    if parsed.username is not None or parsed.password is not None:
        raise NewsArticleFetchError("article URL credentials are not allowed")
    if port not in (None, 443):
        raise NewsArticleFetchError("article URL port is not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NewsArticleFetchError("article URL host is missing")
    return host


async def _assert_public_article_target(url: str, resolver: HostResolver) -> str:
    host = _validated_article_host(url)
    if host == _GOOGLE_NEWS_HOST:
        return host
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        addresses = await resolver(host)
        if not addresses:
            raise NewsArticleFetchError("article host DNS returned no addresses")
    else:
        addresses = (str(literal_address),)
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise NewsArticleFetchError("article host resolves to a non-public address")
    except ValueError as exc:
        raise NewsArticleFetchError("article host DNS returned an invalid address") from exc
    return host


def _is_google_owned_host(host: str) -> bool:
    return (
        host == _GOOGLE_NEWS_HOST
        or host.endswith(".google.com")
        or host.endswith(".googleusercontent.com")
        or host.endswith(".gstatic.com")
    )


def _external_google_news_url(value: str, *, base_url: str) -> str | None:
    candidate = urljoin(base_url, value.strip())
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if _is_google_owned_host(host):
        query = parse_qs(parsed.query)
        nested = next(iter(query.get("url", ()) or query.get("q", ())), None)
        if nested is None:
            return None
        candidate = nested
        try:
            nested_host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
        except ValueError:
            return None
        if _is_google_owned_host(nested_host):
            return None
    try:
        if _validated_article_host(candidate) == _GOOGLE_NEWS_HOST:
            return None
    except NewsArticleFetchError:
        return None
    return candidate


def _provider_url_from_google_html(body: str, *, base_url: str) -> str | None:
    soup = BeautifulSoup(body, "lxml")
    for selector, attribute in (
        ('meta[property="og:url"]', "content"),
        ('link[rel="canonical"]', "href"),
    ):
        for element in soup.select(selector):
            value = element.get(attribute)
            if not isinstance(value, str):
                continue
            candidate = _external_google_news_url(value, base_url=base_url)
            if candidate is not None:
                return candidate
    return None


def _google_decode_params(body: str, *, base_url: str) -> tuple[str, int, str] | None:
    soup = BeautifulSoup(body, "lxml")
    signed = soup.select_one("[data-n-a-sg][data-n-a-ts]")
    if signed is None:
        return None
    signature = signed.get("data-n-a-sg")
    timestamp = signed.get("data-n-a-ts")
    token = urlsplit(base_url).path.rstrip("/").rsplit("/", 1)[-1]
    if (
        not isinstance(signature, str)
        or not signature
        or len(signature) > 4096
        or not isinstance(timestamp, str)
        or not timestamp.isdecimal()
        or not token
        or len(token) > 4096
    ):
        return None
    return token, int(timestamp), signature


def _google_batch_provider_url(body: str) -> str | None:
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate.startswith("["):
            continue
        try:
            rows = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) < 3
                or row[0] != "wrb.fr"
                or row[1] != _GOOGLE_NEWS_BATCH_RPC
                or not isinstance(row[2], str)
            ):
                continue
            try:
                decoded = json.loads(row[2])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(decoded, list)
                and len(decoded) > 1
                and isinstance(decoded[1], str)
            ):
                return decoded[1]
    return None


def _extract_article_text(body: str, *, max_chars: int = ARTICLE_MAX_TEXT_CHARS) -> str:
    soup = BeautifulSoup(body, "lxml")
    for element in soup(
        [
            "script",
            "style",
            "noscript",
            "template",
            "svg",
            "nav",
            "header",
            "footer",
            "aside",
            "form",
        ]
    ):
        element.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    candidates: list[str] = []
    for selector in (
        "[itemprop='articleBody']",
        "article",
        "main",
        ".article-body",
        ".article-content",
        ".story-body",
        ".story-content",
    ):
        for element in soup.select(selector):
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                candidates.append(text)

    paragraphs = [
        " ".join(element.get_text(" ", strip=True).split())
        for element in soup.find_all("p")
    ]
    paragraph_text = " ".join(
        paragraph for paragraph in paragraphs if len(paragraph) >= 40
    )
    if paragraph_text:
        candidates.append(paragraph_text)
    for selector in (
        'meta[name="description"]',
        'meta[property="og:description"]',
    ):
        for element in soup.select(selector):
            value = element.get("content")
            if isinstance(value, str):
                normalized = " ".join(value.split())
                if normalized:
                    candidates.append(normalized)
    if "<" not in body:
        plain_text = " ".join(body.split())
        if plain_text:
            candidates.append(plain_text)

    text = max(candidates, key=len, default="")
    if _PAYWALL_RE.search(text):
        raise NewsArticleFetchError("article body is behind a paywall")
    if len(text) < ARTICLE_MIN_TEXT_CHARS:
        raise NewsArticleFetchError("article body is missing or too short")
    return text[:max_chars].rstrip()


async def _read_article_response(response: httpx.Response) -> bytes:
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not (
        content_type.startswith("text/html")
        or content_type.startswith("application/xhtml+xml")
        or content_type.startswith("text/plain")
    ):
        raise NewsArticleFetchError("article response is not HTML or text")
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > ARTICLE_MAX_RESPONSE_BYTES:
                raise NewsArticleFetchError("article response exceeds the size limit")
        except ValueError:
            pass

    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.aiter_bytes():
        consumed += len(chunk)
        if consumed > ARTICLE_MAX_RESPONSE_BYTES:
            raise NewsArticleFetchError("article response exceeds the size limit")
        chunks.append(chunk)
    if not chunks:
        raise NewsArticleFetchError("article response body is empty")
    return b"".join(chunks)


def _decode_article_body(response: httpx.Response, payload: bytes) -> str:
    encoding = response.encoding or "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    empty_error: str,
    oversized_error: str,
) -> bytes:
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > max_bytes:
                raise NewsArticleFetchError(oversized_error)
        except ValueError:
            pass
    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.aiter_bytes():
        consumed += len(chunk)
        if consumed > max_bytes:
            raise NewsArticleFetchError(oversized_error)
        chunks.append(chunk)
    if not chunks:
        raise NewsArticleFetchError(empty_error)
    return b"".join(chunks)


class GoogleNewsArticleFetcher:
    """Google News 링크를 공개 HTTPS 원문까지만 따라가 제한 본문을 추출한다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        resolver: HostResolver = _resolve_host_addresses,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._robots_cache: dict[str, robotparser.RobotFileParser | None] = {}

    async def _assert_robots_allowed(self, url: str, host: str) -> None:
        if host in self._robots_cache:
            rules = self._robots_cache[host]
        else:
            parsed = urlsplit(url)
            robots_url = f"https://{parsed.netloc}/robots.txt"
            request = self._client.build_request(
                "GET",
                robots_url,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": _ARTICLE_USER_AGENT,
                },
            )
            try:
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise NewsArticleFetchError(
                    f"robots fetch failed: {type(exc).__name__}"
                ) from exc
            try:
                if response.status_code == 404:
                    rules = None
                elif response.status_code in _ARTICLE_REDIRECT_STATUSES:
                    raise NewsArticleFetchError("robots redirect is not allowed")
                elif not response.is_success:
                    raise NewsArticleFetchError(
                        f"robots fetch returned HTTP {response.status_code}"
                    )
                else:
                    chunks: list[bytes] = []
                    consumed = 0
                    async for chunk in response.aiter_bytes():
                        consumed += len(chunk)
                        if consumed > ARTICLE_ROBOTS_MAX_RESPONSE_BYTES:
                            raise NewsArticleFetchError(
                                "robots response exceeds the size limit"
                            )
                        chunks.append(chunk)
                    rules = robotparser.RobotFileParser()
                    rules.set_url(robots_url)
                    rules.parse(
                        b"".join(chunks)
                        .decode("utf-8", errors="replace")
                        .splitlines()
                    )
            finally:
                await response.aclose()
            self._robots_cache[host] = rules
        if rules is not None and not rules.can_fetch(_ARTICLE_USER_AGENT, url):
            raise NewsArticleFetchError("article fetch is disallowed by robots.txt")

    async def _decode_google_provider_url(
        self,
        body: str,
        *,
        base_url: str,
    ) -> str | None:
        params = await asyncio.to_thread(
            _google_decode_params,
            body,
            base_url=base_url,
        )
        if params is None:
            return None
        token, timestamp, signature = params
        rpc_argument = [
            "garturlreq",
            [
                [
                    "X",
                    "X",
                    ["X", "X"],
                    None,
                    None,
                    1,
                    1,
                    "US:en",
                    None,
                    1,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    1,
                ],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            token,
            timestamp,
            signature,
        ]
        rpc = [_GOOGLE_NEWS_BATCH_RPC, json.dumps(rpc_argument)]
        request = self._client.build_request(
            "POST",
            _GOOGLE_NEWS_BATCH_URL,
            headers={
                "Accept": "application/json,text/plain;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": _ARTICLE_USER_AGENT,
            },
            content=urlencode({"f.req": json.dumps([[rpc]])}),
        )
        try:
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise NewsArticleFetchError(
                f"Google News URL decode failed: {type(exc).__name__}"
            ) from exc
        try:
            if not response.is_success:
                raise NewsArticleFetchError(
                    f"Google News URL decode returned HTTP {response.status_code}"
                )
            payload = await _read_bounded_response(
                response,
                max_bytes=ARTICLE_MAX_RESPONSE_BYTES,
                empty_error="Google News URL decode response is empty",
                oversized_error="Google News URL decode response exceeds the size limit",
            )
            decoded_url = await asyncio.to_thread(
                _google_batch_provider_url,
                _decode_article_body(response, payload),
            )
        finally:
            await response.aclose()
        if decoded_url is None:
            return None
        return _external_google_news_url(
            decoded_url,
            base_url=_GOOGLE_NEWS_BATCH_URL,
        )

    async def fetch(self, url: str) -> str:
        current_url = url
        for hop in range(ARTICLE_MAX_REDIRECTS + 1):
            host = await _assert_public_article_target(current_url, self._resolver)
            if host != _GOOGLE_NEWS_HOST:
                await self._assert_robots_allowed(current_url, host)
            request = self._client.build_request(
                "GET",
                current_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                    "User-Agent": _ARTICLE_USER_AGENT,
                },
            )
            try:
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise NewsArticleFetchError(
                    f"article fetch failed: {type(exc).__name__}"
                ) from exc
            try:
                if response.status_code in _ARTICLE_REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise NewsArticleFetchError(
                            "article redirect is missing Location"
                        )
                    if hop >= ARTICLE_MAX_REDIRECTS:
                        raise NewsArticleFetchError("too many article redirects")
                    current_url = urljoin(str(response.url), location)
                    continue
                if not response.is_success:
                    raise NewsArticleFetchError(
                        f"article fetch returned HTTP {response.status_code}"
                    )
                payload = await _read_article_response(response)
                body = _decode_article_body(response, payload)
            finally:
                await response.aclose()

            if host == _GOOGLE_NEWS_HOST:
                provider_url = await asyncio.to_thread(
                    _provider_url_from_google_html,
                    body,
                    base_url=str(response.url),
                )
                if provider_url is None:
                    provider_url = await self._decode_google_provider_url(
                        body,
                        base_url=str(response.url),
                    )
                if provider_url is None:
                    raise NewsArticleFetchError(
                        "Google News page has no decodable public provider URL"
                    )
                if hop >= ARTICLE_MAX_REDIRECTS:
                    raise NewsArticleFetchError("too many article redirects")
                current_url = provider_url
                continue
            return await asyncio.to_thread(_extract_article_text, body)
        raise NewsArticleFetchError("too many article redirects")


def _newest_article_items(
    collected: _CollectedFeeds,
    *,
    limit: int,
) -> list[FeedArticleInput]:
    return sorted(
        collected.articles_by_url.values(),
        key=lambda item: (
            item.published_at is not None,
            item.published_at or datetime.min,
            item.url,
        ),
        reverse=True,
    )[:limit]


def _safe_log_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "<missing>"
    except ValueError:
        return "<invalid>"


async def _enrich_collected_articles(
    collected: _CollectedFeeds,
    fetcher: NewsArticleBodyFetcher,
) -> _CollectedFeeds:
    semaphore = asyncio.Semaphore(ARTICLE_ENRICHMENT_CONCURRENCY)

    async def enrich(item: FeedArticleInput) -> tuple[str, str | None]:
        async with semaphore:
            try:
                return item.url, await fetcher.fetch(item.url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "Google News 원문 확보 실패: host=%s error_type=%s",
                    _safe_log_host(item.url),
                    type(exc).__name__,
                )
                return item.url, None

    candidates = _newest_article_items(
        collected,
        limit=ARTICLE_ENRICHMENT_BATCH_SIZE,
    )
    if not candidates:
        return collected
    fetched = dict(await asyncio.gather(*(enrich(item) for item in candidates)))
    enriched_items = {
        url: (
            replace(item, article_content=fetched[url])
            if fetched.get(url)
            else item
        )
        for url, item in collected.articles_by_url.items()
    }
    return replace(collected, articles_by_url=enriched_items)


async def _enrich_with_client(
    collected: _CollectedFeeds,
    *,
    fetcher: NewsArticleBodyFetcher | None,
    rss_client_was_injected: bool,
) -> _CollectedFeeds:
    if fetcher is not None:
        return await _enrich_collected_articles(collected, fetcher)
    if rss_client_was_injected:
        return collected
    async with httpx.AsyncClient(
        timeout=ARTICLE_FETCH_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        return await _enrich_collected_articles(
            collected,
            GoogleNewsArticleFetcher(client),
        )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


async def _collect_feeds(
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    client: GoogleNewsHttpClient,
    request_interval_seconds: float,
    max_items_per_symbol: int,
    excluded_sources: Collection[str],
) -> _CollectedFeeds:
    articles_by_url: dict[str, FeedArticleInput] = {}
    urls_by_symbol: dict[str, set[str]] = {}
    duplicate_count = 0
    truncated_count = 0
    excluded_count = 0
    successful_symbols = 0
    errors: list[str] = []

    for index, candidate in enumerate(symbols):
        if index > 0 and request_interval_seconds > 0:
            await asyncio.sleep(request_interval_seconds)
        try:
            query = build_symbol_query(market=market, name=candidate.name)
            feed = await fetch_google_news_rss(
                market=market,
                query=query,
                client=client,
                max_items=max_items_per_symbol,
                excluded_sources=excluded_sources,
            )
        except (GoogleNewsRssError, httpx.HTTPError, ValueError) as exc:
            errors.append(f"{candidate.symbol}: {exc}")
            logger.warning(
                "Google News RSS 종목 수집 실패: market=%s symbol=%s error=%s",
                market,
                candidate.symbol,
                exc,
            )
            continue

        successful_symbols += 1
        truncated_count += feed.truncated_count
        excluded_count += feed.excluded_count
        symbol_urls = urls_by_symbol.setdefault(candidate.symbol, set())
        for item in feed.items:
            symbol_urls.add(item.url)
            if item.url in articles_by_url:
                duplicate_count += 1
                continue
            articles_by_url[item.url] = item

    return _CollectedFeeds(
        articles_by_url=articles_by_url,
        urls_by_symbol=urls_by_symbol,
        duplicate_count=duplicate_count,
        truncated_count=truncated_count,
        excluded_count=excluded_count,
        successful_symbols=successful_symbols,
        errors=tuple(errors),
    )


async def _collect_with_client(
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    client: ClientOrNone,
    request_interval_seconds: float,
    max_items_per_symbol: int,
    excluded_sources: Collection[str],
) -> _CollectedFeeds:
    if client is not None:
        return await _collect_feeds(
            market=market,
            symbols=symbols,
            client=client,
            request_interval_seconds=request_interval_seconds,
            max_items_per_symbol=max_items_per_symbol,
            excluded_sources=excluded_sources,
        )
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as owned_client:
        return await _collect_feeds(
            market=market,
            symbols=symbols,
            client=owned_client,
            request_interval_seconds=request_interval_seconds,
            max_items_per_symbol=max_items_per_symbol,
            excluded_sources=excluded_sources,
        )


def _run_status(*, symbol_count: int, collected: _CollectedFeeds) -> str:
    if not collected.errors:
        return "success"
    if collected.successful_symbols > 0 or symbol_count == 0:
        return "partial"
    return "failed"


def _error_message(errors: tuple[str, ...]) -> str | None:
    if not errors:
        return None
    return "; ".join(errors)[:2000]


async def _summarize_after_ingest(
    db: AsyncSession,
    article_urls: Collection[str],
) -> None:
    try:
        result = await summarize_ingested_news(db, tuple(article_urls))
    except Exception:
        await db.rollback()
        logger.exception("Google News RSS 자동 요약 batch 실패")
        return
    logger.info(
        "Google News RSS 자동 요약: status=%s selected=%d summarized=%d failed=%d",
        result.status,
        result.selected,
        result.summarized,
        result.failed,
    )


async def _record_failed_run(
    db: AsyncSession,
    *,
    run_uuid: str,
    market: str,
    started_at: datetime,
    error: BaseException,
) -> None:
    await db.rollback()
    raw_message = str(error).strip()
    message = (raw_message or type(error).__name__)[:2000]
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=market,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status="failed",
        finished_at=_utcnow(),
        counts=_ZERO_COUNTS,
        error_message=message,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await db.commit()


async def _persist_collected(
    db: AsyncSession,
    *,
    run_uuid: str,
    market: str,
    started_at: datetime,
    symbol_count: int,
    collected: _CollectedFeeds,
) -> FeedArticleUpsertCounts:
    unique_items = list(collected.articles_by_url.values())
    changed = await symbol_news_store.count_feed_article_changes(db, unique_items)
    counts = FeedArticleUpsertCounts(
        inserted=changed.inserted,
        updated=changed.updated,
        skipped=(
            changed.skipped
            + collected.duplicate_count
            + collected.truncated_count
            + collected.excluded_count
        ),
    )
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=market,
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    for symbol, urls in collected.urls_by_symbol.items():
        items = [collected.articles_by_url[url] for url in urls]
        await symbol_news_store.upsert_feed_articles(
            db,
            market,
            symbol,
            items,
            feed_source=GOOGLE_NEWS_FEED_SOURCE,
            commit=False,
        )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status=_run_status(symbol_count=symbol_count, collected=collected),
        finished_at=_utcnow(),
        counts=counts,
        error_message=_error_message(collected.errors),
        feed_source=GOOGLE_NEWS_FEED_SOURCE,
    )
    await db.commit()
    return counts


async def ingest_google_news_rss(
    db: AsyncSession,
    *,
    market: str,
    symbols: list[GoogleNewsSymbol],
    run_uuid: str | None = None,
    client: ClientOrNone = None,
    article_fetcher: NewsArticleBodyFetcher | None = None,
    request_interval_seconds: float = REQUEST_INTERVAL_SECONDS,
    max_items_per_symbol: int = MAX_ITEMS_PER_SYMBOL,
    excluded_sources: Collection[str] = DEFAULT_EXCLUDED_SOURCES,
) -> tuple[int, int, int]:
    """한 시장의 종목 피드를 직렬 수집하고 ``(신규, 갱신, 스킵)``을 반환한다.

    검색 종목은 확정 연관 종목이 아니라 관련도 판정 전 후보로만 저장한다.
    최신 고유 기사 중 제한된 수만 공개 HTTPS 원문까지 따라가며, 원문 실패는
    기사별로 격리한다. RSS HTTP·형식 오류는 종목 단위로 격리하며 성공 종목이
    하나라도 있으면 회차는 ``partial``, 전부 실패하면 ``failed``로 닫는다.
    """
    config = market_config(market)
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must not be negative")
    if max_items_per_symbol < 1:
        raise ValueError("max_items_per_symbol must be at least 1")

    current_run_uuid = run_uuid or str(uuid.uuid4())
    started_at = _utcnow()
    try:
        collected = await _collect_with_client(
            market=config.market,
            symbols=symbols,
            client=client,
            request_interval_seconds=request_interval_seconds,
            excluded_sources=excluded_sources,
            max_items_per_symbol=max_items_per_symbol,
        )
        collected = await _enrich_with_client(
            collected,
            fetcher=article_fetcher,
            rss_client_was_injected=client is not None,
        )
        counts = await _persist_collected(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            symbol_count=len(symbols),
            collected=collected,
        )
    except asyncio.CancelledError as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            error=exc,
        )
        raise
    except Exception as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            market=config.market,
            started_at=started_at,
            error=exc,
        )
        logger.exception(
            "Google News RSS 회차 실패: market=%s run_uuid=%s",
            config.market,
            current_run_uuid,
        )
        raise
    if collected.articles_by_url:
        await _summarize_after_ingest(db, collected.articles_by_url)
    return counts.inserted, counts.updated, counts.skipped
