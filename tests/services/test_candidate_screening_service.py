"""ROB-117 — Candidate screening service tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.candidate_screening_service import CandidateScreeningService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wraps_screen_stocks_and_annotates_held(monkeypatch) -> None:
    fake_screen = AsyncMock(
        return_value={
            "stocks": [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "close": 123.4,
                    "volume_24h": 123456.0,
                    "trade_amount_24h": 0.0,
                    "volume_ratio": None,
                    "rsi": 28.5,
                    "market_warning": "KRW-BTC ticker not found",
                },
                {"symbol": "KRW-ETH", "name": "이더리움", "rsi": 32.1},
            ],
            "meta": {"rsi_enrichment": {"attempted": 2, "succeeded": 1}},
            "warnings": ["rsi_enrichment_skipped"],
        }
    )
    db = MagicMock()
    service = CandidateScreeningService(db)
    monkeypatch.setattr(service, "_screen_stocks", fake_screen)
    monkeypatch.setattr(
        service, "_load_held_symbols", AsyncMock(return_value={"KRW-BTC"})
    )

    res = await service.screen(
        user_id=1, market="crypto", strategy="oversold", sort_by=None, limit=10
    )

    assert res.total == 2
    btc = next(c for c in res.candidates if c.symbol == "KRW-BTC")
    eth = next(c for c in res.candidates if c.symbol == "KRW-ETH")
    assert btc.price == pytest.approx(123.4)
    assert btc.volume == pytest.approx(123456.0)
    assert btc.is_held is True
    assert eth.is_held is False
    assert "rsi_enrichment_skipped" in res.warnings
    assert res.rsi_enrichment_attempted == 2
    assert res.rsi_enrichment_succeeded == 1
    assert any("KRW-BTC" in w for w in btc.data_warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_passes_filters_through(monkeypatch) -> None:
    fake_screen = AsyncMock(return_value={"stocks": [], "warnings": []})
    service = CandidateScreeningService(MagicMock())
    monkeypatch.setattr(service, "_screen_stocks", fake_screen)
    monkeypatch.setattr(service, "_load_held_symbols", AsyncMock(return_value=set()))

    await service.screen(
        user_id=1,
        market="kr",
        strategy="momentum",
        sort_by="change_rate",
        limit=20,
        max_per=15.0,
        adv_krw_min=1_000_000_000,
    )
    fake_screen.assert_awaited_once()
    kwargs = fake_screen.await_args.kwargs
    assert kwargs["market"] == "kr"
    assert kwargs["strategy"] == "momentum"
    assert kwargs["sort_by"] == "change_rate"
    assert kwargs["limit"] == 20
    assert kwargs["max_per"] == pytest.approx(15.0)
    assert kwargs["adv_krw_min"] == 1_000_000_000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_held_symbols_uses_toss_merged_quantity_and_owner_scope(
    monkeypatch,
) -> None:
    from app.services.merged_portfolio_service import MergedPortfolioService

    domestic = AsyncMock(
        return_value=[
            SimpleNamespace(ticker="005930", total_quantity=3),
            SimpleNamespace(ticker="000660", total_quantity=0),
        ]
    )
    overseas = AsyncMock(
        return_value=[SimpleNamespace(ticker="aapl", total_quantity=2)]
    )
    monkeypatch.setattr(
        MergedPortfolioService, "get_merged_portfolio_domestic", domestic
    )
    monkeypatch.setattr(
        MergedPortfolioService, "get_merged_portfolio_overseas", overseas
    )
    fetch_crypto = AsyncMock(
        return_value=[SimpleNamespace(ticker="KRW-BTC", quantity=1)]
    )
    monkeypatch.setattr(
        "app.services.upbit_holdings_service.fetch_upbit_holdings_for_user",
        fetch_crypto,
    )

    service = CandidateScreeningService(MagicMock())
    held = await service._load_held_symbols(user_id=42, market="all")

    assert held == {"005930", "AAPL", "KRW-BTC"}
    domestic.assert_awaited_once_with(42)
    overseas.assert_awaited_once_with(42)
    fetch_crypto.assert_awaited_once_with(service.db, 42)
