from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import FillOrder


def _kr(side: str = "ask") -> FillOrder:
    return FillOrder(
        symbol="005930",
        side=side,
        filled_price=68500.0,
        filled_qty=10.0,
        filled_amount=685000.0,
        filled_at="t",
        account="toss",
        market_type="kr",
        currency="KRW",
    )


def _snapshot(*, qty: str = "50", avg: str = "68000") -> SimpleNamespace:
    return SimpleNamespace(
        positions=[
            SimpleNamespace(
                instrument_type="equity_kr",
                symbol="005930",
                quantity=Decimal(qty),
                avg_buy_price=Decimal(avg),
            )
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_sell_realized_pnl_uses_toss_position(monkeypatch):
    fetcher = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(
        "app.services.fill_enrichment.fetch_toss_portfolio_snapshot", fetcher
    )

    enrichment = await fetch_fill_enrichment(_kr(side="ask"))

    assert enrichment is not None
    assert enrichment.realized_pnl_amount == pytest.approx(5000.0)
    assert enrichment.realized_pnl_rate == pytest.approx((68500 / 68000 - 1) * 100)
    fetcher.assert_awaited_once_with(need_sellable=False, need_cash=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_buy_position_uses_toss_position(monkeypatch):
    monkeypatch.setattr(
        "app.services.fill_enrichment.fetch_toss_portfolio_snapshot",
        AsyncMock(return_value=_snapshot(qty="30", avg="68100")),
    )

    enrichment = await fetch_fill_enrichment(_kr(side="bid"))

    assert enrichment is not None
    assert enrichment.position_qty == pytest.approx(30.0)
    assert enrichment.position_avg_price == pytest.approx(68100.0)
    assert enrichment.realized_pnl_amount is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_toss_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(
        "app.services.fill_enrichment.fetch_toss_portfolio_snapshot",
        AsyncMock(side_effect=RuntimeError("broker down")),
    )

    assert await fetch_fill_enrichment(_kr()) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_toss_position_returns_none(monkeypatch):
    monkeypatch.setattr(
        "app.services.fill_enrichment.fetch_toss_portfolio_snapshot",
        AsyncMock(return_value=SimpleNamespace(positions=[])),
    )

    assert await fetch_fill_enrichment(_kr()) is None
