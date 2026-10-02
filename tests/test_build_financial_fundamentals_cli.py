from __future__ import annotations

import datetime as dt

import pytest

from scripts import build_financial_fundamentals_snapshots as cli
from scripts.build_financial_fundamentals_snapshots import parse_args


def test_defaults_to_dry_run():
    args = parse_args(["--symbol", "005930"])
    assert args.dry_run is True
    assert args.commit is False
    assert args.include_quarterly is False
    assert args.market == "kr"


def test_commit_flag_disables_dry_run():
    args = parse_args(["--all", "--commit"])
    assert args.dry_run is False
    assert args.commit is True


def test_all_is_mutually_exclusive_with_symbol():
    with pytest.raises(SystemExit):
        parse_args(["--all", "--symbol", "005930"])


def test_estimate_only_sets_flag():
    args = parse_args(["--symbol", "005930", "--estimate-only"])
    assert args.estimate_only is True
    assert args.commit is False


def test_estimate_only_mutually_exclusive_with_commit():
    with pytest.raises(SystemExit):
        parse_args(["--symbol", "005930", "--estimate-only", "--commit"])


def test_estimate_only_defaults_false():
    args = parse_args(["--symbol", "005930"])
    assert args.estimate_only is False


@pytest.mark.unit
def test_skip_existing_flag() -> None:
    # ROB-441 budget-split: --skip-existing (off by default).
    assert parse_args(["--symbol", "005930"]).skip_existing is False
    args = parse_args(["--limit", "1500", "--skip-existing"])
    assert args.skip_existing is True
    assert args.limit == 1500


@pytest.mark.unit
def test_refresh_due_is_a_separate_mode() -> None:
    assert parse_args(["--symbol", "005930"]).refresh_due is False
    args = parse_args(["--with-quarterly", "--refresh-due", "--all", "--commit"])
    assert args.refresh_due is True and args.skip_existing is False
    # Continuous refresh and the one-shot backfill mode never combine.
    with pytest.raises(SystemExit):
        parse_args(["--refresh-due", "--skip-existing"])
    with pytest.raises(SystemExit):
        parse_args(["--refresh-due", "--symbol", "005930"])


@pytest.mark.asyncio
async def test_run_reports_budget_exhaustion_as_failure(monkeypatch, capsys) -> None:
    from app.jobs import financial_fundamentals_snapshots as job

    seen = {}

    async def _fake_run(request, **kwargs):
        seen["request"] = request
        now = dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.UTC)
        return job.FinancialFundamentalsSnapshotBuildResult(
            market="kr",
            symbols_resolved=3,
            snapshots_built=10,
            committed=False,
            started_at=now,
            finished_at=now,
            projected_requests=84,
            budget_exhausted=True,
        )

    monkeypatch.setattr(job, "run_financial_fundamentals_snapshot_build", _fake_run)
    argv = ["--with-quarterly", "--refresh-due", "--all", "--commit"]
    argv.append("--allow-partial")
    code = await cli.run(parse_args(argv))
    assert code == 3
    assert seen["request"].refresh_due is True
    assert seen["request"].all_symbols is True
    assert "DART BUDGET EXHAUSTED" in capsys.readouterr().out


def _result(**overrides):
    from app.jobs import financial_fundamentals_snapshots as job

    now = dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.UTC)
    values = {
        "market": "kr",
        "symbols_resolved": 0,
        "snapshots_built": 0,
        "committed": False,
        "started_at": now,
        "finished_at": now,
        "projected_requests": 0,
    }
    return job.FinancialFundamentalsSnapshotBuildResult(**{**values, **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_code", "expected_text"),
    [
        # Symbols were requested and every fetch failed: a failed run.
        (
            _result(symbols_resolved=2, no_rows_collected=True),
            4,
            "NO ROWS COLLECTED",
        ),
        # Nothing was due today: no fetch, still a successful run.
        (_result(warnings=("no symbols resolved",)), 0, "nothing due"),
    ],
)
async def test_run_separates_all_failed_from_nothing_due(
    monkeypatch, capsys, result, expected_code, expected_text
) -> None:
    from app.jobs import financial_fundamentals_snapshots as job

    async def _fake_run(request, **kwargs):
        return result

    monkeypatch.setattr(job, "run_financial_fundamentals_snapshot_build", _fake_run)
    argv = ["--with-quarterly", "--refresh-due", "--all", "--commit"]
    code = await cli.run(parse_args([*argv, "--allow-partial"]))
    out = capsys.readouterr().out
    assert code == expected_code
    assert expected_text in out
    assert "committed " not in out  # never reported as a successful commit
