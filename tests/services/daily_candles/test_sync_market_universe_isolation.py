from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    DailyCandleSyncService,
    SyncOneResult,
    SyncTarget,
)


def _target(symbol: str) -> SyncTarget:
    return SyncTarget(market=MarketKey.US, symbol=symbol, partition="NASDAQ")


@pytest.mark.asyncio
async def test_sync_market_universe_continues_after_symbol_failure(monkeypatch):
    """운영 재현: 4번째 심볼의 Toss ``stock-not-found``가 US 유니버스 전체를 중단시켰다."""

    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    repository = SimpleNamespace(session=session)
    unused = AsyncMock()
    service = DailyCandleSyncService(
        repository=repository,
        toss_kr_fetcher=unused,
        toss_us_fetcher=unused,
        yahoo_us_fetcher=unused,
        upbit_crypto_fetcher=unused,
    )
    targets = [_target(symbol) for symbol in ("A", "AA", "AAA", "AAAU", "AAPL")]
    monkeypatch.setattr(service, "_resolve_universe", AsyncMock(return_value=targets))

    async def fake_sync_one(*, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        if target.symbol == "AAAU":
            raise RuntimeError("Toss API error status=404 code='stock-not-found'")
        return SyncOneResult(target=target, rows_upserted=2, fallback_used=False)

    monkeypatch.setattr(service, "sync_one", fake_sync_one)

    result = await service.sync_market_universe(market="us", horizon_bars=300)

    assert result["targets_total"] == 5
    assert result["rows_upserted"] == 8
    assert result["failed"] == 1
    assert result["failed_symbols"] == ["AAAU"]
    session.rollback.assert_awaited_once()
