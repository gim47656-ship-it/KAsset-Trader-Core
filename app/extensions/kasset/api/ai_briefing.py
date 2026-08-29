"""Read-only mobile AI hub reader over already-stored Core artifacts.

Guardrails owned by this module:

* Stored data only. Nothing here generates a summary, calls an AI provider, or
  triggers an ingestion run (ROB-501: no in-process LLM provider).
* News summaries come from the persisted ``NewsAnalysisResult.summary`` only;
  ``prompt``, ``raw_response`` and ``article_content`` never leave the DB.
* Research stays citation-shaped: title/publisher/link plus the same excerpt
  cap the ingestion contract already enforces (``DETAIL_EXCERPT_MAX``).
* Daily routine alerts are owner-scoped read-only evidence and never enter an
  Action, recommendation, or order producer.
* Briefings are projected from eligible ``InvestmentReport`` rows only, so the
  report ``portfolio_snapshot`` / ``market_snapshot`` payloads stay server-side.
* A query failure is surfaced as a sanitized 5xx, never as an empty payload.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.ai_schemas import (
    AiBriefingDataStatus,
    AiBriefingResponse,
    AiBriefingSection,
    AiBriefingStatus,
    AiNewsItem,
    AiNewsSection,
    AiResearchItem,
    AiResearchSection,
    AiResearchStatus,
    AiResearchSymbolRef,
    AiSymbolRef,
)
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import iso_z
from app.extensions.kasset.daily_routine_service import daily_routine_service
from app.models.investment_reports import InvestmentReport
from app.models.news import NewsAnalysisResult, NewsArticle, NewsArticleRelatedSymbol
from app.models.research_reports import ResearchReport
from app.schemas.research_reports import (
    DETAIL_EXCERPT_MAX,
    ResearchReportSymbolCandidate,
)
from app.services.research_reports.query_service import ResearchReportsQueryService

MobileAiMarket = Literal["kr", "us", "crypto"]

MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 10

_MARKETS: frozenset[str] = frozenset({"kr", "us", "crypto"})
_SYMBOL_MAX_CHARS = 40
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")

# Research ingestion is a daily batch; more than three days without a newer
# report means the mobile hub must label the section stale instead of implying
# the stored citations are current.
_RESEARCH_STALE_AFTER = timedelta(hours=72)

_ELIGIBLE_BRIEFING_STATUSES = ("published", "decided")
_ADVISORY_ONLY = "advisory_only"
# ROB-269 freshness vocabulary that the mobile hub can report verbatim; any
# other value (hard_stale/failed/unavailable/missing) collapses to "unknown".
_REPORTABLE_DATA_STATUSES: frozenset[str] = frozenset(
    {"fresh", "soft_stale", "partial"}
)
_STALE_DATA_STATUSES: frozenset[str] = frozenset({"soft_stale", "partial"})
# Android renders this string verbatim in the AI hub, so it must stay
# human-readable Korean rather than a machine code.
_NO_ELIGIBLE_BRIEFING = "저장된 AI 브리핑 제공자가 아직 연결되지 않았습니다."


def normalize_market(value: str) -> MobileAiMarket:
    market = value.strip().lower()
    if market not in _MARKETS:
        raise MobileApiError(
            422, "VALIDATION_ERROR", "market는 kr, us, crypto 중 하나여야 합니다."
        )
    return cast(MobileAiMarket, market)


def normalize_symbol(value: str | None) -> str | None:
    """Return the stored-symbol form, or ``None`` when no filter was sent."""

    if value is None:
        return None
    symbol = value.strip().upper()
    if not symbol:
        return None
    if len(symbol) > _SYMBOL_MAX_CHARS or _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise MobileApiError(422, "VALIDATION_ERROR", "symbol 형식을 확인해 주세요.")
    return symbol


def _normalize_limit(limit: int) -> int:
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise MobileApiError(
            422,
            "VALIDATION_ERROR",
            f"limit은 {MIN_LIMIT}에서 {MAX_LIMIT} 사이여야 합니다.",
        )
    return limit


def _iso_or_none(value: datetime | None) -> str | None:
    return iso_z(value) if value is not None else None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _newest(values: list[datetime | None]) -> datetime | None:
    known = [_as_utc(value) for value in values]
    present = [value for value in known if value is not None]
    return max(present) if present else None


async def _load_news_rows(
    db: AsyncSession, *, market: str, symbol: str | None, limit: int
) -> list[NewsArticle]:
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.market == market)
        .order_by(
            NewsArticle.article_published_at.desc().nulls_last(),
            NewsArticle.id.desc(),
        )
        .limit(limit)
    )
    if symbol is not None:
        # Semi-join on the persisted relation table: a symbol matched by more
        # than one source must not duplicate its article in the page.
        stmt = stmt.where(
            NewsArticle.id.in_(
                select(NewsArticleRelatedSymbol.article_id).where(
                    NewsArticleRelatedSymbol.market == market,
                    NewsArticleRelatedSymbol.symbol == symbol,
                )
            )
        )
    return list((await db.execute(stmt)).scalars().all())


async def _load_related_symbols(
    db: AsyncSession, article_ids: list[int]
) -> dict[int, list[NewsArticleRelatedSymbol]]:
    """Bulk-load persisted relations for the page (no per-article query)."""

    if not article_ids:
        return {}
    stmt = (
        select(NewsArticleRelatedSymbol)
        .where(NewsArticleRelatedSymbol.article_id.in_(article_ids))
        .order_by(
            NewsArticleRelatedSymbol.article_id,
            NewsArticleRelatedSymbol.rank.asc().nulls_last(),
            NewsArticleRelatedSymbol.id,
        )
    )
    by_article: dict[int, list[NewsArticleRelatedSymbol]] = {}
    for row in (await db.execute(stmt)).scalars().all():
        by_article.setdefault(row.article_id, []).append(row)
    return by_article


async def _load_stored_summaries(
    db: AsyncSession, article_ids: list[int]
) -> dict[int, str]:
    """Bulk-load the newest stored analysis summary per article."""

    if not article_ids:
        return {}
    stmt = (
        select(
            NewsAnalysisResult.article_id,
            NewsAnalysisResult.summary,
        )
        .where(NewsAnalysisResult.article_id.in_(article_ids))
        .order_by(
            NewsAnalysisResult.article_id,
            NewsAnalysisResult.created_at.desc(),
            NewsAnalysisResult.id.desc(),
        )
    )
    summary_by_article: dict[int, str] = {}
    for article_id, summary in (await db.execute(stmt)).all():
        if summary and article_id not in summary_by_article:
            summary_by_article[article_id] = summary
    return summary_by_article


def _news_symbol_refs(
    relations: list[NewsArticleRelatedSymbol],
) -> list[AiSymbolRef]:
    seen: set[tuple[str, str]] = set()
    refs: list[AiSymbolRef] = []
    for relation in relations:
        market = (relation.market or "").lower()
        symbol = (relation.symbol or "").strip()
        if market not in _MARKETS or not symbol:
            continue
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        refs.append(AiSymbolRef(symbol=symbol, market=market))
    return refs


def _news_item(
    row: NewsArticle,
    relations: list[NewsArticleRelatedSymbol],
    summary: str | None,
) -> AiNewsItem:
    return AiNewsItem(
        id=f"news:{row.id}",
        headline=row.title,
        source=row.source,
        published_at=_iso_or_none(row.article_published_at),
        market=(row.market or "").lower(),
        symbols=_news_symbol_refs(relations),
        canonical_url=row.url,
        summary=summary,
        data_updated_at=_iso_or_none(row.updated_at or row.created_at),
    )


async def _build_news_section(
    db: AsyncSession, *, market: str, symbol: str | None, limit: int
) -> AiNewsSection:
    rows = await _load_news_rows(db, market=market, symbol=symbol, limit=limit)
    if not rows:
        return AiNewsSection(status="empty", items=[])

    article_ids = [row.id for row in rows]
    relations = await _load_related_symbols(db, article_ids)
    summaries = await _load_stored_summaries(db, article_ids)
    items = [
        _news_item(row, relations.get(row.id, []), summaries.get(row.id))
        for row in rows
    ]
    refreshed_at = _newest([row.updated_at or row.created_at for row in rows])
    return AiNewsSection(
        status="available",
        refreshed_at=_iso_or_none(refreshed_at),
        items=items,
    )


def _research_symbol_refs(
    candidates: list[ResearchReportSymbolCandidate],
) -> list[AiResearchSymbolRef]:
    seen: set[tuple[str, str]] = set()
    refs: list[AiResearchSymbolRef] = []
    for candidate in candidates:
        symbol = (candidate.symbol or "").strip()
        market = (candidate.market or "").lower()
        if not symbol or market not in _MARKETS:
            continue
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            AiResearchSymbolRef(symbol=symbol, market=market, source=candidate.source)
        )
    return refs


def _research_candidates(row: ResearchReport) -> list[ResearchReportSymbolCandidate]:
    candidates: list[ResearchReportSymbolCandidate] = []
    for raw in row.symbol_candidates or []:
        try:
            candidates.append(ResearchReportSymbolCandidate.model_validate(raw))
        except ValueError:
            continue
    return candidates


def _research_item(row: ResearchReport, *, market: str) -> AiResearchItem:
    candidates = _research_candidates(row)
    refs = _research_symbol_refs(candidates)
    excerpt = row.detail_excerpt or row.summary_text
    if excerpt is not None and len(excerpt) > DETAIL_EXCERPT_MAX:
        excerpt = excerpt[:DETAIL_EXCERPT_MAX]
    # Report the stored candidate market; prefer the requested market when the
    # report covers it so a multi-market note is not relabeled.
    if any(ref.market == market for ref in refs):
        item_market = market
    elif refs:
        item_market = refs[0].market
    else:
        item_market = market
    return AiResearchItem(
        id=f"research-report:{row.id}",
        title=row.title or row.detail_title,
        # Credit the publisher when the ingestion payload carried one; the
        # ingest source is the fallback attribution.
        provider=row.attribution_publisher or row.source,
        published_at=_iso_or_none(row.published_at),
        published_at_text=row.published_at_text,
        market=item_market,
        symbols=refs,
        canonical_url=row.detail_url or row.pdf_url,
        excerpt=excerpt,
        data_updated_at=_iso_or_none(row.updated_at),
    )


async def _build_research_section(
    db: AsyncSession,
    *,
    market: MobileAiMarket,
    symbol: str | None,
    limit: int,
    now: datetime,
) -> AiResearchSection:
    rows, _ = await ResearchReportsQueryService(db).find_feed_page(
        limit=limit,
        cursor=None,
        symbol=symbol,
        market_filter=market,
    )
    if not rows:
        return AiResearchSection(status="empty", items=[])

    items = [_research_item(row, market=market) for row in rows]
    newest_published = _newest([row.published_at for row in rows])
    age = None if newest_published is None else now - newest_published
    status: AiResearchStatus = (
        "available" if age is not None and age <= _RESEARCH_STALE_AFTER else "stale"
    )
    refreshed_at = _newest([row.updated_at for row in rows])
    return AiResearchSection(
        status=status,
        refreshed_at=_iso_or_none(refreshed_at),
        items=items,
    )


async def _load_eligible_briefing(
    db: AsyncSession, *, market: str, now: datetime
) -> InvestmentReport | None:
    stmt = (
        select(InvestmentReport)
        .where(
            InvestmentReport.market == market,
            InvestmentReport.status.in_(_ELIGIBLE_BRIEFING_STATUSES),
            InvestmentReport.account_scope.is_(None),
            InvestmentReport.execution_mode == _ADVISORY_ONLY,
            or_(
                InvestmentReport.valid_until.is_(None),
                InvestmentReport.valid_until > now,
            ),
        )
        .order_by(
            InvestmentReport.published_at.desc().nulls_last(),
            InvestmentReport.id.desc(),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


def _briefing_section(report: InvestmentReport | None) -> AiBriefingSection:
    if report is None:
        return AiBriefingSection(
            status="unavailable",
            data_status="unknown",
            unavailable_reason=_NO_ELIGIBLE_BRIEFING,
        )

    overall = (report.snapshot_freshness_summary or {}).get("overall")
    data_status: AiBriefingDataStatus = (
        cast(AiBriefingDataStatus, overall)
        if overall in _REPORTABLE_DATA_STATUSES
        else "unknown"
    )
    # A degraded or unrecognized freshness value must never be advertised as a
    # current briefing. A legacy row with no summary at all keeps "available"
    # with an honest "unknown" data status.
    status: AiBriefingStatus
    if overall is None or data_status == "fresh":
        status = "available"
    else:
        status = "stale"
    return AiBriefingSection(
        status=status,
        id=f"investment-report:{report.id}",
        title=report.title,
        summary=report.summary,
        provider=report.created_by_profile,
        market=report.market,
        as_of=_iso_or_none(report.published_at or report.created_at),
        valid_until=_iso_or_none(report.valid_until),
        data_status=data_status,
        unavailable_reason=None,
    )


async def build_mobile_ai_briefing(
    db: AsyncSession,
    owner_user_id: int,
    *,
    market: str,
    symbol: str | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> AiBriefingResponse:
    """Assemble the owner-scoped mobile AI hub payload from stored artifacts.

    The ``symbol`` filter scopes news and research; a report-level briefing has
    no stored symbol column, so it stays market-scoped. Daily routine alerts
    are additional read-only evidence and do not enter recommendation or order
    producers.
    """

    market_value = normalize_market(market)
    symbol_value = normalize_symbol(symbol)
    limit_value = _normalize_limit(limit)
    instant = _as_utc(now) or datetime.now(UTC)

    try:
        routine_alerts = await daily_routine_service.get_alerts(
            db,
            owner_user_id,
            now=instant,
        )
        news = await _build_news_section(
            db, market=market_value, symbol=symbol_value, limit=limit_value
        )
        research = await _build_research_section(
            db,
            market=market_value,
            symbol=symbol_value,
            limit=limit_value,
            now=instant,
        )
        briefing = _briefing_section(
            await _load_eligible_briefing(db, market=market_value, now=instant)
        )
    except SQLAlchemyError as exc:
        raise MobileApiError(
            503,
            "AI_BRIEFING_UNAVAILABLE",
            "AI 브리핑 데이터를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
        ) from exc

    return AiBriefingResponse(
        status=(
            "available" if (news.items or research.items or routine_alerts) else "empty"
        ),
        as_of=iso_z(instant),
        news=news,
        routine_alerts=routine_alerts,
        research=research,
        briefing=briefing,
    )
