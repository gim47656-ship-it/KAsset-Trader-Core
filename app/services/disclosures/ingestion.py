"""DART 공시목록을 통합 뉴스 저장소에 적재하는 수집 서비스."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import symbol_news_store
from app.services.disclosures.dart_list import DartListError, fetch_disclosure_list
from app.services.disclosures.feed_sources import DART_FEED_SOURCE
from app.services.symbol_news_store import (
    DisclosureArticleInput,
    DisclosureUpsertCounts,
)

logger = logging.getLogger(__name__)

_DART_MARKET = "kr"
_DART_SOURCE = "DART"
_DART_URL_TEMPLATE = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
_ZERO_COUNTS = DisclosureUpsertCounts(inserted=0, updated=0, skipped=0)


class DartPartialIngestionError(RuntimeError):
    """일부 페이지 저장 후 DART 요청이 실패했음을 호출자에게 알린다."""

    def __init__(
        self,
        *,
        run_uuid: str,
        counts: DisclosureUpsertCounts,
        cause: DartListError,
    ) -> None:
        self.run_uuid = run_uuid
        self.counts = counts
        self.cause = cause
        super().__init__(
            f"DART ingestion partial: run_uuid={run_uuid}, "
            f"inserted={counts.inserted}, updated={counts.updated}, "
            f"skipped={counts.skipped}, error={cause}"
        )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _optional_text(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _published_at_from_kst_date(value: str | None) -> datetime | None:
    """DART KST 날짜를 timestamp-without-time-zone의 KST 자정으로 보존한다."""
    if value is None:
        return None
    normalized = value.replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError(f"invalid rcept_dt: {value!r}")
    parsed = date(int(normalized[:4]), int(normalized[4:6]), int(normalized[6:8]))
    return datetime.combine(parsed, time.min)


def _normalize_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[DisclosureArticleInput], int]:
    normalized: list[DisclosureArticleInput] = []
    skipped = 0
    for index, row in enumerate(rows):
        rcept_no = _optional_text(row, "rcept_no")
        title = _optional_text(row, "report_nm")
        if rcept_no is None or title is None:
            skipped += 1
            logger.warning(
                "DART 공시 필수값 누락으로 스킵: index=%d rcept_no=%s",
                index,
                rcept_no,
            )
            continue
        try:
            published_at = _published_at_from_kst_date(_optional_text(row, "rcept_dt"))
        except (TypeError, ValueError):
            skipped += 1
            logger.warning(
                "DART 공시 접수일 파싱 실패로 스킵: rcept_no=%s",
                rcept_no,
            )
            continue
        normalized.append(
            DisclosureArticleInput(
                url=_DART_URL_TEMPLATE.format(rcept_no=rcept_no),
                title=title,
                source=_DART_SOURCE,
                feed_source=DART_FEED_SOURCE,
                market=_DART_MARKET,
                stock_symbol=_optional_text(row, "stock_code"),
                stock_name=_optional_text(row, "corp_name"),
                published_at=published_at,
            )
        )
    return normalized, skipped


async def _persist_outcome(
    db: AsyncSession,
    *,
    run_uuid: str,
    started_at: datetime,
    rows: Sequence[Mapping[str, Any]],
    status: str,
    error_message: str | None,
) -> DisclosureUpsertCounts:
    normalized, invalid_count = _normalize_rows(rows)
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=_DART_MARKET,
        feed_source=DART_FEED_SOURCE,
    )
    stored = await symbol_news_store.upsert_disclosures(db, normalized)
    counts = DisclosureUpsertCounts(
        inserted=stored.inserted,
        updated=stored.updated,
        skipped=stored.skipped + invalid_count,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status=status,
        finished_at=_utcnow(),
        counts=counts,
        error_message=error_message,
        feed_source=DART_FEED_SOURCE,
    )
    await db.commit()
    return counts


async def _record_failed_run(
    db: AsyncSession,
    *,
    run_uuid: str,
    started_at: datetime,
    error: BaseException,
) -> None:
    await db.rollback()
    raw_error_message = str(error).strip()
    error_message = (raw_error_message or type(error).__name__)[:2000]
    await symbol_news_store.create_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        started_at=started_at,
        market=_DART_MARKET,
        feed_source=DART_FEED_SOURCE,
    )
    await symbol_news_store.finish_news_ingestion_run(
        db,
        run_uuid=run_uuid,
        status="failed",
        finished_at=_utcnow(),
        counts=_ZERO_COUNTS,
        error_message=error_message,
        feed_source=DART_FEED_SOURCE,
    )
    await db.commit()


async def ingest_dart_disclosures(
    db: AsyncSession,
    *,
    start_date: date,
    end_date: date,
    stock_symbols: Sequence[str] | None = None,
    run_uuid: str | None = None,
    dart_client: Any | None = None,
) -> tuple[int, int, int]:
    """DART 공시를 수집하고 ``(신규, 갱신, 스킵)`` 건수를 반환한다.

    ``stock_code``가 없는 비상장 법인 공시도 기사 자체는 보존하되
    ``stock_symbol``만 ``None``으로 남긴다. 공시목록 페이지 일부를 받은 뒤
    실패하면 그 행은 ``partial`` 회차로 커밋하고 예외를 다시 표면화한다.
    """
    current_run_uuid = run_uuid or str(uuid.uuid4())
    started_at = _utcnow()
    try:
        rows = await fetch_disclosure_list(
            start_date,
            end_date,
            stock_symbols=stock_symbols,
            client=dart_client,
        )
    except DartListError as exc:
        if exc.partial_filings:
            try:
                counts = await _persist_outcome(
                    db,
                    run_uuid=current_run_uuid,
                    started_at=started_at,
                    rows=exc.partial_filings,
                    status="partial",
                    error_message=str(exc)[:2000],
                )
            except Exception as persist_exc:
                await _record_failed_run(
                    db,
                    run_uuid=current_run_uuid,
                    started_at=started_at,
                    error=persist_exc,
                )
                logger.exception(
                    "DART 부분 수집 저장 실패: run_uuid=%s",
                    current_run_uuid,
                )
                raise
            logger.error(
                "DART 부분 수집: run_uuid=%s status=%s inserted=%d updated=%d "
                "skipped=%d",
                current_run_uuid,
                exc.status,
                counts.inserted,
                counts.updated,
                counts.skipped,
            )
            raise DartPartialIngestionError(
                run_uuid=current_run_uuid,
                counts=counts,
                cause=exc,
            ) from exc
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        logger.error(
            "DART 수집 실패: run_uuid=%s status=%s",
            current_run_uuid,
            exc.status,
        )
        raise
    except asyncio.CancelledError as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        logger.warning("DART 수집 취소: run_uuid=%s", current_run_uuid)
        raise
    except Exception as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        logger.exception("DART 수집 실패: run_uuid=%s", current_run_uuid)
        raise

    try:
        counts = await _persist_outcome(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            rows=rows,
            status="success",
            error_message=None,
        )
    except Exception as exc:
        await _record_failed_run(
            db,
            run_uuid=current_run_uuid,
            started_at=started_at,
            error=exc,
        )
        logger.exception("DART 수집 저장 실패: run_uuid=%s", current_run_uuid)
        raise
    return counts.inserted, counts.updated, counts.skipped
