from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.nhplug_symbol_master import run_nhplug_symbol_master_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="symbols.nhplug.master.sync",
    schedule=[{"cron": "30 3 * * 0", "cron_offset": "Asia/Seoul"}],
)
async def sync_nhplug_symbol_master_task() -> dict[str, int | str | bool]:
    result = await run_nhplug_symbol_master_sync()
    if result.get("status") != "completed":
        logger.error("TaskIQ NH PLUG symbol master sync failed: %s", result)
    return result
