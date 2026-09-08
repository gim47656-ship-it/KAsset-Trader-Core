"""ROB-123 — read-only adapters used by InvestHomeService.

각 reader 는 한 source 의 read-only 데이터만 가져온다.
broker mutation / order / watch / scheduler / worker 경로는 import / 호출 금지.
DB write / backfill 금지 — read-only 조회만 사용.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_upbit_symbol
from app.models.manual_holdings import MarketType
from app.schemas.invest_home import (
    Account,
    CashAmounts,
    Holding,
    InvestHomeWarning,
    PriceStateLiteral,
)
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.invest_home_service import (
    _SourceFetchResult,
    build_account_from_holdings,
)
from app.services.invest_quote_service import InvestQuoteService
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


def _toss_sellable_quantity(position: Any, mutations_enabled: bool) -> float | None:
    """Keep sellable quantity unknown on the general home read path.

    ROB-1310 keeps general home reads off the Toss sellable endpoint. Unknown
    sellability is therefore ``None``; even an accidental lower-layer value is
    not promoted into this display projection.
    """
    del position, mutations_enabled
    return None


def _toss_pending_sell_quantity(position: Any, mutations_enabled: bool) -> float:
    del position, mutations_enabled
    return 0.0


class TossApiHomeReader:
    """Toss Open API live portfolio reader."""

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        del user_id
        try:
            # ROB-549: keep tradeability gated on the live-mutation flag. ROB-1310
            # makes sellable quantity broker-adjacent; this general home reader
            # never fans out to Toss ORDER_INFO.
            from app.core.config import settings as _settings

            mutations_enabled = bool(
                getattr(_settings, "toss_live_order_mutations_enabled", False)
            )
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.snapshot",
            ) as span:
                snapshot = await fetch_toss_portfolio_snapshot(
                    need_sellable=False,
                )
                span.set_data("position_count", len(snapshot.positions))
                span.set_data("error_count", len(snapshot.errors))
            holdings: list[Holding] = []
            value_krw_total = 0.0
            cost_basis_krw_total: float | None = 0.0
            pnl_krw_total: float | None = 0.0
            warning_messages: list[str] = []

            usd_krw_rate: float | None = None
            if any(
                position.instrument_type == "equity_us"
                for position in snapshot.positions
            ):
                try:
                    with sentry_sdk.start_span(
                        op="invest.home.toss_api.phase",
                        name="invest.home.toss_api.fx",
                    ) as span:
                        usd_krw_rate = await get_usd_krw_rate()
                        span.set_tag("success", True)
                except Exception as exc:
                    logger.warning(
                        "USD/KRW FX fetch failed for Toss API reader: %s",
                        exc,
                        exc_info=True,
                    )
                    warning_messages.append(
                        "USD 보유 평가금액 환산을 위한 환율 조회에 실패했습니다."
                    )

            for position in snapshot.positions:
                currency = "KRW" if position.instrument_type == "equity_kr" else "USD"
                market = "KR" if position.instrument_type == "equity_kr" else "US"
                value_native = (
                    float(position.evaluation_amount)
                    if position.evaluation_amount is not None
                    else None
                )
                value_krw: float | None = None
                pnl_krw: float | None = None
                if currency == "KRW":
                    value_krw = value_native
                    pnl_krw = (
                        float(position.profit_loss)
                        if position.profit_loss is not None
                        else None
                    )
                elif usd_krw_rate is not None:
                    value_krw = (
                        value_native * usd_krw_rate
                        if value_native is not None
                        else None
                    )
                    pnl_krw = (
                        float(position.profit_loss) * usd_krw_rate
                        if position.profit_loss is not None
                        else None
                    )
                cost_basis = float(position.quantity * position.avg_buy_price)
                cost_basis_krw: float | None = None
                if currency == "KRW":
                    cost_basis_krw = cost_basis
                elif usd_krw_rate is not None:
                    cost_basis_krw = cost_basis * usd_krw_rate
                if value_krw is not None:
                    value_krw_total += value_krw
                if cost_basis_krw_total is not None and cost_basis_krw is not None:
                    cost_basis_krw_total += cost_basis_krw
                elif cost_basis_krw is None:
                    cost_basis_krw_total = None
                if pnl_krw_total is not None and pnl_krw is not None:
                    pnl_krw_total += pnl_krw
                elif pnl_krw is None:
                    pnl_krw_total = None

                holdings.append(
                    Holding(
                        holdingId=f"toss_api:{position.symbol}",
                        accountId="toss_api_account",
                        source="toss_api",
                        accountKind="live",
                        symbol=position.symbol,
                        market=market,
                        assetType="equity",
                        assetCategory="kr_stock" if market == "KR" else "us_stock",
                        displayName=position.name,
                        quantity=float(position.quantity),
                        averageCost=float(position.avg_buy_price),
                        costBasis=cost_basis,
                        currency=currency,
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=float(position.profit_rate)
                        if position.profit_rate is not None
                        else None,
                        priceState="live",
                        sourceOfTruth=True,
                        isTradeable=mutations_enabled,
                        manualOnly=False,
                        sellableQuantity=_toss_sellable_quantity(
                            position, mutations_enabled
                        ),
                        pendingSellQuantity=_toss_pending_sell_quantity(
                            position, mutations_enabled
                        ),
                        referenceQuantity=float(position.quantity),
                    )
                )

            pnl_rate: float | None = None
            if (
                cost_basis_krw_total
                and cost_basis_krw_total > 0
                and pnl_krw_total is not None
            ):
                pnl_rate = pnl_krw_total / cost_basis_krw_total

            account = Account(
                accountId="toss_api_account",
                displayName="Toss",
                source="toss_api",
                accountKind="live",
                includedInHome=True,
                valueKrw=value_krw_total,
                costBasisKrw=cost_basis_krw_total,
                pnlKrw=pnl_krw_total,
                pnlRate=pnl_rate,
                cashBalances=CashAmounts(
                    krw=float(snapshot.cash_krw)
                    if snapshot.cash_krw is not None
                    else None,
                    usd=float(snapshot.cash_usd)
                    if snapshot.cash_usd is not None
                    else None,
                ),
                buyingPower=CashAmounts(
                    # ROB-707: Toss GET /api/v1/buying-power exposes only
                    # cashBuyingPower (orderable cash). fetch_toss_cash_snapshot
                    # (ROB-696) already fetched it onto snapshot.cash_{krw,usd};
                    # surface it here. Fail-open: None per currency when the
                    # fetch failed (the error is already in snapshot.errors ->
                    # warning). cashBalances is left unchanged above.
                    krw=float(snapshot.cash_krw)
                    if snapshot.cash_krw is not None
                    else None,
                    usd=float(snapshot.cash_usd)
                    if snapshot.cash_usd is not None
                    else None,
                ),
            )
            warning = None
            if snapshot.errors:
                warning_messages.extend(
                    str(item.get("error")) for item in snapshot.errors
                )
            if warning_messages:
                warning = InvestHomeWarning(
                    source="toss_api",
                    message="; ".join(warning_messages),
                )
            return _SourceFetchResult(
                accounts=[account],
                holdings=holdings,
                warning=warning,
            )
        except Exception as exc:
            logger.warning("Toss API fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="toss_api", message=str(exc)),
            )


def _manual_quote_symbol(market_type: MarketType, ticker: str | None) -> str:
    """Quote-layer key for one manual holding.

    Crypto goes through the shared ``to_upbit_symbol`` helper (``BTC`` ->
    ``KRW-BTC``) because that is how the legacy quote contract keys crypto.
    KR/US keep the stored ticker; their DB spelling is already the repository
    convention and is normalized by ``app.core.symbol`` helpers at the broker
    seams, never by string surgery here.
    """

    raw = ticker or ""
    if market_type == MarketType.CRYPTO:
        return to_upbit_symbol(raw)
    return raw


class ManualHomeReader:
    """manual_holdings (Toss 등) read-only reader."""

    def __init__(
        self, db: AsyncSession, quote_service: InvestQuoteService | None = None
    ) -> None:
        self._db = db
        self._service = ManualHoldingsService(db)
        self._quote_service = quote_service

    @staticmethod
    def _source_for_broker(broker_type: str) -> str:
        # ROB-1310 R9 (B4): ``broker_accounts.broker_type`` is a free-form
        # column. An unrecognized value must never be silently attributed to
        # Toss -- ``manual_unknown`` is the explicit, truthful fallback so
        # provenance never lies about which broker a holding came from.
        return {
            "toss": "toss_manual",
            "samsung": "pension_manual",
            "isa": "isa_manual",
            "kis": "kis_manual",
            "upbit": "upbit_manual",
        }.get(broker_type, "manual_unknown")

    async def fetch_held_pairs(self, *, user_id: int) -> list[tuple[str, str]]:
        """Read manual held keys without quote/FX enrichment for calendar."""

        from app.services.portfolio_snapshot import (
            HELD_KEY_MARKETS,
            held_key_symbol,
        )

        raw_holdings = await self._service.get_holdings_by_user(user_id)
        pairs: set[tuple[str, str]] = set()
        for holding in raw_holdings:
            if float(holding.quantity or 0) <= 0:
                continue
            market = str(holding.market_type).lower()
            if market not in HELD_KEY_MARKETS:
                continue
            # ROB-1310: one market-aware seam for every held-key projection.
            symbol = held_key_symbol(market, holding.ticker or "")
            if symbol:
                pairs.add((market, symbol))
        return sorted(pairs)

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            with sentry_sdk.start_span(
                op="invest.home.manual.phase",
                name="invest.home.manual.load_holdings",
            ) as span:
                raw_holdings = await self._service.get_holdings_by_user(user_id)
                span.set_data("raw_holding_count", len(raw_holdings))

            manual_holdings = list(raw_holdings)

            # ROB-1310 R8: a manual CRYPTO holding may be stored as the bare
            # base coin (``BTC``), but the legacy quote contract keys crypto as
            # ``KRW-BTC`` and ``PriceFallbackResolver.resolve`` seeds
            # ``dict.fromkeys(symbols, None)`` -- it only ever returns keys the
            # caller requested. Requesting the raw coin therefore makes the
            # ``KRW-BTC`` read below structurally unable to hit. Normalize
            # through the shared ``to_upbit_symbol`` helper *before* the
            # request (never by string surgery); KR/US keys are unchanged.
            kr_tickers = [
                _manual_quote_symbol(h.market_type, h.ticker)
                for h in manual_holdings
                if h.market_type in {MarketType.KR, MarketType.CRYPTO}
            ]
            us_tickers = [
                h.ticker for h in manual_holdings if h.market_type == MarketType.US
            ]

            kr_prices: dict[str, float | None] = {}
            us_prices: dict[str, float | None] = {}
            usd_krw_rate: float | None = None

            if self._quote_service:
                quote_service = self._quote_service

                async def _fetch_kr_prices() -> dict[str, float | None]:
                    try:
                        with sentry_sdk.start_span(
                            op="invest.home.manual.phase",
                            name="invest.home.manual.fetch_kr_prices",
                        ) as span:
                            span.set_data("ticker_count", len(kr_tickers))
                            prices = await quote_service.fetch_kr_prices(kr_tickers)
                            span.set_data("price_count", len(prices))
                            return prices
                    except Exception:
                        # ROB-1310 R9 (B1): a KR/CRYPTO quote-provider failure
                        # must not fall through to the reader's outer
                        # catch-all -- that would discard every manual
                        # holding/account and misattribute the failure to a
                        # hardcoded source. Every KR/CRYPTO holding simply
                        # reports missing below and the per-source warning
                        # loop attributes it correctly; the concurrent US
                        # fetch is unaffected. No exception text/trace logged.
                        logger.warning("Manual KR/CRYPTO quote fetch failed (isolated)")
                        return {}

                async def _fetch_us_prices() -> dict[str, float | None]:
                    try:
                        with sentry_sdk.start_span(
                            op="invest.home.manual.phase",
                            name="invest.home.manual.fetch_us_prices",
                        ) as span:
                            span.set_data("ticker_count", len(us_tickers))
                            prices = await quote_service.fetch_us_prices(us_tickers)
                            span.set_data("price_count", len(prices))
                            return prices
                    except Exception:
                        # ROB-1310 R9 (B1): same isolation as the KR fetch --
                        # a US provider failure must not discard KR/CRYPTO
                        # valuations that already succeeded.
                        logger.warning("Manual US quote fetch failed (isolated)")
                        return {}

                # ROB-702: KR and US price fetches are independent — run them
                # concurrently so the manual reader's wall time is max(kr, us),
                # not kr + us (~7s -> ~3.5s). ROB-1310 R9 (B1): each fetch now
                # catches its own failure and returns {} instead of letting
                # gather propagate -- a failure in one market must not discard
                # the other market's already-successful prices.
                kr_prices, us_prices = await asyncio.gather(
                    _fetch_kr_prices(), _fetch_us_prices()
                )

                if us_tickers:
                    try:
                        with sentry_sdk.start_span(
                            op="invest.home.manual.phase",
                            name="invest.home.manual.fx",
                        ) as span:
                            usd_krw_rate = await get_usd_krw_rate()
                            span.set_tag("success", True)
                    except Exception:
                        logger.warning("FX fetch failed for ManualHomeReader")

            holdings = []
            # ROB-1310 R8: W2 widened manual holdings past Toss, so a failed
            # valuation must name the manual source it actually happened to.
            unpriced_sources: set[str] = set()
            holding_sources: set[str] = set()

            for h in manual_holdings:
                qty = float(h.quantity)
                avg_price = float(h.avg_price) if h.avg_price else None
                cost_basis = (qty * avg_price) if avg_price else None
                market = {
                    MarketType.KR: "KR",
                    MarketType.US: "US",
                    MarketType.CRYPTO: "CRYPTO",
                }.get(h.market_type)
                if market is None:
                    continue
                currency = "USD" if market == "US" else "KRW"
                quote_symbol = _manual_quote_symbol(h.market_type, h.ticker)

                price = (
                    kr_prices.get(quote_symbol)
                    if market in {"KR", "CRYPTO"}
                    else us_prices.get(h.ticker)
                )
                if price is None and market == "CRYPTO":
                    # Compatibility only: read a raw-coin key back if some
                    # other producer still returns one. Never the request key.
                    price = kr_prices.get(h.ticker)
                price_state: PriceStateLiteral = (
                    "live" if price is not None else "missing"
                )

                value_native = qty * price if price is not None else None
                value_krw: float | None = None
                if value_native is not None:
                    if currency == "KRW":
                        value_krw = value_native
                    elif usd_krw_rate:
                        value_krw = value_native * usd_krw_rate

                holding_source = self._source_for_broker(
                    str(getattr(h.broker_account, "broker_type", "toss")).lower()
                )
                holding_sources.add(holding_source)
                if price is None and (kr_tickers or us_tickers):
                    unpriced_sources.add(holding_source)

                pnl_krw: float | None = None
                pnl_rate: float | None = None
                if value_krw is not None and cost_basis is not None:
                    # For US, cost_basis is in USD. We need cost_basis_krw for pnl_krw.
                    if currency == "KRW":
                        pnl_krw = value_krw - cost_basis
                        if cost_basis > 0:
                            pnl_rate = pnl_krw / cost_basis
                    elif usd_krw_rate:
                        cost_basis_krw = cost_basis * usd_krw_rate
                        pnl_krw = value_krw - cost_basis_krw
                        if cost_basis_krw > 0:
                            pnl_rate = pnl_krw / cost_basis_krw

                holdings.append(
                    Holding(
                        holdingId=f"manual:{h.id}",
                        accountId=str(h.broker_account_id),
                        source=holding_source,
                        accountKind="manual",
                        symbol=h.ticker,
                        market=market,
                        assetType="crypto" if market == "CRYPTO" else "equity",
                        assetCategory=(
                            "crypto"
                            if market == "CRYPTO"
                            else "kr_stock"
                            if market == "KR"
                            else "us_stock"
                        ),
                        displayName=h.display_name or h.ticker,
                        quantity=qty,
                        averageCost=avg_price,
                        costBasis=cost_basis,
                        currency=currency,
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=pnl_rate,
                        priceState=price_state,
                        sourceOfTruth=False,
                        isTradeable=False,
                        manualOnly=True,
                        sellableQuantity=0.0,
                        pendingSellQuantity=0.0,
                        referenceQuantity=qty,
                    )
                )

            manual_accounts: list[Account] = []
            account_names = {
                str(h.broker_account_id): str(
                    getattr(h.broker_account, "account_name", None) or "기본 계좌"
                )
                for h in manual_holdings
            }
            account_sources = {
                str(h.broker_account_id): self._source_for_broker(
                    str(getattr(h.broker_account, "broker_type", "toss")).lower()
                )
                for h in manual_holdings
            }
            # ROB-1310 SHOULD-1: build one Account per DB manual account
            # regardless of whether any holding in it (or any manual holding
            # at all) currently has a known price. The DB account identity
            # (id/displayName/source) must not depend on price availability —
            # a temporarily-unpriced account must not flip to a different
            # hardcoded canonical id/name downstream (MCP projection). The
            # value math still only counts priced holdings and never
            # fabricates a value from cost basis (build_account_from_holdings
            # sums to 0.0, not a guess, when nothing is priced).
            for account_id, account_name in account_names.items():
                account_holdings = [
                    holding for holding in holdings if holding.accountId == account_id
                ]
                manual_accounts.append(
                    build_account_from_holdings(
                        account_id=account_id,
                        display_name=account_name,
                        source=account_sources[account_id],
                        holdings=account_holdings,
                    )
                )

            # One warning per affected manual source, sorted so a batch always
            # reports the same way. The message stays fixed sanitized text --
            # it never carries an exception, a payload or a credential.
            manual_warnings: list[InvestHomeWarning] = []
            if unpriced_sources:
                manual_warnings = [
                    InvestHomeWarning(
                        source=source,
                        message=(
                            "일부 수동 보유는 현재가 조회에 실패해 평가에서 "
                            "제외했습니다."
                        ),
                    )
                    for source in sorted(unpriced_sources)
                ]
            elif not (kr_tickers or us_tickers) and holdings:
                # This case shouldn't happen with the logic above, but for safety
                manual_warnings = [
                    InvestHomeWarning(
                        source=source,
                        message="수동 보유는 현재가가 없어 평가금액에서 제외했습니다.",
                    )
                    for source in sorted(holding_sources)
                ]

            return _SourceFetchResult(
                accounts=manual_accounts,
                holdings=holdings,
                warning=manual_warnings[0] if manual_warnings else None,
                extra_warnings=manual_warnings[1:],
            )
        except Exception:
            # ROB-1310 R9 (B1): quote-provider failures are isolated above and
            # never reach this branch. This is the genuinely catastrophic
            # case -- e.g. the holdings load itself fails -- where no
            # holding/broker is even known yet. ``toss_manual`` would be a
            # false attribution here; ``manual_unknown`` is the only truthful
            # source, and the raw exception text/trace must never leak.
            logger.warning("Manual holdings fetch failed before any source was known")
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source="manual_unknown",
                    message="수동 보유 조회에 실패했습니다.",
                ),
            )
