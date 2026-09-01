from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import DailyCandleSyncService, SyncTarget
from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 1100.0],
            "value": [101000.0, 112200.0],
        }
    )


def _repository(*, upserted: int = 2):
    repository = MagicMock()
    repository.upsert_rows = AsyncMock(return_value=upserted)
    repository.upsert_us_adjusted_close = AsyncMock(return_value=upserted)
    repository.session = MagicMock()
    repository.session.commit = AsyncMock()
    repository.session.rollback = AsyncMock()
    return repository


def _service(repository, *, kr=None, us=None, yahoo=None):
    return DailyCandleSyncService(
        repository=repository,
        toss_kr_fetcher=kr or AsyncMock(return_value=_frame()),
        toss_us_fetcher=us or AsyncMock(return_value=_frame()),
        yahoo_us_fetcher=yahoo or AsyncMock(return_value=[]),
        upbit_crypto_fetcher=AsyncMock(return_value=pd.DataFrame()),
    )


@pytest.mark.asyncio
async def test_kr_equity_uses_toss_and_persists_toss_source() -> None:
    repository = _repository()
    toss = AsyncMock(return_value=_frame())
    service = _service(repository, kr=toss)

    result = await service.sync_one(
        target=SyncTarget(MarketKey.KR, "005930", "KRX"), horizon_bars=2
    )

    toss.assert_awaited_once_with(symbol="005930", n=3)
    rows = repository.upsert_rows.await_args.kwargs["rows"]
    assert [row.source for row in rows] == ["toss", "toss"]
    assert result.rows_upserted == 2
    assert result.fallback_used is False
    repository.session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_us_equity_uses_toss_without_yahoo_fallback() -> None:
    repository = _repository()
    toss = AsyncMock(return_value=_frame())
    yahoo = AsyncMock()
    service = _service(repository, us=toss, yahoo=yahoo)

    result = await service.sync_one(
        target=SyncTarget(MarketKey.US, "AAPL", "NASD"), horizon_bars=2
    )

    toss.assert_awaited_once_with(symbol="AAPL", n=3)
    yahoo.assert_not_awaited()
    rows = repository.upsert_rows.await_args.kwargs["rows"]
    assert [row.source for row in rows] == ["toss", "toss"]
    assert repository.upsert_rows.await_args.kwargs["update_adj_close"] is False
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_empty_toss_response_is_not_synthesized_or_yahoo_rerouted() -> None:
    repository = _repository()
    yahoo = AsyncMock()
    service = _service(
        repository,
        us=AsyncMock(return_value=pd.DataFrame()),
        yahoo=yahoo,
    )

    result = await service.sync_one(
        target=SyncTarget(MarketKey.US, "AAPL", "NASD"), horizon_bars=2
    )

    assert result.rows_upserted == 0
    assert result.skipped_reason == "toss_empty"
    yahoo.assert_not_awaited()
    repository.upsert_rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_yahoo_is_used_only_for_explicit_adjusted_close_enrichment() -> None:
    repository = _repository(upserted=1)
    yahoo = AsyncMock(
        return_value=[
            YahooFallbackRow(
                time_utc=datetime(2020, 1, 2, tzinfo=UTC),
                symbol="AAPL",
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                adj_close=98.0,
                volume=1000.0,
                value=101000.0,
            )
        ]
    )
    service = _service(repository, yahoo=yahoo)

    count = await service.sync_us_adjusted_close(
        target=SyncTarget(MarketKey.US, "AAPL", "NASD"), horizon_bars=1
    )

    assert count == 1
    yahoo.assert_awaited_once_with(symbol="AAPL", n=1)
    row = repository.upsert_us_adjusted_close.await_args.kwargs["rows"][0]
    assert row.source == "yahoo_fallback"
    assert row.adj_close == 98.0
