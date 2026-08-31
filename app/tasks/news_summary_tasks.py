"""일반 뉴스 AI 번역·요약 TaskIQ 진입점.

5분마다 최대 100건을 처리한다. API는 한국어 요약이 완성된 기사만 노출하므로
일시 실패한 행은 다음 회차에서 다시 선택되고 영문 fallback이 사용자에게 새지 않는다.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.news_summary import run_news_summary_backfill
from app.services.news_summary_service import MAX_BATCH_SIZE


@broker.task(
    task_name="news.articles.summarize",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "UTC"}],
)
async def summarize_news_task(
    batch_size: int = MAX_BATCH_SIZE,
    market: str | None = None,
    feed_source: str | None = None,
) -> dict[str, object]:
    """한정된 일반 뉴스 요약 backfill batch를 실행한다."""

    return await run_news_summary_backfill(
        batch_size=batch_size,
        market=market,
        feed_source=feed_source,
    )
