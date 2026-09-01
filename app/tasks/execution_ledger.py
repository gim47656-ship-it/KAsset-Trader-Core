"""Execution-ledger reconciliation tasks.

ROB-214 keeps recurring reconciliation inert by default.  Operators must enable
``execution_ledger_reconcile_scheduler_enabled`` for the scheduler label to be
registered, and writes still require the independent ``EXECUTION_LEDGER_COMMIT_ENABLED``
activation flag.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker as taskiq_broker
from app.schemas.execution_ledger import ReconcileRunBroker
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository


def _scheduled_reconcile_labels() -> list[dict[str, str]]:
    if not settings.execution_ledger_reconcile_scheduler_enabled:
        return []
    return [
        {
            "cron": settings.execution_ledger_reconcile_scheduler_cron,
            "cron_offset": "Asia/Seoul",
        }
    ]


async def _run_reconciliation(broker: ReconcileRunBroker, window_hours: int) -> dict:
    async with AsyncSessionLocal() as db:
        dry_run = not settings.EXECUTION_LEDGER_COMMIT_ENABLED
        try:
            diff = await ExecutionLedgerReconciler(ExecutionLedgerRepository(db)).run(
                broker,
                window_hours=window_hours,
                dry_run=dry_run,
            )
        except Exception:
            if dry_run:
                # Dry-run은 원장 upsert를 건너뛰고 실행 감사 행만 보존한다.
                await db.commit()
            else:
                await db.rollback()
            raise
        # Dry-run은 원장 upsert를 건너뛰고 실행 감사 행만 보존한다.
        await db.commit()
    return diff.model_dump(mode="json")


@taskiq_broker.task(task_name="execution_ledger.reconcile_execution_ledger_smoke")
async def reconcile_execution_ledger_smoke(broker: str, window_hours: int = 24) -> dict:
    """수동 실행 진입점. 커밋 게이트가 꺼져 있으면 dry-run이다."""
    if broker == "kis":
        raise ValueError("provider kis is not operational")
    if broker not in {"toss", "upbit"}:
        raise ValueError("broker must be toss or upbit")
    return await _run_reconciliation(
        broker,  # type: ignore[arg-type]
        window_hours=window_hours,
    )


@taskiq_broker.task(
    task_name="execution_ledger.reconcile_execution_ledger_recurring",
    schedule=_scheduled_reconcile_labels(),
)
async def reconcile_execution_ledger_recurring() -> dict[str, dict]:
    """Toss와 Upbit 체결 원장을 주기적으로 대조한다.

    스케줄은 기본 비활성이고, 명시적으로 켜도 별도 커밋 게이트가
    활성화되지 않으면 dry-run으로 실행된다.
    """
    window_hours = settings.execution_ledger_reconcile_scheduler_window_hours
    return {
        "toss": await _run_reconciliation("toss", window_hours=window_hours),
        "upbit": await _run_reconciliation("upbit", window_hours=window_hours),
    }
