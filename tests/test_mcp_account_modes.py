from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import DummyMCP


def test_default_account_mode_is_toss_live():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode()

    assert routing.account_mode == "toss_live"
    assert routing.is_toss_live is True
    assert routing.warnings == []


@pytest.mark.parametrize("selector_name", ["account_mode", "account_type"])
@pytest.mark.parametrize("value", ["real", "live"])
def test_ambiguous_live_selectors_reject(selector_name: str, value: str):
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    with pytest.raises(ValueError, match="use account_mode='toss_live'"):
        normalize_account_mode(**{selector_name: value})


def test_account_type_paper_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_type="paper")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True
    assert routing.warnings


def test_account_mode_simulated_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode="simulated")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True


@pytest.mark.parametrize("mode", ["kis_live", "kis_mock"])
def test_explicit_kis_modes_remain_parseable_for_historical_rejection(mode: str):
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode=mode)

    assert routing.account_mode == mode
    assert routing.is_kis_live is (mode == "kis_live")
    assert routing.is_kis_mock is (mode == "kis_mock")


def test_conflicting_account_selectors_fail():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    with pytest.raises(ValueError, match="conflicting account selectors"):
        normalize_account_mode(account_mode="kis_mock", account_type="paper")


def test_validate_kis_mock_config_reports_names_only():
    from app.core.config import validate_kis_mock_config

    class DummySettings:
        kis_mock_enabled = False
        kis_mock_app_key = None
        kis_mock_app_secret = "secret-value"
        kis_mock_account_no = ""

    missing = validate_kis_mock_config(DummySettings())

    assert missing == [
        "KIS_MOCK_ENABLED",
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_ACCOUNT_NO",
    ]
    assert "secret-value" not in repr(missing)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "place_order",
            {"symbol": "005930", "side": "buy", "price": 70000.0},
        ),
        (
            "cancel_order",
            {"order_id": "legacy-order", "symbol": "005930"},
        ),
        (
            "modify_order",
            {
                "order_id": "legacy-order",
                "symbol": "005930",
                "new_price": 70000.0,
            },
        ),
        (
            "get_order_history",
            {"symbol": "005930"},
        ),
    ],
)
async def test_generic_kis_operations_fail_closed_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    tool_name: str,
    arguments: dict[str, object],
):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    forbidden = AsyncMock(side_effect=AssertionError("provider dispatch must not run"))
    monkeypatch.setattr(
        orders_registration.order_execution, "_place_order_impl", forbidden
    )
    monkeypatch.setattr(orders_registration, "cancel_order_impl", forbidden)
    monkeypatch.setattr(orders_registration, "modify_order_impl", forbidden)
    monkeypatch.setattr(
        orders_registration.orders_history,
        "get_order_history_impl",
        forbidden,
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_preview_order",
        forbidden,
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_place_order",
        forbidden,
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_cancel_order",
        forbidden,
    )
    monkeypatch.setattr(
        orders_registration.orders_toss_variants,
        "toss_modify_order",
        forbidden,
    )

    result = await mcp.tools[tool_name](**arguments, account_mode=mode)

    assert result["success"] is False
    assert result["error"] == "provider kis is not operational"
    assert result["account_mode"] == mode
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["cancel_order", "modify_order"])
async def test_db_simulated_mutations_remain_unsupported(tool_name: str):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    arguments: dict[str, object] = {"order_id": "test-order"}
    if tool_name == "modify_order":
        arguments.update(symbol="005930", new_price=70000.0)

    result = await mcp.tools[tool_name](
        **arguments,
        account_mode="db_simulated",
    )

    assert result["success"] is False
    assert "not supported" in result["error"].lower()
    assert result["account_mode"] == "db_simulated"
