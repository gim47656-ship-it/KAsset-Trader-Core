"""SEC EDGAR 공시 수집 TaskIQ 진입점.

반복 스케줄은 두지 않는다. 운영자는 수동 CLI 또는 외부 스케줄러로 호출한다.
"""

from __future__ import annotations

from datetime import date

from app.core.taskiq_broker import broker
from app.jobs.sec_edgar_ingestion import run_sec_edgar_ingestion


@broker.task(task_name="news.sec.ingest")
async def ingest_sec_edgar_task(
    since_date: str,
    stock_symbols: list[str] | None = None,
) -> dict[str, object]:
    """필수 날짜 하한을 작업 경계에서 검증한 뒤 SEC 수집 잡을 호출한다."""
    try:
        parsed_since_date = date.fromisoformat(since_date)
    except ValueError as exc:
        return {"status": "failed", "error": f"invalid ISO date: {exc}"}
    return await run_sec_edgar_ingestion(
        since_date=parsed_since_date,
        stock_symbols=stock_symbols,
    )
