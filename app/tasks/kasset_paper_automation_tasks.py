"""KAsset PAPER recommendation automation schedule declaration.

The sweep is fail-closed: with ``AI_PAPER_AUTO_EXECUTION_ENABLED`` false the
task is a metadata-only no-op, so keeping it on the schedule is safe for
deployments that have not opted in.
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
