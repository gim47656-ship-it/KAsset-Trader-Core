"""관리자 비밀번호 지연 입력과 1회용 이메일 복구 토큰을 추가한다.

로그인 실패 상태는 사용자 행에 작게 유지하고, 복구 원문 코드는 저장하지 않는다.
데이터베이스에는 SHA-256 해시만 남기며 성공한 코드는 즉시 사용 처리한다.

Revision ID: 20260830_admin_recovery
Revises: 20260830_ai_call_events
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_admin_recovery"
down_revision = "20260830_ai_call_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "login_cooldown_level",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("login_cooldown_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "web_session_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_users_failed_login_attempts_nonnegative",
        "users",
        "failed_login_attempts >= 0",
    )
    op.create_check_constraint(
        "ck_users_login_cooldown_level_range",
        "users",
        "login_cooldown_level BETWEEN 0 AND 5",
    )
    op.create_check_constraint(
        "ck_users_web_session_version_nonnegative",
        "users",
        "web_session_version >= 0",
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="ck_password_reset_tokens_token_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_password_reset_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_password_reset_tokens"),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_password_reset_tokens_token_hash",
        ),
    )
    op.create_index(
        "ix_password_reset_tokens_user_created_at",
        "password_reset_tokens",
        ["user_id", "created_at"],
    )
    op.create_index(
        "uq_password_reset_tokens_user_active",
        "password_reset_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_password_reset_tokens_user_active",
        table_name="password_reset_tokens",
        if_exists=True,
    )
    op.drop_index(
        "ix_password_reset_tokens_user_created_at",
        table_name="password_reset_tokens",
    )
    op.drop_table("password_reset_tokens")
    op.drop_constraint(
        "ck_users_web_session_version_nonnegative",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "ck_users_login_cooldown_level_range",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "ck_users_failed_login_attempts_nonnegative",
        "users",
        type_="check",
    )
    op.drop_column("users", "login_cooldown_until")
    op.drop_column("users", "login_cooldown_level")
    op.drop_column("users", "web_session_version")
    op.drop_column("users", "failed_login_attempts")
