"""Toss report scope/reconcile broker migration의 additive 계약을 검증한다."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260902_toss_report_scopes.py"
    )
    spec = importlib.util.spec_from_file_location("toss_report_scope_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar(self) -> object | None:
        return self._value


class _Bind:
    def __init__(self, values: list[object | None]) -> None:
        self._values = iter(values)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _ScalarResult:
        self.calls.append((str(statement), params))
        return _ScalarResult(next(self._values))


class _Recorder:
    def __init__(self, values: list[object | None] | None = None) -> None:
        self.bind = _Bind(values or [])
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_bind(self) -> _Bind:
        return self.bind

    def drop_constraint(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("drop_constraint", args, kwargs))

    def create_check_constraint(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_check_constraint", args, kwargs))


def _created_checks(recorder: _Recorder) -> dict[tuple[str, str], str]:
    return {
        (args[1], args[0]): str(args[2])
        for name, args, _ in recorder.calls
        if name == "create_check_constraint"
    }


def test_upgrade_only_widens_existing_checks_with_toss_values() -> None:
    migration = _load_migration()
    recorder = _Recorder()
    migration.op = recorder

    migration.upgrade()

    checks = _created_checks(recorder)
    assert len(checks) == 9
    for table, name in migration._SCOPE_CONSTRAINTS:
        expression = checks[(table, name)]
        assert "'toss_live'" in expression
        assert "'kis_live'" in expression
        assert "'kis_mock'" in expression
        assert "'alpaca_paper'" in expression
        assert "'upbit_live'" in expression
    assert (
        checks[("investment_reports", "ck_investment_reports_live_advisory_only")]
        == migration._NEW_LIVE_ADVISORY_CHECK
    )
    assert (
        checks[("execution_ledger_reconcile_runs", "execution_ledger_runs_broker")]
        == "broker IN ('kis','upbit','toss')"
    )
    assert (
        checks[("execution_ledger", "execution_ledger_broker")]
        == "broker IN ('kis','upbit','toss')"
    )
    assert {name for name, _, _ in recorder.calls} == {
        "drop_constraint",
        "create_check_constraint",
    }


def test_downgrade_restores_old_checks_when_no_toss_rows_exist() -> None:
    migration = _load_migration()
    recorder = _Recorder([None] * 8)
    migration.op = recorder

    migration.downgrade()

    checks = _created_checks(recorder)
    assert len(recorder.bind.calls) == 8
    assert (
        checks[("execution_ledger_reconcile_runs", "execution_ledger_runs_broker")]
        == "broker IN ('kis','upbit')"
    )
    assert (
        checks[("execution_ledger", "execution_ledger_broker")]
        == "broker IN ('kis','upbit')"
    )
    for table, name in migration._SCOPE_CONSTRAINTS:
        assert checks[(table, name)] == migration._OLD_SCOPE_CHECK


@pytest.mark.parametrize(
    "query_results, expected_fragment",
    [
        ([1], "review.investment_reports"),
        ([None] * 6 + [1], "review.execution_ledger"),
        ([None] * 7 + [1], "review.execution_ledger_reconcile_runs"),
    ],
)
def test_downgrade_fails_before_ddl_when_toss_rows_exist(
    query_results: list[object | None], expected_fragment: str
) -> None:
    migration = _load_migration()
    recorder = _Recorder(query_results)
    migration.op = recorder

    with pytest.raises(RuntimeError, match=expected_fragment):
        migration.downgrade()

    assert recorder.calls == []
