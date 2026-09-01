from __future__ import annotations

import pytest

from app.services.invest_price_fallback import PriceFallbackResolver


@pytest.mark.asyncio
async def test_toss_circuit_error_uses_snapshot() -> None:
    async def toss_fetch(_symbols: list[str]) -> dict[str, float | None]:
        raise RuntimeError("circuit open")

    async def snapshot_fetch(symbols: list[str]) -> dict[str, float | None]:
        return dict.fromkeys(symbols, 7.0)

    resolver = PriceFallbackResolver(
        toss_fetch=toss_fetch,
        snapshot_fetch=snapshot_fetch,
        market="us",
    )

    assert await resolver.resolve(["AAPL"]) == {"AAPL": 7.0}
