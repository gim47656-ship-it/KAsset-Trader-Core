"""Static contract for the stable CI gate and docs-only fast path.

The classifier may skip resource-heavy jobs only when every changed path is a
known documentation path. Any mixed, unknown, deleted, renamed, or shared-CI
change remains full coverage. ``ci-required`` must explicitly authorize only
the aggregate children skipped by that proven docs-only verdict.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"

#: Live branch-protection snapshot verified before this cutover.
REQUIRED_CHECK_NAMES = (
    "ci-required",
    "migration (PostgreSQL 15)",
    "frontend",
)
AGGREGATE_CHILD_JOB_IDS = ("lint", "taskiq-smoke", "test")
DOCS_GATED_JOB_IDS = (
    "lint",
    "migration",
    "test",
    "taskiq-smoke",
    "security",
    "alpaca-track-fast-tests",
    "intraday-harness-v2-tests",
    "kiwoom-dual-surface-smoke",
    "alpaca-track-walkforward-tests",
    "frontend",
)
DOCS_GATE_CONDITION = (
    "needs.change-classifier.outputs.run_all == 'true' || "
    "needs.change-classifier.outputs.lanes != 'docs'"
)

AGGREGATE_JOB_ID = "ci-required"
CLASSIFIER_JOB_ID = "change-classifier"
MIGRATION_JOB_ID = "migration"
FRONTEND_JOB_ID = "frontend"

@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _displayed_names(job_id: str, job: dict[str, Any]) -> list[str]:
    """Reproduce the check name GitHub displays for a job."""

    label = job.get("name", job_id)
    matrix = job.get("strategy", {}).get("matrix")
    if not matrix:
        return [label]
    keys = list(matrix.keys())
    return [
        f"{label} ({', '.join(str(value) for value in combo)})"
        for combo in itertools.product(*(matrix[key] for key in keys))
    ]


# --------------------------------------------------------------------------
# Protected names and docs-only job gates
# --------------------------------------------------------------------------


def test_protected_check_names_remain_stable(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    names = [
        *_displayed_names(AGGREGATE_JOB_ID, jobs[AGGREGATE_JOB_ID]),
        *_displayed_names(MIGRATION_JOB_ID, jobs[MIGRATION_JOB_ID]),
        *_displayed_names(FRONTEND_JOB_ID, jobs[FRONTEND_JOB_ID]),
    ]
    assert names == list(REQUIRED_CHECK_NAMES)


def test_test_job_matrix_shape_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["jobs"]["test"]["strategy"]["matrix"] == {
        "python-version": ["3.13"],
        "group": [1, 2, 3, 4],
    }


@pytest.mark.parametrize("job_id", DOCS_GATED_JOB_IDS)
def test_resource_job_has_the_exact_docs_only_gate(
    workflow: dict[str, Any], job_id: str
) -> None:
    job = workflow["jobs"][job_id]
    assert job["needs"] == CLASSIFIER_JOB_ID
    assert job["if"] == DOCS_GATE_CONDITION


def test_kasset_migration_job_still_uses_pg15(workflow: dict[str, Any]) -> None:
    job = workflow["jobs"][MIGRATION_JOB_ID]
    assert job["services"]["postgres"]["image"] == "postgres:15-alpine"
    assert job.get("continue-on-error") is not True


def test_kasset_migration_job_round_trips_all_new_revisions(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][MIGRATION_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert (
        "tests/services/paper_cohort/test_migration.py"
        "::test_real_postgresql_upgrade_downgrade_upgrade_single_head" in script
    )
    assert "timescale" not in script.lower()


# --------------------------------------------------------------------------
# The aggregate exists and cannot disappear on failure
# --------------------------------------------------------------------------


def test_aggregate_job_exists_with_a_constant_displayed_name(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    assert job["name"] == AGGREGATE_JOB_ID
    assert _displayed_names(AGGREGATE_JOB_ID, job) == [AGGREGATE_JOB_ID]
    assert "matrix" not in job.get("strategy", {})


def test_aggregate_job_runs_even_when_children_fail(
    workflow: dict[str, Any],
) -> None:
    """Without `always()` the aggregate is skipped on any child failure."""

    assert workflow["jobs"][AGGREGATE_JOB_ID]["if"] == "always()"


def test_aggregate_job_needs_every_currently_required_job_and_the_classifier(
    workflow: dict[str, Any],
) -> None:
    needs = workflow["jobs"][AGGREGATE_JOB_ID]["needs"]
    assert set(needs) == {*AGGREGATE_CHILD_JOB_IDS, CLASSIFIER_JOB_ID}


def test_aggregate_required_flags_match_its_needs_list(
    workflow: dict[str, Any],
) -> None:
    """A `needs:` entry with no `--required` flag would be silently ignored."""

    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    tokens = script.replace("\\\n", " ").split()
    declared = [
        tokens[index + 1]
        for index, token in enumerate(tokens)
        if token == "--required" and index + 1 < len(tokens)
    ]
    assert sorted(declared) == sorted(job["needs"])


def test_aggregate_authorizes_only_docs_only_child_skips(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    env = next(
        step["env"]
        for step in job["steps"]
        if step.get("name") == "Evaluate required child results"
    )

    assert env["DOCS_ONLY"] == (
        "${{ needs.change-classifier.outputs.run_all == 'false' && "
        "needs.change-classifier.outputs.lanes == 'docs' }}"
    )
    authorized = [
        line.split()[-1]
        for line in script.splitlines()
        if "--authorize-skip" in line
    ]
    assert sorted(authorized) == sorted(AGGREGATE_CHILD_JOB_IDS)
    assert "--allow-undeclared" not in script


def test_aggregate_invokes_the_checked_in_aggregator_script(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][AGGREGATE_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert "scripts/ci/aggregate_required.py" in script
    assert (WORKFLOW_PATH.parents[2] / "scripts/ci/aggregate_required.py").is_file()


# --------------------------------------------------------------------------
# Classifier cutover
# --------------------------------------------------------------------------


def test_classifier_job_exists_and_checks_out_full_history(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][CLASSIFIER_JOB_ID]
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_classifier_job_invokes_the_checked_in_classifier_script(
    workflow: dict[str, Any],
) -> None:
    job = workflow["jobs"][CLASSIFIER_JOB_ID]
    script = "\n".join(step.get("run", "") for step in job["steps"] if step.get("run"))
    assert "scripts/ci/classify_changes.py" in script
    assert (WORKFLOW_PATH.parents[2] / "scripts/ci/classify_changes.py").is_file()


def test_only_docs_gated_jobs_and_aggregate_read_classifier_outputs(
    workflow: dict[str, Any],
) -> None:
    allowed = {*DOCS_GATED_JOB_IDS, AGGREGATE_JOB_ID}
    for job_id, job in workflow["jobs"].items():
        if "needs.change-classifier.outputs" in str(job):
            assert job_id in allowed


def test_docs_gate_is_fail_closed_for_every_non_docs_verdict(
    workflow: dict[str, Any],
) -> None:
    for job_id in DOCS_GATED_JOB_IDS:
        condition = workflow["jobs"][job_id]["if"]
        assert "run_all == 'true'" in condition
        assert "lanes != 'docs'" in condition


def test_notify_is_neutral_when_webhook_secret_is_absent(
    workflow: dict[str, Any],
) -> None:
    notify = workflow["jobs"]["notify"]
    script = "\n".join(
        step.get("run", "") for step in notify["steps"] if step.get("run")
    )

    assert 'if [[ -z "${DISCORD_WEBHOOK:-}" ]]' in script
    assert "exit 0" in script
    assert "curl --fail-with-body --silent --show-error" in script

# --------------------------------------------------------------------------
# Nothing else about the workflow moved
# --------------------------------------------------------------------------


def test_workflow_triggers_are_unchanged(workflow: dict[str, Any]) -> None:
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
        "workflow_dispatch": None,
    }


def test_concurrency_block_is_unchanged(workflow: dict[str, Any]) -> None:
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number "
        "|| github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
