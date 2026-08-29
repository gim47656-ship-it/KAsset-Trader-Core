"""SEC EDGAR 공시를 통합 뉴스 저장소에 적재하는 수집 서비스."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import symbol_news_store
from app.services.disclosures.feed_sources import SEC_FEED_SOURCE
from app.services.disclosures.sec_edgar import (
    SEC_MARKET,
    CompanyTickerCache,
    SecEdgarClient,
    SecEdgarError,
    SecEdgarHttpClient,
    SecRateLimiter,
    build_submission_url,
    company_ticker_cache,
    parse_submissions,
    resolve_sec_user_agent,
)
from app.services.symbol_news_store import (
    DisclosureArticleInput,
    DisclosureUpsertCounts,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 20.0
_ZERO_COUNTS = DisclosureUpsertCounts(inserted=0, updated=0, skipped=0)


@dataclass(frozen=True)
class SecSymbolIssue:
    symbol: str
    reason: str


@dataclass(frozen=True)
class SecIngestionResult:
    run_uuid: str
    status: str
    inserted: int
    updated: int
    skipped: int
    successful_symbols: int
    skipped_symbols: tuple[SecSymbolIssue, ...]
    failed_symbols: tuple[SecSymbolIssue, ...]
    form_counts: Mapping[str, int]


@dataclass(frozen=True)
class _CollectedSec:
    items: tuple[DisclosureArticleInput, ...]
    successful_symbols: int
    skipped_symbols: tuple[SecSymbolIssue, ...]
    failed_symbols: tuple[SecSymbolIssue, ...]
    form_counts: Mapping[str, int]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _symbols(values: Sequence[str]) -> list[str]:
    return list(
        dict.fromkeys(value.strip().upper() for value in values if value.strip())
    )


def _status(collected: _CollectedSec) -> str:
    if not collected.failed_symbols:
        return "success"
    if collected.successful_symbols > 0:
        return "partial"
    return "failed"


def _error_message(issues: tuple[SecSymbolIssue, ...]) -> str | None:
    if not issues:
        return None
    return "; ".join(f"{issue.symbol}: {issue.reason}" for issue in issues)[:2000]


async def _collect(
    *,
    symbols: list[str],
    since_date: date,
    client: SecEdgarClient,
    ticker_cache: CompanyTickerCache,
) -> _CollectedSec:
    try:
        tickers = await ticker_cache.get(client)
    except SecEdgarError as exc:
        return _CollectedSec(
            items=(),
            successful_symbols=0,
            skipped_symbols=(),
            failed_symbols=tuple(
                SecSymbolIssue(symbol=symbol, reason=f"ticker_catalog_error: {exc}")
                for symbol in symbols
            ),
            form_counts={},
        )

    collected_items: list[DisclosureArticleInput] = []
    skipped_symbols: list[SecSymbolIssue] = []
    failed_symbols: list[SecSymbolIssue] = []
    successful_symbols = 0
    form_counts: Counter[str] = Counter()
    counted_ciks: set[str] = set()
    submissions_by_cik: dict[str, Mapping[str, object] | SecEdgarError] = {}

    for symbol in symbols:
        cik = tickers.get(symbol)
        if cik is None:
            skipped_symbols.append(
                SecSymbolIssue(
                    symbol=symbol,
                    reason="ticker_not_in_company_tickers",
                )
            )
            logger.info("SEC CIK 매핑 없는 종목 스킵: symbol=%s", symbol)
            continue

        cached = submissions_by_cik.get(cik)
        if cached is None:
            try:
                cached = await client.get_json(build_submission_url(cik))
            except SecEdgarError as exc:
                cached = exc
            submissions_by_cik[cik] = cached
        if isinstance(cached, SecEdgarError):
            failed_symbols.append(SecSymbolIssue(symbol=symbol, reason=str(cached)))
            logger.warning(
                "SEC submissions 종목 수집 실패: symbol=%s error=%s",
                symbol,
                cached,
            )
            continue

        try:
            parsed = parse_submissions(
                cached,
                symbol=symbol,
                cik=cik,
                since_date=since_date,
            )
        except SecEdgarError as exc:
            failed_symbols.append(SecSymbolIssue(symbol=symbol, reason=str(exc)))
            logger.warning(
                "SEC submissions 종목 형식 실패: symbol=%s error=%s",
                symbol,
                exc,
            )
            continue

        successful_symbols += 1
        collected_items.extend(parsed.items)
        if cik not in counted_ciks:
            form_counts.update(parsed.form_counts)
            counted_ciks.add(cik)

    return _CollectedSec(
        items=tuple(collected_items),
        successful_symbols=successful_symbols,
        skipped_symbols=tuple(skipped_symbols),
        failed_symbols=tuple(failed_symbols),
        form_counts=dict(form_counts),
    )


async def _collect_with_client(
    *,
    symbols: list[str],
    since_date: date,
    http_client: SecEdgarHttpClient | None,
    user_agent: str | None,
    rate_limiter: SecRateLimiter | None,
    ticker_cache: CompanyTickerCache,
) -> _CollectedSec:
    configured_user_agent = resolve_sec_user_agent(user_agent)
    if http_client is not None:
        return await _collect(
            symbols=symbols,
            since_date=since_date,
            client=SecEdgarClient(
                http_client,
                user_agent=configured_user_agent,
                rate_limiter=rate_limiter,
            ),
            ticker_cache=ticker_cache,
        )
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as owned_client:
        return await _collect(
            symbols=symbols,
            since_date=since_date,
            client=SecEdgarClient(
                owned_client,
                user_agent=configured_user_agent,
                rate_limiter=rate_limiter,
            ),
            ticker_cache=ticker_cache,
        )


async def _persist_collected(
    db: AsyncSession,
    *,
    run_uuid: str,
    started_at: datetime,
    collected: _CollectedSec,
) -> tuple[DisclosureUpsertCounts, str]:
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=SEC_MARKET,
        feed_source=SEC_FEED_SOURCE,
    )
    stored = await symbol_news_store.upsert_disclosures(db, list(collected.items))
    counts = DisclosureUpsertCounts(
        inserted=stored.inserted,
        updated=stored.updated,
        skipped=stored.skipped + len(collected.skipped_symbols),
    )
    status = _status(collected)
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status=status,
        finished_at=_utcnow(),
        counts=counts,
        error_message=_error_message(collected.failed_symbols),
        feed_source=SEC_FEED_SOURCE,
    )
    await db.commit()
    return counts, status


async def _record_failed_run(
    db: AsyncSession,
    *,
    run_uuid: str,
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
        market=SEC_MARKET,
        feed_source=SEC_FEED_SOURCE,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status="failed",
        finished_at=_utcnow(),
        counts=_ZERO_COUNTS,
        error_message=message,
        feed_source=SEC_FEED_SOURCE,
    )
    await db.commit()


async def ingest_sec_edgar(
    db: AsyncSession,
    *,
    symbols: Sequence[str],
    since_date: date,
    run_uuid: str | None = None,
    http_client: SecEdgarHttpClient | None = None,
    user_agent: str | None = None,
    rate_limiter: SecRateLimiter | None = None,
    ticker_cache: CompanyTickerCache = company_ticker_cache,
) -> SecIngestionResult:
    """날짜 하한 안의 SEC 공시를 전량 수집하고 종목별 결과를 기록한다."""
    current_run_uuid = run_uuid or str(uuid.uuid4())
    started_at = _utcnow()
    requested_symbols = _symbols(symbols)

    try:
        if requested_symbols:
            try:
                collected = await _collect_with_client(
                    symbols=requested_symbols,
                    since_date=since_date,
                    http_client=http_client,
                    user_agent=user_agent,
                    rate_limiter=rate_limiter,
                    ticker_cache=ticker_cache,
                )
            except SecEdgarError as exc:
                collected = _CollectedSec(
                    items=(),
                    successful_symbols=0,
                    skipped_symbols=(),
                    failed_symbols=tuple(
                        SecSymbolIssue(symbol=symbol, reason=str(exc))
                        for symbol in requested_symbols
                    ),
                    form_counts={},
                )
        else:
            collected = _CollectedSec(
                items=(),
                successful_symbols=0,
                skipped_symbols=(),
                failed_symbols=(),
                form_counts={},
            )
        counts, status = await _persist_collected(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            collected=collected,
        )
    except asyncio.CancelledError as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        raise
    except Exception as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        logger.exception("SEC EDGAR 회차 저장 실패: run_uuid=%s", current_run_uuid)
        raise

    return SecIngestionResult(
        run_uuid=current_run_uuid,
        status=status,
        inserted=counts.inserted,
        updated=counts.updated,
        skipped=counts.skipped,
        successful_symbols=collected.successful_symbols,
        skipped_symbols=collected.skipped_symbols,
        failed_symbols=collected.failed_symbols,
        form_counts=collected.form_counts,
    )
