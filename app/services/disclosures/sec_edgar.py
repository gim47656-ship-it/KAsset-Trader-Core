"""SEC EDGAR 티커·제출내역 조회와 공시 정규화."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
import redis.asyncio as redis

from app.core.async_rate_limiter import (
    AsyncSlidingWindowRateLimiter,
    get_limiter,
)
from app.core.config import settings
from app.services.disclosures.feed_sources import SEC_FEED_SOURCE
from app.services.symbol_news_store import DisclosureArticleInput

logger = logging.getLogger(__name__)

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
)
SEC_MARKET = "us"
SEC_SOURCE = "SEC EDGAR"
SEC_USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
SEC_FORM_KEYWORD_PREFIX = "sec_form:"
COMPANY_TICKER_CACHE_TTL_SECONDS = 24 * 60 * 60
_COMPANY_TICKER_REDIS_KEY = "sec_edgar:company_tickers:v1"
_KST = timezone(timedelta(hours=9))
_CONTACT_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class SecEdgarError(RuntimeError):
    """SEC 응답이나 설정이 수집 계약을 만족하지 않을 때 발생한다."""


class SecEdgarHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response: ...


class SecRateLimiter(Protocol):
    async def acquire(self) -> object: ...


@dataclass(frozen=True)
class ParsedSubmissions:
    company_name: str
    items: tuple[DisclosureArticleInput, ...]
    form_counts: Mapping[str, int]


def resolve_sec_user_agent(explicit: str | None = None) -> str:
    """운영 설정에서 연락 이메일이 포함된 SEC User-Agent를 읽는다."""
    value = explicit if explicit is not None else os.getenv(SEC_USER_AGENT_ENV)
    normalized = value.strip() if value else ""
    contact = _CONTACT_EMAIL_RE.search(normalized)
    if contact is None or not normalized[: contact.start()].strip():
        raise SecEdgarError(
            f"{SEC_USER_AGENT_ENV} must identify the caller and include a contact email"
        )
    return normalized


async def get_shared_sec_rate_limiter() -> AsyncSlidingWindowRateLimiter:
    return await get_limiter(
        "sec_edgar",
        "_global",
        rate=10,
        period=1.0,
    )


class SecEdgarClient:
    """필수 User-Agent와 프로세스 공용 10 req/s 제한을 적용하는 HTTP 경계."""

    def __init__(
        self,
        http_client: SecEdgarHttpClient,
        *,
        user_agent: str,
        rate_limiter: SecRateLimiter | None = None,
    ) -> None:
        self._http_client = http_client
        self._user_agent = resolve_sec_user_agent(user_agent)
        self._rate_limiter = rate_limiter

    async def get_json(self, url: str) -> Mapping[str, Any]:
        limiter = self._rate_limiter or await get_shared_sec_rate_limiter()
        try:
            await limiter.acquire()
            response = await self._http_client.get(
                url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SecEdgarError(f"SEC EDGAR request failed for {url}: {exc}") from exc
        if response.status_code != 200:
            raise SecEdgarError(f"SEC EDGAR HTTP {response.status_code} for {url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SecEdgarError(f"SEC EDGAR invalid JSON for {url}") from exc
        if not isinstance(payload, Mapping):
            raise SecEdgarError(f"SEC EDGAR JSON root must be an object for {url}")
        return payload


type TickerRedisFactory = Callable[[], Awaitable[Any]]


async def _create_ticker_redis_client() -> Any:
    return redis.from_url(
        settings.get_redis_url(),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        decode_responses=True,
    )


class CompanyTickerCache:
    """성공한 SEC 티커 맵을 Redis와 프로세스 메모리에서 24시간 재사용한다."""

    def __init__(
        self,
        *,
        ttl_seconds: float = COMPANY_TICKER_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        redis_factory: TickerRedisFactory | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._redis_factory = redis_factory
        self._redis_client: Any | None = None
        self._expires_at = 0.0
        self._value: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    async def _get_redis_client(self) -> Any | None:
        if self._redis_client is not None:
            return self._redis_client
        if self._redis_factory is None:
            return None
        try:
            self._redis_client = await self._redis_factory()
        except Exception as exc:  # noqa: BLE001 — 캐시 장애는 원천 조회로 복구
            logger.warning(
                "SEC ticker Redis 초기화 실패: error_type=%s",
                type(exc).__name__,
            )
            return None
        return self._redis_client

    async def _read_shared(self) -> dict[str, str] | None:
        redis_client = await self._get_redis_client()
        if redis_client is None:
            return None
        try:
            raw = await redis_client.get(_COMPANY_TICKER_REDIS_KEY)
        except Exception as exc:  # noqa: BLE001 — 캐시 장애는 원천 조회로 복구
            logger.warning(
                "SEC ticker Redis 읽기 실패: error_type=%s",
                type(exc).__name__,
            )
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                return None
            parsed = {
                ticker.upper(): normalize_cik(cik)
                for ticker, cik in payload.items()
                if isinstance(ticker, str) and ticker.strip()
            }
        except (SecEdgarError, TypeError, ValueError):
            logger.warning("SEC ticker Redis 캐시 형식이 잘못되어 원천을 다시 조회")
            return None
        return parsed or None

    async def _write_shared(self, value: Mapping[str, str]) -> None:
        redis_client = await self._get_redis_client()
        if redis_client is None:
            return
        try:
            await redis_client.set(
                _COMPANY_TICKER_REDIS_KEY,
                json.dumps(value, sort_keys=True),
                ex=max(1, int(self._ttl_seconds)),
            )
        except Exception as exc:  # noqa: BLE001 — 메모리 캐시는 이미 유효
            logger.warning(
                "SEC ticker Redis 쓰기 실패: error_type=%s",
                type(exc).__name__,
            )

    async def get(self, client: SecEdgarClient) -> dict[str, str]:
        now = self._clock()
        if self._value is not None and now < self._expires_at:
            return dict(self._value)
        async with self._lock:
            now = self._clock()
            if self._value is not None and now < self._expires_at:
                return dict(self._value)
            parsed = await self._read_shared()
            if parsed is None:
                payload = await client.get_json(SEC_COMPANY_TICKERS_URL)
                parsed = parse_company_tickers(payload)
                await self._write_shared(parsed)
            self._value = parsed
            self._expires_at = now + self._ttl_seconds
            return dict(parsed)


company_ticker_cache = CompanyTickerCache(
    redis_factory=_create_ticker_redis_client,
)


def _normalized_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise SecEdgarError(f"SEC field {field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise SecEdgarError(f"SEC field {field} must not be empty")
    return normalized


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SecEdgarError(f"SEC field {field} must be a string or null")
    normalized = value.strip()
    return normalized or None


def normalize_cik(value: object) -> str:
    """CIK를 선행 0 없는 숫자 문자열로 정규화한다."""
    if isinstance(value, bool):
        raise SecEdgarError("SEC CIK must be a positive integer")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise SecEdgarError("SEC CIK must be a positive integer")
    if not text.isdigit() or int(text) <= 0:
        raise SecEdgarError(f"invalid SEC CIK: {value!r}")
    return str(int(text))


def padded_cik(value: object) -> str:
    normalized = normalize_cik(value)
    if len(normalized) > 10:
        raise SecEdgarError(f"SEC CIK exceeds 10 digits: {value!r}")
    return normalized.zfill(10)


def parse_company_tickers(payload: Mapping[str, Any]) -> dict[str, str]:
    """company_tickers 열을 대문자 티커→선행 0 없는 CIK로 변환한다."""
    result: dict[str, str] = {}
    for key, raw_row in payload.items():
        if not isinstance(raw_row, Mapping):
            raise SecEdgarError(f"SEC ticker row {key!r} must be an object")
        ticker = _normalized_text(raw_row.get("ticker"), field="ticker").upper()
        cik = normalize_cik(raw_row.get("cik_str"))
        existing = result.get(ticker)
        if existing is not None and existing != cik:
            raise SecEdgarError(f"SEC ticker {ticker} maps to multiple CIK values")
        result[ticker] = cik
    if not result:
        raise SecEdgarError("SEC company_tickers response is empty")
    return result


def build_submission_url(cik: object) -> str:
    return SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=padded_cik(cik))


def build_archive_url(
    *,
    cik: object,
    accession_number: object,
    primary_document: object,
) -> str:
    normalized_cik = normalize_cik(cik)
    accession = _normalized_text(
        accession_number,
        field="accessionNumber",
    ).replace("-", "")
    if not accession.isdigit():
        raise SecEdgarError(f"invalid SEC accessionNumber: {accession_number!r}")
    document = _normalized_text(primary_document, field="primaryDocument")
    if document.startswith("/") or ".." in document.split("/"):
        raise SecEdgarError(f"invalid SEC primaryDocument: {primary_document!r}")
    return SEC_ARCHIVE_URL_TEMPLATE.format(
        cik=normalized_cik,
        accession=accession,
        document=document,
    )


def parse_acceptance_datetime(value: object) -> datetime | None:
    """SEC 접수 UTC 시각을 DB 규약인 tz 없는 KST로 변환한다."""
    normalized = _optional_text(value, field="acceptanceDateTime")
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecEdgarError(f"invalid SEC acceptanceDateTime: {normalized!r}") from exc
    if parsed.tzinfo is None:
        raise SecEdgarError(f"SEC acceptanceDateTime has no timezone: {normalized!r}")
    # DB 열은 다른 뉴스 수집기와 같은 규약인 tz 없는 KST로 저장한다.
    return parsed.astimezone(_KST).replace(tzinfo=None)


def _filing_date(value: object) -> date:
    normalized = _normalized_text(value, field="filingDate")
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise SecEdgarError(f"invalid SEC filingDate: {normalized!r}") from exc


def _title(
    *,
    form: str,
    description: str | None,
    items: str | None,
    company_name: str,
) -> str:
    equivalent_descriptions = {form.casefold(), f"form {form}".casefold()}
    if description and description.casefold() not in equivalent_descriptions:
        detail = description
    elif items:
        detail = items
    else:
        detail = company_name
    return f"{form} — {detail}"


def _recent_columns(payload: Mapping[str, Any]) -> Mapping[str, Sequence[object]]:
    filings = payload.get("filings")
    if not isinstance(filings, Mapping):
        raise SecEdgarError("SEC submissions filings must be an object")
    recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        raise SecEdgarError("SEC submissions filings.recent must be an object")
    accession_numbers = recent.get("accessionNumber")
    if not isinstance(accession_numbers, Sequence) or isinstance(
        accession_numbers, (str, bytes)
    ):
        raise SecEdgarError("SEC recent.accessionNumber must be an array")
    row_count = len(accession_numbers)
    columns: dict[str, Sequence[object]] = {}
    for key, raw_column in recent.items():
        if not isinstance(raw_column, Sequence) or isinstance(raw_column, (str, bytes)):
            raise SecEdgarError(f"SEC recent.{key} must be an array")
        if len(raw_column) != row_count:
            raise SecEdgarError(
                f"SEC recent column length mismatch: accessionNumber={row_count}, "
                f"{key}={len(raw_column)}"
            )
        columns[str(key)] = raw_column
    for required in ("form", "primaryDocument", "filingDate"):
        if required not in columns:
            raise SecEdgarError(f"SEC recent.{required} is missing")
    return columns


def _optional_column(
    columns: Mapping[str, Sequence[object]],
    key: str,
    row_count: int,
) -> Sequence[object]:
    return columns.get(key, (None,) * row_count)


def parse_submissions(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    cik: object,
    since_date: date,
) -> ParsedSubmissions:
    """최신순 열-배열을 날짜 하한까지 전량 공시 입력으로 변환한다."""
    company_name = _normalized_text(payload.get("name"), field="name")
    normalized_symbol = _normalized_text(symbol, field="symbol").upper()
    normalized_cik = normalize_cik(cik)
    columns = _recent_columns(payload)
    row_count = len(columns["accessionNumber"])
    descriptions = _optional_column(columns, "primaryDocDescription", row_count)
    filing_items = _optional_column(columns, "items", row_count)
    acceptances = _optional_column(columns, "acceptanceDateTime", row_count)
    normalized: list[DisclosureArticleInput] = []
    form_counts: Counter[str] = Counter()

    for index in range(row_count):
        if _filing_date(columns["filingDate"][index]) < since_date:
            break
        form = _normalized_text(columns["form"][index], field=f"form[{index}]")
        description = _optional_text(
            descriptions[index],
            field=f"primaryDocDescription[{index}]",
        )
        items = _optional_text(filing_items[index], field=f"items[{index}]")
        normalized.append(
            DisclosureArticleInput(
                url=build_archive_url(
                    cik=normalized_cik,
                    accession_number=columns["accessionNumber"][index],
                    primary_document=columns["primaryDocument"][index],
                ),
                title=_title(
                    form=form,
                    description=description,
                    items=items,
                    company_name=company_name,
                ),
                source=SEC_SOURCE,
                feed_source=SEC_FEED_SOURCE,
                market=SEC_MARKET,
                stock_symbol=normalized_symbol,
                stock_name=company_name,
                published_at=parse_acceptance_datetime(acceptances[index]),
                keywords=[f"{SEC_FORM_KEYWORD_PREFIX}{form}"],
            )
        )
        form_counts[form] += 1

    return ParsedSubmissions(
        company_name=company_name,
        items=tuple(normalized),
        form_counts=dict(form_counts),
    )
