from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.rung_reason_vocabulary import RUNG_VOID_REASON_GROUPS
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260714_rob850_paper_evaluation.py"


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


def test_migration_descends_from_latest_main_head_and_is_the_single_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "20260714_rob850_paper_evaluation"' in source
    assert 'down_revision = "20260714_rob878_shadow"' in source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1


def test_migration_defines_immutable_triggers() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "reject_evaluation_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source


def test_migration_defines_all_tables_and_key_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "evaluation_configs",
        "evaluation_epochs",
        "evaluation_scorecards",
        "evaluation_verdicts",
        "uq_evaluation_config_hash",
        "uq_evaluation_epoch_id",
        "uq_evaluation_epoch_identity",
        "uq_evaluation_epoch_lineage",
        "uq_evaluation_epoch_start",
        "uq_evaluation_scorecard_evaluation_view",
        "uq_evaluation_verdict_evaluation",
        "uq_evaluation_verdict_full_identity",
        "uq_evaluation_verdict_idempotency",
        "uq_paper_cohort_assignment_evaluation_identity",
        "fk_evaluation_epoch_cohort",
        "fk_evaluation_epoch_assignment",
        "fk_evaluation_epoch_assignment_identity",
        "fk_evaluation_epoch_cohort_lineage",
        "fk_evaluation_epoch_prior_lineage",
        "fk_evaluation_epoch_config",
        "fk_evaluation_scorecard_epoch_identity",
        "fk_evaluation_scorecard_verdict_identity",
        "fk_evaluation_verdict_epoch_identity",
        "ck_evaluation_scorecard_view_currency_consistency",
        "ck_evaluation_verdict_status",
        "ck_evaluation_epoch_reset_reason",
        "ck_evaluation_epoch_prior_not_self",
        "validate_evaluation_completeness",
    ):
        assert required in source


@pytest.mark.asyncio
async def test_real_postgresql_upgrade_downgrade_upgrade_single_head() -> None:
    base_url = validate_run_owned_database_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-850 migration acceptance requires PostgreSQL")
    database = f"rob850_migration_{uuid4().hex}"
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
            # Base metadata includes the current head, while this test stamps
            # ROB-850 and replays every later migration.
            await connection.execute(
                text("DROP TABLE research.strategy_learning_events")
            )
            # ROB-1109 is later than this reconstructed boundary. Current
            # metadata already contains its restored table, so remove it before
            # upgrading the post-ROB-850 chain.
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
            # W5's durable callback inbox and recovery cursor are later than
            # this reconstructed boundary and already in Base.metadata; drop
            # both so the upgrade chain creates them instead of colliding.
            for table in (
                "telegram_callback_recovery_cursor",
                "telegram_callback_inbox",
                "screener_pick_log",
            ):
                await connection.execute(text(f"DROP TABLE review.{table}"))
            # ROB-1286's repricing claim table is later than this boundary and
            # already in Base.metadata; drop it so the migration creates it.
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
            # Current metadata also includes the post-claim-lease KAsset
            # observability, lifecycle, AI, and shadow-risk tables. Remove
            # dependents first so the later migration chain creates them.
            for table in (
                "kasset_research_cohort_members",
                "kasset_research_cohorts",
                "kasset_corporate_action_fetch_coverage",
                "kr_corporate_action_evidence",
                "kr_stock_lifecycle_observations",
                "review.ai_call_events",
                "password_reset_tokens",
                "kasset_ai_runtime_config",
                "kasset_shadow_daily_high_watermarks",
                "kasset_shadow_loss_locks",
                "review.kasset_paper_execution_events",
                "review.kasset_automation_cycle_events",
                "review.kasset_intraday_rvol_shadow",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            for column in (
                "translated_title",
                "translated_excerpt",
            ):
                await connection.execute(
                    text(f"ALTER TABLE news_analysis_results DROP COLUMN {column}")
                )
            for column in (
                "failed_login_attempts",
                "login_cooldown_level",
                "login_cooldown_until",
                "web_session_version",
            ):
                await connection.execute(
                    text(f"ALTER TABLE users DROP COLUMN {column}")
                )
            await connection.execute(
                text("ALTER TABLE kr_symbol_universe DROP COLUMN std_pdno")
            )
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
            # KAsset Android and AI review tables are later than this
            # reconstructed boundary and already present in current metadata.
            await connection.execute(text("DROP TABLE review.ai_recommendations"))
            # Current KAsset automation tables are also post-boundary. Drop them
            # before the Android order tables they reference, then let the
            # 20260829/20260830 migration chain recreate the exact shapes.
            await connection.execute(
                text("DROP TABLE review.kasset_strategy_promotions")
            )
            for table in (
                "kasset_paper_position_states",
                "kasset_routine_price_alert_events",
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
            ("stamp", "20260714_rob850_paper_evaluation"),
            ("downgrade", "20260714_rob878_shadow"),
            ("upgrade", "head"),
            ("downgrade", "20260714_rob878_shadow"),
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
        assert current.stdout.strip().startswith(f"{expected_head} (head)")

        async with engine.connect() as connection:
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS t "
                    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
                    "WHERE p.proname = 'reject_evaluation_mutation' "
                    "AND NOT t.tgisinternal"
                )
            )
            assert triggers == 8
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()
