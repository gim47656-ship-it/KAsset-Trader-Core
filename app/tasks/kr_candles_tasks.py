from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.kr_candles import run_kr_candles_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="candles.kr.sync",
    schedule=[{"cron": "*/10 * * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_candles_incremental_task() -> dict[str, object]:
    try:
        # 운영 시세 공급자는 Toss로 고정하며 다른 공급자 경로는 열지 않는다.
        return await run_kr_candles_sync(mode="incremental", source="toss")
    except Exception as exc:
        logger.error("TaskIQ KR candles sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "incremental",
            "source": "toss",
            "error": str(exc),
        }
