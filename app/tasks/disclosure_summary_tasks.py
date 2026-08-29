"""SEC/DART 공시 요약 TaskIQ 진입점.

반복 스케줄은 등록하지 않으며 운영자가 제한 batch를 명시적으로 호출한다.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.disclosure_summary import run_disclosure_summary_backfill
from app.services.disclosures.summary_service import DEFAULT_BATCH_SIZE


@broker.task(task_name="news.disclosures.summarize")
async def summarize_disclosures_task(
    batch_size: int = DEFAULT_BATCH_SIZE,
    feed_source: str | None = None,
) -> dict[str, object]:
    """한정된 공시 요약 backfill batch를 실행한다."""
    return await run_disclosure_summary_backfill(
        batch_size=batch_size,
        feed_source=feed_source,
    )
