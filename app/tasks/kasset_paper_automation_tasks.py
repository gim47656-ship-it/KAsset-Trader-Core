"""KAsset AUTO_PAPER execution schedule declaration.

The sweep is fail-closed: ``AI_PAPER_AUTO_EXECUTION_ENABLED`` is default-off,
and every owner policy plus kill switch is re-read before PAPER submission.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.extensions.kasset.automation.job import run_paper_automation_once


@broker.task(
    task_name="kasset.paper_automation.run",
    schedule=[{"cron": "*/5 * * * *"}],
)
async def kasset_paper_automation_run() -> dict[str, object]:
    return await run_paper_automation_once()
