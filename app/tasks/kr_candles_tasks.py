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
        # 이 배포의 운영 범위는 Toss(시세) + NH(주문)다. 기본값 KIS로 도는
        # 스케줄 job은 자격이 없는 서버에서 매 10분 빈손으로 끝난다. KIS를
        # 명시적으로 지정하는 CLI/직접 호출 경로는 그대로 둔다.
        return await run_kr_candles_sync(mode="incremental", source="toss")
    except Exception as exc:
        logger.error("TaskIQ KR candles sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "incremental",
            "source": "toss",
            "error": str(exc),
        }
