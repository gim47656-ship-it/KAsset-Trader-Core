from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import nhplug_symbol_master_tasks as mod


def test_nhplug_symbol_master_task_is_registered_weekly() -> None:
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES
    assert mod.sync_nhplug_symbol_master_task.task_name == "symbols.nhplug.master.sync"
    labels = getattr(mod.sync_nhplug_symbol_master_task, "labels", {}) or {}
    assert labels.get("schedule") == [
        {"cron": "30 3 * * 0", "cron_offset": "Asia/Seoul"}
    ]


@pytest.mark.asyncio
async def test_nhplug_symbol_master_task_runs_sync_job() -> None:
    expected = {"status": "completed", "total": 10, "krx": 6, "us": 4}
    with patch.object(
        mod,
        "run_nhplug_symbol_master_sync",
        AsyncMock(return_value=expected),
    ) as sync_job:
        result = await mod.sync_nhplug_symbol_master_task()

    sync_job.assert_awaited_once_with()
    assert result == expected
