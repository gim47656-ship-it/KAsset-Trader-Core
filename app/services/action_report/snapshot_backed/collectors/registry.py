"""Production collector registry for the snapshot-backed report generator.

This module assembles a :class:`SnapshotCollectorRegistry` populated with
the read-only collectors in this package. It is *separate* from
:func:`app.services.investment_snapshots.collectors.default_collector_registry`,
which intentionally remains empty (Phase 2 invariant) so existing callers
that rely on the bundle service for unrelated purposes are unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.investor_flow import (
    InvestorFlowSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.kr_market_ranking import (
    KrMarketRankingSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    AltseasonFn,
    IndexQuoteFn,
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsFetchFn,
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.brokers.upbit.orders import (
    fetch_open_orders as _upbit_fetch_open_orders,
)
from app.services.investment_snapshots.collectors import SnapshotCollectorRegistry


class _UpbitOpenOrdersAdapter:
    """Read-only adapter exposing only ``fetch_open_orders``.

    The Upbit broker module also exports order placement/cancellation
    functions. Wrapping just the read function here keeps the registry
    wiring intentionally narrow — the collector cannot reach mutation
    paths via the bound client.
    """

    @staticmethod
    async def fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return await _upbit_fetch_open_orders(market=market)


class _UpbitQuoteOrderbookAdapter:
    """Public Upbit orderbook read adapter for per-symbol crypto liquidity.

    ``last_price``는 orderbook에 없으므로 ``None``으로 두고 spread/depth만
    제공한다. 계좌·주문 mutation 경로는 노출하지 않는다.
    """

    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]:
        _ = venue  # Upbit has a single venue; argument kept for protocol parity.
        # Lazy import keeps httpx / the Upbit module out of the registry import
        # graph (mirrors the news / index fns) and narrow to the read function.
        from app.services.upbit_orderbook import fetch_orderbook

        raw = await fetch_orderbook(symbol)
        units = (raw or {}).get("orderbook_units") or []
        top = units[0] if units else {}
        timestamp = (raw or {}).get("timestamp")
        return {
            "last_price": None,
            "best_bid": top.get("bid_price"),
            "best_ask": top.get("ask_price"),
            "bid_depth": top.get("bid_size"),
            "ask_depth": top.get("ask_size"),
            "venue": "upbit",
            "as_of": str(timestamp) if timestamp else None,
            "session": "24h",
            "nxt_eligible": False,
        }


def _build_market_index_quote_fn() -> IndexQuoteFn:
    """Read-only adapter over the deterministic fundamentals index source.

    Given index symbols, returns one row per resolved index by calling the
    yfinance/Naver-backed ``get_market_index`` handler per symbol (concurrently).
    Fail-open per symbol: a symbol whose fetch errors is simply omitted. The
    handler is imported lazily so the heavy yfinance dependency is not pulled at
    registry import time, and this stays a thin pass-through (the per-market
    symbol selection lives in the collector). No order/mutation surface.
    """

    async def _index_quote_fn(symbols: list[str]) -> list[dict[str, Any]]:
        import asyncio

        from app.mcp_server.tooling.fundamentals._market_index import (
            handle_get_market_index,
        )

        async def _one(sym: str) -> list[dict[str, Any]]:
            try:
                result = await handle_get_market_index(
                    symbol=sym, period="day", count=1
                )
            except Exception:  # noqa: BLE001 — best-effort index quote
                return []
            if not isinstance(result, dict):
                return []
            return [r for r in (result.get("indices") or []) if isinstance(r, dict)]

        gathered = await asyncio.gather(*[_one(sym) for sym in symbols])
        return [row for rows in gathered for row in rows]

    return _index_quote_fn


def _build_altseason_fn() -> AltseasonFn:
    """Read-only adapter over the Upbit altseason source (ROB-381 PR3).

    Returns the UBAI/UBMI ratio + 24h alt-vs-BTC breadth snapshot. Failures are
    deliberately allowed to reach ``MarketEventsSnapshotCollector`` so it can
    retain the original diagnostic while degrading only the optional breadth
    field. No order/mutation surface.
    """

    async def _altseason_fn() -> dict[str, Any] | None:
        from app.services.external.upbit_index import fetch_upbit_altseason

        return await fetch_upbit_altseason()

    return _altseason_fn


def _build_news_fetch_fn() -> NewsFetchFn:
    """Per-symbol on-demand news adapter over ``symbol_news_service`` (ROB-423).

    Given (symbol, market, limit) returns a normalized ``SymbolNewsFetchResult``.
    Imported lazily; no MCP/LLM/order surface. The collector wraps the call so a
    fetch error degrades the optional ``news`` kind without blocking the bundle.
    """

    async def _news_fetch_fn(symbol: str, market: str, limit: int):
        from app.services.symbol_news_service import fetch_symbol_news

        return await fetch_symbol_news(symbol, market, limit=limit)

    return _news_fetch_fn


def production_collector_registry(session: AsyncSession) -> SnapshotCollectorRegistry:
    """Return a populated registry for the snapshot-backed generator.

    Required-kind collectors are wired to read-only DB-backed services.
    Optional-kind collectors are either thin DB readers (news) or
    fail-open stubs. Adding a new collector here is the single place
    needed to expose it to the generator.
    """
    registry = SnapshotCollectorRegistry()

    # Required kinds — DB-backed, read-only.
    registry.register(PortfolioSnapshotCollector(session))
    registry.register(JournalSnapshotCollector(session))
    registry.register(WatchContextSnapshotCollector(session))
    registry.register(
        MarketEventsSnapshotCollector(
            session,
            index_quote_fn=_build_market_index_quote_fn(),
            altseason_fn=_build_altseason_fn(),
        )
    )

    # Optional kinds — DB-backed where possible. ROB-366 B8 — wire the
    # market-aware news article source so US (and KR) bundles serve real
    # market-scoped news instead of an empty/KR-bleeding research feed.
    registry.register(
        NewsSnapshotCollector(session, news_fetch_fn=_build_news_fetch_fn())
    )
    # KR/US의 KIS 전용 quote/orderbook은 등록하지 않는다. Crypto는 공개
    # Upbit orderbook만 선택적으로 보강한다.
    registry.register(
        SymbolSnapshotCollector(
            session,
            upbit_quote_client=_UpbitQuoteOrderbookAdapter(),
        )
    )
    registry.register(CandidateUniverseSnapshotCollector(session))
    registry.register(KrMarketRankingSnapshotCollector(session))
    registry.register(InvestorFlowSnapshotCollector(session))
    registry.register(InvestPageSnapshotCollector(session))
    # Remote-debug probes remain fail-open stubs — they are operator-driven
    # only, and automated wiring is intentionally out of scope.
    registry.register(NaverRemoteDebugStubCollector())
    registry.register(TossRemoteDebugStubCollector())
    registry.register(BrowserProbeStubCollector())

    # Equity는 Toss OPEN orders, crypto는 좁은 Upbit read adapter를 사용한다.
    registry.register(
        PendingOrdersSnapshotCollector(
            upbit_client=_UpbitOpenOrdersAdapter(),
        )
    )

    return registry
