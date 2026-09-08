"""ROB-1284 — the two seams the gate suite never entered.

`test_rob1284_sweep_gates` proves the *decision* rules by substituting the
service's collection points.  That substitution left two production code paths
with no test at all — not "weakly tested", untested:

**E-A — `RestingRungSweepService.fetch_evidence`.**  Every gate fixture
overrides it wholesale, so the real ledger and resolving-key read loop never
ran under pytest. This test drives the surviving Toss ledger through real
`select()` statements and verifies that terminal evidence produces the expected
transition without writes during planning.

**E-B — `run_resting_rung_sweep`.**  The reconcile-side wrapper had zero
matches under ``tests/``.  It is the only thing that turns the reconcile pass's
``dry_run`` into the sweep's write gates and issues the ``commit``, and it runs
on the automatic path — the production DML wrapper was the least-tested object
in the change.

So these tests deliberately do *not* stub the units under test.  E-A drives the
real ``fetch_evidence`` through a session that answers real ``select()``
statements; E-B drives the real wrapper and watches what reaches the session.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import proposal_rung_convergence as prc
from app.mcp_server.tooling.proposal_rung_convergence import run_resting_rung_sweep
from app.services.order_proposals.resting_sweep import RungVerdict
from app.services.order_proposals.resting_sweep_service import RestingRungSweepService
from tests.services.order_proposals.test_rob1284_sweep_gates import (
    NOW,
    _candidate,
    _RecordingSession,
    _terminal_evidence,
)

pytestmark = pytest.mark.unit


# ============================================================ E-A: fetch_evidence


class _LedgerRow:
    """The attribute surface `fetch_evidence` reads off a ledger ORM row."""

    def __init__(self) -> None:
        self.id = 150
        self.status = "expired"
        self.order_no = "0023769000"
        self.idempotency_key = "idem-1"
        self.correlation_id = "corr-1"
        self.filled_qty = None
        self.reconciled_at = None
        self.updated_at = None


class _Rows:
    def __init__(self, rows: tuple[Any, ...]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Any | None:
        return self._rows[0] if len(self._rows) == 1 else None

    def scalar_one_or_none(self) -> Any | None:
        return self.one_or_none()

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


def _statement_entity(statement: Any) -> str:
    """Which model a statement selects from — the observation point for E-A.

    ``fetch_evidence`` is the only thing in this test that builds ``select()``
    statements, so this list *is* the proof the production loop ran: a fixture
    override could not produce it.
    """
    descriptions = getattr(statement, "column_descriptions", None)
    if not descriptions:
        return "<non-select>"
    entity = descriptions[0].get("entity")
    return getattr(entity, "__name__", None) or "<non-select>"


class _LedgerReadSession:
    """Answers the real ledger SELECTs; fails the ones it is told to fail.

    Reads and writes are recorded separately.  ``entities`` is what
    ``fetch_evidence`` asked for, in order; ``mutating`` is any write verb.  A
    read-shaped session cannot use "no execute at all" as its write detector the
    way ``_RecordingSession`` does, so writes are identified by the statement
    leaving the retained ledger models — which is exactly what a rung transition
    would do.
    """

    def __init__(
        self,
        *,
        rows_by_model: dict[str, tuple[Any, ...]],
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self._rows_by_model = rows_by_model
        self._fail_on = frozenset(fail_on)
        self.entities: list[str] = []
        self.mutating: list[str] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _Rows:
        entity = _statement_entity(statement)
        self.entities.append(entity)
        if entity in self._fail_on:
            raise TimeoutError(f"ledger read timed out: {entity}")
        return _Rows(self._rows_by_model.get(entity, ()))

    async def commit(self) -> None:
        self.mutating.append("commit")

    async def flush(self) -> None:
        self.mutating.append("flush")

    async def refresh(self, *args: Any, **kwargs: Any) -> None:
        self.mutating.append("refresh")

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.mutating.append("add")


_LEDGER_MODELS = ("TossLiveOrderLedger",)

# `_candidate()` carries all three resolving keys, so each ledger is queried
# three times: broker_order_id, idempotency_key, correlation_id.
_FULL_SCAN = [name for name in _LEDGER_MODELS for _ in range(3)]


class _RealFetchEvidence(RestingRungSweepService):
    """The service with `fetch_evidence` left alone.

    Only the two neighbouring collection points are substituted:
    `collect_candidates` (so no proposals table is needed) and
    `verify_ownership` (a separate seam, already covered in the gate suite).
    """

    def __init__(self) -> None:
        self.session = _LedgerReadSession(
            rows_by_model={"TossLiveOrderLedger": (_LedgerRow(),)},
        )
        super().__init__(session=self.session)  # type: ignore[arg-type]

    async def collect_candidates(self):  # type: ignore[override]
        return [_candidate()]

    async def verify_ownership(self, candidate, evidence):  # type: ignore[override]
        return evidence, None


@pytest.mark.asyncio
async def test_production_fetch_evidence_loop_is_the_one_under_test():
    """The real loop reads the Toss ledger and yields a transition."""
    svc = _RealFetchEvidence()
    decisions = await svc.plan(now=NOW)

    assert svc.session.entities == _FULL_SCAN
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.verdict is RungVerdict.TRANSITION
    assert decision.target_state == "expired"
    assert [e.ledger_id for e in decision.evidence] == [150]
    assert [e.ledger_table for e in decision.evidence] == [
        "review.toss_live_order_ledger"
    ]
    assert svc.session.mutating == []


# ================================================== E-B: run_resting_rung_sweep


class _WiringSession(_RecordingSession):
    """`_RecordingSession` usable as the wrapper's `async with` session."""

    async def __aenter__(self) -> _WiringSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _WiringProbe(RestingRungSweepService):
    """The **real** service, with a transitionable plan, recording its call.

    ``apply`` is not replaced — it is wrapped, then delegated to.  So the
    wrapper's arguments are observed *and* actually executed against the real
    write gates, which is what makes "no write reached the session" a fact about
    production behaviour rather than about a stub.
    """

    def __init__(self, session: Any) -> None:
        super().__init__(session)
        self.apply_kwargs: dict[str, Any] | None = None

    async def collect_candidates(self):  # type: ignore[override]
        return [_candidate()]

    async def fetch_evidence(self, candidate):  # type: ignore[override]
        return (_terminal_evidence(),)

    async def verify_ownership(self, candidate, evidence):  # type: ignore[override]
        return evidence, None

    async def apply(self, **kwargs: Any):  # type: ignore[override]
        self.apply_kwargs = dict(kwargs)
        return await super().apply(**kwargs)


def _install_wiring_probe(monkeypatch) -> tuple[_WiringSession, list[_WiringProbe]]:
    session = _WiringSession()
    probes: list[_WiringProbe] = []

    def _factory():
        return lambda: session

    def _service(db: Any) -> _WiringProbe:
        probe = _WiringProbe(db)
        probes.append(probe)
        return probe

    monkeypatch.setattr(prc, "_session_factory", _factory)
    monkeypatch.setattr(prc, "RestingRungSweepService", _service)
    return session, probes


@pytest.mark.asyncio
async def test_wrapper_forwards_the_callers_dry_run_and_writes_nothing(monkeypatch):
    """A dry-run reconcile pass must stay a dry run all the way to the writes.

    The wrapper is the only translation from the reconcile pass's ``dry_run``
    into the sweep's two write gates. If it hard-coded a live call, every
    service-level gate test would still pass while the automatic reconcile path
    wrote to production.
    """
    session, probes = _install_wiring_probe(monkeypatch)

    out = await run_resting_rung_sweep(dry_run=True)

    assert len(probes) == 1
    assert probes[0].apply_kwargs is not None
    assert probes[0].apply_kwargs["dry_run"] is True  # forwarded, not re-decided
    assert probes[0].apply_kwargs["confirm"] is False  # and not confirmed behind it
    assert session.calls == []  # no execute, no commit — zero DB work
    assert out["ran"] is True
    assert out["dry_run"] is True
    assert out["applied"] == 0
    # Teeth: there really was a row the sweep would have written.
    assert out["summary"]["by_verdict"]["TRANSITION"] == 1


@pytest.mark.asyncio
async def test_wrapper_commits_only_on_the_non_dry_run_path(monkeypatch):
    """The other direction, so "always dry_run" is not a passing implementation."""
    session, probes = _install_wiring_probe(monkeypatch)

    out = await run_resting_rung_sweep(dry_run=False)

    assert probes[0].apply_kwargs is not None
    assert probes[0].apply_kwargs["dry_run"] is False
    assert probes[0].apply_kwargs["confirm"] is True  # confirm is derived, not free
    assert "commit" in session.calls
    assert out["ran"] is True
    assert out["dry_run"] is False


@pytest.mark.asyncio
async def test_wrapper_reports_a_sweep_failure_instead_of_raising(monkeypatch):
    """A failed sweep must not take down the reconcile that already booked."""
    session = _WiringSession()

    def _boom(db: Any) -> Any:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(prc, "_session_factory", lambda: lambda: session)
    monkeypatch.setattr(prc, "RestingRungSweepService", _boom)

    out = await run_resting_rung_sweep(dry_run=True)

    assert out == {"ran": False, "error": "sweep exploded"}
    assert session.calls == []
