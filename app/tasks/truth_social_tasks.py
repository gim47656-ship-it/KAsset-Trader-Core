"""Donald J. Trump 공식 Truth Social 시장 뉴스 수집 작업."""

from __future__ import annotations

import logging

from app.core.database import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.truth_social_ingestion import ingest_truth_social

logger = logging.getLogger(__name__)


@broker.task(
    task_name="news.truth_social.ingest",
    schedule=[{"cron": "2,12,22,32,42,52 * * * *", "cron_offset": "UTC"}],
)
async def ingest_truth_social_task() -> dict[str, object]:
    """공식 계정 게시물을 수집하고 한국어 요약 결과를 반환한다."""

    async with AsyncSessionLocal() as db:
        try:
            result = await ingest_truth_social(db)
        except Exception:
            await db.rollback()
            logger.exception("Truth Social 공식 피드 수집 실패")
            raise
    return result.to_dict()


__all__ = ["ingest_truth_social_task"]
