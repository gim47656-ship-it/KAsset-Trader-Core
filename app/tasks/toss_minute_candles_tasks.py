from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.toss_minute_candles import run_toss_minute_candle_sync

TOSS_MINUTE_SCHEDULE = [
    {"cron": "* 8-19 * * 1-5", "cron_offset": "Asia/Seoul"},
    {"cron": "0 20 * * 1-5", "cron_offset": "Asia/Seoul"},
]


@broker.task(
    task_name="research.candles.kr.toss.1m.sync",
    schedule=TOSS_MINUTE_SCHEDULE,
)
async def sync_toss_minute_candles_task() -> dict[str, object]:
    return await run_toss_minute_candle_sync()
