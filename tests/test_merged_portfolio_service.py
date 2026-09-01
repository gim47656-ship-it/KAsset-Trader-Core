"""Toss 기반 통합 포트폴리오 서비스 테스트."""

from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.manual_holdings import MarketType
from app.services.merged_portfolio_service import (
    HoldingInfo,
    MergedHolding,
    MergedPortfolioService,
)
from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    TossPortfolioSnapshot,
)


@pytest.fixture
def service() -> MergedPortfolioService:
    return MergedPortfolioService(AsyncMock())


def _position(
    symbol: str,
    *,
    market: str = "kr",
    quantity: str = "10",
    avg_price: str = "70000",
    current_price: str = "77800",
) -> TossPortfolioPosition:
    is_kr = market == "kr"
    return TossPortfolioPosition(
        account="toss_live",
        account_name="Toss",
        broker="toss",
        source="toss_api",
        instrument_type="equity_kr" if is_kr else "equity_us",
        market=market,
        symbol=symbol,
        name=symbol,
        quantity=Decimal(quantity),
        avg_buy_price=Decimal(avg_price),
        current_price=Decimal(current_price),
        evaluation_amount=Decimal(quantity) * Decimal(current_price),
        profit_loss=(Decimal(quantity) * (Decimal(current_price) - Decimal(avg_price))),
        profit_rate=(
            (Decimal(current_price) - Decimal(avg_price)) / Decimal(avg_price)
        ),
        sellable_quantity=None,
    )


def _manual_row(
    symbol: str,
    *,
    broker: str = "toss",
    quantity: str = "5",
    avg_price: str = "65000",
):
    return SimpleNamespace(
        ticker=symbol,
        quantity=Decimal(quantity),
        avg_price=Decimal(avg_price),
        display_name=symbol,
        broker_account=SimpleNamespace(broker_type=broker),
    )


def test_merged_portfolio_has_no_kis_runtime_surface() -> None:
    from app.services import merged_portfolio_service as module

    assert not hasattr(module, "KISClient")
    assert not hasattr(MergedPortfolioService, "_apply_kis_holdings")
    assert (
        "kis_client"
        not in inspect.signature(
            MergedPortfolioService._build_merged_portfolio
        ).parameters
    )
    for method in (
        MergedPortfolioService.get_merged_portfolio_domestic,
        MergedPortfolioService.get_merged_portfolio_overseas,
    ):
        assert "kis_client" not in inspect.signature(method).parameters


def test_calculate_combined_avg_uses_all_published_holdings() -> None:
    holdings = [
        HoldingInfo(broker="toss", quantity=100, avg_price=40_000),
        HoldingInfo(broker="pension", quantity=200, avg_price=50_000),
    ]

    assert MergedPortfolioService.calculate_combined_avg(holdings) == pytest.approx(
        46_666.6666667
    )
    assert MergedPortfolioService.calculate_combined_avg([]) == 0


def test_apply_toss_holdings_maps_live_position(service) -> None:
    merged: dict[str, MergedHolding] = {}

    service._apply_toss_holdings(
        merged,
        [_position("005930", quantity="100")],
        MarketType.KR,
    )

    holding = merged["005930"]
    assert holding.toss_quantity == 100
    assert holding.toss_avg_price == pytest.approx(70_000)
    assert holding.current_price == pytest.approx(77_800)
    assert holding.evaluation == pytest.approx(7_780_000)
    assert [(row.broker, row.quantity) for row in holding.holdings] == [("toss", 100)]
    assert holding.kis_quantity == 0


@pytest.mark.asyncio
async def test_fetch_toss_holdings_filters_requested_equity_market(
    service, monkeypatch
) -> None:
    from app.services import merged_portfolio_service as module

    fetch_snapshot = AsyncMock(
        return_value=TossPortfolioSnapshot(
            positions=[_position("005930"), _position("AAPL", market="us")]
        )
    )
    monkeypatch.setattr(module, "fetch_toss_portfolio_snapshot", fetch_snapshot)

    result = await service._fetch_toss_holdings(MarketType.US)

    assert [row.symbol for row in result] == ["AAPL"]
    fetch_snapshot.assert_awaited_once_with(
        need_sellable=False,
        need_cash=False,
    )


@pytest.mark.asyncio
async def test_manual_holdings_are_owner_scoped_and_live_toss_deduplicated(
    service,
) -> None:
    merged: dict[str, MergedHolding] = {}
    service._apply_toss_holdings(
        merged,
        [_position("005930", quantity="10")],
        MarketType.KR,
    )
    service.manual_holdings_service.get_holdings_by_user = AsyncMock(
        return_value=[
            _manual_row("005930", quantity="99"),
            _manual_row("000660", quantity="3", avg_price="120000"),
        ]
    )

    await service._apply_manual_holdings(
        merged,
        user_id=42,
        market_type=MarketType.KR,
    )

    service.manual_holdings_service.get_holdings_by_user.assert_awaited_once_with(
        42,
        market_type=MarketType.KR,
    )
    assert merged["005930"].toss_quantity == 10
    assert len(merged["005930"].holdings) == 1
    assert merged["000660"].toss_quantity == 3


@pytest.mark.asyncio
async def test_missing_price_uses_shared_market_data_quote(
    service, monkeypatch
) -> None:
    from app.services.market_data import service as market_data

    get_quote = AsyncMock(return_value=SimpleNamespace(price=Decimal("230000")))
    monkeypatch.setattr(market_data, "get_quote", get_quote)
    merged = {
        "005380": MergedHolding(
            ticker="005380",
            name="현대차",
            market_type="KR",
            current_price=0,
            total_quantity=5,
            holdings=[HoldingInfo(broker="toss", quantity=5, avg_price=220_000)],
        )
    }

    await service._fetch_missing_prices(merged, MarketType.KR)

    assert merged["005380"].current_price == pytest.approx(230_000)
    get_quote.assert_awaited_once_with("005380", "kr")


@pytest.mark.asyncio
async def test_missing_price_failure_remains_unpriced(service, monkeypatch) -> None:
    from app.services.market_data import service as market_data

    monkeypatch.setattr(
        market_data,
        "get_quote",
        AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )
    merged = {
        "AAPL": MergedHolding(
            ticker="AAPL",
            name="Apple",
            market_type="US",
            current_price=0,
            total_quantity=2,
            holdings=[HoldingInfo(broker="pension", quantity=2, avg_price=180)],
        )
    }

    await service._fetch_missing_prices(merged, MarketType.US)

    assert merged["AAPL"].current_price == 0


def test_finalize_holdings_calculates_quantity_average_and_profit(service) -> None:
    merged = {
        "005930": MergedHolding(
            ticker="005930",
            name="삼성전자",
            market_type="KR",
            current_price=77_800,
            holdings=[
                HoldingInfo(broker="toss", quantity=100, avg_price=70_000),
                HoldingInfo(broker="pension", quantity=50, avg_price=76_000),
            ],
        )
    }

    service._finalize_holdings(merged)

    holding = merged["005930"]
    assert holding.total_quantity == 150
    assert holding.combined_avg_price == pytest.approx(72_000)
    assert holding.evaluation == pytest.approx(11_670_000)
    assert holding.profit_loss == pytest.approx(870_000)
    assert holding.profit_rate == pytest.approx(5_800 / 72_000)


@pytest.mark.asyncio
async def test_build_merged_portfolio_combines_toss_and_manual_without_duplication(
    service, monkeypatch
) -> None:
    from app.services.market_data import service as market_data

    service._fetch_toss_holdings = AsyncMock(
        return_value=[_position("005930", quantity="10")]
    )
    service.manual_holdings_service.get_holdings_by_user = AsyncMock(
        return_value=[
            _manual_row("005930", quantity="99"),
            _manual_row(
                "000660",
                broker="pension",
                quantity="3",
                avg_price="120000",
            ),
        ]
    )
    monkeypatch.setattr(
        market_data,
        "get_quote",
        AsyncMock(return_value=SimpleNamespace(price=Decimal("125000"))),
    )
    service._attach_analysis_and_settings = AsyncMock()

    result = await service._build_merged_portfolio(
        user_id=7,
        market_type=MarketType.KR,
    )

    by_symbol = {row.ticker: row for row in result}
    assert set(by_symbol) == {"005930", "000660"}
    assert by_symbol["005930"].total_quantity == 10
    assert by_symbol["000660"].total_quantity == 3
    assert by_symbol["000660"].current_price == pytest.approx(125_000)
    assert all(
        source.broker != "kis" for holding in result for source in holding.holdings
    )
    service._attach_analysis_and_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_reference_prices_ignore_legacy_holdings_and_use_toss_snapshot(
    service,
) -> None:
    service._build_merged_portfolio = AsyncMock(
        return_value=[
            MergedHolding(
                ticker="AAPL",
                name="Apple",
                market_type="US",
                toss_quantity=4,
                toss_avg_price=180,
                combined_avg_price=180,
                total_quantity=4,
                holdings=[HoldingInfo(broker="toss", quantity=4, avg_price=180)],
            )
        ]
    )

    result = await service.get_reference_prices(
        11,
        "aapl",
        MarketType.US,
        {"AAPL": {"quantity": 999, "avg_price": 1}},
    )

    assert result.toss_avg == pytest.approx(180)
    assert result.toss_quantity == 4
    assert result.combined_avg == pytest.approx(180)
    assert result.total_quantity == 4
    assert result.kis_avg is None
    assert result.kis_quantity == 0
    service._build_merged_portfolio.assert_awaited_once_with(
        11,
        MarketType.US,
        attach_metadata=False,
    )
