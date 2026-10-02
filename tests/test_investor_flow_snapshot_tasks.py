from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.jobs.investor_flow_snapshots import (
    InvestorFlowSnapshotBuildResult,
    InvestorFlowSnapshotSample,
)
from app.tasks import investor_flow_snapshot_tasks as tasks


def test_parse_args_defaults_to_dry_run_and_rejects_invalid_combinations():
    from scripts import build_investor_flow_snapshots as cli

    args = cli.parse_args(["--market", "kr"])
    assert args.market == "kr"
    assert args.limit == 20
    assert args.days == 20
    assert args.commit is False
    assert args.dry_run is True

    commit_args = cli.parse_args(["--market", "kr", "--symbol", "005930", "--commit"])
    assert commit_args.commit is True
    assert commit_args.dry_run is False

    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--all", "--limit", "20"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--days", "0"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--days", str(cli.MAX_DAYS + 1)])


@pytest.mark.asyncio
async def test_task_wrapper_defaults_to_dry_run_and_returns_camel_case(monkeypatch):
    captured = {}

    async def fake_runner(request):
        captured["commit"] = request.commit
        captured["days"] = request.days
        return InvestorFlowSnapshotBuildResult(
            market="kr",
            symbols_resolved=1,
            snapshots_built=1,
            symbols_with_rows=1,
            committed=request.commit,
            batches=1,
            started_at=dt.datetime(2026, 5, 12, 7, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, 7, 1, tzinfo=dt.UTC),
            snapshot_date_distribution={"2026-05-12": 1},
            idempotency={"wouldInsert": 1, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            samples=(
                InvestorFlowSnapshotSample(
                    market="kr",
                    symbol="005930",
                    snapshot_date=dt.date(2026, 5, 12),
                    source="naver_finance",
                    foreign_net=100,
                    institution_net=50,
                    individual_net=-150,
                    double_buy=True,
                    double_sell=False,
                ),
            ),
        )

    monkeypatch.setattr(tasks, "run_investor_flow_snapshot_build", fake_runner)

    raw_func = getattr(
        tasks.build_investor_flow_snapshots,
        "original_func",
        tasks.build_investor_flow_snapshots,
    )
    payload = await raw_func(market="kr", symbols=["005930"], days=20)

    assert captured == {"commit": False, "days": 20}
    assert payload["symbolsResolved"] == 1
    assert payload["snapshotsBuilt"] == 1
    assert payload["committed"] is False
    assert payload["idempotency"]["wouldInsert"] == 1
    assert payload["samples"][0]["snapshotDate"] == "2026-05-12"
    assert payload["status"] == "ok" and payload["symbolsWithRows"] == 1
    assert payload["samples"][0]["doubleBuy"] is True


def test_recurring_schedule_is_default_off():
    # ROB-438: the module now has a recurring scheduled task, but it is DEFAULT-OFF
    # — merging this PR alone registers no cron. The manual build task still carries
    # no schedule; the scheduled task's cron labels are empty unless the schedule
    # flag is set (operator-gated, mirroring invest_screener ROB-281).
    from unittest.mock import patch

    from app.tasks import TASKIQ_TASK_MODULES

    assert tasks in TASKIQ_TASK_MODULES
    labels = getattr(tasks.build_investor_flow_snapshots, "labels", {}) or {}
    assert labels.get("schedule") is None  # manual task: no schedule
    with patch.object(tasks.settings, "investor_flow_schedule_enabled", False):
        assert tasks._kr_flow_schedule("40 16 * * 1-5") == []
    with patch.object(tasks.settings, "investor_flow_schedule_enabled", True):
        assert tasks._kr_flow_schedule("40 16 * * 1-5") == [
            {"cron": "40 16 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]


def _result(resolved: int, with_rows: int) -> InvestorFlowSnapshotBuildResult:
    return InvestorFlowSnapshotBuildResult(
        market="kr",
        symbols_resolved=resolved,
        snapshots_built=with_rows * 20,
        committed=False,
        batches=1,
        started_at=dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
        symbols_with_rows=with_rows,
    )


def test_flow_status_reports_real_row_coverage():
    assert tasks._flow_status(_result(3944, 3944)) == "ok"
    assert tasks._flow_status(_result(3944, 3900)) == "partial"
    assert tasks._flow_status(_result(3944, 0)) == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now_utc", "expect_run"),
    [
        # Sat 08:30 KST, Fri session
        (dt.datetime(2026, 10, 2, 23, 30, tzinfo=dt.UTC), True),
        # Tue 08:30 KST after substitute-holiday Mon
        (dt.datetime(2026, 10, 5, 23, 30, tzinfo=dt.UTC), True),
        # Sun: Sat not session
        (dt.datetime(2026, 10, 3, 23, 30, tzinfo=dt.UTC), False),
        # Fri 10/9 holiday, Thu session
        (dt.datetime(2026, 10, 8, 23, 30, tzinfo=dt.UTC), True),
        # Sat after holiday Fri
        (dt.datetime(2026, 10, 9, 23, 30, tzinfo=dt.UTC), False),
    ],
)
async def test_scheduled_gate_runs_when_today_or_yesterday_is_session(
    monkeypatch, now_utc, expect_run
):
    calls = []

    async def fake_build(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    class _FrozenDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc

    monkeypatch.setattr(tasks, "build_investor_flow_snapshots", fake_build)
    monkeypatch.setattr(
        tasks,
        "dt",
        SimpleNamespace(datetime=_FrozenDT, UTC=dt.UTC, timedelta=dt.timedelta),
    )
    monkeypatch.setattr(tasks.settings, "investor_flow_snapshots_commit_enabled", False)
    raw = getattr(
        tasks.scheduled_kr_investor_flow,
        "original_func",
        tasks.scheduled_kr_investor_flow,
    )
    result = await raw()
    assert bool(calls) is expect_run
    if not expect_run:
        assert result == {"status": "skipped_holiday", "market": "kr"}
    else:
        assert calls[0]["all_symbols"] is True and calls[0]["commit"] is False
