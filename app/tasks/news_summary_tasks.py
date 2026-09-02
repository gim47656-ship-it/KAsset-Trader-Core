"""일반 뉴스 AI 번역·요약 TaskIQ 진입점.

5분마다 최대 20건을 처리하되 모델 호출당 최대 10건으로 묶고 UTC 일일 호출 상한을
적용한다. 실패한 번역은 6시간 backoff 뒤 같은 분석 행을 갱신한다.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.news_summary import run_news_summary_backfill
from app.services.news_summary_service import DEFAULT_BATCH_SIZE


@broker.task(
    task_name="news.articles.summarize",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "UTC"}],
)
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
