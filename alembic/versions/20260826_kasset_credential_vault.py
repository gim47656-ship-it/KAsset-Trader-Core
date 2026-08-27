"""Add encrypted KAsset broker credential vault.

Revision ID: 20260826_kasset_credential_vault
Revises: 20260826_kasset_android_core
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260826_kasset_credential_vault"
down_revision = "20260826_kasset_android_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kasset_broker_credentials",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_app_key", sa.Text(), nullable=False),
        sa.Column("encrypted_app_secret", sa.Text(), nullable=False),
        sa.Column("encrypted_account_no", sa.Text(), nullable=False),
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
        sa.Column("last_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("provider = 'NH'", name="ck_kasset_credential_provider"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", name="uq_kasset_broker_credential_provider"),
    )


def downgrade() -> None:
    op.drop_table("kasset_broker_credentials")
