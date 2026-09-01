from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.kasset_paper_ops as cli
from app.services.daily_candles.readiness import (
    BenchmarkCoverage,
    CohortEvidence,
    DailyCandlesReadiness,
    MarketReadiness,
)


def test_promotion_approve_accepts_only_candidate_identity_and_reason() -> None:
    args = cli.parse_args(
        [
            "promotion-approve",
            "--candidate-id",
            "41",
            "--reason",
            "운영자 검토 완료",
        ]
    )

    assert args.candidate_id == 41
    assert args.reason == "운영자 검토 완료"
    assert not hasattr(args, "metrics")
    assert not hasattr(args, "evidence")

    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "promotion-approve",
                "--candidate-id",
                "41",
                "--reason",
                "운영자 검토 완료",
                "--total-return",
                "0.25",
            ]
        )


@pytest.mark.asyncio
async def test_promotion_approve_delegates_persisted_candidate_id_only(
    monkeypatch,
    capsys,
) -> None:
    args = cli.parse_args(
        [
            "promotion-approve",
            "--candidate-id",
            "41",
            "--reason",
            "운영자 검토 완료",
        ]
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    calls: list[tuple[int, str]] = []

    class FakeService:
        def __init__(self, db: object) -> None:
            assert db is session

        async def approve_candidate(
            self,
            candidate_id: int,
            *,
            at: object,
            operator_reason: str,
        ) -> object:
            assert at is not None
            calls.append((candidate_id, operator_reason))
            return SimpleNamespace(
                as_evidence=lambda: {
                    "promotionCandidateId": candidate_id,
                    "state": "paper_approved",
                }
            )

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "StrategyPromotionService", FakeService)

    rc = await cli.run(args)

    assert rc == 0
    assert calls == [(41, "운영자 검토 완료")]
    output = capsys.readouterr().out
    assert '"promotionCandidateId": 41' in output
    assert '"state": "paper_approved"' in output


@pytest.mark.asyncio
async def test_readiness_emits_cohort_and_separate_history_promotion_results(
    monkeypatch,
    capsys,
) -> None:
    args = cli.parse_args(
        [
            "readiness",
            "--as-of",
            "2026-01-06T12:00:00Z",
            "--kr-cohort-id",
            "kr-explicit",
            "--us-cohort-id",
            "us-explicit",
        ]
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    cohort = CohortEvidence(
        cohort_id="us-explicit",
        market="us",
        selection_as_of=datetime(2025, 1, 2, tzinfo=UTC),
        selection_date=date(2025, 1, 2),
        effective_date=date(2025, 6, 1),
        method="latest_market_cap",
        requested_size=100,
        active_member_count=100,
        valuation_snapshot_date=date(2025, 1, 1),
        valuation_snapshot_source="yahoo",
        evidence_scope="forward_paper",
    )
    benchmark = BenchmarkCoverage(
        market="us",
        symbol="SPY",
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 5, tzinfo=UTC),
        count=252,
        source="kis",
        sources=("kis",),
        status="available",
    )
    market = MarketReadiness(
        market="us",
        cohort=cohort,
        evaluated_window_start=date(2025, 1, 1),
        evaluated_window_end=date(2026, 1, 5),
        latest_completed_session=date(2026, 1, 6),
        ingest_lag_session_count=1,
        unevidenced_session_count=0,
        unevidenced_sessions=(),
        total_symbol_count=100,
        cohort_active_member_count=100,
        forced_member_count=0,
        benchmark_member_count=1,
        active_symbol_count=100,
        inactive_symbol_count=0,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=100,
        eligible_symbol_count=100,
        eligible_symbols=tuple(f"US-{index}" for index in range(100)),
        excluded_symbols=(),
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=0,
        calendar_status="available",
        price_adjustment_status="covered",
        corporate_action_status="clear",
        corporate_action_covered_symbol_count=100,
        adjustment_covered_symbol_count=100,
        list_date_covered_symbol_count=100,
        members_listed_after_cohort_start=0,
        delist_date_covered_inactive_count=0,
        point_in_time_available=False,
        inactive_with_candles_count=0,
        delisted_symbol_count=0,
        delisted_with_candles_count=0,
        includes_delisted=False,
        fallback_only=False,
        benchmark=benchmark,
        daily_history_ready=True,
        promotion_ready=True,
        historical_evidence_ready=False,
        daily_history_blockers=(),
        blockers=(),
        historical_evidence_blockers=(
            "us:cohort_not_historical_pit",
            "us:cohort_window_predates_effective_date",
        ),
        unresolved_evidence=(
            "us:cohort_not_historical_pit",
            "us:cohort_window_predates_effective_date",
            "us:ingest_lag_window_rolled_back",
        ),
        reasons=(
            "us:cohort_not_historical_pit",
            "us:cohort_window_predates_effective_date",
        ),
    )
    report = DailyCandlesReadiness(
        as_of=args.as_of,
        required_history_bars=252,
        markets=(market,),
        daily_history_ready=True,
        promotion_ready=True,
        historical_evidence_ready=False,
        daily_history_blockers=(),
        blockers=(),
        historical_evidence_blockers=market.historical_evidence_blockers,
        unresolved_evidence=market.unresolved_evidence,
        reasons=market.reasons,
    )
    calls: list[dict[str, object]] = []

    class FakeReadinessService:
        def __init__(self, db: object) -> None:
            assert db is session

        async def measure(self, **kwargs: object) -> DailyCandlesReadiness:
            calls.append(kwargs)
            return report

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "DailyCandlesReadinessService", FakeReadinessService)

    rc = await cli.run(args)

    assert rc == 0
    assert calls == [
        {
            "as_of": datetime(2026, 1, 6, 12, tzinfo=UTC),
            "cohort_ids": {
                "kr": "kr-explicit",
                "us": "us-explicit",
            },
        }
    ]
    output = capsys.readouterr().out
    assert '"dailyHistoryReady": true' in output
    assert '"promotionReady": true' in output
    assert '"historicalEvidenceReady": false' in output
    assert '"us": "us-explicit"' in output
    assert '"us:cohort_not_historical_pit"' in output
    assert '"us:cohort_window_predates_effective_date"' in output
    assert '"us:ingest_lag_window_rolled_back"' in output
    # Nothing to recover: no daily-history blocker means an empty recovery plan.
    assert '"복구계획": []' in output


def _blocked_market(
    *,
    market: str,
    cohort_id: str | None,
    daily_blockers: tuple[str, ...],
) -> MarketReadiness:
    cohort = (
        CohortEvidence(
            cohort_id=cohort_id,
            market=market,  # type: ignore[arg-type]
            selection_as_of=datetime(2025, 1, 2, tzinfo=UTC),
            selection_date=date(2025, 1, 2),
            effective_date=date(2025, 6, 1),
            method="latest_market_cap",
            requested_size=100,
            active_member_count=100,
            valuation_snapshot_date=date(2025, 1, 1),
            valuation_snapshot_source="naver_finance" if market == "kr" else "yahoo",
            evidence_scope="forward_paper",
        )
        if cohort_id is not None
        else None
    )
    return MarketReadiness(
        market=market,  # type: ignore[arg-type]
        cohort=cohort,
        evaluated_window_start=date(2025, 1, 1),
        evaluated_window_end=date(2026, 1, 5),
        latest_completed_session=date(2026, 1, 6),
        ingest_lag_session_count=0,
        unevidenced_session_count=0,
        unevidenced_sessions=(),
        total_symbol_count=100,
        cohort_active_member_count=100,
        forced_member_count=0,
        benchmark_member_count=1,
        active_symbol_count=100,
        inactive_symbol_count=0,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=0,
        eligible_symbol_count=0,
        eligible_symbols=(),
        excluded_symbols=(),
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=0,
        calendar_status="available",
        price_adjustment_status="incomplete",
        corporate_action_status="unknown",
        corporate_action_covered_symbol_count=0,
        adjustment_covered_symbol_count=0,
        list_date_covered_symbol_count=100,
        members_listed_after_cohort_start=0,
        delist_date_covered_inactive_count=0,
        point_in_time_available=False,
        inactive_with_candles_count=0,
        delisted_symbol_count=0,
        delisted_with_candles_count=0,
        includes_delisted=False,
        fallback_only=False,
        benchmark=BenchmarkCoverage(
            market=market,  # type: ignore[arg-type]
            symbol="KOSPI" if market == "kr" else "SPY",
            start=None,
            end=None,
            count=0,
            source=None,
            sources=(),
            status="unavailable",
        ),
        daily_history_ready=False,
        promotion_ready=False,
        historical_evidence_ready=False,
        daily_history_blockers=daily_blockers,
        blockers=daily_blockers,
        historical_evidence_blockers=(),
        unresolved_evidence=(),
        reasons=daily_blockers,
    )


def test_recovery_plan_maps_every_daily_blocker_to_one_command() -> None:
    """A 0-candidate deployment must read its exact next commands off readiness."""

    kr = _blocked_market(
        market="kr",
        cohort_id="kr-cohort-1",
        daily_blockers=(
            "kr:insufficient_history",
            "kr:adjustment_coverage_incomplete",
            "kr:benchmark_unavailable",
        ),
    )
    us = _blocked_market(
        market="us",
        cohort_id=None,
        daily_blockers=("us:cohort_not_found", "us:eligible_symbols_zero"),
    )
    report = DailyCandlesReadiness(
        as_of=datetime(2026, 1, 6, 12, tzinfo=UTC),
        required_history_bars=252,
        markets=(kr, us),
        daily_history_ready=False,
        promotion_ready=False,
        historical_evidence_ready=False,
        daily_history_blockers=kr.blockers + us.blockers,
        blockers=kr.blockers + us.blockers,
        historical_evidence_blockers=(),
        unresolved_evidence=(),
        reasons=(),
    )

    plan = cli._recovery_plan(report)

    assert [item["blocker"] for item in plan] == [
        "kr:insufficient_history",
        "kr:adjustment_coverage_incomplete",
        "kr:benchmark_unavailable",
        "us:cohort_not_found",
        "us:eligible_symbols_zero",
    ]
    assert all(item["명령"] for item in plan)
    backfill = plan[0]["명령"]
    assert "scripts/backfill_daily_candles.py" in backfill
    assert "--market kr" in backfill
    assert "--cohort-id kr-cohort-1" in backfill
    assert "--horizon-bars 400" in backfill
    assert "--include-benchmark" in backfill
    assert plan[1]["명령"] == backfill
    assert "--benchmark-only" in plan[2]["명령"]
    # No cohort yet: the operator must build the cohort before any backfill.
    assert "scripts/build_kasset_research_cohort.py" in plan[3]["명령"]
    assert "--market us" in plan[3]["명령"]
    assert plan[4]["명령"] == plan[3]["명령"]


def test_recovery_plan_names_manual_repair_for_integrity_blockers() -> None:
    market = _blocked_market(
        market="kr",
        cohort_id="kr-cohort-1",
        daily_blockers=("kr:duplicate_bar_timestamp", "kr:unknown_future_code"),
    )
    report = DailyCandlesReadiness(
        as_of=datetime(2026, 1, 6, 12, tzinfo=UTC),
        required_history_bars=252,
        markets=(market,),
        daily_history_ready=False,
        promotion_ready=False,
        historical_evidence_ready=False,
        daily_history_blockers=market.blockers,
        blockers=market.blockers,
        historical_evidence_blockers=(),
        unresolved_evidence=(),
        reasons=(),
    )

    plan = cli._recovery_plan(report)

    assert "public.kr_candles_1d" in plan[0]["명령"]
    # An unmapped code must still produce an explicit action, never silence.
    assert plan[1]["이유"] == "정의된 복구 동작이 없습니다."
    assert "public.kr_candles_1d" in plan[1]["명령"]


def test_backtest_build_defaults_to_the_forward_paper_track() -> None:
    assert cli.parse_args(["backtest-build"]).track == "forward_paper"
    assert (
        cli.parse_args(["backtest-build", "--track", "historical_pit"]).track
        == "historical_pit"
    )
    with pytest.raises(SystemExit):
        cli.parse_args(["backtest-build", "--track", "anything_else"])
