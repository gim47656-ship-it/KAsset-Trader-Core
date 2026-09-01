from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment
from app.models.rung_reason_vocabulary import RUNG_VOID_REASON_GROUPS

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260714_rob849_paper_cohort.py"
ROB870_MIGRATION = REPO / "alembic/versions/20260714_rob870_approval_batches.py"


async def _assert_rung_reason_schema(engine) -> None:
    async with engine.connect() as connection:
        column = (
            (
                await connection.execute(
                    text(
                        "SELECT data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'review' "
                        "AND table_name = 'order_proposal_rungs' "
                        "AND column_name = 'void_reason_group'"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        assert column is not None
        assert column["data_type"] == "text"
        assert column["is_nullable"] == "YES"
        check_definitions = (
            (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(c.oid) "
                        "FROM pg_constraint AS c "
                        "WHERE c.conrelid = 'review.order_proposal_rungs'::regclass "
                        "AND c.contype = 'c' "
                        "AND pg_get_constraintdef(c.oid) "
                        "ILIKE '%void_reason_group%'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(check_definitions) == 1
        check_definition = check_definitions[0]
        assert isinstance(check_definition, str)
        assert all(
            f"'{group}'" in check_definition for group in RUNG_VOID_REASON_GROUPS
        )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cohort() -> PaperValidationCohort:
    nonce = uuid4().hex
    return PaperValidationCohort(
        cohort_id=f"cohort-{nonce}",
        cohort_hash=_hash(nonce),
        venues=["binance", "alpaca"],
        symbols=["BTCUSDT", "ETHUSDT"],
        market="spot",
        leverage="1",
        interval="1m",
        required_lookback=30,
        max_capture_skew_ms=5_000,
        max_ticker_age_ms=5_000,
        capital_notional_usd="100",
        assignment_count=1,
        activated_at=datetime.now(UTC),
    )


def test_migration_descends_from_merged_rob870_and_maintains_single_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    rob870_source = ROB870_MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "20260714_rob849_paper_cohort"' in source
    assert 'down_revision = "20260714_rob870_approval_batches"' in source
    assert 'down_revision = "20260713_rob848_paper_validation"' in rob870_source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    lineage = {
        revision.revision for revision in script.iterate_revisions(heads[0], "base")
    }
    assert "20260714_rob849_paper_cohort" in lineage


def test_migration_defines_composition_and_immutable_triggers() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "validate_paper_cohort_composition" in source
    assert "reject_paper_cohort_audit_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source


def test_migration_defines_full_lineage_reservation_fence_and_claim_states() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "paper_cohort_target_reservations",
        "paper_cohort_terminal_fences",
        "fk_paper_cohort_decision_assignment_lineage",
        "fk_paper_cohort_decision_snapshot_lineage",
        "fk_paper_cohort_intent_decision_lineage",
        "fk_paper_run_order_link_intent_lineage",
        "fk_paper_cohort_target_reservation_intent_lineage",
        "ck_paper_run_order_link_venue_ledger",
        "ck_paper_cohort_run_claim_state_consistency",
        "ck_paper_cohort_terminal_fence_text_bounds",
        "reconciliation_required",
    ):
        assert required in source


@pytest.mark.asyncio
@pytest.mark.usefixtures("retrospective_action_control_lock")
async def test_real_postgresql_upgrade_downgrade_upgrade_single_head() -> None:
    base_url = make_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-849 migration acceptance requires PostgreSQL")
    database = f"rob849_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        user=base_url.username,
        password=base_url.password,
        host=base_url.host,
        port=base_url.port,
        database="postgres",
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = base_url.set(database=database)
    target_url_text = target_url.render_as_string(hide_password=False)
    engine = create_async_engine(target_url_text)
    try:
        async with engine.begin() as connection:
            for schema in ("paper", "research", "review"):
                await connection.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                )
            await connection.run_sync(Base.metadata.create_all)
            # Base metadata represents the current application head. Rebuild
            # the ROB-849 boundary so later migrations are exercised instead
            # of colliding with tables that create_all already materialized.
            for table in (
                "strategy_learning_events",
                "evaluation_scorecards",
                "evaluation_verdicts",
                "evaluation_epochs",
                "evaluation_configs",
            ):
                await connection.execute(text(f"DROP TABLE research.{table}"))
            await connection.execute(
                text("DROP TABLE review.trade_retrospective_action_control")
            )
            await connection.execute(
                text("DROP TABLE review.trade_retrospective_actions")
            )
            # ROB-1109 is later than this reconstructed boundary. Current
            # metadata already contains its restored table, so remove it before
            # upgrading the post-ROB-849 chain.
            await connection.execute(
                text("DROP TABLE review.watch_order_intent_ledger")
            )
            # ROB-1036 is likewise later than this boundary; its three
            # append-only tables are already in Base.metadata.
            for table in (
                "invalid_sample_cleanup_lifecycle_events",
                "invalid_sample_cleanup_bindings",
                "sample_eligibility_decisions",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))
            # Same for the kis_mock pre-submit signal ledger, added after this
            # boundary and already present in Base.metadata.
            await connection.execute(text("DROP TABLE review.kis_mock_signal_ledger"))
            # ROB-1286's repricing claim table is likewise later than this
            # boundary and already in Base.metadata, so drop it and let the
            # migration create it.
            await connection.execute(
                text("DROP TABLE review.watch_event_repricing_claims")
            )
            # ROB-1283's decision_bucket column is later than this boundary and
            # is already materialized by create_all, so drop it (its CHECK and
            # index go with it) and let the migration add it back.
            await connection.execute(
                text("ALTER TABLE review.trade_forecasts DROP COLUMN decision_bucket")
            )
            # ROB-s257 E-2 is later than this reconstructed boundary. Current
            # metadata already contains its nullable observation column, so
            # drop it and let the migration add it back.
            await connection.execute(
                text(
                    "ALTER TABLE review.order_proposal_rungs "
                    "DROP COLUMN void_reason_group"
                )
            )
            # B1 loss-cut approval is later than this reconstructed boundary.
            # Remove its current-head tables and additive columns so the head
            # upgrade exercises the migration instead of colliding with
            # objects materialized by Base.metadata.create_all.
            await connection.execute(
                text("DROP TABLE review.order_proposal_approval_events")
            )
            await connection.execute(
                text("DROP TABLE review.order_proposal_loss_cut_scopes")
            )
            for column in (
                "publication_ref_digest",
                "evidence_hash",
                "scope_hash",
                "channel",
            ):
                await connection.execute(
                    text(
                        "ALTER TABLE review.order_proposal_approval_dispatch_attempts "
                        f"DROP COLUMN {column}"
                    )
                )
            for column in (
                "approved_by_subject",
                "approved_by_channel",
                "approval_dispatch_evidence_hash",
                "approval_dispatch_scope_hash",
                "approval_dispatch_channel",
            ):
                await connection.execute(
                    text(f"ALTER TABLE review.order_proposals DROP COLUMN {column}")
                )
            # W5's durable callback inbox and recovery cursor are later than
            # this reconstructed boundary and already in Base.metadata; drop
            # both so the upgrade chain creates them instead of colliding.
            for table in (
                "telegram_callback_recovery_cursor",
                "telegram_callback_inbox",
                "screener_pick_log",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))
            # Funding advisory is later than this reconstructed boundary. Drop
            # its current-head metadata tables so the additive migrations are
            # exercised by the upgrade chain instead of colliding with create_all.
            for table in (
                "funding_advisory_proposal_links",
                "funding_advisory_deliveries",
                "funding_advisory_revisions",
                "funding_advisories",
                "external_cash_declarations",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))
            # KAsset Android and AI review tables are later than this
            # reconstructed boundary and already present in current metadata.
            await connection.execute(text("DROP TABLE review.ai_recommendations"))
            # Append-only AI/automation operator ledgers are also post-boundary.
            # Current metadata materializes them, so let their migrations rebuild
            # both tables during each round trip.
            for table in (
                "kasset_paper_execution_events",
                "kasset_automation_cycle_events",
                "ai_call_events",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))
            for table in (
                "kasset_shadow_loss_locks",
                "kasset_shadow_daily_high_watermarks",
                "kasset_ai_runtime_config",
                "password_reset_tokens",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            for column in (
                "login_cooldown_until",
                "login_cooldown_level",
                "web_session_version",
                "failed_login_attempts",
            ):
                await connection.execute(
                    text(f"ALTER TABLE users DROP COLUMN {column}")
                )
            # Current KAsset automation tables are also post-boundary. Drop them
            # before the Android order tables they reference, then let the
            # 20260829/20260830 migration chain recreate the exact shapes.
            await connection.execute(
                text("DROP TABLE review.kasset_strategy_promotions")
            )
            for table in (
                "kasset_paper_position_states",
                "kasset_ai_daily_routine_settings",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            await connection.execute(
                text("DROP INDEX IF EXISTS paper.uq_paper_trades_account_correlation")
            )
            for table in (
                "kasset_push_deliveries",
                "kasset_android_paper_orders",
                "kasset_android_paper_accounts",
                "kasset_android_runtime_state",
                "kasset_global_runtime_state",
                "kasset_device_sessions",
                "kasset_broker_credentials",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            # PAPER USD initial capital is a post-boundary additive column.
            # Remove the current-head shape so its migration owns each round trip.
            await connection.execute(
                text("ALTER TABLE paper.paper_accounts DROP COLUMN initial_capital_usd")
            )
            # The P0 currency migration is also post-boundary; current metadata
            # carries its per-currency snapshot columns and the relaxed
            # nullability of the legacy mixed-currency columns.
            await connection.execute(
                text(
                    "ALTER TABLE paper.paper_daily_snapshots "
                    "DROP COLUMN equity_krw, "
                    "DROP COLUMN equity_usd, "
                    "DROP COLUMN daily_return_krw_pct, "
                    "DROP COLUMN daily_return_usd_pct, "
                    "DROP COLUMN valuation_complete_krw, "
                    "DROP COLUMN valuation_complete_usd, "
                    "ALTER COLUMN positions_value SET NOT NULL, "
                    "ALTER COLUMN total_equity SET NOT NULL"
                )
            )
            # The KAsset multi-user migration adds these case-insensitive
            # unique indexes; current metadata already materializes them, so
            # drop both and let the migration add them back.
            for index in ("uq_users_username_ci", "uq_users_email_ci"):
                await connection.execute(text(f"DROP INDEX IF EXISTS {index}"))
            # Google login is added after this boundary. Dropping its column
            # also removes the partial unique index materialized by metadata.
            await connection.execute(text("ALTER TABLE users DROP COLUMN google_sub"))
            # KAsset nickname aliases and the NHPLUG symbol master are also
            # later than this boundary and already present in current metadata.
            await connection.execute(
                text("ALTER TABLE instruments DROP COLUMN aliases")
            )
            await connection.execute(text("DROP TABLE symbol_master"))
            await connection.execute(text("DROP TABLE research.kr_candles_1m_toss"))
            # KR lifecycle evidence and persisted research cohorts are also
            # introduced after this boundary.
            for table in (
                "kasset_research_cohort_members",
                "kasset_research_cohorts",
                "kasset_corporate_action_fetch_coverage",
                "kr_corporate_action_evidence",
                "kr_stock_lifecycle_observations",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            await connection.execute(
                text(
                    "ALTER TABLE kr_symbol_universe "
                    "DROP COLUMN std_pdno, "
                    "ALTER COLUMN listing_status TYPE VARCHAR(20)"
                )
            )
            # News translation fields are post-boundary additive columns too.
            # Remove the current-head shape so its migration can add them.
            await connection.execute(
                text(
                    "ALTER TABLE news_analysis_results "
                    "DROP COLUMN translated_title, "
                    "DROP COLUMN translated_excerpt"
                )
            )

        env = {**os.environ, "DATABASE_URL": target_url_text}

        def alembic(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        commands = (
            ("stamp", "20260714_rob849_paper_cohort"),
            ("downgrade", "20260713_rob848_paper_validation"),
            ("upgrade", "head"),
            ("downgrade", "20260713_rob848_paper_validation"),
            ("upgrade", "head"),
        )
        for command in commands:
            completed = await asyncio.to_thread(alembic, *command)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            if command == ("upgrade", "head"):
                await _assert_rung_reason_schema(engine)
        current = await asyncio.to_thread(alembic, "current")
        assert current.returncode == 0, current.stdout + current.stderr
        config = Config(str(REPO / "alembic.ini"))
        config.set_main_option("script_location", str(REPO / "alembic"))
        expected_head = ScriptDirectory.from_config(config).get_current_head()
        assert expected_head is not None
        assert f"{expected_head} (head)" in current.stdout

        async with engine.connect() as connection:
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS t "
                    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
                    "WHERE p.proname = 'reject_paper_cohort_audit_mutation' "
                    "AND NOT t.tgisinternal"
                )
            )
            assert triggers == 16
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()


@pytest.mark.asyncio
async def test_cohort_update_delete_and_truncate_are_rejected(
    db_session: AsyncSession,
) -> None:
    row = _cohort()
    nonce = uuid4().hex
    experiment = ResearchStrategyExperiment(
        experiment_id=_hash(f"{nonce}:experiment"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:strategy"),
        code_hash=_hash(f"{nonce}:code"),
        params_hash=_hash(f"{nonce}:params"),
        dataset_manifest_hash=_hash(f"{nonce}:dataset"),
        universe_hash=_hash(f"{nonce}:universe"),
        pit_hash=_hash(f"{nonce}:pit"),
        frozen_config_hash=_hash(f"{nonce}:config"),
        policy_hash=_hash(f"{nonce}:policy"),
        benchmark_hash=_hash(f"{nonce}:benchmark"),
        cost_hash=_hash(f"{nonce}:cost"),
        mdd_hash=_hash(f"{nonce}:mdd"),
        manifest={},
    )
    db_session.add(experiment)
    await db_session.flush()
    run = ResearchBacktestRun(
        run_id=f"run-{nonce}",
        strategy_name=experiment.strategy_key,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=1,
        profit_factor="1",
        max_drawdown="0",
        strategy_experiment_id=experiment.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"trial-{nonce}",
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(row)
    db_session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"assignment-{nonce}",
            cohort_id=row.cohort_id,
            ordinal=0,
            role="champion",
            validation_id=f"validation-{nonce}",
            validation_version=1,
            experiment_id=experiment.experiment_id,
            source_backtest_run_id=run.id,
            strategy_version_id=experiment.strategy_version,
            target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
            experiment_hash=experiment.experiment_id,
            strategy_hash=experiment.strategy_hash,
            config_hash=experiment.frozen_config_hash,
            policy_hash=experiment.policy_hash,
            input_hash=_hash(f"{nonce}:input"),
        )
    )
    await db_session.flush()
    cohort_pk = row.id
    cohort_id = row.cohort_id
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(PaperValidationCohort)
            .where(PaperValidationCohort.id == cohort_pk)
            .values(required_lookback=31)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(PaperValidationCohort).where(PaperValidationCohort.id == cohort_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.paper_validation_cohorts CASCADE")
        )
    await db_session.rollback()

    challenger = ResearchStrategyExperiment(
        experiment_id=_hash(f"{nonce}:challenger-experiment"),
        strategy_key=f"challenger-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:challenger-strategy"),
        code_hash=_hash(f"{nonce}:challenger-code"),
        params_hash=_hash(f"{nonce}:challenger-params"),
        dataset_manifest_hash=_hash(f"{nonce}:challenger-dataset"),
        universe_hash=_hash(f"{nonce}:challenger-universe"),
        pit_hash=_hash(f"{nonce}:challenger-pit"),
        frozen_config_hash=_hash(f"{nonce}:challenger-config"),
        policy_hash=_hash(f"{nonce}:challenger-policy"),
        benchmark_hash=_hash(f"{nonce}:challenger-benchmark"),
        cost_hash=_hash(f"{nonce}:challenger-cost"),
        mdd_hash=_hash(f"{nonce}:challenger-mdd"),
        manifest={},
    )
    db_session.add(challenger)
    await db_session.flush()
    challenger_run = ResearchBacktestRun(
        run_id=f"challenger-run-{nonce}",
        strategy_name=challenger.strategy_key,
        strategy_version=challenger.strategy_version,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=1,
        profit_factor="1",
        max_drawdown="0",
        strategy_experiment_id=challenger.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"challenger-trial-{nonce}",
    )
    db_session.add(challenger_run)
    await db_session.flush()
    db_session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"challenger-assignment-{nonce}",
            cohort_id=cohort_id,
            ordinal=1,
            role="challenger",
            validation_id=f"challenger-validation-{nonce}",
            validation_version=1,
            experiment_id=challenger.experiment_id,
            source_backtest_run_id=challenger_run.id,
            strategy_version_id=challenger.strategy_version,
            target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
            experiment_hash=challenger.experiment_id,
            strategy_hash=challenger.strategy_hash,
            config_hash=challenger.frozen_config_hash,
            policy_hash=challenger.policy_hash,
            input_hash=_hash(f"{nonce}:challenger-input"),
        )
    )
    with pytest.raises(DBAPIError, match="requires exactly one champion"):
        await db_session.commit()
    await db_session.rollback()
