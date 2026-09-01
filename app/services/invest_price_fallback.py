"""``/invest``용 Toss → 과거 snapshot fail-open 가격 체인."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.brokers.toss.dto import TossPrice

logger = logging.getLogger(__name__)

PriceMap = dict[str, float | None]
Fetcher = Callable[[list[str]], Awaitable[PriceMap]]

TOSS_FIRST_ORDER: tuple[str, ...] = ("toss", "snapshot")


class PriceFallbackResolver:
    """Toss에서 찾지 못한 가격을 과거 snapshot으로 보강한다."""

    def __init__(
        self,
        *,
        toss_fetch: Fetcher | None,
        snapshot_fetch: Fetcher,
        market: str,
    ) -> None:
        self._toss_fetch = toss_fetch
        self._snapshot_fetch = snapshot_fetch
        self._market = market
        self._order = TOSS_FIRST_ORDER

    async def resolve(self, symbols: list[str]) -> PriceMap:
        if not symbols:
            return {}
        results: PriceMap = dict.fromkeys(symbols, None)
        layers: dict[str, Fetcher | None] = {
            "toss": self._toss_fetch,
            "snapshot": self._snapshot_fetch,
        }
        missing = symbols  # first layer runs on the full list
        for name in self._order:
            fetch = layers[name]
            if fetch is None:  # e.g. Toss disabled -> skip this layer
                continue
            await self._apply_layer(name, fetch, missing, results)
            missing = self._missing(symbols, results)
            if not missing:
                return results
        return results

    async def _apply_layer(
        self, name: str, fetch: Fetcher, symbols: list[str], results: PriceMap
    ) -> None:
        try:
            fetched = await fetch(symbols)
        except Exception as exc:  # noqa: BLE001 — fail-open per layer
            logger.warning(
                "invest price fallback: %s layer failed for market=%s (%d symbols): %s",
                name,
                self._market,
                len(symbols),
                exc,
            )
            return
        resolved = 0
        for sym in symbols:
            price = fetched.get(sym)
            if price is not None and results.get(sym) is None:
                results[sym] = price
                resolved += 1
        logger.info(
            "invest price fallback: %s resolved %d/%d for market=%s",
            name,
            resolved,
            len(symbols),
            self._market,
        )

    @staticmethod
    def _missing(symbols: list[str], results: PriceMap) -> list[str]:
        return [s for s in symbols if results.get(s) is None]


_TOSS_PRICE_BATCH = 200


class TossPriceClient(Protocol):
    async def prices(self, symbols: list[str] | tuple[str, ...]) -> list[TossPrice]: ...


def _chunk(symbols: list[str], size: int = _TOSS_PRICE_BATCH) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


async def fetch_toss_batch_prices(
    client: TossPriceClient, symbols: list[str]
) -> dict[str, float | None]:
    """200개 이하 chunk마다 Toss ``/api/v1/prices``를 한 번 호출한다.

    호출이 실패하면 fail-open으로 빈 결과를 반환한다.
    """
    if not symbols:
        return {}
    # 대문자로 정규화된 provider 응답을 호출자가 요청한 symbol key에 다시 연결한다.
    by_upper = {s.upper(): s for s in symbols}
    out: dict[str, float | None] = {}
    try:
        for batch in _chunk([s.upper() for s in symbols]):
            for price in await client.prices(batch):
                requested = by_upper.get(str(price.symbol).upper())
                if requested is not None:
                    out[requested] = float(price.last_price)
    except Exception as exc:  # noqa: BLE001 — fail-open, resolver falls through
        logger.warning(
            "invest price fallback: toss batch prices failed (%d symbols): %s",
            len(symbols),
            exc,
        )
        return {}
    return out
