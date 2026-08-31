"""Donald J. Trump 공식 Truth Social 게시물의 시장 뉴스 수집기."""

from __future__ import annotations

import html
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import symbol_news_store
from app.services.market_news_briefing_formatter import format_market_news_briefing
from app.services.news_summary_service import summarize_ingested_news
from app.services.symbol_news_store import FeedArticleInput

logger = logging.getLogger(__name__)

TRUTH_SOCIAL_HOST = "truthsocial.com"
TRUTH_SOCIAL_ACCOUNT_ID = "107780257626128497"
TRUTH_SOCIAL_ACCOUNT = "realDonaldTrump"
TRUTH_SOCIAL_PROFILE_URL = "https://truthsocial.com/@realDonaldTrump"
TRUTH_SOCIAL_FEED_SOURCE = "truth_social_official"
TRUTH_SOCIAL_SOURCE = "Donald J. Trump · Truth Social"
_LOOKUP_URL = "https://truthsocial.com/api/v1/accounts/lookup"
_STATUSES_URL = (
    f"https://truthsocial.com/api/v1/accounts/{TRUTH_SOCIAL_ACCOUNT_ID}/statuses"
)
_HTTP_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_POSTS = 40
_KST = timezone(timedelta(hours=9))


class TruthSocialError(RuntimeError):
    """공식 계정 또는 게시물 응답을 신뢰할 수 없음을 나타낸다."""


class TruthSocialHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response: ...


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


@dataclass(frozen=True, slots=True)
class TruthSocialIngestionResult:
    run_uuid: str
    fetched: int
    relevant: int
    inserted: int
    updated: int
    skipped: int
    summary_status: str
    summarized: int
    summary_failed: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _plain_text(value: object) -> str:
    parser = _PlainTextParser()
    parser.feed(str(value or ""))
    parser.close()
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _json_object(response: httpx.Response, *, label: str) -> dict[str, Any]:
    response.raise_for_status()
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise TruthSocialError(f"{label} response exceeds size limit")
    payload = response.json()
    if not isinstance(payload, dict):
        raise TruthSocialError(f"{label} response must be an object")
    return payload


def _json_array(response: httpx.Response, *, label: str) -> list[dict[str, Any]]:
    response.raise_for_status()
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise TruthSocialError(f"{label} response exceeds size limit")
    payload = response.json()
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise TruthSocialError(f"{label} response must be an object array")
    return payload


def _validate_account(account: dict[str, Any]) -> None:
    if (
        str(account.get("id") or "") != TRUTH_SOCIAL_ACCOUNT_ID
        or str(account.get("acct") or "") != TRUTH_SOCIAL_ACCOUNT
        or str(account.get("url") or "") != TRUTH_SOCIAL_PROFILE_URL
        or account.get("verified") is not True
    ):
        raise TruthSocialError("Truth Social official account identity mismatch")


def _status_url(status_id: str, value: object) -> str:
    expected = f"{TRUTH_SOCIAL_PROFILE_URL}/{status_id}"
    candidate = str(value or "")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise TruthSocialError("Truth Social status URL is invalid") from exc
    if (
        candidate != expected
        or parsed.scheme != "https"
        or (parsed.hostname or "").lower().rstrip(".") != TRUTH_SOCIAL_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TruthSocialError("Truth Social status URL identity mismatch")
    return candidate


def _published_at(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TruthSocialError("Truth Social created_at is invalid") from exc
    if parsed.tzinfo is None:
        raise TruthSocialError("Truth Social created_at has no timezone")
    return parsed.astimezone(_KST).replace(tzinfo=None)


def _market_relevant(title: str, article_content: str) -> bool:
    briefing = format_market_news_briefing(
        [
            {
                "title": title,
                "summary": article_content,
                "keywords": ["Donald Trump", "Truth Social"],
                "stock_symbol": None,
                "feed_source": TRUTH_SOCIAL_FEED_SOURCE,
            }
        ],
        market="us",
        limit=1,
    )
    return briefing.summary["included"] == 1


def _article_input(status: dict[str, Any]) -> FeedArticleInput | None:
    account = status.get("account")
    if not isinstance(account, dict):
        raise TruthSocialError("Truth Social status account is missing")
    _validate_account(account)
    if status.get("reblog") is not None or status.get("visibility") != "public":
        return None
    status_id = str(status.get("id") or "")
    if not status_id.isdecimal():
        raise TruthSocialError("Truth Social status id is invalid")
    url = _status_url(status_id, status.get("url") or status.get("uri"))
    post_text = _plain_text(status.get("content"))
    if not post_text:
        return None
    card = status.get("card")
    card_title = ""
    card_description = ""
    if isinstance(card, dict):
        card_title = " ".join(str(card.get("title") or "").split())
        card_description = " ".join(str(card.get("description") or "").split())
    article_content = "\n".join(
        part for part in (post_text, card_title, card_description) if part
    )[:4_000]
    if not _market_relevant(post_text, article_content):
        return None
    return FeedArticleInput(
        url=url,
        title=post_text[:500],
        source=TRUTH_SOCIAL_SOURCE,
        published_at=_published_at(status.get("created_at")),
        article_content=article_content,
    )


async def _collect(client: TruthSocialHttpClient) -> tuple[int, list[FeedArticleInput]]:
    account_response = await client.get(
        _LOOKUP_URL,
        params={"acct": TRUTH_SOCIAL_ACCOUNT},
    )
    _validate_account(_json_object(account_response, label="account lookup"))
    statuses_response = await client.get(
        _STATUSES_URL,
        params={
            "exclude_replies": "true",
            "exclude_reblogs": "true",
            "limit": str(_MAX_POSTS),
        },
    )
    statuses = _json_array(statuses_response, label="account statuses")
    items: list[FeedArticleInput] = []
    for status in statuses:
        item = _article_input(status)
        if item is not None:
            items.append(item)
    return len(statuses), items


async def ingest_truth_social(
    db: AsyncSession,
    *,
    http_client: TruthSocialHttpClient | None = None,
) -> TruthSocialIngestionResult:
    """공식 계정을 검증하고 시장 관련 게시물만 저장한 뒤 한국어로 요약한다."""

    run_uuid = f"truth-social-{uuid.uuid4().hex}"
    if http_client is None:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as owned_client:
            fetched, items = await _collect(owned_client)
    else:
        fetched, items = await _collect(http_client)

    started_at = datetime.now(tz=UTC).replace(tzinfo=None)
    counts = await symbol_news_store.count_feed_article_changes(db, items)
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market="us",
        feed_source=TRUTH_SOCIAL_FEED_SOURCE,
    )
    await symbol_news_store.upsert_feed_articles(
        db,
        "us",
        None,
        items,
        feed_source=TRUTH_SOCIAL_FEED_SOURCE,
        commit=False,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status="success",
        finished_at=datetime.now(tz=UTC).replace(tzinfo=None),
        counts=counts,
        error_message=None,
        feed_source=TRUTH_SOCIAL_FEED_SOURCE,
    )
    await db.commit()

    summary = await summarize_ingested_news(db, [item.url for item in items])
    logger.info(
        "Truth Social 공식 피드 수집: fetched=%d relevant=%d inserted=%d updated=%d summarized=%d failed=%d",
        fetched,
        len(items),
        counts.inserted,
        counts.updated,
        summary.summarized,
        summary.failed,
    )
    return TruthSocialIngestionResult(
        run_uuid=run_uuid,
        fetched=fetched,
        relevant=len(items),
        inserted=counts.inserted,
        updated=counts.updated,
        skipped=counts.skipped + fetched - len(items),
        summary_status=summary.status,
        summarized=summary.summarized,
        summary_failed=summary.failed,
    )


__all__ = [
    "TRUTH_SOCIAL_ACCOUNT",
    "TRUTH_SOCIAL_ACCOUNT_ID",
    "TRUTH_SOCIAL_FEED_SOURCE",
    "TRUTH_SOCIAL_PROFILE_URL",
    "TruthSocialError",
    "TruthSocialIngestionResult",
    "ingest_truth_social",
]
