"""ROB-214 execution-ledger reconciliation task registration tests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.unit
def test_execution_ledger_task_module_is_discovered_by_taskiq_init() -> None:
    import app.tasks as tasks_pkg
    from app.tasks import execution_ledger

    assert execution_ledger in tasks_pkg.TASKIQ_TASK_MODULES


@pytest.mark.unit
def test_recurring_reconciliation_task_has_no_default_schedule() -> None:
    from app.tasks.execution_ledger import reconcile_execution_ledger_recurring

    labels = getattr(reconcile_execution_ledger_recurring, "labels", {}) or {}
    schedule = labels.get("schedule") if isinstance(labels, dict) else None

    assert not schedule, f"Schedule must be empty until approval. Found: {schedule!r}"


@pytest.mark.unit
def test_reconciliation_task_defaults_to_dry_run_when_commit_gate_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import execution_ledger as mod

    captured: dict[str, object] = {}

    class FakeDiff:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            captured["mode"] = mode
            return {"ok": True}

    class FakeReconciler:
        def __init__(self, repository: object) -> None:
            captured["repository"] = repository

        async def run(
            self, broker: str, *, window_hours: int, dry_run: bool
        ) -> FakeDiff:
            captured["broker"] = broker
            captured["window_hours"] = window_hours
            captured["dry_run"] = dry_run
            return FakeDiff()

    class FakeRepository:
        def __init__(self, db: object) -> None:
            captured["db"] = db

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def commit(self) -> None:
            captured["committed"] = True

        async def rollback(self) -> None:  # pragma: no cover - should not be called
            raise AssertionError("dry-run reconciliation must not roll back audit row")

    monkeypatch.setattr(mod.settings, "EXECUTION_LEDGER_COMMIT_ENABLED", False)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(mod, "ExecutionLedgerReconciler", FakeReconciler)
    monkeypatch.setattr(mod, "ExecutionLedgerRepository", FakeRepository)

    result = asyncio.run(
        mod.reconcile_execution_ledger_smoke(broker="toss", window_hours=6)
    )

    assert result == {"ok": True}
    assert captured["broker"] == "toss"
    assert captured["window_hours"] == 6
    assert captured["dry_run"] is True
    assert captured["committed"] is True
    assert captured["mode"] == "json"


@pytest.mark.unit
def test_reconciliation_task_rejects_kis_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import execution_ledger as mod

    def forbidden_session() -> None:
        raise AssertionError("KIS rejection must happen before opening a DB session")

    monkeypatch.setattr(mod, "AsyncSessionLocal", forbidden_session)

    with pytest.raises(ValueError, match="provider kis is not operational"):
        asyncio.run(mod.reconcile_execution_ledger_smoke(broker="kis", window_hours=6))


@pytest.mark.unit
def test_recurring_reconciliation_uses_toss_and_upbit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import execution_ledger as mod

    calls: list[tuple[str, int]] = []

    async def fake_run(broker: str, window_hours: int) -> dict[str, str]:
        calls.append((broker, window_hours))
        return {"broker": broker}

    monkeypatch.setattr(
        mod.settings, "execution_ledger_reconcile_scheduler_window_hours", 12
    )
    monkeypatch.setattr(mod, "_run_reconciliation", fake_run)

    result = asyncio.run(mod.reconcile_execution_ledger_recurring())

    assert result == {
        "toss": {"broker": "toss"},
        "upbit": {"broker": "upbit"},
    }
    assert calls == [("toss", 12), ("upbit", 12)]


@pytest.mark.unit
def test_reconciliation_task_commits_dry_run_audit_when_reconciler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import execution_ledger as mod

    captured: dict[str, object] = {}

    class FakeReconciler:
        def __init__(self, repository: object) -> None:
            captured["repository"] = repository

        async def run(self, broker: str, *, window_hours: int, dry_run: bool) -> None:
            captured["broker"] = broker
            captured["window_hours"] = window_hours
            captured["dry_run"] = dry_run
            raise RuntimeError("filled-orders fetch returned errors")

    class FakeRepository:
        def __init__(self, db: object) -> None:
            captured["db"] = db

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def commit(self) -> None:
            captured["committed"] = True

        async def rollback(self) -> None:  # pragma: no cover - should not be called
            raise AssertionError("dry-run failure audit row must be committed")

    monkeypatch.setattr(mod.settings, "EXECUTION_LEDGER_COMMIT_ENABLED", False)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(mod, "ExecutionLedgerReconciler", FakeReconciler)
    monkeypatch.setattr(mod, "ExecutionLedgerRepository", FakeRepository)

    with pytest.raises(RuntimeError, match="filled-orders fetch returned errors"):
        asyncio.run(
            mod.reconcile_execution_ledger_smoke(broker="upbit", window_hours=6)
        )

    assert captured["broker"] == "upbit"
    assert captured["window_hours"] == 6
    assert captured["dry_run"] is True
    assert captured["committed"] is True
