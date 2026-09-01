"""투자 보고서 도구 설명의 운영 account_scope 계약을 검증한다."""

from __future__ import annotations

import app.mcp_server.tooling.investment_hermes_handlers as hermes_handlers
import app.mcp_server.tooling.investment_reports_handlers as handlers

_VALID_ACCOUNT_SCOPES = ("toss_live", "alpaca_paper", "upbit_live")


def _capture(register) -> dict[str, str]:
    captured: dict[str, str] = {}

    class _FakeMCP:
        def tool(self, *, name, description):
            captured[name] = description
            return lambda fn: fn

    register(_FakeMCP())
    return captured


def test_create_description_lists_valid_account_scopes():
    desc = _capture(handlers.register_investment_report_tools)[
        "investment_report_create"
    ]
    for scope in _VALID_ACCOUNT_SCOPES:
        assert scope in desc, f"create description must name account_scope {scope!r}"
    assert "kis_live" in desc
    assert "non-operational" in desc


def test_create_from_hermes_description_advertises_active_scopes():
    # PAPER 조합 경로와 신규 Toss 운영 scope를 함께 설명해야 한다.
    desc = _capture(hermes_handlers.register_investment_hermes_tools)[
        "investment_report_create_from_hermes_composition"
    ]
    assert "alpaca_paper" in desc
    assert "toss_live" in desc
    assert "kis_*" in desc


def test_draft_mutation_descriptions_state_draft_only_and_no_broker_mutation():
    captured = _capture(handlers.register_investment_report_tools)

    for name in ("investment_report_add_items", "investment_report_update"):
        desc = captured[name]
        assert "Draft-only" in desc
        assert "No broker / order / watch mutation" in desc


def test_add_items_description_mentions_duplicate_client_item_key():
    desc = _capture(handlers.register_investment_report_tools)[
        "investment_report_add_items"
    ]
    assert "duplicate client_item_key" in desc
