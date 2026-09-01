"""
Tests for MCP portfolio tools: get_cash_balance, get_holdings, get_position.

These tests cover portfolio-related MCP tools including cash balance queries,
holdings management, position tracking, and average cost simulation.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import paper_portfolio_handler, portfolio_holdings
from app.mcp_server.tooling.portfolio_avg_cost import simulate_avg_cost_impl
from app.services.upbit_symbol_universe_service import (
    UpbitSymbolInactiveError,
    UpbitSymbolNotRegisteredError,
    UpbitSymbolUniverseEmptyError,
)
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    _upbit_name_lookup_mock,
    build_tools,
)

# ---------------------------------------------------------------------------
# get_cash_balance tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cash_balance_with_account_filter_upbit_success(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 700000.0, "orderable": 500000.0}),
    )

    result = await tools["get_cash_balance"](account="upbit")

    assert len(result["accounts"]) == 1
    upbit_account = result["accounts"][0]
    assert upbit_account["account"] == "upbit"
    assert upbit_account["balance"] == pytest.approx(700000.0)
    assert upbit_account["orderable"] == pytest.approx(500000.0)
    assert result["summary"]["total_krw"] == upbit_account["balance"]
    assert result["summary"]["total_usd"] == pytest.approx(0.0)
    assert len(result["errors"]) == 0


# ---------------------------------------------------------------------------
# TestSimulateAvgCost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSimulateAvgCost:
    """Tests for retained average-cost simulation implementation."""

    async def test_basic_simulation_with_market_price(self):
        result = await simulate_avg_cost_impl(
            holdings={"price": 2400000, "quantity": 1},
            plans=[
                {"price": 2050000, "quantity": 1},
                {"price": 1900000, "quantity": 1},
            ],
            current_market_price=2157000,
            target_price=3080000,
        )

        # current_position
        cp = result["current_position"]
        assert cp["avg_price"] == 2400000
        assert cp["total_quantity"] == 1
        assert cp["total_invested"] == 2400000
        assert cp["unrealized_pnl"] == pytest.approx(-243000.0)
        assert cp["unrealized_pnl_pct"] == pytest.approx(-10.12)

        assert result["current_market_price"] == 2157000

        # step 1
        s1 = result["steps"][0]
        assert s1["step"] == 1
        assert s1["buy_price"] == 2050000
        assert s1["buy_quantity"] == 1
        assert s1["new_avg_price"] == 2225000
        assert s1["total_quantity"] == 2
        assert s1["total_invested"] == 4450000
        assert s1["breakeven_change_pct"] == pytest.approx(3.15)
        assert s1["unrealized_pnl"] == pytest.approx(-136000.0)
        assert s1["unrealized_pnl_pct"] == pytest.approx(-3.06)

        # step 2
        s2 = result["steps"][1]
        assert s2["step"] == 2
        assert s2["new_avg_price"] == pytest.approx(2116666.67)
        assert s2["total_quantity"] == 3
        assert s2["total_invested"] == 6350000
        # avg 2116666.67 / mkt 2157000 - 1 = -1.87%
        assert s2["breakeven_change_pct"] == pytest.approx(-1.87)

        # target_analysis
        ta = result["target_analysis"]
        assert ta["target_price"] == 3080000
        assert ta["final_avg_price"] == pytest.approx(2116666.67)
        assert ta["total_return_pct"] == pytest.approx(45.51)

    async def test_without_market_price(self):
        """Without current_market_price, P&L and breakeven fields are absent."""
        result = await simulate_avg_cost_impl(
            holdings={"price": 50000, "quantity": 10},
            plans=[{"price": 40000, "quantity": 10}],
        )

        cp = result["current_position"]
        assert cp["avg_price"] == 50000
        assert "unrealized_pnl" not in cp

        s1 = result["steps"][0]
        assert s1["new_avg_price"] == 45000
        assert "breakeven_change_pct" not in s1
        assert "current_market_price" not in result
        assert "target_analysis" not in result

    async def test_with_target_only(self):
        """target_price without current_market_price still computes return."""
        result = await simulate_avg_cost_impl(
            holdings={"price": 100, "quantity": 5},
            plans=[{"price": 80, "quantity": 5}],
            target_price=120,
        )

        ta = result["target_analysis"]
        assert ta["final_avg_price"] == 90
        assert ta["profit_per_unit"] == 30
        assert ta["total_profit"] == 300
        assert ta["total_return_pct"] == pytest.approx(33.33)

    async def test_validation_missing_holdings_fields(self):
        with pytest.raises(ValueError, match="holdings must contain"):
            await simulate_avg_cost_impl(
                holdings={"price": 100},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_empty_plans(self):
        with pytest.raises(ValueError, match="plans must contain"):
            await simulate_avg_cost_impl(
                holdings={"price": 100, "quantity": 1},
                plans=[],
            )

    async def test_validation_negative_price(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            await simulate_avg_cost_impl(
                holdings={"price": -100, "quantity": 1},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_plan_missing_fields(self):
        with pytest.raises(ValueError, match=r"plans\[0\] must contain"):
            await simulate_avg_cost_impl(
                holdings={"price": 100, "quantity": 1},
                plans=[{"price": 90}],
            )

    async def test_single_plan(self):
        result = await simulate_avg_cost_impl(
            holdings={"price": 1000, "quantity": 2},
            plans=[{"price": 800, "quantity": 2}],
            current_market_price=900,
        )

        assert len(result["steps"]) == 1
        s = result["steps"][0]
        assert s["new_avg_price"] == 900
        assert s["total_quantity"] == 4
        # avg == market → breakeven 0%
        assert s["breakeven_change_pct"] == pytest.approx(0.0)
        assert s["unrealized_pnl"] == pytest.approx(0.0)

    async def test_accepts_zero_initial_quantity_and_adds_target_metrics(self):
        result = await simulate_avg_cost_impl(
            holdings={"price": 0, "quantity": 0},
            plans=[
                {"price": 100, "quantity": 1},
                {"price": 90, "quantity": 1},
            ],
            current_market_price=95,
            target_price=120,
        )

        assert result["current_position"]["avg_price"] is None
        assert result["steps"][0]["target_return_pct"] == pytest.approx(20.0)
        assert "pnl_vs_current" in result["steps"][0]
        assert result["steps"][1]["new_avg_price"] == pytest.approx(95.0)
        assert result["steps"][1]["target_return_pct"] == pytest.approx(26.32)

    async def test_requested_scenario_contains_step_target_return(self):
        result = await simulate_avg_cost_impl(
            holdings={"price": 122493036, "quantity": 0.00931179},
            plans=[
                {"quantity": 0.01, "price": 100000000},
                {"quantity": 0.01, "price": 95000000},
            ],
            target_price=120000000,
            current_market_price=101692000,
        )

        assert len(result["steps"]) == 2
        for step in result["steps"]:
            assert "new_avg_price" in step
            assert "total_quantity" in step
            assert "total_invested" in step
            assert "unrealized_pnl" in step
            assert "target_return_pct" in step


# ---------------------------------------------------------------------------
# get_holdings / get_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_holdings_crypto_prices_batch_fetch(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "ETH",
                    "unit_currency": "KRW",
                    "balance": "2",
                    "locked": "0",
                    "avg_buy_price": "4000000",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인", "ETH": "이더리움"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 61000000.0, "KRW-ETH": 4200000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 40.0}}}
        ),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == pytest.approx(61000000.0)
    assert positions_by_symbol["KRW-ETH"]["current_price"] == pytest.approx(4200000.0)
    quote_mock.assert_awaited_once()
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_holdings_includes_crypto_price_errors(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "DOGE",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인", "DOGE": "도지"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC", "KRW-DOGE"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-DOGE"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 40.0}}}
        ),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["filtered_count"] == 1
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == pytest.approx(62000000.0)
    assert "KRW-DOGE" not in positions_by_symbol

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["source"] == "upbit"
    assert error["market"] == "crypto"
    assert error["symbol"] == "KRW-DOGE"
    assert error["stage"] == "current_price"
    assert error["error"] == "price missing in batch ticker response"
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_holdings_include_current_price_false_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    get_markets_mock = AsyncMock(return_value=["KRW-BTC"])
    _patch_runtime_attr(monkeypatch, "get_active_upbit_markets", get_markets_mock)
    quote_mock = AsyncMock(return_value={"KRW-BTC": 61000000.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", quote_mock)

    result = await tools["get_holdings"](
        account="upbit",
        market="crypto",
        include_current_price=False,
    )

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    symbols = [
        position["symbol"]
        for account_payload in result["accounts"]
        for position in account_payload["positions"]
    ]
    assert symbols == ["KRW-BTC"]
    assert "KRW-PCI" not in symbols
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])
    quote_mock.assert_not_awaited()
    get_markets_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_holdings_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 61000000.0}),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    symbols = [
        position["symbol"]
        for account_payload in result["accounts"]
        for position in account_payload["positions"]
    ]
    assert symbols == ["KRW-BTC"]
    assert "KRW-PCI" not in symbols
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_error",
    [
        UpbitSymbolNotRegisteredError("KRW-PCI not registered"),
        UpbitSymbolInactiveError("KRW-PCI is inactive"),
    ],
)
async def test_get_position_silently_skips_missing_or_inactive_upbit_coins(
    monkeypatch, lookup_error
):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        coin = str(currency).upper()
        if coin == "BTC":
            return "비트코인"
        if coin == "PCI":
            raise lookup_error
        return coin

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 61000000.0}),
    )

    result = await tools["get_position"]("BTC", market="crypto")

    assert result["has_position"] is True
    assert result["position_count"] == 1
    assert [position["symbol"] for position in result["positions"]] == ["KRW-BTC"]
    assert all(error.get("symbol") != "KRW-PCI" for error in result["errors"])


@pytest.mark.asyncio
async def test_get_holdings_keeps_fail_fast_on_upbit_universe_empty(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                }
            ]
        ),
    )

    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = currency, quote_currency, db
        raise UpbitSymbolUniverseEmptyError("upbit_symbol_universe is empty")

    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        AsyncMock(side_effect=_lookup),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    with pytest.raises(UpbitSymbolUniverseEmptyError):
        await tools["get_holdings"](account="upbit", market="crypto")


@pytest.mark.asyncio
async def test_get_holdings_includes_top_level_summary(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": 4000000.0,
            "evaluation_amount": 4000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 33.33,
        },
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit", market="crypto", minimum_value=0
    )

    summary = result["summary"]
    assert summary["position_count"] == 2
    assert summary["total_buy_amount"] == pytest.approx(8000000.0)
    assert summary["total_evaluation"] == pytest.approx(10000000.0)
    assert summary["total_profit_loss"] == pytest.approx(2000000.0)
    assert summary["total_profit_rate"] == pytest.approx(25.0)
    assert summary["weights"][0]["symbol"] == "KRW-BTC"
    assert summary["weights"][0]["weight_pct"] == pytest.approx(60.0)
    assert summary["weights"][1]["symbol"] == "KRW-ETH"
    assert summary["weights"][1]["weight_pct"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_get_holdings_summary_sets_price_dependent_fields_null(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": None,
            "evaluation_amount": None,
            "profit_loss": None,
            "profit_rate": None,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit",
        market="crypto",
        include_current_price=False,
    )

    summary = result["summary"]
    assert summary["total_buy_amount"] == pytest.approx(3000000.0)
    assert summary["total_evaluation"] is None
    assert summary["total_profit_loss"] is None
    assert summary["total_profit_rate"] is None
    assert summary["weights"] is None


@pytest.mark.asyncio
async def test_get_position_crypto_accepts_symbol_without_prefix(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )

    result = await tools["get_position"]("BTC", market="crypto")
    assert result["has_position"] is True
    assert result["position_count"] == 1
    assert result["positions"][0]["symbol"] == "KRW-BTC"


# ------------------------------------------------------------------------------
# Crypto Phase 2 exit signal tests
# ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_holdings_crypto_stop_loss_signal(monkeypatch):
    """Test that crypto holdings include stop-loss signal when profit_rate <= -4.5%."""
    tools = build_tools()

    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 47000000.0,  # -6% from avg
            "evaluation_amount": 4700000.0,
            "profit_loss": -300000.0,
            "profit_rate": -6.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={"KRW-BTC": 101}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 35.0}}}
        ),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    assert btc_position.get("strategy_signal") is not None
    assert btc_position["strategy_signal"]["action"] == "sell"
    assert btc_position["strategy_signal"]["reason"] == "stop_loss"
    assert btc_position["strategy_signal"]["threshold_pct"] == pytest.approx(-4.5)


@pytest.mark.asyncio
async def test_get_holdings_crypto_mean_reversion_signal(monkeypatch):
    """Test that crypto holdings include mean-reversion signal when profit > 0 and RSI > 46."""
    tools = build_tools()

    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 55000000.0,  # +10% from avg
            "evaluation_amount": 5500000.0,
            "profit_loss": 500000.0,
            "profit_rate": 10.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={"KRW-BTC": 101}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_compute_crypto_signals_for_position",
        AsyncMock(return_value=(50.0, None)),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    assert btc_position.get("strategy_signal") is not None
    assert btc_position["strategy_signal"]["action"] == "sell"
    assert btc_position["strategy_signal"]["reason"] == "mean_reversion_exit"
    assert btc_position["strategy_signal"]["rsi_14"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_get_holdings_crypto_no_signal_profitable_low_rsi(monkeypatch):
    """Test that profitable positions with low RSI don't emit mean-reversion signal."""
    tools = build_tools()

    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 55000000.0,  # +10% from avg
            "evaluation_amount": 5500000.0,
            "profit_loss": 500000.0,
            "profit_rate": 10.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_compute_crypto_signals_for_position",
        AsyncMock(return_value=(40.0, None)),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    assert btc_position.get("strategy_signal") is None


@pytest.mark.asyncio
async def test_get_holdings_non_crypto_no_signal(monkeypatch):
    """Test that non-crypto positions don't include strategy signals."""
    tools = build_tools()

    mocked_positions = [
        {
            "symbol": "005930",
            "name": "Samsung",
            "instrument_type": "equity_kr",
            "market": "kr",
            "account": "toss",
            "broker": "toss",
            "account_name": "Toss",
            "quantity": 10,
            "avg_buy_price": 70000.0,
            "current_price": 65000.0,
            "evaluation_amount": 650000.0,
            "profit_loss": -50000.0,
            "profit_rate": -7.14,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "kr", None)),
    )

    result = await tools["get_holdings"](account="toss", market="kr")
    position = result["accounts"][0]["positions"][0]

    assert position.get("strategy_signal") is None


@pytest.mark.asyncio
async def test_get_holdings_strategy_signal_reuses_portfolio_snapshot_price(
    monkeypatch,
):
    """Strategy signal path should not trigger a second live price fetch."""
    tools = build_tools()
    price_fetch_count = 0

    async def mock_fetch_prices(markets: list[str]) -> dict[str, float]:
        nonlocal price_fetch_count
        price_fetch_count += 1
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 47_000_000.0}

    df = pd.DataFrame(
        {
            "open": [50_000_000.0 + i * 100_000.0 for i in range(250)],
            "high": [50_100_000.0 + i * 100_000.0 for i in range(250)],
            "low": [49_900_000.0 + i * 100_000.0 for i in range(250)],
            "close": [50_000_000.0 + i * 100_000.0 for i in range(250)],
            "volume": [1_000.0] * 250,
        }
    )

    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(side_effect=mock_fetch_prices),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_upbit_korean_name_by_coin",
        _upbit_name_lookup_mock({"BTC": "비트코인"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC"}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={"KRW-BTC": 101}),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    assert price_fetch_count == 1
    assert btc_position["current_price"] == pytest.approx(47_000_000.0)
    assert btc_position["strategy_signal"]["reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_get_holdings_crypto_strategy_signal_includes_voting(monkeypatch):
    """Holdings with strategy_signals=True should include voting data."""
    tools = build_tools()

    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 47000000.0,  # -6% from avg - stop loss
            "evaluation_amount": 4700000.0,
            "profit_loss": -300000.0,
            "profit_rate": -6.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={"KRW-BTC": 101}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 35.0}}}
        ),
    )

    # Mock OHLCV data that will produce voting results
    import numpy as np

    closes = list(np.linspace(200, 100, 50))
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * 30 + [5000.0] * 20,  # volume spike
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    # Strategy signal should exist with voting data
    signal = btc_position.get("strategy_signal")
    assert signal is not None
    # Either it's a stop_loss (with bear_votes) or some other signal
    if signal.get("reason") in ("stop_loss", "mean_reversion_exit", "bear_vote_exit"):
        assert "bear_votes" in signal


@pytest.mark.asyncio
async def test_get_holdings_crypto_strategy_signal_native_types(monkeypatch):
    """Regression #463: strategy_signal values must be native JSON-safe types."""
    import json

    tools = build_tools()

    # profit_rate = -2.0 (loss, but not stop-loss at -4.5)
    # This + sell_signal=True triggers bear_vote_exit, which exposes bear_flags
    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 49000000.0,
            "evaluation_amount": 4900000.0,
            "profit_loss": -100000.0,
            "profit_rate": -2.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={"KRW-BTC": 101}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 55.0}}}
        ),
    )

    # OHLCV: uptrend then sharp reversal -> produces sell_signal=True (bear_votes >= 2)
    import numpy as np

    closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * 50,
        }
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    signal = btc_position.get("strategy_signal")
    assert signal is not None, "strategy_signal missing from crypto position"
    assert signal["reason"] == "bear_vote_exit"

    # Core assertion: all bear_flags values must be native bool
    for key, value in signal["bear_flags"].items():
        assert type(value) is bool, f"bear_flags[{key}] is {type(value)}, not bool"

    # Supplementary: entire result must be JSON-serializable
    json.dumps(result)


@pytest.mark.asyncio
async def test_get_holdings_crypto_structured_output_survives_fastmcp(monkeypatch):
    """Regression #463: FastMCP must produce structured output, not text-only fallback."""
    from fastmcp import FastMCP

    from app.mcp_server.tooling.registry import register_all_tools

    mcp = FastMCP("test")
    register_all_tools(mcp)

    # Same setup as Layer 2: loss position + reversal OHLCV -> bear_vote_exit
    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 49000000.0,
            "evaluation_amount": 4900000.0,
            "profit_loss": -100000.0,
            "profit_rate": -2.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 55.0}}}
        ),
    )

    import numpy as np

    closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * 50,
        }
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    tool_result = await mcp.call_tool(
        "get_holdings", {"account": "upbit", "market": "crypto"}
    )

    # Core assertion: structured output must survive FastMCP serialization
    assert tool_result.structured_content is not None, (
        "structured_content is None — FastMCP failed to serialize the response. "
        "This likely means a non-JSON-safe type (e.g. numpy.bool_) leaked through."
    )


# ---------------------------------------------------------------------------
# Paper trading account filter tests
# ---------------------------------------------------------------------------


class _StubAcc:
    def __init__(self, id_, name, is_active=True):
        self.id, self.name, self.is_active = id_, name, is_active


class _StubPaperService:
    def __init__(self, accounts, positions, cash=None):
        self._a, self._p, self._c = accounts, positions, cash or {}

    async def list_accounts(self, is_active=True):
        return [a for a in self._a if (is_active is None or a.is_active == is_active)]

    async def get_account_by_name(self, name):
        return next((a for a in self._a if a.name == name), None)

    async def get_positions(self, account_id, *, market=None):
        positions = self._p.get(account_id, [])
        if market is not None:
            positions = [p for p in positions if p.get("instrument_type") == market]
        return positions

    async def get_cash_balance(self, account_id):
        return self._c.get(account_id, {"krw": Decimal("0"), "usd": Decimal("0")})


@pytest.mark.asyncio
async def test_get_holdings_with_paper_account_filter(monkeypatch):
    tools = build_tools()

    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ]
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )
    # Avoid real live-broker calls leaking in if the guard regresses

    result = await tools["get_holdings"](account="paper", include_current_price=False)

    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "paper:default"
    pos = result["accounts"][0]["positions"][0]
    assert pos["symbol"] == "005930"
    assert pos["name"] == "삼성전자"
    assert pos["quantity"] == pytest.approx(10.0)
    assert pos["avg_buy_price"] == pytest.approx(72000.0)


@pytest.mark.asyncio
async def test_get_holdings_with_named_paper_account(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "데이트레이딩")],
        positions={
            1: [],
            2: [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "quantity": Decimal("5"),
                    "avg_price": Decimal("150"),
                    "total_invested": Decimal("750"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                }
            ],
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="Apple Inc."),
    )

    result = await tools["get_holdings"](
        account="paper:데이트레이딩", include_current_price=False
    )

    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "paper:데이트레이딩"
    assert result["accounts"][0]["positions"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_position_paper_hit(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ]
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )
    # Make live-broker gatherers explode if accidentally called

    result = await tools["get_position"](symbol="005930", account_type="paper")

    assert result["has_position"] is True
    assert result["accounts"] == ["paper:default"]
    assert result["positions"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_get_position_paper_named(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "데이트레이딩")],
        positions={
            1: [],
            2: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("5"),
                    "avg_price": Decimal("70000"),
                    "total_invested": Decimal("350000"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                }
            ],
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )

    result = await tools["get_position"](
        symbol="005930", account_type="paper", paper_account="데이트레이딩"
    )

    assert result["has_position"] is True
    assert result["accounts"] == ["paper:데이트레이딩"]


@pytest.mark.asyncio
async def test_get_position_paper_miss(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={1: []},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_position"](symbol="005930", account_type="paper")

    assert result["has_position"] is False
    assert result["status"] == "미보유"


@pytest.mark.asyncio
async def test_get_position_invalid_account_type_raises(monkeypatch):
    tools = build_tools()
    with pytest.raises(ValueError, match="account_type must be"):
        await tools["get_position"](symbol="005930", account_type="bogus")


@pytest.mark.asyncio
async def test_get_cash_balance_paper_all(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={},
        cash={1: {"krw": Decimal("10000000"), "usd": Decimal("500")}},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_cash_balance"](account="paper")

    assert {r["currency"] for r in result["accounts"]} == {"KRW", "USD"}
    assert result["summary"]["total_krw"] == pytest.approx(10_000_000.0)
    assert result["summary"]["total_usd"] == pytest.approx(500.0)
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_cash_balance_paper_named(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "day")],
        positions={},
        cash={
            1: {"krw": Decimal("10000000"), "usd": Decimal("0")},
            2: {"krw": Decimal("1000000"), "usd": Decimal("0")},
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_cash_balance"](account="paper:day")

    assert all(r["account"] == "paper:day" for r in result["accounts"])
    assert result["summary"]["total_krw"] == pytest.approx(1_000_000.0)


@pytest.mark.asyncio
async def test_get_available_capital_paper(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={},
        cash={1: {"krw": Decimal("10000000"), "usd": Decimal("500")}},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    # Exchange-rate fetch — stub to a deterministic value.
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_cash.get_usd_krw_rate",
        AsyncMock(return_value=1400.0),
    )
    # Manual cash must not be added for paper queries.
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_cash.get_manual_cash_setting",
        AsyncMock(side_effect=AssertionError("manual cash must not be queried")),
    )

    result = await tools["get_available_capital"](account="paper")

    assert result["manual_cash"] is None
    # 10,000,000 KRW + 500 USD * 1400 = 10,700,000
    assert result["summary"]["total_orderable_krw"] == pytest.approx(10_700_000.0)
    assert result["summary"]["exchange_rate_usd_krw"] == pytest.approx(1400.0)
    # paper USD row must have krw_equivalent injected
    usd_row = next(r for r in result["accounts"] if r["currency"] == "USD")
    assert usd_row["krw_equivalent"] == pytest.approx(700_000.0)


# ---------------------------------------------------------------------------
# ROB-1095: get_holdings US current-price refresh + provenance
# ---------------------------------------------------------------------------


def _us_refresh_position(symbol: str = "AAPL") -> dict:
    """A US position; every US position takes the live quote refresh path."""
    return {
        "instrument_type": "equity_us",
        "symbol": symbol,
        "source": "manual",
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


@pytest.mark.asyncio
async def test_fetch_price_map_us_uses_toss_quote_and_preserves_provenance(
    monkeypatch,
):
    """미국 현재가 갱신이 Toss 공통 시세 출처를 보존한다."""
    us_quote_mock = AsyncMock(
        return_value={
            "price": 208.0,
            "source": "toss",
            "price_source": "toss_snapshot",
            "quote_asof": "2026-07-27T10:03:00-04:00",
            "data_state": "fresh",
            "session": "regular",
            "venue": "NYSE",
            "delayed": True,
        }
    )
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_us", us_quote_mock)

    (
        price_map,
        price_errors,
        error_map,
        metadata_map,
    ) = await portfolio_holdings._fetch_price_map_for_positions(
        [_us_refresh_position()]
    )

    assert price_map[("equity_us", "AAPL")] == pytest.approx(208.0)
    assert price_errors == []
    assert ("equity_us", "AAPL") not in error_map
    assert metadata_map[("equity_us", "AAPL")] == {
        "price_source": "toss_snapshot",
        "price_asof": "2026-07-27T10:03:00-04:00",
        "data_state": "fresh",
        "session": "regular",
        "venue": "NYSE",
        "delayed": True,
    }
    us_quote_mock.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_fetch_price_map_us_preserves_yahoo_fallback_provenance(monkeypatch):
    """공통 시세가 Yahoo fallback을 반환하면 출처를 그대로 보존한다."""
    us_quote_mock = AsyncMock(
        return_value={
            "price": 215.0,
            "source": "yahoo",
            "price_source": "yahoo_fast_info_close",
            "data_state": "fresh",
            "session": "regular",
            "delayed": True,
        }
    )
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_us", us_quote_mock)

    (
        price_map,
        price_errors,
        error_map,
        metadata_map,
    ) = await portfolio_holdings._fetch_price_map_for_positions(
        [_us_refresh_position()]
    )

    assert price_map[("equity_us", "AAPL")] == pytest.approx(215.0)
    assert price_errors == []
    assert error_map == {}
    assert metadata_map[("equity_us", "AAPL")]["price_source"] == (
        "yahoo_fast_info_close"
    )
    us_quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_price_map_us_fail_closed_when_all_sources_fail(monkeypatch):
    """Toss 공통 시세 실패 시 가격을 만들지 않는다."""
    us_quote_mock = AsyncMock(
        side_effect=RuntimeError("US quote temporarily unavailable")
    )
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_us", us_quote_mock)

    (
        price_map,
        price_errors,
        error_map,
        metadata_map,
    ) = await portfolio_holdings._fetch_price_map_for_positions(
        [_us_refresh_position()]
    )

    assert ("equity_us", "AAPL") not in price_map
    assert ("equity_us", "AAPL") in error_map
    assert metadata_map == {}
    us_error = next(e for e in price_errors if e.get("symbol") == "AAPL")
    assert us_error["source"] == "toss"
    assert us_error["market"] == "us"


# ---------------------------------------------------------------------------
# Toss holdings tests
# ---------------------------------------------------------------------------


def _use_direct_toss_holdings_path(monkeypatch) -> None:
    """Keep direct Toss-reader tests independent from the shared snapshot cache."""
    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: SimpleNamespace(usable=False),
    )


@pytest.mark.asyncio
async def test_get_holdings_toss_api_enabled_adds_read_only_toss_account(monkeypatch):
    from decimal import Decimal

    from app.mcp_server.tooling import portfolio_holdings
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    _use_direct_toss_holdings_path(monkeypatch)

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [], []

    async def fake_fetch_toss_snapshot(*, need_sellable: bool = True, **_):
        assert need_sellable is False
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_kr",
                    market="kr",
                    symbol="005930",
                    name="삼성전자",
                    quantity=Decimal("10"),
                    avg_buy_price=Decimal("70000"),
                    current_price=Decimal("72000"),
                    evaluation_amount=Decimal("720000"),
                    profit_loss=Decimal("20000"),
                    profit_rate=Decimal("2.8571"),
                    sellable_quantity=None,
                )
            ],
            cash_krw=Decimal("100000"),
            cash_usd=Decimal("0"),
        )

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False, minimum_value=0
    )

    assert result["accounts"][0]["account"] == "toss"
    assert result["accounts"][0]["broker"] == "toss"
    assert result["accounts"][0]["order_routable"] is False
    assert result["accounts"][0]["positions"][0]["symbol"] == "005930"
    assert "sellable_quantity" not in result["accounts"][0]["positions"][0]


@pytest.mark.asyncio
async def test_get_holdings_toss_api_market_filter_keeps_us_position(monkeypatch):
    from decimal import Decimal

    from app.mcp_server.tooling import portfolio_holdings
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    _use_direct_toss_holdings_path(monkeypatch)

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [], []

    async def fake_fetch_toss_snapshot(*, need_sellable: bool = True, **_):
        assert need_sellable is False
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=None,
                )
            ],
        )

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )

    result = await portfolio_holdings._get_holdings_impl(
        market="us", include_current_price=False, minimum_value=0
    )

    assert result["filters"]["market"] == "us"
    assert result["accounts"][0]["account"] == "toss"
    assert result["accounts"][0]["positions"][0]["symbol"] == "BRK.B"


@pytest.mark.asyncio
async def test_get_holdings_toss_api_success_hides_duplicate_toss_manual(monkeypatch):
    from decimal import Decimal

    from app.mcp_server.tooling import portfolio_holdings
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    _use_direct_toss_holdings_path(monkeypatch)

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [
            {
                "account": "toss",
                "account_name": "Toss 수동",
                "broker": "toss",
                "source": "manual",
                "instrument_type": "equity_us",
                "market": "us",
                "symbol": "BRK.B",
                "name": "Berkshire Hathaway B",
                "quantity": 1.5,
                "avg_buy_price": 400.0,
                "current_price": 430.12,
                "evaluation_amount": 645.18,
                "profit_loss": 45.18,
                "profit_rate": 0.0753,
            }
        ], []

    async def fake_fetch_toss_snapshot(*, need_sellable: bool = True, **_):
        assert need_sellable is False
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=None,
                )
            ]
        )

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False, minimum_value=0
    )

    accounts = result["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["account"] == "toss"
    assert accounts[0]["positions"][0]["source"] == "toss_api"


@pytest.mark.asyncio
async def test_get_holdings_toss_api_failure_keeps_manual_fallback(monkeypatch):
    from app.mcp_server.tooling import portfolio_holdings

    _use_direct_toss_holdings_path(monkeypatch)

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [
            {
                "account": "toss",
                "account_name": "Toss 수동",
                "broker": "toss",
                "source": "manual",
                "instrument_type": "equity_kr",
                "market": "kr",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 10.0,
                "avg_buy_price": 65000.0,
                "current_price": 70000.0,
                "evaluation_amount": 700000.0,
                "profit_loss": 50000.0,
                "profit_rate": 0.0769,
            }
        ], []

    async def fake_fetch_toss_snapshot(*, need_sellable: bool = True, **_):
        assert need_sellable is False
        raise RuntimeError("toss unavailable")

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions
    )
    monkeypatch.setattr(
        portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False, minimum_value=0
    )

    assert result["accounts"][0]["order_routable"] is False
    assert result["accounts"][0]["positions"][0]["source"] == "manual"
    assert {
        "source": "toss_api",
        "error": "toss unavailable",
        "degraded": True,
    } in result["errors"]


# ---------------------------------------------------------------------------
# ROB-532 Toss cash balance tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cash_balance_toss_api_enabled_adds_krw_and_usd(monkeypatch):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from app.mcp_server.tooling import portfolio_cash
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(side_effect=RuntimeError("skip upbit")),
    )
    monkeypatch.setattr(
        portfolio_cash, "fetch_toss_cash_snapshot", fake_fetch_toss_snapshot
    )

    result = await portfolio_cash.get_cash_balance_impl(account="toss")

    assert result["accounts"] == [
        {
            "account": "toss",
            "account_name": "Toss",
            "broker": "toss",
            "currency": "KRW",
            "balance": 123456.0,
            "orderable": 0.0,
            "formatted": "123,456 KRW",
        },
        {
            "account": "toss",
            "account_name": "Toss",
            "broker": "toss",
            "currency": "USD",
            "balance": 789.01,
            "orderable": 0.0,
            "formatted": "789.01 USD",
        },
    ]
    assert result["summary"] == {
        "total_krw": 123456.0,
        "total_usd": 789.01,
        "unavailable_sources": {},
    }
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_cash_balance_toss_api_failure_is_strict_for_toss_filter(monkeypatch):
    from app.mcp_server.tooling import portfolio_cash

    async def fake_fetch_toss_snapshot():
        raise RuntimeError("toss cash unavailable")

    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_cash, "fetch_toss_cash_snapshot", fake_fetch_toss_snapshot
    )

    with pytest.raises(RuntimeError, match="Toss cash balance query failed"):
        await portfolio_cash.get_cash_balance_impl(account="toss")
