"""토스 정규장 종가 조회의 세션별 캐시 계약 테스트."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.extensions.kasset.api.toss_market_data import TossSharedMarketData
from app.services.brokers.toss.market_calendar import TossSessionWindow


class _CandleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def candles(self, symbol: str, **kwargs: Any) -> object:
        self.calls.append((symbol, kwargs))
        before = datetime.fromisoformat(kwargs["before"])
        timestamp = before - timedelta(seconds=30)
        close = Decimal("1692000") if timestamp.day == 1 else Decimal("1700000")
        return SimpleNamespace(
            candles=[SimpleNamespace(timestamp=timestamp, close_price=close)]
        )

    async def prices(self, symbols: Sequence[str]) -> list[object]:
        del symbols
        return []

    async def aclose(self) -> None:
        return None


def _window(day: int) -> TossSessionWindow:
    return TossSessionWindow(
        start=datetime(2026, 9, day, 0, tzinfo=UTC),
        end=datetime(2026, 9, day, 6, 30, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_regular_closes_reuses_same_window_and_separates_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    client = _CandleClient()
    service = TossSharedMarketData(client_factory=lambda: client)
    symbols = ["000660", "005930"]

    try:
        first = await service.regular_closes(symbols, window=_window(1))
        repeated = await service.regular_closes(symbols, window=_window(1))
        next_day = await service.regular_closes(symbols, window=_window(2))
    finally:
        await service.aclose()

    assert (
        first
        == repeated
        == {
            "000660": Decimal("1692000"),
            "005930": Decimal("1692000"),
        }
    )
    assert next_day == {
        "000660": Decimal("1700000"),
        "005930": Decimal("1700000"),
    }
    assert [symbol for symbol, _ in client.calls] == [
        "000660",
        "005930",
        "000660",
        "005930",
    ]
    assert all(call[1]["interval"] == "1m" for call in client.calls)
    assert all(call[1]["count"] == 1 for call in client.calls)
