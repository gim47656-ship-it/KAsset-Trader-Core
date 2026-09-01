"""투자 평가에 쓰는 Toss → 과거 snapshot 읽기 전용 시세 서비스."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.brokers.toss.client import TossReadClient
from app.services.invest_price_fallback import (
    Fetcher,
    PriceFallbackResolver,
    fetch_toss_batch_prices,
)
from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
)
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    get_us_exchange_by_symbol,
)

logger = logging.getLogger(__name__)


class InvestQuoteService:
    """Toss를 먼저 조회하고 DB snapshot으로 보강하는 읽기 전용 평가 가격."""

    def __init__(
        self,
        db: AsyncSession,
        toss_client: TossReadClient | None = None,
    ) -> None:
        self._db = db
        self._toss_client = toss_client

    async def fetch_kr_prices(self, symbols: list[str]) -> dict[str, float | None]:
        return await self._resolve(symbols, market="kr")

    async def fetch_us_prices(self, symbols: list[str]) -> dict[str, float | None]:
        eligible: list[str] = []
        results: dict[str, float | None] = dict.fromkeys(symbols, None)
        for symbol in symbols:
            try:
                await get_us_exchange_by_symbol(symbol, self._db)
            except (USSymbolNotRegisteredError, USSymbolInactiveError):
                continue
            eligible.append(symbol)
        results.update(await self._resolve(eligible, market="us"))
        return results

    async def _resolve(
        self,
        symbols: list[str],
        *,
        market: str,
    ) -> dict[str, float | None]:
        if not symbols:
            return {}
        toss_fetch, owned = self._build_toss_fetch()
        try:
            resolver = PriceFallbackResolver(
                toss_fetch=toss_fetch,
                snapshot_fetch=lambda syms: self._snapshot_latest(market, syms),
                market=market,
            )
            return await resolver.resolve(symbols)
        finally:
            if owned is not None:
                await owned.aclose()

    def _build_toss_fetch(self) -> tuple[Fetcher | None, TossReadClient | None]:
        if self._toss_client is not None:
            client = self._toss_client
            return (lambda syms: fetch_toss_batch_prices(client, syms), None)
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return (None, None)
        try:
            client = TossReadClient.from_settings()
        except Exception as exc:
            logger.warning(
                "invest price fallback: Toss client construction failed; "
                "skipping Toss layer: %s",
                exc,
            )
            return (None, None)
        return (lambda syms: fetch_toss_batch_prices(client, syms), client)

    async def _snapshot_latest(
        self,
        market: str,
        symbols: list[str],
    ) -> dict[str, float | None]:
        try:
            found = await MarketQuoteSnapshotsRepository(self._db).latest_prices(
                market,
                symbols,
            )
        except Exception as exc:
            logger.warning("invest price snapshot read failed (%s): %s", market, exc)
            return {}
        return dict(found)
