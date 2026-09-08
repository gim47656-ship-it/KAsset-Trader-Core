import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.readonly.json"

# spec §6 deny-list (논리 도구명). 새 mutation 도구가 생기면 여기 + JSON에 추가해야 테스트 통과.
KNOWN_MUTATION_TOOLS = frozenset(
    {
        "place_order",
        "toss_preview_order",
        "paper_place_limit_order",
        "paper_cancel_pending_order",
        "paper_reconcile_orders",
        "buy_ladder_fill_preview",
        "sell_ladder_fill_preview",
        "set_user_setting",
        "update_manual_holdings",
        "investment_report_prepare_intraday_context",
        "cancel_order",
        "modify_order",
        "toss_place_order",
        "toss_modify_order",
        "toss_cancel_order",
        "toss_reconcile_orders",
        "paper_validation_register",
        "paper_validation_advance",
        "paper_validation_append_hypothesis",
        "paper_validation_append_review",
        "paper_validation_authorize_order_submit",
        "paper_validation_confirm_promotion",
        "paper_validation_reject_or_abort",
        "investment_report_create",
        "investment_report_add_items",
        "investment_report_update",
        "investment_report_decide_item",
        "investment_report_activate_watch",
        "investment_report_set_status",
        "investment_report_generate_from_bundle",
        "investment_report_create_from_hermes_composition",
        "investment_report_prepare_bundle",
        "investment_stage_artifacts_ingest_from_hermes",
        "investment_watch_create",
        "investment_watch_void",
        "investment_watch_expire",
        "sweep_expired_watches",
        "support_reserve_net_consume",
    }
)


def _deny() -> list[str]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return data["permissions"]["deny"]


def _denied_mcp_suffixes() -> set[str]:
    return {e.split("__")[-1] for e in _deny() if e.startswith("mcp__")}


def test_settings_file_is_valid_json_with_deny_array():
    assert isinstance(_deny(), list) and len(_deny()) > 0


def test_denies_all_known_mutation_tools():
    missing = KNOWN_MUTATION_TOOLS - _denied_mcp_suffixes()
    assert not missing, f"deny-list 누락 mutation 도구: {sorted(missing)}"


def test_denies_filesystem_and_bash_builtins():
    deny = set(_deny())
    assert {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"} <= deny


def test_session_context_append_is_NOT_denied():
    # 자가치유 핸드오프 적재는 의도적 허용 — deny되면 출력 경로가 막힌다.
    assert not any(e.endswith("__session_context_append") for e in _deny())


def test_analysis_bundle_read_is_allowed_but_capture_is_not() -> None:
    from app.mcp_server.tooling.analysis_readonly_registration import (
        ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES,
        ANALYSIS_READONLY_TOOL_NAMES,
    )

    assert "analysis_bundle_get" in ANALYSIS_READONLY_TOOL_NAMES
    assert "analysis_bundle_create" not in ANALYSIS_READONLY_TOOL_NAMES
    assert "analysis_bundle_create" in ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES


def test_paper_validation_denies_mutations_but_allows_audit_read() -> None:
    from app.mcp_server.tooling.paper_validation_registration import (
        PAPER_VALIDATION_MUTATION_TOOL_NAMES,
        PAPER_VALIDATION_TOOL_NAMES,
    )

    assert PAPER_VALIDATION_TOOL_NAMES - PAPER_VALIDATION_MUTATION_TOOL_NAMES == {
        "paper_validation_get_audit"
    }
    assert PAPER_VALIDATION_MUTATION_TOOL_NAMES <= KNOWN_MUTATION_TOOLS
    assert PAPER_VALIDATION_MUTATION_TOOL_NAMES <= _denied_mcp_suffixes()
