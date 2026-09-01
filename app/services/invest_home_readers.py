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
    InvestHomeHiddenCounts,
    InvestHomeWarning,
    PriceStateLiteral,
)
from app.services.brokers.upbit.client import (
    fetch_multiple_current_prices,
    fetch_my_coins,
)
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.invest_home_service import (
    _SourceFetchResult,
    build_account_from_holdings,
)
from app.services.invest_quote_service import InvestQuoteService
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
from app.services.upbit_symbol_universe_service import (
    get_active_upbit_markets,
    get_upbit_warning_markets,
)

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


class UpbitHomeReader:
    """Upbit read-only reader."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _fetch_current_prices(self, market_codes: list[str]) -> dict[str, float]:
        """Fetch Upbit prices without letting one delisted code blank the whole batch."""

        try:
            prices = await fetch_multiple_current_prices(market_codes)
        except Exception:
            prices = {}

        missing_codes = [code for code in market_codes if code not in prices]
        if not missing_codes:
            return prices

        for code in missing_codes:
            try:
                single = await fetch_multiple_current_prices([code])
            except Exception:
                continue
            if code in single:
                prices[code] = single[code]
        return prices

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            coins = await fetch_my_coins()
            krw_row = next((c for c in coins if c.get("currency") == "KRW"), None)

            crypto_rows = [
                c
                for c in coins
                if str(c.get("currency")) != "KRW"
                and (float(c.get("balance", 0) or 0) + float(c.get("locked", 0) or 0))
                > 0
            ]

            # Inactive filter
            active_markets = await get_active_upbit_markets(
                self._db, quote_currency="KRW"
            )
            caution_markets = await get_upbit_warning_markets(
                self._db, quote_currency="KRW"
            )

            tradable_rows = []
            inactive_rows = []
            for c in crypto_rows:
                market_code = f"KRW-{c.get('currency')}"
                if market_code in active_markets and market_code not in caution_markets:
                    tradable_rows.append(c)
                else:
                    inactive_rows.append(c)

            market_codes = [f"KRW-{c.get('currency')}" for c in tradable_rows]
            price_warning: InvestHomeWarning | None = None
            current_prices: dict[str, float] = {}
            if market_codes:
                try:
                    current_prices = await self._fetch_current_prices(market_codes)
                except Exception as exc:
                    logger.warning("Upbit price fetch failed: %s", exc, exc_info=True)
                    price_warning = InvestHomeWarning(
                        source="upbit",
                        message="코인 평가금액 산출을 위한 현재가 조회에 실패했습니다.",
                    )
                missing_price_codes = sorted(set(market_codes) - set(current_prices))
                if missing_price_codes and price_warning is None:
                    logger.warning(
                        "Upbit missing current prices for %s",
                        ",".join(missing_price_codes),
                    )
                    price_warning = InvestHomeWarning(
                        source="upbit",
                        message="일부 코인은 현재가가 없어 평가금액에서 제외했습니다.",
                    )

            holdings = []
            hidden_holdings = []
            hidden_counts = InvestHomeHiddenCounts()
            hidden_counts.upbitInactive = len(inactive_rows)

            # Process inactive
            for c in inactive_rows:
                currency = str(c.get("currency"))
                qty = float(c.get("balance", 0)) + float(c.get("locked", 0))
                hidden_holdings.append(
                    Holding(
                        holdingId=f"upbit:hidden:{currency}",
                        accountId="upbit_account",
                        source="upbit",
                        accountKind="live",
                        symbol=currency,
                        market="CRYPTO",
                        assetType="crypto",
                        assetCategory="crypto",
                        displayName=currency,
                        quantity=qty,
                        currency="KRW",
                        priceState="missing",
                    )
                )

            # Process tradable
            for c in tradable_rows:
                currency = str(c.get("currency"))
                market_code = f"KRW-{currency}"
                qty = float(c.get("balance", 0)) + float(c.get("locked", 0))
                avg_price = float(c.get("avg_buy_price", 0))
                current_price = current_prices.get(market_code)
                value_krw = qty * current_price if current_price is not None else None
                cost_basis = qty * avg_price if avg_price > 0 else None
                pnl_krw = (
                    value_krw - cost_basis
                    if value_krw is not None and cost_basis is not None
                    else None
                )
                pnl_rate = (
                    pnl_krw / cost_basis
                    if pnl_krw is not None and cost_basis is not None and cost_basis > 0
                    else None
                )

                h = Holding(
                    holdingId=f"upbit:{currency}",
                    accountId="upbit_account",
                    source="upbit",
                    accountKind="live",
                    symbol=currency,
                    market="CRYPTO",
                    assetType="crypto",
                    assetCategory="crypto",
                    displayName=currency,
                    quantity=qty,
                    averageCost=avg_price if avg_price > 0 else None,
                    costBasis=cost_basis,
                    currency="KRW",
                    valueNative=value_krw,
                    valueKrw=value_krw,
                    pnlKrw=pnl_krw,
                    pnlRate=pnl_rate,
                    priceState="live" if current_price is not None else "missing",
                )

                if value_krw is not None and value_krw < 5000:
                    hidden_holdings.append(h)
                    hidden_counts.upbitDust += 1
                else:
                    holdings.append(h)

            priced_holdings = [h for h in holdings if h.valueKrw is not None]
            coin_value_krw = sum(
                h.valueKrw for h in priced_holdings if h.valueKrw is not None
            )
            priced_cost_vals = [h.costBasis for h in priced_holdings]
            coin_cost_basis_krw = (
                sum(v for v in priced_cost_vals if v is not None)
                if priced_cost_vals and all(v is not None for v in priced_cost_vals)
                else None
            )
            account_pnl_krw = (
                coin_value_krw - coin_cost_basis_krw
                if coin_cost_basis_krw is not None
                else None
            )
            account_pnl_rate = (
                account_pnl_krw / coin_cost_basis_krw
                if account_pnl_krw is not None and coin_cost_basis_krw > 0
                else None
            )
            account = Account(
                accountId="upbit_account",
                displayName="Upbit",
                source="upbit",
                accountKind="live",
                includedInHome=True,
                valueKrw=coin_value_krw,
                costBasisKrw=coin_cost_basis_krw,
                pnlKrw=account_pnl_krw,
                pnlRate=account_pnl_rate,
                cashBalances=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
                buyingPower=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
            )
            return _SourceFetchResult(
                accounts=[account],
                holdings=holdings,
                warning=price_warning,
                hidden_holdings=hidden_holdings,
                hidden_counts=hidden_counts,
            )
        except Exception as exc:
            logger.warning("Upbit fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="upbit", message=str(exc)),
            )


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


# ---------------------------------------------------------------------------
# ROB-238: Alpaca Paper reader
# ---------------------------------------------------------------------------


class AlpacaPaperHomeReader:
    """Alpaca Paper 계좌 read-only reader (ROB-238).

    Exposes only get_account / list_positions from AlpacaPaperBrokerService.
    Mutation methods (submit_order, cancel_order) are never imported or called.
    Returns paper-tagged rows excluded from home totals.
    """

    source: str = "alpaca_paper"

    def __init__(self) -> None:
        pass

    @staticmethod
    def _make_service() -> Any:
        from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

        return AlpacaPaperBrokerService()

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        from app.services.brokers.alpaca.exceptions import (
            AlpacaPaperConfigurationError,
            AlpacaPaperEndpointError,
        )

        try:
            svc = self._make_service()
        except AlpacaPaperConfigurationError:
            logger.info("Alpaca Paper reader: not configured, skipping")
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source="alpaca_paper",
                    message="Alpaca Paper 미설정 (자격증명 없음)",
                ),
            )
        except AlpacaPaperEndpointError as exc:
            logger.warning("Alpaca Paper endpoint error: %s", exc)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source="alpaca_paper",
                    message="Alpaca Paper 엔드포인트 오류",
                ),
            )
        except Exception as exc:
            logger.warning("Alpaca Paper service init failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source="alpaca_paper",
                    message="Alpaca Paper 초기화 실패",
                ),
            )

        try:
            account_snap = await svc.get_account()
            positions = await svc.list_positions()

            usd_krw_rate: float | None = None
            fx_warning: InvestHomeWarning | None = None
            try:
                usd_krw_rate = await get_usd_krw_rate()
            except Exception as exc:
                logger.warning("USD/KRW FX fetch failed for Alpaca: %s", exc)
                fx_warning = InvestHomeWarning(
                    source="alpaca_paper",
                    message="USD 환율 조회 실패 — KRW 환산 불가",
                )

            holdings: list[Holding] = []
            for pos in positions:
                qty = float(pos.qty)
                avg_price = float(pos.avg_entry_price)
                cost_basis_usd = qty * avg_price
                value_native: float | None = (
                    float(pos.market_value) if pos.market_value is not None else None
                )
                pnl_native: float | None = (
                    float(pos.unrealized_pl) if pos.unrealized_pl is not None else None
                )
                current_price: float | None = (
                    float(pos.current_price) if pos.current_price is not None else None
                )

                value_krw: float | None = (
                    value_native * usd_krw_rate
                    if value_native is not None and usd_krw_rate is not None
                    else None
                )
                pnl_krw: float | None = (
                    pnl_native * usd_krw_rate
                    if pnl_native is not None and usd_krw_rate is not None
                    else None
                )
                pnl_rate: float | None = (
                    pnl_native / cost_basis_usd
                    if pnl_native is not None and cost_basis_usd > 0
                    else None
                )

                holdings.append(
                    Holding(
                        holdingId=f"alpaca_paper:{pos.symbol}",
                        accountId="alpaca_paper_account",
                        source="alpaca_paper",
                        accountKind="paper",
                        symbol=pos.symbol,
                        market="US",
                        assetType="equity",
                        assetCategory="us_stock",
                        displayName=pos.symbol,
                        quantity=qty,
                        averageCost=avg_price if avg_price > 0 else None,
                        costBasis=cost_basis_usd if cost_basis_usd > 0 else None,
                        currency="USD",
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=pnl_rate,
                        priceState="live" if current_price is not None else "missing",
                    )
                )

            investment_value_krw = sum(
                h.valueKrw for h in holdings if h.valueKrw is not None
            )
            valued_holdings = [h for h in holdings if h.valueKrw is not None]
            cost_basis_krw_values = [
                h.costBasis * (h.valueKrw / h.valueNative)
                for h in valued_holdings
                if h.costBasis is not None
                and h.valueNative is not None
                and h.valueNative > 0
            ]
            account_cost_basis_krw = (
                sum(cost_basis_krw_values)
                if cost_basis_krw_values
                and len(cost_basis_krw_values) == len(valued_holdings)
                else None
            )
            account_pnl_krw = (
                investment_value_krw - account_cost_basis_krw
                if account_cost_basis_krw is not None
                else None
            )
            account_pnl_rate = (
                account_pnl_krw / account_cost_basis_krw
                if account_pnl_krw is not None and account_cost_basis_krw > 0
                else None
            )
            # Cash and buying power in USD — keep native, convert to KRW for account
            cash_usd = float(account_snap.cash)
            buying_power_usd = float(account_snap.buying_power)

            account = Account(
                accountId="alpaca_paper_account",
                displayName="Alpaca Paper",
                source="alpaca_paper",
                accountKind="paper",
                includedInHome=False,
                valueKrw=investment_value_krw,
                costBasisKrw=account_cost_basis_krw,
                pnlKrw=account_pnl_krw,
                pnlRate=account_pnl_rate,
                cashBalances=CashAmounts(usd=cash_usd),
                buyingPower=CashAmounts(usd=buying_power_usd),
            )
            return _SourceFetchResult(
                accounts=[account], holdings=holdings, warning=fx_warning
            )

        except Exception as exc:
            logger.warning("Alpaca Paper fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source="alpaca_paper",
                    message="Alpaca Paper 조회 실패",
                ),
            )
