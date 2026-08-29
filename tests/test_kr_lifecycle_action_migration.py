from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260830_kr_lifecycle_ca.py"
    )
    spec = importlib.util.spec_from_file_location("kr_lifecycle_ca_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def test_migration_is_chained_bounded_and_downgrades_every_added_table() -> None:
    migration = _load_migration()
    assert migration.revision == "20260830_kr_lifecycle_ca"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "20260830_kasset_claim_lease"

    recorder = _Recorder()
    migration.op = recorder
    migration.upgrade()
    created = [args[0] for name, args, _ in recorder.calls if name == "create_table"]
    assert created == [
        "kr_stock_lifecycle_observations",
        "kr_corporate_action_evidence",
        "kasset_corporate_action_fetch_coverage",
        "kasset_research_cohorts",
        "kasset_research_cohort_members",
    ]
    widened = [
        kwargs["type_"].length
        for name, args, kwargs in recorder.calls
        if name == "alter_column" and args == ("kr_symbol_universe", "listing_status")
    ]
    assert widened == [64]

    recorder.calls.clear()
    migration.downgrade()
    dropped = [args[0] for name, args, _ in recorder.calls if name == "drop_table"]
    assert dropped == [
        "kasset_research_cohort_members",
        "kasset_research_cohorts",
        "kasset_corporate_action_fetch_coverage",
        "kr_corporate_action_evidence",
        "kr_stock_lifecycle_observations",
    ]
    assert any(
        name == "drop_column" and args == ("kr_symbol_universe", "std_pdno")
        for name, args, _ in recorder.calls
    )
    narrowed = [
        kwargs["type_"].length
        for name, args, kwargs in recorder.calls
        if name == "alter_column" and args == ("kr_symbol_universe", "listing_status")
    ]
    assert narrowed == [20]
