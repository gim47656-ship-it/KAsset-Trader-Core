"""일반 뉴스 AI 번역·요약 TaskIQ 진입점.

반복 스케줄은 등록하지 않으며 운영자가 비용이 제한된 batch를 명시적으로 호출한다.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.news_summary import run_news_summary_backfill
from app.services.news_summary_service import DEFAULT_BATCH_SIZE


@broker.task(task_name="news.articles.summarize")
async def summarize_news_task(
    batch_size: int = DEFAULT_BATCH_SIZE,
    market: str | None = None,
    feed_source: str | None = None,
) -> dict[str, object]:
    """한정된 일반 뉴스 요약 backfill batch를 실행한다."""

    return await run_news_summary_backfill(
        batch_size=batch_size,
        market=market,
        feed_source=feed_source,
    )
