"""KAsset PAPER promotion을 persisted research evidence에 결합한다.

Revision ID: 20260830_kasset_promotion_trust
Revises: 20260830_kasset_position_cycles
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260830_kasset_promotion_trust"
down_revision = "20260830_kasset_position_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_TABLE = "kasset_strategy_promotions"
_CANDIDATE_INDEX = "ix_kasset_strategy_promotion_candidate"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("promotion_candidate_id", sa.BigInteger(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("strategy_artifact_fingerprint", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("source_commit", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("evidence_schema_version", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.create_foreign_key(
        "fk_kasset_strategy_promotion_candidate",
        _TABLE,
        "promotion_candidates",
        ["promotion_candidate_id"],
        ["id"],
        source_schema=_SCHEMA,
        referent_schema="research",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_kasset_strategy_promotion_artifact_fingerprint",
        _TABLE,
        "strategy_artifact_fingerprint IS NULL "
        "OR strategy_artifact_fingerprint ~ '^[0-9a-f]{64}$'",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "ck_kasset_strategy_promotion_source_commit",
        _TABLE,
        "source_commit IS NULL OR source_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "ck_kasset_strategy_promotion_evidence_schema",
        _TABLE,
        "evidence_schema_version IS NULL OR btrim(evidence_schema_version) <> ''",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "ck_kasset_strategy_promotion_trust_bundle",
        _TABLE,
        "num_nonnulls(promotion_candidate_id, "
        "strategy_artifact_fingerprint, source_commit, "
        "evidence_schema_version) IN (0, 4)",
        schema=_SCHEMA,
    )
    op.create_index(
        _CANDIDATE_INDEX,
        _TABLE,
        ["promotion_candidate_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("promotion_candidate_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_CANDIDATE_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_constraint(
        "ck_kasset_strategy_promotion_trust_bundle",
        _TABLE,
        type_="check",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "ck_kasset_strategy_promotion_evidence_schema",
        _TABLE,
        type_="check",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "ck_kasset_strategy_promotion_source_commit",
        _TABLE,
        type_="check",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "ck_kasset_strategy_promotion_artifact_fingerprint",
        _TABLE,
        type_="check",
        schema=_SCHEMA,
    )
    op.drop_constraint(
        "fk_kasset_strategy_promotion_candidate",
        _TABLE,
        type_="foreignkey",
        schema=_SCHEMA,
    )
    op.drop_column(_TABLE, "evidence_schema_version", schema=_SCHEMA)
    op.drop_column(_TABLE, "source_commit", schema=_SCHEMA)
    op.drop_column(_TABLE, "strategy_artifact_fingerprint", schema=_SCHEMA)
    op.drop_column(_TABLE, "promotion_candidate_id", schema=_SCHEMA)
