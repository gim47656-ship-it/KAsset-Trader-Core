"""Cut KAsset Android persistence over to canonical per-user ownership.

Revision ID: 20260827_kasset_multi_user_core
Revises: 20260827_ai_recommendations
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260827_kasset_multi_user_core"
down_revision = "20260827_ai_recommendations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OWNER_CHECK = """
DO $$
DECLARE
    trader_count integer;
    legacy_row_count bigint;
BEGIN
    SELECT count(*) INTO trader_count
    FROM users
    WHERE role::text = 'trader';

    SELECT
        (SELECT count(*) FROM kasset_android_paper_orders)
        + (SELECT count(*) FROM kasset_android_runtime_state)
        + (SELECT count(*) FROM kasset_broker_credentials)
        + (SELECT count(*) FROM review.ai_recommendations)
    INTO legacy_row_count;

    IF legacy_row_count > 0 AND trader_count <> 1 THEN
        RAISE EXCEPTION
            'KAsset owner backfill requires exactly one trader for % legacy rows; found %',
            legacy_row_count,
            trader_count;
    END IF;
END
$$
"""

_OWNER_SQL = "(SELECT id FROM users WHERE role::text = 'trader')"


def upgrade() -> None:
    op.execute(sa.text(_OWNER_CHECK))
    op.create_index(
        "uq_users_username_ci",
        "users",
        [sa.text("lower(username)")],
        unique=True,
        postgresql_where=sa.text("username IS NOT NULL"),
    )
    op.create_index(
        "uq_users_email_ci",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.add_column(
        "kasset_android_paper_orders",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "kasset_android_runtime_state",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "kasset_broker_credentials",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ai_recommendations",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.add_column(
        "ai_recommendations",
        sa.Column("paper_execution_status", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "ai_recommendations",
        sa.Column(
            "paper_execution_claimed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "ai_recommendations",
        sa.Column(
            "paper_execution_completed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "ai_recommendations",
        sa.Column("paper_order_id", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "ai_recommendations",
        sa.Column("paper_execution_error", sa.Text(), nullable=True),
        schema="review",
    )

    op.execute(
        sa.text(
            "UPDATE kasset_android_paper_orders "
            f"SET owner_user_id = {_OWNER_SQL} "
            "WHERE owner_user_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE kasset_android_runtime_state "
            f"SET owner_user_id = {_OWNER_SQL} "
            "WHERE owner_user_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE kasset_broker_credentials "
            f"SET owner_user_id = {_OWNER_SQL} "
            "WHERE owner_user_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE review.ai_recommendations "
            f"SET owner_user_id = {_OWNER_SQL} "
            "WHERE owner_user_id IS NULL"
        )
    )

    op.create_table(
        "kasset_global_runtime_state",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kill_switch_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_kasset_global_runtime_state_singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_global_runtime_state"),
    )
    op.execute(
        sa.text(
            "INSERT INTO kasset_global_runtime_state (id, kill_switch_enabled) "
            "VALUES (1, false)"
        )
    )

    op.alter_column("kasset_android_paper_orders", "owner_user_id", nullable=False)
    op.alter_column("kasset_android_runtime_state", "owner_user_id", nullable=False)
    op.alter_column("kasset_broker_credentials", "owner_user_id", nullable=False)
    op.alter_column(
        "ai_recommendations", "owner_user_id", nullable=False, schema="review"
    )
    op.drop_constraint(
        "pk_kasset_android_runtime_state",
        "kasset_android_runtime_state",
        type_="primary",
    )
    op.drop_column("kasset_android_runtime_state", "id")
    op.create_primary_key(
        "pk_kasset_android_runtime_state",
        "kasset_android_runtime_state",
        ["owner_user_id"],
    )

    op.create_foreign_key(
        "fk_kasset_android_paper_orders_owner_user_id_users",
        "kasset_android_paper_orders",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_kasset_android_runtime_state_owner_user_id_users",
        "kasset_android_runtime_state",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_kasset_broker_credentials_owner_user_id_users",
        "kasset_broker_credentials",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_ai_recommendations_owner_user_id_users",
        "ai_recommendations",
        "users",
        ["owner_user_id"],
        ["id"],
        source_schema="review",
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "uq_kasset_android_client_order_id",
        "kasset_android_paper_orders",
        type_="unique",
    )
    op.drop_constraint(
        "uq_kasset_android_broker_order_id",
        "kasset_android_paper_orders",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_kasset_android_paper_order_owner_id",
        "kasset_android_paper_orders",
        ["owner_user_id", "id"],
    )
    op.create_unique_constraint(
        "uq_kasset_android_paper_order_owner_client",
        "kasset_android_paper_orders",
        ["owner_user_id", "client_order_id"],
    )
    op.create_unique_constraint(
        "uq_kasset_android_paper_order_owner_broker",
        "kasset_android_paper_orders",
        ["owner_user_id", "broker_order_id"],
    )
    op.create_index(
        "ix_kasset_android_paper_order_owner_created",
        "kasset_android_paper_orders",
        ["owner_user_id", "created_at"],
    )

    op.drop_constraint(
        "uq_kasset_broker_credential_provider",
        "kasset_broker_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_kasset_broker_credential_owner_id",
        "kasset_broker_credentials",
        ["owner_user_id", "id"],
    )
    op.create_unique_constraint(
        "uq_kasset_broker_credential_owner_provider",
        "kasset_broker_credentials",
        ["owner_user_id", "provider"],
    )

    op.drop_index(
        "ix_ai_recommendations_decision_created_at",
        table_name="ai_recommendations",
        schema="review",
    )
    op.create_unique_constraint(
        "uq_ai_recommendations_owner_id",
        "ai_recommendations",
        ["owner_user_id", "id"],
        schema="review",
    )
    op.create_index(
        "ix_ai_recommendations_owner_decision_created_at",
        "ai_recommendations",
        ["owner_user_id", "decision", sa.text("created_at DESC"), sa.text("id DESC")],
        schema="review",
    )
    op.create_check_constraint(
        "ck_ai_recommendations_paper_execution_status",
        "ai_recommendations",
        "paper_execution_status IS NULL OR "
        "paper_execution_status IN ('CLAIMED', 'SUCCEEDED', 'FAILED')",
        schema="review",
    )
    op.create_check_constraint(
        "ck_ai_recommendations_paper_execution_coherent",
        "ai_recommendations",
        "(paper_execution_status IS NULL "
        "AND paper_execution_claimed_at IS NULL "
        "AND paper_execution_completed_at IS NULL "
        "AND paper_order_id IS NULL "
        "AND paper_execution_error IS NULL) OR "
        "(paper_execution_status = 'CLAIMED' "
        "AND paper_execution_claimed_at IS NOT NULL "
        "AND paper_execution_completed_at IS NULL "
        "AND paper_order_id IS NULL "
        "AND paper_execution_error IS NULL) OR "
        "(paper_execution_status = 'SUCCEEDED' "
        "AND paper_execution_claimed_at IS NOT NULL "
        "AND paper_execution_completed_at IS NOT NULL "
        "AND paper_order_id IS NOT NULL "
        "AND paper_execution_error IS NULL) OR "
        "(paper_execution_status = 'FAILED' "
        "AND paper_execution_claimed_at IS NOT NULL "
        "AND paper_execution_completed_at IS NOT NULL "
        "AND paper_order_id IS NULL "
        "AND length(btrim(paper_execution_error)) > 0)",
        schema="review",
    )

    op.create_table(
        "kasset_android_paper_accounts",
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_kasset_android_paper_accounts_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            name="uq_kasset_android_paper_account_owner",
        ),
        sa.ForeignKeyConstraint(
            ["paper_account_id"],
            ["paper.paper_accounts.id"],
            name="fk_kasset_android_paper_accounts_paper_account_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "paper_account_id",
            name="pk_kasset_android_paper_accounts",
        ),
        sa.UniqueConstraint(
            "paper_account_id",
            name="uq_kasset_android_paper_account_link",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO kasset_android_paper_accounts "
            "(owner_user_id, paper_account_id) "
            "SELECT DISTINCT owner_user_id, paper_account_id "
            "FROM kasset_android_paper_orders "
            "ON CONFLICT DO NOTHING"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO kasset_android_paper_accounts "
            "(owner_user_id, paper_account_id) "
            f"SELECT {_OWNER_SQL}, id FROM paper.paper_accounts "
            "WHERE name = 'KAsset Android PAPER' "
            "ON CONFLICT DO NOTHING"
        )
    )
    op.create_foreign_key(
        "fk_kasset_android_order_owner_paper_account",
        "kasset_android_paper_orders",
        "kasset_android_paper_accounts",
        ["owner_user_id", "paper_account_id"],
        ["owner_user_id", "paper_account_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "kasset_device_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("device_name", sa.Text(), nullable=False),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_kasset_device_sessions_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kasset_device_sessions"),
        sa.UniqueConstraint(
            "owner_user_id",
            "device_id",
            name="uq_kasset_device_session_owner_device",
        ),
        sa.UniqueConstraint(
            "refresh_token_hash",
            name="uq_kasset_device_session_refresh_hash",
        ),
    )
    op.create_index(
        "ix_kasset_device_session_owner_active",
        "kasset_device_sessions",
        ["owner_user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                owner_count integer;
            BEGIN
                SELECT count(DISTINCT owner_user_id) INTO owner_count
                FROM (
                    SELECT owner_user_id FROM kasset_android_paper_orders
                    UNION ALL
                    SELECT owner_user_id FROM kasset_android_runtime_state
                    UNION ALL
                    SELECT owner_user_id FROM kasset_broker_credentials
                    UNION ALL
                    SELECT owner_user_id FROM kasset_android_paper_accounts
                    UNION ALL
                    SELECT owner_user_id FROM review.ai_recommendations
                    UNION ALL
                    SELECT owner_user_id FROM kasset_device_sessions
                ) owned;
                IF owner_count > 1 THEN
                    RAISE EXCEPTION
                        'KAsset downgrade requires at most one owner; found %',
                        owner_count;
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM kasset_android_paper_accounts link
                    JOIN paper.paper_accounts account
                      ON account.id = link.paper_account_id
                    WHERE account.name <> 'KAsset Android PAPER'
                ) THEN
                    RAISE EXCEPTION
                        'KAsset downgrade cannot preserve post-cutover PAPER account ownership';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM review.ai_recommendations
                    WHERE paper_execution_status IS NOT NULL
                ) THEN
                    RAISE EXCEPTION
                        'KAsset downgrade cannot preserve PAPER recommendation execution history';
                END IF;
            END
            $$
            """
        )
    )

    op.drop_constraint(
        "fk_kasset_android_order_owner_paper_account",
        "kasset_android_paper_orders",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_kasset_device_session_owner_active",
        table_name="kasset_device_sessions",
    )
    op.drop_table("kasset_device_sessions")
    op.drop_table("kasset_android_paper_accounts")

    op.drop_table("kasset_global_runtime_state")

    op.drop_constraint(
        "ck_ai_recommendations_paper_execution_coherent",
        "ai_recommendations",
        type_="check",
        schema="review",
    )
    op.drop_constraint(
        "ck_ai_recommendations_paper_execution_status",
        "ai_recommendations",
        type_="check",
        schema="review",
    )
    op.drop_index(
        "ix_ai_recommendations_owner_decision_created_at",
        table_name="ai_recommendations",
        schema="review",
    )
    op.drop_constraint(
        "uq_ai_recommendations_owner_id",
        "ai_recommendations",
        type_="unique",
        schema="review",
    )
    op.create_index(
        "ix_ai_recommendations_decision_created_at",
        "ai_recommendations",
        ["decision", sa.text("created_at DESC"), sa.text("id DESC")],
        schema="review",
    )
    op.drop_constraint(
        "fk_ai_recommendations_owner_user_id_users",
        "ai_recommendations",
        type_="foreignkey",
        schema="review",
    )
    op.drop_column("ai_recommendations", "paper_execution_error", schema="review")
    op.drop_column("ai_recommendations", "paper_order_id", schema="review")
    op.drop_column(
        "ai_recommendations", "paper_execution_completed_at", schema="review"
    )
    op.drop_column("ai_recommendations", "paper_execution_claimed_at", schema="review")
    op.drop_column("ai_recommendations", "paper_execution_status", schema="review")
    op.drop_column("ai_recommendations", "owner_user_id", schema="review")

    op.drop_constraint(
        "uq_kasset_broker_credential_owner_provider",
        "kasset_broker_credentials",
        type_="unique",
    )
    op.drop_constraint(
        "uq_kasset_broker_credential_owner_id",
        "kasset_broker_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_kasset_broker_credential_provider",
        "kasset_broker_credentials",
        ["provider"],
    )
    op.drop_constraint(
        "fk_kasset_broker_credentials_owner_user_id_users",
        "kasset_broker_credentials",
        type_="foreignkey",
    )
    op.drop_column("kasset_broker_credentials", "owner_user_id")

    op.drop_constraint(
        "pk_kasset_android_runtime_state",
        "kasset_android_runtime_state",
        type_="primary",
    )
    op.add_column(
        "kasset_android_runtime_state",
        sa.Column("id", sa.BigInteger(), nullable=True),
    )
    op.execute(sa.text("UPDATE kasset_android_runtime_state SET id = 1"))
    op.alter_column("kasset_android_runtime_state", "id", nullable=False)
    op.create_primary_key(
        "pk_kasset_android_runtime_state",
        "kasset_android_runtime_state",
        ["id"],
    )
    op.drop_constraint(
        "fk_kasset_android_runtime_state_owner_user_id_users",
        "kasset_android_runtime_state",
        type_="foreignkey",
    )
    op.drop_column("kasset_android_runtime_state", "owner_user_id")

    op.drop_index(
        "ix_kasset_android_paper_order_owner_created",
        table_name="kasset_android_paper_orders",
    )
    op.drop_constraint(
        "uq_kasset_android_paper_order_owner_broker",
        "kasset_android_paper_orders",
        type_="unique",
    )
    op.drop_constraint(
        "uq_kasset_android_paper_order_owner_client",
        "kasset_android_paper_orders",
        type_="unique",
    )
    op.drop_constraint(
        "uq_kasset_android_paper_order_owner_id",
        "kasset_android_paper_orders",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_kasset_android_broker_order_id",
        "kasset_android_paper_orders",
        ["broker_order_id"],
    )
    op.create_unique_constraint(
        "uq_kasset_android_client_order_id",
        "kasset_android_paper_orders",
        ["client_order_id"],
    )
    op.drop_constraint(
        "fk_kasset_android_paper_orders_owner_user_id_users",
        "kasset_android_paper_orders",
        type_="foreignkey",
    )
    op.drop_column("kasset_android_paper_orders", "owner_user_id")
    op.drop_index("uq_users_email_ci", table_name="users")
    op.drop_index("uq_users_username_ci", table_name="users")
