"""Unit tests for app/services/portfolio_data_collector.PortfolioDataCollector."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.portfolio_data_collector import PortfolioDataCollector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_collector():
    db = MagicMock()
    return PortfolioDataCollector(db)


def _make_toss_position(**overrides):
    defaults = {
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "name": "삼성전자",
        "account_name": "Toss",
        "quantity": Decimal("10"),
        "avg_buy_price": Decimal("50000"),
        "current_price": Decimal("55000"),
        "evaluation_amount": Decimal("550000"),
        "profit_loss": Decimal("50000"),
        "profit_rate": Decimal("0.10"),
        "sellable_quantity": Decimal("10"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_collector_module_does_not_expose_kis_runtime_symbols():
    import app.services.portfolio_data_collector as module

    assert not hasattr(module, "KISClient")
    assert not hasattr(PortfolioDataCollector, "_collect_kis_components")
    assert not hasattr(PortfolioDataCollector, "_collect_kis_kr_components")
    assert not hasattr(PortfolioDataCollector, "_collect_kis_us_components")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_toss_maps_kr_and_us_general_snapshot():
    collector = _make_collector()
    fetcher = AsyncMock(
        return_value=SimpleNamespace(
            positions=[
                _make_toss_position(profit_loss=None),
                _make_toss_position(
                    instrument_type="equity_us",
                    symbol="AAPL",
                    name="Apple",
                    quantity=Decimal("5"),
                    avg_buy_price=Decimal("150"),
                    current_price=Decimal("170"),
                    evaluation_amount=Decimal("850"),
                    profit_loss=Decimal("100"),
                    profit_rate=Decimal("0.1333"),
                ),
                _make_toss_position(symbol="000660", quantity=Decimal("0")),
                _make_toss_position(instrument_type="crypto", symbol="KRW-BTC"),
            ],
            errors=[],
        )
    )
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.fetch_toss_portfolio_snapshot",
        fetcher,
    ):
        components = await collector._collect_toss_components(warnings)

    assert [(row["market_type"], row["symbol"]) for row in components] == [
        ("KR", "005930"),
        ("US", "AAPL"),
    ]
    assert components[0]["account_key"] == "live:toss"
    assert components[0]["broker"] == "toss"
    assert components[0]["profit_rate"] == pytest.approx(0.10)
    assert components[0]["profit_loss"] is None
    assert "sellable_quantity" not in components[0]
    assert warnings == []
    fetcher.assert_awaited_once_with(need_sellable=False, need_cash=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_toss_appends_warning_on_failure():
    collector = _make_collector()
    fetcher = AsyncMock(side_effect=RuntimeError("network error"))
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.fetch_toss_portfolio_snapshot",
        fetcher,
    ):
        components = await collector._collect_toss_components(warnings)

    assert components == []
    assert len(warnings) == 1
    assert "Toss" in warnings[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_toss_surfaces_sanitized_partial_code():
    collector = _make_collector()
    fetcher = AsyncMock(
        return_value=SimpleNamespace(
            positions=[],
            errors=[{"code": "snapshot_partial"}],
        )
    )
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.fetch_toss_portfolio_snapshot",
        fetcher,
    ):
        components = await collector._collect_toss_components(warnings)

    assert components == []
    assert warnings == ["Toss holdings partial: snapshot_partial"]


# ---------------------------------------------------------------------------
# _collect_upbit_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_upbit_appends_warning_on_fetch_failure():
    collector = _make_collector()
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.upbit_service.fetch_my_coins",
        side_effect=RuntimeError("upbit down"),
    ):
        components = await collector._collect_upbit_components(
            warnings, active_upbit_markets=None, enforce_upbit_universe=False
        )

    assert components == []
    assert len(warnings) == 1
    assert "Upbit" in warnings[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_upbit_skips_krw_currency():
    collector = _make_collector()
    warnings: list[str] = []

    with patch(
        "app.services.portfolio_data_collector.upbit_service.fetch_my_coins",
        return_value=[
            {
                "currency": "KRW",
                "balance": "100000",
                "locked": "0",
                "avg_buy_price": "1",
            }
        ],
    ):
        components = await collector._collect_upbit_components(
            warnings, active_upbit_markets=None, enforce_upbit_universe=False
        )

    assert components == []


# ---------------------------------------------------------------------------
# _collect_manual_components
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_manual_appends_warning_on_failure():
    collector = _make_collector()
    collector.manual_holdings_service = AsyncMock()
    collector.manual_holdings_service.get_holdings_by_user.side_effect = RuntimeError(
        "db error"
    )
    warnings: list[str] = []

    components = await collector._collect_manual_components(
        user_id=1,
        warnings=warnings,
        active_upbit_markets=None,
        enforce_upbit_universe=False,
    )

    assert components == []
    assert len(warnings) == 1
    assert "Manual" in warnings[0]


# ---------------------------------------------------------------------------
# _run_collection_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_collection_task_returns_empty_list_on_exception():
    collector = _make_collector()

    async def _failing_func(warnings):
        raise ValueError("boom")

    result, w = await collector._run_collection_task(_failing_func)
    assert result == []
    assert len(w) == 1
    assert "boom" in w[0]
