"""Tests for MCP profile-driven tool registration.

Verifies that:
- DEFAULT profile은 generic Toss/Upbit 및 typed Toss 도구만 등록합니다.
- 모든 활성 profile에서 KIS 주문 도구가 물리적으로 제외됩니다.
- resolve_mcp_profile은 제거된 legacy profile을 거부합니다.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.account_read_registration import (
    ACCOUNT_READ_FORBIDDEN_TOOL_NAMES,
    ACCOUNT_READ_TOOL_NAMES,
)
from app.mcp_server.tooling.analysis_readonly_registration import (
    ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES,
    ANALYSIS_READONLY_TOOL_NAMES,
)
from app.mcp_server.tooling.order_proposal_tools import ORDER_PROPOSAL_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.paper_account_registration import PAPER_ACCOUNT_TOOL_NAMES
from app.mcp_server.tooling.paper_analytics_registration import (
    PAPER_ANALYTICS_TOOL_NAMES,
)
from app.mcp_server.tooling.paper_journal_registration import PAPER_JOURNAL_TOOL_NAMES
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.tradingcodex_execution_registration import (
    TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES,
    TRADINGCODEX_EXECUTION_TOOL_NAMES,
)
from tests._mcp_tooling_support import DummyMCP

_ACTIVE_GENERIC_ORDER_TOOL_NAMES = ORDER_TOOL_NAMES
_RETIRED_KIS_ORDER_TOOL_NAMES = {
    "kis_live_place_order",
    "kis_live_cancel_order",
    "kis_live_modify_order",
    "kis_live_get_order_history",
    "kis_live_reconcile_orders",
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kis_mock_get_order_history",
}
_DB_PAPER_TOOL_NAMES = (
    PAPER_ACCOUNT_TOOL_NAMES | PAPER_ANALYTICS_TOOL_NAMES | PAPER_JOURNAL_TOOL_NAMES
)
_CRYPTO_RESEARCH_TOOL_NAMES = {
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_crypto_fear_greed",
    "get_crypto_top_movers",
}

# ROB-503: generic 이름은 제거됨 (crypto-only 구현인데 이름이 시장 비특정).
# get_fear_greed_index는 ROB-488에서 get_crypto_fear_greed로 리네임.
_REMOVED_GENERIC_TOOL_NAMES = {
    "get_fear_greed_index",
    "get_funding_rate",
    "get_open_interest",
    "get_long_short_ratio",
}


def _build_mcp(profile: McpProfile) -> DummyMCP:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=profile)
    return mcp


class TestDefaultProfile:
    def test_registers_generic_orders(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert _ACTIVE_GENERIC_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_does_not_register_retired_kis_order_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert _RETIRED_KIS_ORDER_TOOL_NAMES.isdisjoint(mcp.tools.keys())

    def test_registers_typed_toss_live_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert TOSS_LIVE_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_does_not_register_split_profile_tools(self) -> None:
        # The DB paper surface is profile-isolated and never appears in DEFAULT.
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert _DB_PAPER_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestDbPaperProfile:
    def test_registers_db_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DB_PAPER)
        assert _DB_PAPER_TOOL_NAMES <= mcp.tools.keys()


class TestCryptoProfile:
    def test_keeps_generic_research_surface(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert {"get_quote", "screen_stocks", "get_holdings"} <= mcp.tools.keys()

    def test_keeps_crypto_discovery_tool(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert "get_crypto_top_movers" in mcp.tools

    def test_registers_the_generic_order_surface(self) -> None:
        # Generic account_mode order tools stay registered; the crypto-pinned
        # reconcile wrapper was removed with its provider.
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert _ACTIVE_GENERIC_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_does_not_register_retired_kis_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert _RETIRED_KIS_ORDER_TOOL_NAMES.isdisjoint(mcp.tools.keys())


_ORDER_SURFACE_MATRIX: dict[McpProfile, set[str]] = {
    McpProfile.DEFAULT: (
        _ACTIVE_GENERIC_ORDER_TOOL_NAMES
        | TOSS_LIVE_ORDER_TOOL_NAMES
        | PAPER_LIMIT_ORDER_TOOL_NAMES
    ),
    McpProfile.CRYPTO: set(_ACTIVE_GENERIC_ORDER_TOOL_NAMES),
    McpProfile.DB_PAPER: set(),
    # ROB-697 M1 — shadow-replay registers zero order/mutation tools by design
    # (frozen-context read + policy + route_request only, early-return).
    McpProfile.SHADOW_REPLAY: set(),
    McpProfile.ANALYSIS_READONLY: {"toss_get_positions"},
    # ROB-760 — account_read exposes the active generic and Toss read-only
    # order-history tools, so they must be listed here; separate
    # forbidden-surface tests below prove the mutation/write tools stay absent.
    McpProfile.ACCOUNT_READ: {
        "get_order_history",
        "toss_get_order_history",
        "toss_get_positions",
        "toss_get_orderable_cash",
    },
    McpProfile.TRADINGCODEX_EXECUTION: {
        "place_order",
        "cancel_order",
        "get_order_history",
        "sell_ladder_fill_preview",
        "buy_ladder_fill_preview",
        "toss_preview_order",
        "toss_place_order",
        "toss_cancel_order",
        "toss_get_order_history",
        "toss_get_positions",
        "toss_get_orderable_cash",
    },
    # ROB-1286 — the watch-fire repricing session. It may create an order
    # *proposal*; it holds no order-mutation tool at all, so this row is the
    # empty set and any future addition here is a visible diff.
    McpProfile.WATCH_REPRICING: set(),
}
_ALL_ORDER_TOOL_NAMES = (
    _ACTIVE_GENERIC_ORDER_TOOL_NAMES
    | _RETIRED_KIS_ORDER_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
)


class TestOrderSurfaceMatrix:
    """Pin the exact order/mutation surface per profile (ROB-488).

    Catches both accidental additions (e.g. live tools leaking into a paper
    profile) and accidental removals (e.g. crypto losing its trading entry
    point) — set equality, not just subset.
    """

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_order_surface_matches_matrix(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        registered_order_tools = _ALL_ORDER_TOOL_NAMES & mcp.tools.keys()
        assert registered_order_tools == _ORDER_SURFACE_MATRIX[profile], (
            f"profile={profile.value} order surface drifted: "
            f"extra={sorted(registered_order_tools - _ORDER_SURFACE_MATRIX[profile])}, "
            f"missing={sorted(_ORDER_SURFACE_MATRIX[profile] - registered_order_tools)}"
        )


# ROB-697 M1 — shadow-replay is a deliberate exception to the "every profile
# has the full read-only research surface" invariant below: it early-returns
# before the "Always" research-registration block, so it carries NO live-fetch
# tool (crypto research included). ``TestShadowReplayIsResearchSurfaceException``
# pins that omission explicitly.
_PROFILES_WITH_RESEARCH_SURFACE = [
    p
    for p in McpProfile
    if p
    not in (
        McpProfile.SHADOW_REPLAY,
        McpProfile.ANALYSIS_READONLY,
        McpProfile.ACCOUNT_READ,
        McpProfile.TRADINGCODEX_EXECUTION,
        # ROB-1286 — allowlist-only and early-returns before the "Always"
        # research block, like the other closed-world profiles above.
        McpProfile.WATCH_REPRICING,
    )
]


class TestCryptoResearchToolsAllProfiles:
    """ROB-503: crypto read-only research tools register on EVERY profile
    that reaches the "Always" research block.

    ROB-488 had gated them to MCP_PROFILE=crypto, which broke single-server
    operation (crypto live trading runs on the DEFAULT server). Read-only
    tools carry no order-surface risk, so profile isolation buys nothing —
    except for shadow-replay (ROB-697), whose validity guard is that it
    carries NO live-fetch tool at all; see _PROFILES_WITH_RESEARCH_SURFACE.
    """

    @pytest.mark.parametrize("profile", _PROFILES_WITH_RESEARCH_SURFACE)
    def test_crypto_research_tools_registered(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        missing = _CRYPTO_RESEARCH_TOOL_NAMES - mcp.tools.keys()
        assert not missing, f"profile={profile.value} missing: {sorted(missing)}"

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_removed_generic_names_absent(self, profile: McpProfile) -> None:
        mcp = _build_mcp(profile)
        leaked = _REMOVED_GENERIC_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"profile={profile.value} leaked old names: {sorted(leaked)}"


class TestShadowReplayIsResearchSurfaceException:
    """ROB-697 M1 — pin that shadow-replay is the ONE profile without the
    default research surface (the load-bearing validity guard: a headless
    replay must not be able to reach live market data)."""

    def test_shadow_replay_has_no_crypto_research_tools(self) -> None:
        mcp = _build_mcp(McpProfile.SHADOW_REPLAY)
        assert _CRYPTO_RESEARCH_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestAnalysisReadonlyProfile:
    def test_registers_exact_analysis_readonly_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings, "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED", True, raising=False
        )
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        assert set(mcp.tools) == ANALYSIS_READONLY_TOOL_NAMES

    def test_bundle_gate_registers_get_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings, "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED", True, raising=False
        )
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        assert "analysis_bundle_get" in mcp.tools
        assert "analysis_bundle_create" not in mcp.tools

    def test_does_not_register_forbidden_surfaces(self) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        leaked = ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"analysis_readonly leaked forbidden tools: {sorted(leaked)}"

    def test_keeps_route_request_inside_registered_surface(self) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        assert "route_request" in mcp.tools
        assert "get_trading_policy" in mcp.tools
        assert "get_quote" in mcp.tools
        assert "place_order" not in mcp.tools
        assert "toss_place_order" not in mcp.tools
        assert "toss_get_positions" in mcp.tools

    @pytest.mark.asyncio
    async def test_analysis_artifact_save_requires_explicit_created_by(self) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        tool = mcp.tools["analysis_artifact_save"]

        result = await tool(
            market="kr",
            kind="session_summary",
            title="missing label",
            payload={},
        )

        assert result == {
            "success": False,
            "error": "created_by_required",
            "tool": "analysis_artifact_save",
            "detail": "analysis_readonly persistence calls must pass an explicit created_by label such as 'codex'.",
        }

    @pytest.mark.asyncio
    async def test_session_context_append_requires_created_by_per_entry(self) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        tool = mcp.tools["session_context_append"]

        result = await tool(
            entries=[
                {
                    "market": "kr",
                    "entry_type": "handoff_note",
                    "title": "missing label",
                    "body": "body",
                }
            ]
        )

        assert result["success"] is False
        assert result["error"] == "created_by_required"
        assert result["tool"] == "session_context_append"
        assert result["entry_indexes"] == [0]

    @pytest.mark.asyncio
    async def test_session_context_append_preserves_validation_for_non_dict_entry(
        self,
    ) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        tool = mcp.tools["session_context_append"]

        result = await tool(entries=["not-a-dict"])

        assert result["success"] is False
        assert result["error"] == "invalid_request"

    def test_persistence_tools_are_registered_but_list_resolve_are_not(self) -> None:
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)
        assert {
            "analysis_artifact_save",
            "analysis_artifact_get",
            "forecast_save",
        } <= mcp.tools.keys()
        assert "analysis_artifact_list" not in mcp.tools
        assert "forecast_resolve" not in mcp.tools
        assert "get_forecasts" not in mcp.tools
        assert "get_forecast_calibration" not in mcp.tools


class TestAccountReadProfile:
    def test_registers_exact_account_read_allowlist(self) -> None:
        mcp = _build_mcp(McpProfile.ACCOUNT_READ)
        assert set(mcp.tools) == ACCOUNT_READ_TOOL_NAMES

    def test_does_not_register_forbidden_surfaces(self) -> None:
        mcp = _build_mcp(McpProfile.ACCOUNT_READ)
        leaked = ACCOUNT_READ_FORBIDDEN_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, f"account_read leaked forbidden tools: {sorted(leaked)}"

    def test_has_no_write_or_persistence_tools(self) -> None:
        mcp = _build_mcp(McpProfile.ACCOUNT_READ)
        write_or_persistence = {
            "place_order",
            "cancel_order",
            "modify_order",
            "kis_live_place_order",
            "kis_live_cancel_order",
            "kis_live_modify_order",
            "kis_live_reconcile_orders",
            "kis_mock_place_order",
            "kis_mock_cancel_order",
            "kis_mock_modify_order",
            "live_reconcile_orders",
            "toss_preview_order",
            "toss_place_order",
            "toss_modify_order",
            "toss_cancel_order",
            "toss_reconcile_orders",
            "update_manual_holdings",
            "get_available_capital",
            "get_position",
            "analysis_artifact_save",
            "analysis_artifact_get",
            "forecast_save",
            "session_context_append",
            "session_context_get_recent",
            "get_user_setting",
            "list_active_watches",
        }
        leaked = write_or_persistence & mcp.tools.keys()
        assert not leaked, (
            f"account_read leaked write/persistence tools: {sorted(leaked)}"
        )

    def test_expected_account_read_tools_are_present(self) -> None:
        mcp = _build_mcp(McpProfile.ACCOUNT_READ)
        assert {
            "get_holdings",
            "toss_get_positions",
            "get_cash_balance",
            "toss_get_orderable_cash",
            "get_order_history",
            "toss_get_order_history",
        } <= mcp.tools.keys()


class TestTradingCodexExecutionProfile:
    def test_registers_exact_tradingcodex_execution_allowlist(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        # ROB-816: order_proposal_* tools are additionally gated by
        # settings.ORDER_PROPOSALS_ENABLED (default off), unlike every other
        # tool group in this allowlist which registers unconditionally. They
        # are only present in the actually-registered surface when the flag
        # is flipped on.
        expected = TRADINGCODEX_EXECUTION_TOOL_NAMES
        if not settings.ORDER_PROPOSALS_ENABLED:
            expected = expected - ORDER_PROPOSAL_TOOL_NAMES
        assert set(mcp.tools) == expected

    def test_does_not_register_forbidden_execution_surfaces(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        leaked = TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES & mcp.tools.keys()
        assert not leaked, (
            f"tradingcodex_execution leaked forbidden tools: {sorted(leaked)}"
        )

    def test_expected_execution_tools_are_present(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        assert {
            "get_holdings",
            "get_cash_balance",
            "get_order_history",
            "toss_get_order_history",
            "place_order",
            "cancel_order",
            "toss_preview_order",
            "toss_place_order",
            "toss_cancel_order",
            "sell_ladder_fill_preview",
            "buy_ladder_fill_preview",
            "get_fx_rate",
            "route_request",
            "get_trading_policy",
            "list_active_watches",
            "investment_watch_events_list_recent",
            "investment_watch_create",
            "forecast_save",
            "get_forecasts",
            "save_trade_retrospective",
            "get_trade_retrospectives",
            "trade_retrospective_pending",
        } <= mcp.tools.keys()

    def test_removed_broker_namespaces_are_absent(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        assert not [name for name in mcp.tools if name.startswith("kiwoom_")]
        assert not [name for name in mcp.tools if name.startswith("alpaca_")]

    def test_does_not_register_modify_reconcile_or_unsafe_persistence_tools(
        self,
    ) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        forbidden = {
            "modify_order",
            "kis_live_modify_order",
            "kis_live_reconcile_orders",
            "live_reconcile_orders",
            "toss_modify_order",
            "toss_reconcile_orders",
            "analysis_artifact_save",
            "analysis_artifact_get",
            "analysis_artifact_list",
            "forecast_resolve",
            "get_forecast_calibration",
            "get_retrospective_aggregate",
            "session_context_append",
            "session_context_get_recent",
            "update_manual_holdings",
            "get_user_setting",
            "set_user_setting",
            "investment_report_create",
            "investment_report_add_items",
            "investment_report_update",
            "investment_report_decide_item",
            "investment_report_activate_watch",
            "investment_report_set_status",
            "investment_watch_recommend",
        }
        leaked = forbidden & mcp.tools.keys()
        assert not leaked, (
            f"tradingcodex_execution leaked unsafe tools: {sorted(leaked)}"
        )

    @pytest.mark.asyncio
    async def test_forecast_save_requires_explicit_created_by(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        tool = mcp.tools["forecast_save"]

        result = await tool(
            created_by=" ",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={
                "kind": "price_target",
                "direction": "at_or_above",
                "target_price": 75000.0,
            },
            probability=0.55,
            review_date="2026-07-15",
        )

        assert result == {
            "success": False,
            "error": "created_by_required",
            "tool": "forecast_save",
            "detail": "tradingcodex_execution write calls must pass explicit created_by such as 'tradingcodex'.",
        }

    @pytest.mark.asyncio
    async def test_save_trade_retrospective_requires_created_by_profile(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        tool = mcp.tools["save_trade_retrospective"]

        result = await tool(
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="toss_live",
            outcome="filled",
            created_by_profile=" ",
        )

        assert result == {
            "success": False,
            "error": "created_by_required",
            "tool": "save_trade_retrospective",
            "detail": "tradingcodex_execution write calls must pass explicit created_by_profile such as 'tradingcodex'.",
        }

    # ROB-768 Task 2 — direct watch create needs explicit created_by on the
    # tradingcodex_execution profile. Blank/missing must fail closed BEFORE
    # touching the DB.
    @pytest.mark.asyncio
    async def test_investment_watch_create_requires_explicit_created_by(self) -> None:
        mcp = _build_mcp(McpProfile.TRADINGCODEX_EXECUTION)
        tool = mcp.tools["investment_watch_create"]

        result = await tool(
            created_by=" ",
            market="kr",
            symbol="005930",
            intent="trend_recovery_review",
            rationale="missing provenance",
            watch_condition={
                "metric": "price",
                "operator": "above",
                "threshold": 70000,
            },
            valid_until="2026-07-15T09:00:00+09:00",
        )

        assert result == {
            "success": False,
            "error": "created_by_required",
            "tool": "investment_watch_create",
            "detail": "tradingcodex_execution write calls must pass explicit created_by such as 'tradingcodex'.",
        }


class TestRouteProposalReadinessByProfile:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "profile",
        [McpProfile.DEFAULT, McpProfile.TRADINGCODEX_EXECUTION],
    )
    async def test_proposal_gate_on_is_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile: McpProfile,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
        mcp = _build_mcp(profile)

        result = await mcp.tools["route_request"](
            intent="buy_analysis",
            market="kr",
        )

        assert "order_proposal_create" in mcp.tools
        assert result["success"] is True
        assert result["route_contract"]["execution_ready"] is True
        assert result["standard_tool_sequence"][-1]["tool"] == ("order_proposal_create")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "profile",
        [McpProfile.DEFAULT, McpProfile.TRADINGCODEX_EXECUTION],
    )
    async def test_proposal_gate_off_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile: McpProfile,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", False)
        mcp = _build_mcp(profile)

        result = await mcp.tools["route_request"](
            intent="profit_taking",
            market="kr",
        )

        assert "order_proposal_create" not in mcp.tools
        assert result["success"] is False
        assert result["error"] == "required_route_tool_unavailable"
        assert result["route_contract"]["execution_ready"] is False

    @pytest.mark.asyncio
    async def test_analysis_readonly_fails_closed_even_with_gate_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
        mcp = _build_mcp(McpProfile.ANALYSIS_READONLY)

        result = await mcp.tools["route_request"](
            intent="buy_analysis",
            market="kr",
        )

        assert "order_proposal_create" not in mcp.tools
        assert result["success"] is False
        assert result["error"] == "required_route_tool_unavailable"
        assert result["route_contract"]["execution_ready"] is False


class TestResolveMcpProfile:
    def test_none_returns_default(self) -> None:
        assert resolve_mcp_profile(None) is McpProfile.DEFAULT

    def test_empty_string_returns_default(self) -> None:
        assert resolve_mcp_profile("") is McpProfile.DEFAULT

    def test_whitespace_only_returns_default(self) -> None:
        assert resolve_mcp_profile("   ") is McpProfile.DEFAULT

    def test_explicit_default(self) -> None:
        assert resolve_mcp_profile("default") is McpProfile.DEFAULT

    def test_removed_hermes_paper_kis_rejects(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("hermes-paper-kis")

    def test_us_paper_rejects(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("us-paper")

    def test_db_paper(self) -> None:
        assert resolve_mcp_profile("db-paper") is McpProfile.DB_PAPER

    def test_crypto(self) -> None:
        assert resolve_mcp_profile("crypto") is McpProfile.CRYPTO

    def test_kiwoom_rejects(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("kiwoom")

    def test_shadow_replay(self) -> None:
        assert resolve_mcp_profile("shadow-replay") is McpProfile.SHADOW_REPLAY

    def test_analysis_readonly(self) -> None:
        assert resolve_mcp_profile("analysis_readonly") is McpProfile.ANALYSIS_READONLY

    def test_account_read(self) -> None:
        assert resolve_mcp_profile("account_read") is McpProfile.ACCOUNT_READ

    def test_tradingcodex_execution(self) -> None:
        assert (
            resolve_mcp_profile("tradingcodex_execution")
            is McpProfile.TRADINGCODEX_EXECUTION
        )

    def test_paper_execution_rejects(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("paper_execution")

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("unknown-profile")

    def test_invalid_string_mentions_allowed_values(self) -> None:
        with pytest.raises(ValueError, match="default"):
            resolve_mcp_profile("bogus")
