"""Toss 운영 scope와 reconcile broker를 additive하게 허용한다.

기존 KIS/Upbit/Alpaca 값과 행은 그대로 보존하고 ``toss_live``/``toss``만
허용 집합에 추가한다. downgrade는 새 값이 하나라도 있으면 데이터 삭제나
재분류 대신 명시적으로 중단한다.

Revision ID: 20260902_toss_report_scopes
Revises: 20260901_kasset_fcm_push
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260902_toss_report_scopes"
down_revision = "20260901_kasset_fcm_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_OLD_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','alpaca_paper','upbit_live')"
)
_NEW_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','toss_live','alpaca_paper','upbit_live')"
)
_OLD_LIVE_ADVISORY_CHECK = (
    "account_scope IS DISTINCT FROM 'kis_live' OR execution_mode = 'advisory_only'"
)
_NEW_LIVE_ADVISORY_CHECK = (
    "account_scope NOT IN ('kis_live','toss_live') OR execution_mode = 'advisory_only'"
)
_OLD_RECONCILE_BROKER_CHECK = "broker IN ('kis','upbit')"
_NEW_RECONCILE_BROKER_CHECK = "broker IN ('kis','upbit','toss')"
_OLD_EXECUTION_BROKER_CHECK = "broker IN ('kis','upbit')"
_NEW_EXECUTION_BROKER_CHECK = "broker IN ('kis','upbit','toss')"
_SCOPE_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("investment_reports", "ck_investment_reports_account_scope"),
    ("investment_snapshot_runs", "ck_investment_snapshot_runs_account_scope"),
    ("investment_snapshots", "ck_investment_snapshots_account_scope"),
    ("investment_snapshot_bundles", "ck_investment_snapshot_bundles_account_scope"),
    (
        "investment_symbol_intermediate_reports",
        "ck_investment_symbol_intermediate_reports_account_scope",
    ),
    ("operator_session_context", "ck_operator_session_context_account_scope"),
)


def _replace_check(table: str, name: str, expression: str) -> None:
    op.drop_constraint(name, table, schema=_SCHEMA, type_="check")
    op.create_check_constraint(name, table, expression, schema=_SCHEMA)


def upgrade() -> None:
    for table, name in _SCOPE_CONSTRAINTS:
        _replace_check(table, name, _NEW_SCOPE_CHECK)
    _replace_check(
        "investment_reports",
        "ck_investment_reports_live_advisory_only",
        _NEW_LIVE_ADVISORY_CHECK,
    )
    _replace_check(
        "execution_ledger_reconcile_runs",
        "execution_ledger_runs_broker",
        _NEW_RECONCILE_BROKER_CHECK,
    )
    _replace_check(
        "execution_ledger",
        "execution_ledger_broker",
        _NEW_EXECUTION_BROKER_CHECK,
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table, _ in _SCOPE_CONSTRAINTS:
        exists = bind.execute(
            sa.text(
                f'SELECT 1 FROM {_SCHEMA}."{table}" '
                "WHERE account_scope = :scope LIMIT 1"
            ),
            {"scope": "toss_live"},
        ).scalar()
        if exists is not None:
            raise RuntimeError(
                "downgrade blocked: toss_live rows exist in "
                f"{_SCHEMA}.{table}; no data rewrite or deletion is permitted"
            )
    toss_execution = bind.execute(
        sa.text(
            f"SELECT 1 FROM {_SCHEMA}.execution_ledger WHERE broker = :broker LIMIT 1"
        ),
        {"broker": "toss"},
    ).scalar()
    if toss_execution is not None:
        raise RuntimeError(
            "downgrade blocked: toss rows exist in review.execution_ledger; "
            "no data rewrite or deletion is permitted"
        )

    toss_reconcile_run = bind.execute(
        sa.text(
            f"SELECT 1 FROM {_SCHEMA}.execution_ledger_reconcile_runs "
            "WHERE broker = :broker LIMIT 1"
        ),
        {"broker": "toss"},
    ).scalar()
    if toss_reconcile_run is not None:
        raise RuntimeError(
            "downgrade blocked: toss rows exist in "
            "review.execution_ledger_reconcile_runs; no data rewrite or deletion "
            "is permitted"
        )

    _replace_check(
        "execution_ledger_reconcile_runs",
        "execution_ledger_runs_broker",
        _OLD_RECONCILE_BROKER_CHECK,
    )
    _replace_check(
        "execution_ledger",
        "execution_ledger_broker",
        _OLD_EXECUTION_BROKER_CHECK,
    )

    _replace_check(
        "investment_reports",
        "ck_investment_reports_live_advisory_only",
        _OLD_LIVE_ADVISORY_CHECK,
    )
    for table, name in _SCOPE_CONSTRAINTS:
        _replace_check(table, name, _OLD_SCOPE_CHECK)
