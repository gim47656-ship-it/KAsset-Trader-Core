from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.constants import (
    US_BENCHMARK_PARTITION,
    US_BENCHMARK_SYMBOL,
)
from app.services.daily_candles.sync_service import DailyCandleSyncService


def _rows(*pairs: tuple[str, str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(symbol=symbol, exchange=exchange) for symbol, exchange in pairs
    ]


@pytest.mark.asyncio
async def test_us_universe_targets_leave_spy_to_benchmark_sync() -> None:
    """운영 재현: 유니버스 경로가 SPY를 AMEX로 써서 NASD 벤치마크 행과 날짜가 겹쳤고,
    60세션 벤치마크 계산이 중복 timestamp에 fail-closed돼 US Daily Setup이 전부
    ``daily_relative_strength=unavailable``로 탈락했다."""

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _rows(("AAPL", "NASD"), (US_BENCHMARK_SYMBOL, "AMEX")),  # universe
                _rows((US_BENCHMARK_SYMBOL, "AMEX"), ("NVDA", "NASD")),  # watchlist
                [],  # cohort members
            ]
        )
    )
    unused = AsyncMock()
    service = DailyCandleSyncService(
        repository=SimpleNamespace(session=session),
        toss_kr_fetcher=unused,
        toss_us_fetcher=unused,
        yahoo_us_fetcher=unused,
        upbit_crypto_fetcher=unused,
    )

    targets = await service._resolve_universe(market="us")

    assert [(t.symbol, t.partition) for t in targets] == [
        ("AAPL", "NASD"),
        ("NVDA", "NASD"),
    ]
    assert US_BENCHMARK_PARTITION == "NASD"
