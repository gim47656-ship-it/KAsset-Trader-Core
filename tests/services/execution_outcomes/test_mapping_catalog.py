from __future__ import annotations

import json
from pathlib import Path

from app.services.execution_outcomes import (
    CURRENT_RESPONSE_MAPPINGS,
    BrokerAcceptance,
    BrokerSurface,
    EvidenceFinality,
    LegacyResponseMapping,
    TerminalEvidence,
    TrackingState,
)

FIXTURE = Path(__file__).parent / "fixtures" / "current_response_semantics.json"
OUTCOME_FIELDS = (
    "request_validated",
    "mutation_sent",
    "broker_acceptance",
    "tracking",
    "local_recorded",
    "reconcile_required",
    "terminal_evidence",
)


def _outcome_values(entry: LegacyResponseMapping) -> list[bool | str]:
    values: list[bool | str] = []
    for field_name in OUTCOME_FIELDS:
        value = getattr(entry.outcome, field_name)
        values.append(value.value if hasattr(value, "value") else value)
    return values


def _mapping(mapping_id: str) -> LegacyResponseMapping:
    matches = [
        entry for entry in CURRENT_RESPONSE_MAPPINGS if entry.mapping_id == mapping_id
    ]
    assert len(matches) == 1
    return matches[0]


def test_mapping_catalog_matches_frozen_current_response_fixture() -> None:
    expected = json.loads(FIXTURE.read_text())
    actual = {
        entry.mapping_id: {
            "surface": entry.surface.value,
            "legacy_success_meaning": entry.legacy_success_meaning.value,
            "legacy_response_markers": list(entry.legacy_response_markers),
            "outcome": _outcome_values(entry),
        }
        for entry in CURRENT_RESPONSE_MAPPINGS
    }

    assert expected["schema_version"] == "execution-outcome-mapping-fixture/v2"
    assert expected["outcome_fields"] == list(OUTCOME_FIELDS)
    assert actual == expected["mappings"]


def test_catalog_covers_every_approved_broker_surface() -> None:
    assert {entry.surface for entry in CURRENT_RESPONSE_MAPPINGS} == set(BrokerSurface)
    marker_sets = {
        frozenset(entry.legacy_response_markers) for entry in CURRENT_RESPONSE_MAPPINGS
    }
    assert len(marker_sets) == len(CURRENT_RESPONSE_MAPPINGS)


def test_legacy_success_true_needs_explicit_evidence_to_mean_filled() -> None:
    success_true_rows = [
        entry
        for entry in CURRENT_RESPONSE_MAPPINGS
        if "success=true" in entry.legacy_response_markers
    ]

    assert success_true_rows
    assert any(not entry.outcome.mutation_sent for entry in success_true_rows)
    assert any(
        entry.outcome.broker_acceptance is BrokerAcceptance.ACCEPTED
        and entry.outcome.terminal_evidence is TerminalEvidence.NONE
        for entry in success_true_rows
    )
    for entry in success_true_rows:
        if entry.outcome.terminal_evidence is TerminalEvidence.FILLED:
            assert "order_status=filled" in entry.legacy_response_markers


def test_generic_tracking_rows_split_on_broker_order_id_presence() -> None:
    tracked = _mapping("generic.live_accepted_pending_fill")
    untracked = _mapping("generic.live_accepted_untracked_pending_fill")
    assert "order_id=non_empty" in tracked.legacy_response_markers
    assert tracked.outcome.tracking is TrackingState.TRACKED
    assert "order_id=absent" in untracked.legacy_response_markers
    assert untracked.outcome.tracking is TrackingState.UNTRACKED
    assert untracked.outcome.reconcile_required is True


def test_unknown_rows_are_fail_closed() -> None:
    unknown_rows = [
        entry
        for entry in CURRENT_RESPONSE_MAPPINGS
        if entry.outcome.broker_acceptance is BrokerAcceptance.UNKNOWN
    ]

    assert unknown_rows
    assert all(entry.outcome.reconcile_required for entry in unknown_rows)
    assert all(not entry.outcome.automatic_resend_allowed for entry in unknown_rows)
    assert all(
        entry.outcome.evidence_finality is EvidenceFinality.NON_TERMINAL
        for entry in unknown_rows
    )


def test_catalog_is_declarative_and_has_reviewable_source_locators() -> None:
    assert all(
        entry.source_locator.startswith("app/") for entry in CURRENT_RESPONSE_MAPPINGS
    )
    assert all("::" in entry.source_locator for entry in CURRENT_RESPONSE_MAPPINGS)
