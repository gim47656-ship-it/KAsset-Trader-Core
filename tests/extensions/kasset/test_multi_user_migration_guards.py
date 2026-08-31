"""Real-database proof for the multi-user cutover migration guards.

``20260827_kasset_multi_user_core`` refuses to guess ownership: upgrading with
legacy rows requires exactly one trader, and downgrading refuses to collapse
more than one owner. Both failure branches run here against a scratch
PostgreSQL database through the real alembic CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.models.base import Base
from tests._run_owned_database import validate_run_owned_database_url

REPO = Path(__file__).resolve().parents[3]
PARENT_REVISION = "20260824_s257_rung_reason"
PRE_CUTOVER_REVISION = "20260827_ai_recommendations"

_BOUNDARY_TABLES = (
    "review.kasset_paper_execution_events",
    "review.kasset_automation_cycle_events",
    "review.ai_call_events",
    "review.ai_recommendations",
    "review.kasset_strategy_promotions",
    "kasset_research_cohort_members",
    "kasset_research_cohorts",
    "kasset_corporate_action_fetch_coverage",
    "kr_corporate_action_evidence",
    "kr_stock_lifecycle_observations",
    "kasset_shadow_loss_locks",
    "kasset_shadow_daily_high_watermarks",
    "kasset_ai_runtime_config",
    "password_reset_tokens",
    "kasset_paper_position_states",
    "kasset_ai_daily_routine_settings",
    "kasset_android_paper_orders",
    "kasset_android_paper_accounts",
    "kasset_android_runtime_state",
    "kasset_global_runtime_state",
    "kasset_device_sessions",
    "kasset_broker_credentials",
    "symbol_master",
)


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


async def _insert_user(engine, username: str, role: str) -> int:
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "INSERT INTO users "
                "(email, username, hashed_password, role, tz, base_currency, "
                " is_active) "
                "VALUES (:email, :username, 'x', :role, 'Asia/Seoul', 'KRW', true) "
                "RETURNING id"
            ),
            {
                "email": f"{username}@example.com",
                "username": username,
                "role": role,
            },
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_upgrade_and_downgrade_ownership_guards_fail_closed() -> None:
    base_url = validate_run_owned_database_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("the cutover guard acceptance requires PostgreSQL")

    database = f"kasset_guard_{uuid4().hex}"
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
    env = {**os.environ, "DATABASE_URL": target_url_text}
    try:
        # Reconstruct the pre-KAsset boundary from current metadata.
        async with engine.begin() as connection:
            for schema in ("paper", "research", "review"):
                await connection.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                )
            await connection.run_sync(Base.metadata.create_all)
            for table in _BOUNDARY_TABLES:
                await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await connection.execute(
                text("DROP INDEX IF EXISTS paper.uq_paper_trades_account_correlation")
            )
            for index in ("uq_users_username_ci", "uq_users_email_ci"):
                await connection.execute(text(f"DROP INDEX IF EXISTS {index}"))
            # The Google sign-in migration is later than this boundary and its
            # column/index are already materialized by create_all.
            await connection.execute(
                text("ALTER TABLE users DROP COLUMN IF EXISTS google_sub")
            )
            # The nickname aliases migration is also later than this boundary.
            await connection.execute(
                text("ALTER TABLE instruments DROP COLUMN aliases")
            )
            # Current-head metadata also contains the later authentication,
            # PAPER capital, KR lifecycle, and translated-news additions.
            for column in (
                "login_cooldown_until",
                "login_cooldown_level",
                "web_session_version",
                "failed_login_attempts",
            ):
                await connection.execute(
                    text(f"ALTER TABLE users DROP COLUMN {column}")
                )
            await connection.execute(
                text("ALTER TABLE paper.paper_accounts DROP COLUMN initial_capital_usd")
            )
            # The P0 currency migration is also later than this boundary, so
            # current metadata already carries its per-currency snapshot columns
            # and the relaxed nullability of the legacy mixed-currency columns.
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
            await connection.execute(
                text(
                    "ALTER TABLE kr_symbol_universe "
                    "DROP COLUMN std_pdno, "
                    "ALTER COLUMN listing_status TYPE VARCHAR(20)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE news_analysis_results "
                    "DROP COLUMN translated_title, "
                    "DROP COLUMN translated_excerpt"
                )
            )

        stamped = _alembic(env, "stamp", PARENT_REVISION)
        assert stamped.returncode == 0, stamped.stderr
        pre = _alembic(env, "upgrade", PRE_CUTOVER_REVISION)
        assert pre.returncode == 0, pre.stderr

        # Two traders plus a legacy row: the owner backfill must fail closed.
        first_trader = await _insert_user(engine, f"guard-a-{uuid4().hex}", "trader")
        second_trader = await _insert_user(engine, f"guard-b-{uuid4().hex}", "trader")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kasset_android_runtime_state "
                    "(id, kill_switch_enabled, trading_mode, max_order_ratio, "
                    " max_symbol_ratio) "
                    "VALUES (1, false, 'PAPER', 0.1000, 0.2500)"
                )
            )
        ambiguous = _alembic(env, "upgrade", "head")
        assert ambiguous.returncode != 0
        assert "requires exactly one trader" in ambiguous.stderr

        # With one unambiguous trader the cutover assigns ownership.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE users SET role = 'viewer' WHERE id = :id"),
                {"id": second_trader},
            )
        upgraded = _alembic(env, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        async with engine.connect() as connection:
            owner = await connection.execute(
                text("SELECT owner_user_id FROM kasset_android_runtime_state")
            )
            assert [int(row[0]) for row in owner] == [first_trader]

        # A second owner appearing after the cutover blocks the downgrade.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kasset_android_runtime_state "
                    "(owner_user_id, kill_switch_enabled, trading_mode, "
                    " max_order_ratio, max_symbol_ratio) "
                    "VALUES (:owner, false, 'PAPER', 0.1000, 0.2500)"
                ),
                {"owner": second_trader},
            )
        blocked = _alembic(env, "downgrade", PRE_CUTOVER_REVISION)
        assert blocked.returncode != 0
        assert "requires at most one owner" in blocked.stderr

        # Back to one owner, the downgrade completes.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM kasset_android_runtime_state "
                    "WHERE owner_user_id = :owner"
                ),
                {"owner": second_trader},
            )
        downgraded = _alembic(env, "downgrade", PRE_CUTOVER_REVISION)
        assert downgraded.returncode == 0, downgraded.stderr
    finally:
        await engine.dispose()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await admin.close()
