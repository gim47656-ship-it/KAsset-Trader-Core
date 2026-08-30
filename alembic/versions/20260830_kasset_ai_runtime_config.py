"""AI route 정책 singleton 테이블을 추가한다.

lane별 route ID 순서 배열만 저장한다. provider/model/base URL/API key/subscription
명령은 저장하지 않는다. 삽입되는 기본값은 이 마이그레이션 이전의 환경변수 기반
동작과 동일한 fallback 순서다. 따라서 업그레이드 직후 활성 route 순서는 바뀌지
않는다.

기본값은 의도적으로 리터럴로 적는다. 애플리케이션 상수를 import하면 나중에 상수를
고칠 때 이미 적용된 마이그레이션의 의미가 조용히 바뀐다.

Revision ID: 20260830_ai_runtime_config
Revises: 20260830_admin_recovery
Create Date: 2026-08-30
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260830_ai_runtime_config"
down_revision = "20260830_admin_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "kasset_ai_runtime_config"

#: 환경변수만 있던 시절의 fallback 순서를 그대로 재현한다.
DEFAULT_ROUTE_POLICY: dict[str, list[str]] = {
    "summary_luna": ["direct_luna", "openrouter_flash"],
    "review_luna": ["mcp_tool", "direct_luna", "openrouter_flash"],
    "review_terra": ["mcp_tool", "direct_terra", "openrouter_pro"],
    "review_sol": ["mcp_tool", "direct_sol", "openrouter_pro"],
    "compat_skill": ["subscription_cli", "direct_terra", "openrouter_pro"],
}


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "revision",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "route_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=f"ck_{TABLE_NAME}_singleton"),
        sa.CheckConstraint(
            "revision >= 0",
            name=f"ck_{TABLE_NAME}_revision_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=f"fk_{TABLE_NAME}_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TABLE_NAME}"),
    )

    op.execute(
        sa.text(
            f"INSERT INTO {TABLE_NAME} (id, revision, route_policy) "
            "VALUES (1, 1, CAST(:route_policy AS jsonb)) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(
            route_policy=json.dumps(
                DEFAULT_ROUTE_POLICY,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    )


def downgrade() -> None:
    # 이 테이블만 제거한다. provider 환경변수와 review.ai_call_events 원장은
    # 건드리지 않는다.
    op.drop_table(TABLE_NAME)
