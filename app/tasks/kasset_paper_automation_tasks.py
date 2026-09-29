"""KAsset AUTO_PAPER execution schedule declarations.

The sweeps are fail-closed: ``AI_PAPER_AUTO_EXECUTION_ENABLED`` is default-off,
and every owner policy plus kill switch is re-read before PAPER submission.

- ``kasset.paper_automation.run``: 5분마다 모든 시장의 추천을 집행한다.
- ``kasset.realtime.kr.run``: KRX 정규장 동안 1분마다 KRX 보유 보호 평가 →
  실시간 흐름 악화 청산 → KRX 추천만 집행한다(2026-09-29 사용자 승인).
"""

from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.extensions.kasset.automation.job import run_paper_automation_once
from app.extensions.kasset.automation.realtime_kr import run_kr_realtime_once
from app.tasks.kasset_market_events_tasks import _advisory_single_flight

logger = logging.getLogger(__name__)

# kasset_market_events_tasks의 advisory namespace를 공유한다(키 1~3 사용 중).
_REALTIME_KR_LOCK_KEY = 4


@broker.task(
    task_name="kasset.paper_automation.run",
    schedule=[{"cron": "*/5 * * * *"}],
)
async def kasset_paper_automation_run() -> dict[str, object]:
    return await run_paper_automation_once()


@broker.task(
    task_name="kasset.realtime.kr.run",
    schedule=[{"cron": "* 9-15 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def kasset_realtime_kr_run() -> dict[str, object]:
    async with _advisory_single_flight(_REALTIME_KR_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("kasset realtime KR cycle skipped: already_running")
            return {"enabled": True, "skipped": "realtime_kr_already_running"}
        return await run_kr_realtime_once()
