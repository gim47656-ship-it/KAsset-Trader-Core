"""Google News RSS 수집 TaskIQ 진입점.

반복 스케줄은 두지 않는다. 운영자는 수동 CLI 또는 외부 스케줄러로 호출한다.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.google_news_rss_ingestion import run_google_news_rss_ingestion


@broker.task(task_name="news.google_news.ingest")
async def ingest_google_news_rss_task(
    market: str = "kr",
    stock_symbols: list[str] | None = None,
    excluded_sources: list[str] | None = None,
) -> dict[str, object]:
    """시장과 선택 종목을 그대로 수집 잡 경계에 전달한다."""
    return await run_google_news_rss_ingestion(
        market=market,
        stock_symbols=stock_symbols,
        excluded_sources=excluded_sources,
    )
