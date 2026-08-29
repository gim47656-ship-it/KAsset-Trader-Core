from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_claim_lease_and_trade_correlation_schema_contract(
    db_session: AsyncSession,
) -> None:
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("claim lease migration contract requires PostgreSQL")
    columns = {
        row.column_name: row
        for row in (
            await db_session.execute(
                text(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'review' "
                    "AND table_name = 'ai_recommendations' "
                    "AND column_name IN ("
                    "'paper_execution_token', "
                    "'paper_execution_claimed_at', "
                    "'paper_execution_lease_expires_at', "
                    "'paper_execution_attempt_count')"
                )
            )
        ).all()
    }
    assert set(columns) == {
        "paper_execution_token",
        "paper_execution_claimed_at",
        "paper_execution_lease_expires_at",
        "paper_execution_attempt_count",
    }
    assert columns["paper_execution_token"].is_nullable == "YES"
    assert columns["paper_execution_lease_expires_at"].is_nullable == "YES"
    assert columns["paper_execution_attempt_count"].is_nullable == "NO"
    assert "0" in str(columns["paper_execution_attempt_count"].column_default)

    constraints = {
        row.conname: row.definition
        for row in (
            await db_session.execute(
                text(
                    "SELECT constraint_row.conname, "
                    "pg_get_constraintdef(constraint_row.oid) AS definition "
                    "FROM pg_constraint AS constraint_row "
                    "JOIN pg_class AS table_row "
                    "ON table_row.oid = constraint_row.conrelid "
                    "JOIN pg_namespace AS schema_row "
                    "ON schema_row.oid = table_row.relnamespace "
                    "WHERE schema_row.nspname = 'review' "
                    "AND table_row.relname = 'ai_recommendations' "
                    "AND constraint_row.conname IN ("
                    "'ck_ai_recommendations_paper_execution_coherent', "
                    "'fk_ai_recommendation_owner_paper_order')"
                )
            )
        ).all()
    }
    coherent = constraints["ck_ai_recommendations_paper_execution_coherent"]
    assert "paper_execution_token" in coherent
    assert "paper_execution_lease_expires_at" in coherent
    assert "paper_execution_attempt_count" in coherent
    owner_order_fk = constraints["fk_ai_recommendation_owner_paper_order"]
    assert "FOREIGN KEY (owner_user_id, paper_order_id)" in owner_order_fk
    assert "kasset_android_paper_orders" in owner_order_fk
    assert "(owner_user_id, id)" in owner_order_fk

    index_definition = await db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'paper' "
            "AND tablename = 'paper_trades' "
            "AND indexname = 'uq_paper_trades_account_correlation'"
        )
    )
    assert index_definition is not None
    assert "UNIQUE INDEX" in index_definition
    assert "(account_id, correlation_id)" in index_definition
    assert "WHERE (correlation_id IS NOT NULL)" in index_definition
