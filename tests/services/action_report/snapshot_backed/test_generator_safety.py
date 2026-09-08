"""ROB-274 — report generation must not call broker / watch mutation methods.

These tests spy every known mutation method on the broker clients and the
WatchActivationService. If ``SnapshotBackedReportGenerator.generate()`` ever
calls one (directly or transitively), the spy's ``side_effect`` raises
AssertionError and the test fails loudly.

This is the safety belt that backs ROB-274's "report generation must be
side-effect free" invariant. The generator is read-only; this test proves it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.investment_reports import (
    IngestReportItem,
    WatchConditionPayload,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleResponse
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
)

# --------------------------------------------------------------------------
# Mutation method enumerations (canonical lists, from Step 1 grep).
# Read-only methods like inquire_*/fetch_*/get_* are deliberately omitted.
# --------------------------------------------------------------------------

# app.services.brokers.toss.client.TossReadClient — the one broker client with
# order-mutation methods that survives.
_TOSS_MUTATION_METHODS = [
    "place_order",
    "modify_order",
    "cancel_order",
]

# app.services.investment_reports.watch_activation.WatchActivationService
_WATCH_MUTATION_METHODS = [
    "activate",
]


# --------------------------------------------------------------------------
# Fakes mirroring test_generator.py (Task 7).
# --------------------------------------------------------------------------


def _minimal_ensure_response() -> EnsureBundleResponse:
    return EnsureBundleResponse(
        bundle_uuid=uuid.uuid4(),
        status="complete",  # type: ignore[arg-type]
        created=True,
        coverage_summary={
            "required": {
                "portfolio": "fresh",
                "journal": "fresh",
                "watch_context": "fresh",
                "market": "fresh",
            },
            "optional": {},
        },
        freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
        missing_sources=[],
        warnings=[],
        run_uuid=None,
    )


def _build_request() -> ReportGenerationRequest:
    """Smallest valid request that exercises the full generate() path."""
    return ReportGenerationRequest(
        market="kr",
        account_scope="toss_live",
        created_by_profile="claude_code",
        title="safety test",
        summary="safety test",
        status="draft",
        kst_date="2026-05-20",
        items=[
            IngestReportItem(
                client_item_key="w-1",
                item_kind="watch",
                symbol="005930",
                intent="trend_recovery_review",
                rationale="r",
                watch_condition=WatchConditionPayload(
                    metric="price",
                    operator="above",
                    threshold=Decimal("100"),
                ),
                valid_until=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7),
            )
        ],
    )


class _FakeSnapshotsRepository:
    """No-op repo that returns no bundle items (forces empty ClassifierContext)."""

    def __init__(self) -> None:
        self.get_bundle_calls: list[uuid.UUID] = []
        self.list_items_calls: list[int] = []

    async def get_bundle_by_uuid(self, bundle_uuid: uuid.UUID) -> Any:
        self.get_bundle_calls.append(bundle_uuid)
        return MagicMock(id=1)

    async def list_bundle_items_with_snapshots(self, bundle_id: int) -> list[Any]:
        self.list_items_calls.append(bundle_id)
        return []


def _build_generator() -> tuple[
    SnapshotBackedReportGenerator,
    AsyncMock,
    AsyncMock,
]:
    fake_ensure = AsyncMock()
    fake_ensure.ensure = AsyncMock(return_value=_minimal_ensure_response())

    fake_ingest = AsyncMock()
    fake_ingest.ingest = AsyncMock(return_value=MagicMock(report_uuid=uuid.uuid4()))
    # ROB-352 — generate() now consults the ingestion service for an existing
    # report before recomputing; return None so the safety tests exercise the
    # full (non-reuse) generation path, and ingest_with_outcome reports a fresh
    # (non-reused) insert.
    fake_ingest.get_existing_with_item_count = AsyncMock(return_value=None)
    fake_ingest.ingest_with_outcome = AsyncMock(
        return_value=(MagicMock(report_uuid=uuid.uuid4()), False, 0)
    )

    generator = SnapshotBackedReportGenerator(
        session=MagicMock(),
        ensure_service=fake_ensure,
        ingestion_service=fake_ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    return generator, fake_ensure, fake_ingest


def _install_spies(
    monkeypatch: pytest.MonkeyPatch,
    target: Any,
    method_names: list[str],
    label: str,
    spy_calls: list[str],
) -> None:
    """Replace every named attr on ``target`` with a spy that raises if called."""
    for name in method_names:
        if not hasattr(target, name):
            continue

        def _make_spy(method_name: str):
            def _spy(*_args: Any, **_kwargs: Any) -> Any:
                spy_calls.append(method_name)
                raise AssertionError(f"generator must not call {label}.{method_name}")

            return _spy

        monkeypatch.setattr(target, name, _make_spy(name), raising=False)


# --------------------------------------------------------------------------
# Safety tests — one per broker / mutation surface.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_does_not_call_toss_mutation_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every TossReadClient order-mutation method has a spy that raises."""
    from app.services.brokers.toss import client as toss_module

    spy_calls: list[str] = []
    _install_spies(
        monkeypatch,
        toss_module.TossReadClient,
        _TOSS_MUTATION_METHODS,
        "TossReadClient",
        spy_calls,
    )

    generator, _, _ = _build_generator()
    await generator.generate(_build_request())

    assert spy_calls == [], f"unexpected Toss mutation calls: {spy_calls}"


@pytest.mark.asyncio
async def test_generate_does_not_call_watch_activation_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WatchActivationService mutation methods have spies that raise if called.

    The generator is meant to be read-only — it must never activate a watch.
    Watch activation is a separate, explicitly-approved write path.
    """
    from app.services.investment_reports import watch_activation as watch_mod

    activation_class = getattr(watch_mod, "WatchActivationService", None)
    if activation_class is None:
        pytest.skip("WatchActivationService not found at expected import path")

    spy_calls: list[str] = []
    _install_spies(
        monkeypatch,
        activation_class,
        _WATCH_MUTATION_METHODS,
        "WatchActivationService",
        spy_calls,
    )

    generator, _, _ = _build_generator()
    await generator.generate(_build_request())

    assert spy_calls == [], (
        f"unexpected WatchActivationService mutation calls: {spy_calls}"
    )
