"""DART 공시 수집 TaskIQ 진입점.

반복 스케줄은 두지 않는다. 운영자는 수동 CLI 또는 외부 스케줄러로 호출한다.
"""

from __future__ import annotations

from datetime import date

from app.core.taskiq_broker import broker
from app.jobs.dart_disclosure_ingestion import run_dart_disclosure_ingestion


def _optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@broker.task(task_name="news.dart.ingest")
async def ingest_dart_disclosures_task(
    from_date: str | None = None,
    to_date: str | None = None,
    recent_days: int = 1,
    stock_symbols: list[str] | None = None,
) -> dict[str, object]:
    """문자열 날짜 인자를 작업 경계에서 검증한 뒤 수집 잡을 호출한다."""
    try:
        parsed_from = _optional_date(from_date)
        parsed_to = _optional_date(to_date)
    except ValueError as exc:
        return {"status": "failed", "error": f"invalid ISO date: {exc}"}
    return await run_dart_disclosure_ingestion(
        from_date=parsed_from,
        to_date=parsed_to,
        recent_days=recent_days,
        stock_symbols=stock_symbols,
    )
