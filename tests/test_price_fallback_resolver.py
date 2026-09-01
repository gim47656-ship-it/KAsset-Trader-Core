from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from app.services.invest_price_fallback import PriceFallbackResolver


def _fetcher(
    values: dict[str, float | None],
    *,
    calls: list[list[str]] | None = None,
    error: Exception | None = None,
) -> Callable[[list[str]], Awaitable[dict[str, float | None]]]:
    async def fetch(symbols: list[str]) -> dict[str, float | None]:
        if calls is not None:
            calls.append(list(symbols))
        if error is not None:
            raise error
        return {symbol: values.get(symbol) for symbol in symbols}

    return fetch


@pytest.mark.asyncio
async def test_toss_then_snapshot_only_for_missing_symbols() -> None:
    toss_calls: list[list[str]] = []
    snapshot_calls: list[list[str]] = []
    resolver = PriceFallbackResolver(
        toss_fetch=_fetcher({"A": 10.0}, calls=toss_calls),
        snapshot_fetch=_fetcher({"B": 20.0}, calls=snapshot_calls),
        market="us",
    )

    result = await resolver.resolve(["A", "B", "C"])

    assert result == {"A": 10.0, "B": 20.0, "C": None}
    assert toss_calls == [["A", "B", "C"]]
    assert snapshot_calls == [["B", "C"]]


@pytest.mark.asyncio
async def test_toss_failure_fails_open_to_snapshot() -> None:
    resolver = PriceFallbackResolver(
        toss_fetch=_fetcher({}, error=RuntimeError("toss down")),
        snapshot_fetch=_fetcher({"A": 9.0}),
        market="kr",
    )

    assert await resolver.resolve(["A"]) == {"A": 9.0}


@pytest.mark.asyncio
async def test_disabled_toss_uses_snapshot_and_empty_input_is_empty() -> None:
    resolver = PriceFallbackResolver(
        toss_fetch=None,
        snapshot_fetch=_fetcher({"A": 1.0}),
        market="kr",
    )

    assert await resolver.resolve(["A"]) == {"A": 1.0}
    assert await resolver.resolve([]) == {}
