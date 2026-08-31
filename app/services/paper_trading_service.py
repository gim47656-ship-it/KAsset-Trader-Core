"""Paper Trading Service — virtual account/order/position management."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import quantize_crypto_qty as _q_crypto_qty
from app.core.money import quantize_money as _q_money
from app.core.money import quantize_pct as _q_pct
from app.core.timezone import now_kst
from app.extensions.kasset.models import KAssetPaperPositionState
from app.models.paper_trading import (
    PaperAccount,
    PaperDailySnapshot,
    PaperPosition,
    PaperTrade,
)
from app.models.trading import InstrumentType
from app.services.brokers.upbit.client import fetch_multiple_current_prices

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")

# ---------------------------------------------------------------------------
# Currency partitioning
#
# Paper accounts hold exactly two cash ledgers (``cash_krw``, ``cash_usd``), so
# every reported money metric belongs to one of these currencies. Values from
# different currencies are never added: there is no FX rate in the paper ledger
# and inventing one would fabricate performance.
# ---------------------------------------------------------------------------
REPORTED_CURRENCIES: tuple[str, ...] = ("KRW", "USD")

# Stable, redacted valuation failure codes. Provider exception text is
# provider-controlled and may carry request detail, so it never reaches a
# caller; it is logged by exception class name only.
VALUATION_ERROR_QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
VALUATION_ERROR_QUOTE_INVALID = "QUOTE_INVALID"
VALUATION_ERROR_COST_BASIS_UNAVAILABLE = "COST_BASIS_UNAVAILABLE"

# Upbit's ticker response carries no provider timestamp, so a crypto quote has
# no ``as_of``. Reporting the server clock instead would forge provenance.
UPBIT_QUOTE_SOURCE = "UPBIT_TICKER"

# A quote older than this is reported stale. The rule depends only on the
# provider's own ``as_of`` and the observation moment, so two readers of the
# same quote at the same moment always agree; an unknown ``as_of`` stays
# unknown (``None``) instead of defaulting to fresh.
PAPER_QUOTE_STALE_AFTER = timedelta(minutes=15)

_SNAPSHOT_EQUITY_COLUMNS: dict[str, str] = {
    "KRW": "equity_krw",
    "USD": "equity_usd",
}
_SNAPSHOT_RETURN_COLUMNS: dict[str, str] = {
    "KRW": "daily_return_krw_pct",
    "USD": "daily_return_usd_pct",
}
_SNAPSHOT_COMPLETE_COLUMNS: dict[str, str] = {
    "KRW": "valuation_complete_krw",
    "USD": "valuation_complete_usd",
}


def position_currency(instrument_type: str) -> str:
    """Cash-ledger currency used to settle and report a holding.

    US equities debit the USD ledger. Every other supported paper instrument,
    including crypto pairs, debits the KRW ledger in ``execute_order`` and must
    stay in that same reporting bucket.
    """
    return "USD" if instrument_type == "equity_us" else "KRW"


def parse_quote_as_of(value: object) -> datetime | None:
    """Provider quote timestamp as an aware datetime, or ``None`` if absent.

    Never substitutes the current time: an unparseable or missing timestamp
    stays missing so ``quote_is_stale`` reports unknown rather than fresh.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PositionQuote:
    """A price plus the provenance that proves where and when it came from."""

    price: Decimal
    source: str
    as_of: datetime | None
    session: str | None

    def is_stale(self, *, now: datetime) -> bool | None:
        if self.as_of is None:
            return None
        return (now - self.as_of) > PAPER_QUOTE_STALE_AFTER


@dataclass(slots=True)
class CurrencyValuation:
    """Live-position valuation for a single currency.

    ``positions_value``/``unrealized_pnl`` only accumulate positions that were
    actually valued; ``valuation_complete`` tells a reader whether the totals
    describe the whole currency or just part of it.
    """

    positions_count: int = 0
    positions_valued: int = 0
    positions_value: Decimal = _ZERO
    total_invested: Decimal = _ZERO
    unrealized_pnl: Decimal = _ZERO

    @property
    def valuation_complete(self) -> bool:
        return self.positions_valued == self.positions_count


def snapshot_equity(snapshot: PaperDailySnapshot, currency: str) -> Decimal | None:
    column = _SNAPSHOT_EQUITY_COLUMNS.get(currency)
    if column is None:
        return None
    value = getattr(snapshot, column, None)
    return None if value is None else Decimal(str(value))


def snapshot_is_currency_safe(snapshot: PaperDailySnapshot, currency: str) -> bool:
    """Whether a snapshot row may enter this currency's equity series.

    Pre-P0 rows only carry the mixed KRW+USD equity and are therefore never
    safe; a P0 row is safe only if that currency was fully valued that day.
    """
    column = _SNAPSHOT_COMPLETE_COLUMNS.get(currency)
    if column is None:
        return False
    if getattr(snapshot, column, None) is not True:
        return False
    return snapshot_equity(snapshot, currency) is not None


def unsupported_currency_evidence(
    *,
    valuations: Mapping[str, CurrencyValuation],
    trade_counts: Mapping[str, int] | None = None,
) -> dict[str, dict[str, int]]:
    """Counts of rows held in a currency the reporter does not report.

    Such rows are never folded into KRW or USD — that would be exactly the
    cross-currency arithmetic this module removes — but they must not vanish
    silently either, so they are disclosed as bounded counts.
    """
    evidence: dict[str, dict[str, int]] = {}
    for currency, valuation in valuations.items():
        if currency in REPORTED_CURRENCIES:
            continue
        evidence.setdefault(currency, {})["positions"] = valuation.positions_count
    for currency, count in (trade_counts or {}).items():
        if currency in REPORTED_CURRENCIES:
            continue
        evidence.setdefault(currency, {})["trades"] = count
    return evidence


# ---------------------------------------------------------------------------
# Fee schedule
# ---------------------------------------------------------------------------
FEE_RATES: dict[str, dict[str, float]] = {
    "equity_kr": {"buy": 0.00015, "sell": 0.00015, "tax_sell": 0.0018},
    "equity_us": {"buy": 0.0007, "sell": 0.0007, "min_fee_usd": 1.0},
    "crypto": {"buy": 0.0005, "sell": 0.0005},
}


def calculate_fee(instrument_type: str, side: str, amount: Decimal) -> Decimal:
    """Calculate paper trading fee based on instrument type and amount."""
    rates = FEE_RATES.get(instrument_type)
    if not rates:
        raise ValueError(f"Unsupported instrument_type: {instrument_type}")

    if instrument_type == "equity_kr":
        fee_rate = rates["buy"] if side == "buy" else rates["sell"]
        fee = amount * Decimal(str(fee_rate))
        if side == "sell":
            fee += amount * Decimal(str(rates["tax_sell"]))
        return _q_money(fee)

    if instrument_type == "equity_us":
        fee = amount * Decimal(str(rates["buy"]))
        min_fee = Decimal(str(rates["min_fee_usd"]))
        return _q_money(max(fee, min_fee))

    if instrument_type == "crypto":
        fee_rate = rates["buy"] if side == "buy" else rates["sell"]
        return _q_money(amount * Decimal(str(fee_rate)))

    raise ValueError(f"Unsupported instrument_type: {instrument_type}")


# ---------------------------------------------------------------------------
# Paper Trading Service
# ---------------------------------------------------------------------------
class PaperTradingService:
    """Manage paper trading accounts, orders, and positions."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # Account management
    # ------------------------------------------------------------------ #
    async def create_account(
        self,
        name: str,
        initial_capital_krw: Decimal = Decimal("100000000"),
        initial_capital_usd: Decimal = Decimal("0"),
        description: str | None = None,
        strategy_name: str | None = None,
    ) -> PaperAccount:
        account = PaperAccount(
            name=name,
            initial_capital=initial_capital_krw,
            initial_capital_usd=initial_capital_usd,
            cash_krw=initial_capital_krw,
            cash_usd=initial_capital_usd,
            description=description,
            strategy_name=strategy_name,
            is_active=True,
        )
        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def get_account(self, account_id: int) -> PaperAccount | None:
        result = await self.db.execute(
            select(PaperAccount).where(PaperAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_account_by_name(self, name: str) -> PaperAccount | None:
        result = await self.db.execute(
            select(PaperAccount).where(PaperAccount.name == name)
        )
        return result.scalar_one_or_none()

    async def list_accounts(
        self,
        is_active: bool | None = True,
        strategy_name: str | None = None,
    ) -> list[PaperAccount]:
        stmt = select(PaperAccount)
        if is_active is not None:
            stmt = stmt.where(PaperAccount.is_active == is_active)
        if strategy_name is not None:
            stmt = stmt.where(PaperAccount.strategy_name == strategy_name)
        stmt = stmt.order_by(PaperAccount.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def reset_account(self, account_id: int) -> PaperAccount:
        from sqlalchemy import delete as sa_delete

        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        await self.db.execute(
            select(PaperPosition.id)
            .where(PaperPosition.account_id == account_id)
            .with_for_update()
        )
        # Close managed cycles before removing their current position rows.
        closed_at = now_kst()
        await self.db.execute(
            update(KAssetPaperPositionState)
            .where(
                KAssetPaperPositionState.paper_account_id == account_id,
                KAssetPaperPositionState.closed_at.is_(None),
            )
            .values(paper_position_id=None, closed_at=closed_at)
        )
        await self.db.execute(
            sa_delete(PaperPosition).where(PaperPosition.account_id == account_id)
        )
        # Reset cash to initial.
        account.cash_krw = account.initial_capital
        account.cash_usd = account.initial_capital_usd
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def delete_account(self, account_id: int) -> bool:
        account = await self.get_account(account_id)
        if account is None:
            return False
        await self.db.delete(account)
        await self.db.commit()
        return True

    # ------------------------------------------------------------------ #
    # Price fetch
    # ------------------------------------------------------------------ #
    async def _fetch_quote(self, symbol: str, instrument_type: str) -> PositionQuote:
        """Current price plus the provider provenance that produced it.

        Provenance is passed through verbatim — the resolver already records
        which channel answered and when — so a reader can tell a live tick from
        a stored close instead of trusting an undated number.
        """
        session: str | None = None
        as_of: datetime | None = None
        if instrument_type in {"equity_kr", "equity_us"}:
            from app.extensions.kasset.api.krx_quotes import quote_for_market

            market = "KRX" if instrument_type == "equity_kr" else "US"
            quote = await quote_for_market(self.db, market=market, symbol=symbol)
            price = quote.price
            source = quote.source
            as_of = parse_quote_as_of(quote.as_of)
            session = quote.session
        elif instrument_type == "crypto":
            prices = await fetch_multiple_current_prices([symbol])
            price = prices.get(symbol)
            if price is None:
                raise ValueError(f"No price for {symbol}")
            source = UPBIT_QUOTE_SOURCE
        else:
            raise ValueError(f"Unsupported instrument_type: {instrument_type}")

        if price is None:
            raise ValueError(f"Could not fetch current price for {symbol}")
        return PositionQuote(
            price=Decimal(str(price)),
            source=source,
            as_of=as_of,
            session=session,
        )

    async def _fetch_current_price(self, symbol: str, instrument_type: str) -> Decimal:
        """Price-only view of ``_fetch_quote`` for the order pricing paths."""
        return (await self._fetch_quote(symbol, instrument_type)).price

    @staticmethod
    def _validate_resolved_market_price(value: Decimal | float | int) -> Decimal:
        """Fail-closed validation for a caller-resolved market reference price.

        The caller already resolved and *approved* this price (risk assessment,
        limit crossing). Never repair it by re-fetching a different provider's
        price — a bad reference price must reject the order.
        """
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise ValueError(
                f"resolved_market_price is not a valid decimal: {value!r}"
            ) from err
        if not parsed.is_finite():
            raise ValueError(f"resolved_market_price must be finite, got {value!r}")
        if parsed <= 0:
            raise ValueError(f"resolved_market_price must be positive, got {value!r}")
        return parsed

    # ------------------------------------------------------------------ #
    # Preview order
    # ------------------------------------------------------------------ #
    async def preview_order(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | float | int | None = None,
        price: Decimal | float | int | None = None,
        amount: Decimal | float | int | None = None,
        resolved_market_price: Decimal | float | int | None = None,
    ) -> dict[str, Any]:
        side = side.lower()
        order_type = order_type.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if order_type not in ("limit", "market"):
            raise ValueError("order_type must be 'limit' or 'market'")
        if order_type == "limit" and resolved_market_price is not None:
            raise ValueError(
                "resolved_market_price is only valid for market orders; "
                "a limit order fills at the caller-provided price"
            )

        from app.mcp_server.tooling.shared import resolve_market_type

        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        if not account.is_active:
            raise ValueError(f"Account {account_id} is inactive")

        # Resolve market type and normalized symbol
        instrument_type, resolved_symbol = resolve_market_type(symbol, None)

        # Currency detection — same rule the reporting side partitions by, so a
        # trade and the position it builds always share one currency bucket.
        currency = position_currency(instrument_type)

        # Determine price. A caller that already resolved and approved a market
        # reference price passes it in; re-fetching here would fill at a
        # different provider's price than the one the risk check approved.
        if order_type == "limit":
            if price is None:
                raise ValueError("price is required for limit orders")
            fill_price = Decimal(str(price))
        elif resolved_market_price is not None:
            fill_price = self._validate_resolved_market_price(resolved_market_price)
        else:
            fill_price = await self._fetch_current_price(
                resolved_symbol, instrument_type
            )

        # Determine quantity
        if quantity is not None:
            qty = Decimal(str(quantity))
        elif amount is not None:
            qty = Decimal(str(amount)) / fill_price
        else:
            raise ValueError("Either quantity or amount must be provided")

        if instrument_type == "crypto":
            qty = _q_crypto_qty(qty)
        else:
            # integer shares for equities
            qty = Decimal(int(qty))

        if qty <= 0:
            raise ValueError(f"Computed quantity is not positive: {qty}")

        gross = _q_money(qty * fill_price)
        fee = calculate_fee(instrument_type, side, gross)
        total_cost = _q_money(gross + fee) if side == "buy" else _q_money(gross - fee)

        return {
            "success": True,
            "dry_run": True,
            "account_id": account_id,
            "preview": {
                "symbol": resolved_symbol,
                "instrument_type": instrument_type,
                "side": side,
                "order_type": order_type,
                "quantity": qty,
                "price": fill_price,
                "gross": gross,
                "fee": fee,
                "total_cost": total_cost,
                "currency": currency,
            },
        }

    # ------------------------------------------------------------------ #
    # Internal position lookup
    # ------------------------------------------------------------------ #
    async def _get_position(self, account_id: int, symbol: str) -> PaperPosition | None:
        result = await self.db.execute(
            select(PaperPosition)
            .where(
                PaperPosition.account_id == account_id,
                PaperPosition.symbol == symbol,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Order execution
    # ------------------------------------------------------------------ #
    async def execute_order(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | float | int | None = None,
        price: Decimal | float | int | None = None,
        amount: Decimal | float | int | None = None,
        resolved_market_price: Decimal | float | int | None = None,
        reason: str = "",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        # 1. Preview order to get finalized quantity/price/costs
        preview = await self.preview_order(
            account_id=account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            resolved_market_price=resolved_market_price,
        )
        p = preview["preview"]
        account = await self.get_account(account_id)  # refresh (same row)
        assert account is not None  # preview_order already validated

        resolved_symbol = p["symbol"]
        instrument_type = p["instrument_type"]
        qty = p["quantity"]
        fill_price = p["price"]
        gross = p["gross"]
        fee = p["fee"]
        total_cost = p["total_cost"]
        currency = p["currency"]

        realized_pnl: Decimal | None = None

        if side.lower() == "buy":
            # Balance check
            if currency == "USD":
                if account.cash_usd < total_cost:
                    raise ValueError(
                        f"Insufficient USD balance: have {account.cash_usd}, "
                        f"need {total_cost}"
                    )
                account.cash_usd = _q_money(account.cash_usd - total_cost)
            else:
                if account.cash_krw < total_cost:
                    raise ValueError(
                        f"Insufficient KRW balance: have {account.cash_krw}, "
                        f"need {total_cost}"
                    )
                account.cash_krw = _q_money(account.cash_krw - total_cost)

            # Update/Create position
            position = await self._get_position(account_id, resolved_symbol)
            if position:
                # Weighted average calculation
                old_total = position.total_invested
                new_total = old_total + gross
                new_qty = position.quantity + qty
                position.avg_price = _q_money(new_total / new_qty)
                position.quantity = new_qty
                position.total_invested = new_total
                position.updated_at = now_kst()
            else:
                position = PaperPosition(
                    account_id=account_id,
                    symbol=resolved_symbol,
                    instrument_type=InstrumentType(instrument_type),
                    quantity=qty,
                    avg_price=fill_price,
                    total_invested=gross,
                )
                self.db.add(position)

        else:  # sell
            position = await self._get_position(account_id, resolved_symbol)
            if not position:
                raise ValueError(f"No position to sell for {resolved_symbol}")
            if position.quantity < qty:
                raise ValueError(
                    f"Insufficient quantity to sell: have {position.quantity}, "
                    f"need {qty}"
                )

            # Proceeds credit
            if currency == "USD":
                account.cash_usd = _q_money(account.cash_usd + total_cost)
            else:
                account.cash_krw = _q_money(account.cash_krw + total_cost)

            # Realized PnL calculation
            # PnL = (sell_price - avg_buy_price) * quantity - fee
            cost_basis = position.avg_price * qty
            realized_pnl = _q_money(gross - cost_basis - fee)

            # Update/Delete position
            if position.quantity == qty:
                managed_state = await self.db.scalar(
                    select(KAssetPaperPositionState)
                    .where(
                        KAssetPaperPositionState.paper_position_id == position.id,
                        KAssetPaperPositionState.closed_at.is_(None),
                    )
                    .with_for_update()
                )
                if managed_state is not None:
                    managed_state.paper_position_id = None
                    managed_state.closed_at = now_kst()
                await self.db.delete(position)
            else:
                position.quantity -= qty
                position.total_invested = _q_money(position.total_invested - cost_basis)
                position.updated_at = now_kst()

        # 3. Create Trade record
        trade = PaperTrade(
            account_id=account_id,
            symbol=resolved_symbol,
            instrument_type=InstrumentType(instrument_type),
            side=side,
            order_type=order_type,
            quantity=qty,
            price=fill_price,
            total_amount=gross,
            fee=fee,
            currency=currency,
            reason=reason,
            correlation_id=correlation_id,
            realized_pnl=realized_pnl,
        )
        self.db.add(trade)

        await self.db.commit()

        # 4. Prepare result
        res = {
            "success": True,
            "dry_run": False,
            "account_id": account_id,
            "preview": p,
            "execution": {
                **p,
                "realized_pnl": realized_pnl,
                "executed_at": now_kst(),
            },
        }
        return res

    # ------------------------------------------------------------------ #
    # Query tools
    # ------------------------------------------------------------------ #
    async def get_positions(
        self, account_id: int, *, market: str | None = None
    ) -> list[dict[str, Any]]:
        """Live positions with per-row valuation and quote provenance.

        Every row either carries the provenance of the quote it was valued with
        (``quote_source``/``quote_as_of``/``quote_session``/``quote_is_stale``)
        or a stable ``valuation_error`` code. Valuation fields stay ``None``
        when the quote is missing or unusable — a position is never valued at
        cost, at a stale-but-undisclosed price, or at a fabricated one.
        """
        stmt = select(PaperPosition).where(PaperPosition.account_id == account_id)
        if market is not None:
            stmt = stmt.where(PaperPosition.instrument_type == market)
        result = await self.db.execute(stmt)
        positions = result.scalars().all()

        observed_at = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for p in positions:
            instrument_type = p.instrument_type.value
            item: dict[str, Any] = {
                "symbol": p.symbol,
                "instrument_type": instrument_type,
                "currency": position_currency(instrument_type),
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "total_invested": p.total_invested,
                "current_price": None,
                "evaluation_amount": None,
                "unrealized_pnl": None,
                "pnl_pct": None,
                "quote_source": None,
                "quote_as_of": None,
                "quote_session": None,
                "quote_is_stale": None,
                "valuation_error": None,
            }
            out.append(item)

            try:
                quote = await self._fetch_quote(p.symbol, instrument_type)
            except Exception as exc:
                # Provider text can carry request detail, so only the exception
                # class reaches the log and only a stable code reaches callers.
                logger.warning(
                    "paper position quote unavailable (account=%s symbol=%s): %s",
                    account_id,
                    p.symbol,
                    type(exc).__name__,
                )
                item["valuation_error"] = VALUATION_ERROR_QUOTE_UNAVAILABLE
                continue

            item["quote_source"] = quote.source
            item["quote_as_of"] = quote.as_of
            item["quote_session"] = quote.session
            item["quote_is_stale"] = quote.is_stale(now=observed_at)

            if not quote.price.is_finite() or quote.price <= 0:
                item["valuation_error"] = VALUATION_ERROR_QUOTE_INVALID
                continue

            eval_amt = _q_money(quote.price * p.quantity)
            item["current_price"] = quote.price
            item["evaluation_amount"] = eval_amt
            if p.total_invested > 0:
                item["unrealized_pnl"] = _q_money(eval_amt - p.total_invested)
                item["pnl_pct"] = _q_pct((eval_amt / p.total_invested - 1) * 100)
            else:
                # No cost basis to compare against; the market value stands but
                # a profit rate would be invented.
                item["valuation_error"] = VALUATION_ERROR_COST_BASIS_UNAVAILABLE
        return out

    async def get_position(self, account_id: int, symbol: str) -> dict[str, Any] | None:
        from app.mcp_server.tooling.shared import resolve_market_type

        resolved_symbol = resolve_market_type(symbol, None)[1]
        pos = await self._get_position(account_id, resolved_symbol)
        if pos is None:
            return None
        positions = await self.get_positions(account_id=account_id)
        for item in positions:
            if item["symbol"] == resolved_symbol:
                return item
        return None

    async def get_cash_balance(self, account_id: int) -> dict[str, Decimal]:
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        return {"krw": account.cash_krw, "usd": account.cash_usd}

    async def get_trade_history(
        self,
        account_id: int,
        symbol: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperTrade).where(PaperTrade.account_id == account_id)
        if symbol:
            stmt = stmt.where(PaperTrade.symbol == symbol)
        if side:
            stmt = stmt.where(PaperTrade.side == side)
        if days:
            cutoff = now_kst() - timedelta(days=days)
            stmt = stmt.where(PaperTrade.executed_at >= cutoff)

        stmt = stmt.order_by(PaperTrade.executed_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        trades = result.scalars().all()

        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "instrument_type": t.instrument_type.value,
                "side": t.side,
                "order_type": t.order_type,
                "quantity": t.quantity,
                "price": t.price,
                "total_amount": t.total_amount,
                "fee": t.fee,
                "currency": t.currency,
                "realized_pnl": t.realized_pnl,
                "reason": t.reason,
                "executed_at": t.executed_at,
            }
            for t in trades
        ]

    async def _evaluate_positions_by_currency(
        self,
        account_id: int,
        *,
        positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, CurrencyValuation]:
        """Valuation of live positions, bucketed by settlement currency.

        Accepts an already-fetched position list so a caller that needs both the
        rows and the totals pays for one round of quotes, not two.
        """
        rows = (
            positions
            if positions is not None
            else await self.get_positions(account_id=account_id)
        )
        buckets: dict[str, CurrencyValuation] = {}
        for row in rows:
            currency = str(
                row.get("currency")
                or position_currency(str(row.get("instrument_type") or ""))
            )
            bucket = buckets.setdefault(currency, CurrencyValuation())
            bucket.positions_count += 1
            bucket.total_invested += Decimal(str(row["total_invested"]))
            evaluation = row.get("evaluation_amount")
            unrealized = row.get("unrealized_pnl")
            if evaluation is None or unrealized is None:
                continue
            bucket.positions_valued += 1
            bucket.positions_value += Decimal(str(evaluation))
            bucket.unrealized_pnl += Decimal(str(unrealized))
        return buckets

    @staticmethod
    def _summary_metrics(currency: str, valuation: CurrencyValuation) -> dict[str, Any]:
        """Holdings totals for one currency; ``None`` where they are unprovable.

        A partially-valued currency reports no evaluated total at all rather
        than a total that silently omits the positions it could not price.
        """
        complete = valuation.valuation_complete
        total_pnl_pct = None
        if complete and valuation.total_invested > 0:
            total_pnl_pct = _q_pct(
                (valuation.positions_value / valuation.total_invested - 1) * 100
            )
        return {
            "currency": currency,
            "positions_count": valuation.positions_count,
            "positions_valued": valuation.positions_valued,
            "valuation_complete": complete,
            "total_invested": valuation.total_invested,
            "total_evaluated": valuation.positions_value if complete else None,
            "total_pnl": valuation.unrealized_pnl if complete else None,
            "total_pnl_pct": total_pnl_pct,
        }

    async def get_portfolio_summary(self, account_id: int) -> dict[str, Any]:
        """Holdings summary, partitioned by settlement currency.

        There is deliberately no portfolio-wide invested/evaluated/PnL total:
        the account holds two independent cash ledgers and no FX rate, so any
        single number would be a sum of unlike units.
        """
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        positions = await self.get_positions(account_id)
        valuations = await self._evaluate_positions_by_currency(
            account_id, positions=positions
        )

        return {
            "account_name": account.name,
            "cash_krw": account.cash_krw,
            "cash_usd": account.cash_usd,
            "positions_count": len(positions),
            "currencies": {
                currency: self._summary_metrics(
                    currency, valuations.get(currency, CurrencyValuation())
                )
                for currency in REPORTED_CURRENCIES
            },
            "unsupported_currencies": unsupported_currency_evidence(
                valuations=valuations
            ),
        }

    # ------------------------------------------------------------------ #
    # Daily snapshot
    # ------------------------------------------------------------------ #
    @staticmethod
    def _currency_daily_return_pct(
        *,
        currency: str,
        prior: PaperDailySnapshot | None,
        equity: Decimal,
        valuation_complete: bool,
    ) -> Decimal | None:
        """Day-over-day return for one currency, or ``None`` if unprovable.

        Both ends of the comparison must be currency-safe: a partially valued
        today, or a prior row that is pre-P0 or itself partial, yields no
        number rather than a return computed against a different basis.
        """
        if prior is None or not valuation_complete:
            return None
        if not snapshot_is_currency_safe(prior, currency):
            return None
        prior_equity = snapshot_equity(prior, currency)
        if prior_equity is None or prior_equity <= 0:
            return None
        return _q_pct((equity / prior_equity - Decimal("1")) * Decimal("100"))

    async def record_daily_snapshot(self, account_id: int) -> PaperDailySnapshot:
        """Record today's equity per currency.

        The legacy mixed-currency columns are left untouched: existing rows keep
        their historical values and new rows leave them empty rather than
        writing another KRW+USD sum.
        """
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        today = now_kst().date()

        existing_today = (
            await self.db.execute(
                select(PaperDailySnapshot).where(
                    PaperDailySnapshot.account_id == account_id,
                    PaperDailySnapshot.snapshot_date == today,
                )
            )
        ).scalar_one_or_none()

        valuations = await self._evaluate_positions_by_currency(account_id)
        cash = {"KRW": account.cash_krw, "USD": account.cash_usd}
        equity: dict[str, Decimal] = {}
        complete: dict[str, bool] = {}
        for currency in REPORTED_CURRENCIES:
            valuation = valuations.get(currency, CurrencyValuation())
            equity[currency] = _q_money(cash[currency] + valuation.positions_value)
            complete[currency] = valuation.valuation_complete

        # Only P0-era rows can serve as a per-currency basis; a pre-P0 row has
        # no per-currency equity at all.
        prior = (
            await self.db.execute(
                select(PaperDailySnapshot)
                .where(
                    PaperDailySnapshot.account_id == account_id,
                    PaperDailySnapshot.snapshot_date < today,
                    PaperDailySnapshot.equity_krw.is_not(None),
                )
                .order_by(PaperDailySnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        values: dict[str, Any] = {
            "cash_krw": account.cash_krw,
            "cash_usd": account.cash_usd,
            "equity_krw": equity["KRW"],
            "equity_usd": equity["USD"],
            "valuation_complete_krw": complete["KRW"],
            "valuation_complete_usd": complete["USD"],
        }
        for currency in REPORTED_CURRENCIES:
            values[_SNAPSHOT_RETURN_COLUMNS[currency]] = (
                self._currency_daily_return_pct(
                    currency=currency,
                    prior=prior,
                    equity=equity[currency],
                    valuation_complete=complete[currency],
                )
            )

        if existing_today is None:
            snapshot = PaperDailySnapshot(
                account_id=account_id, snapshot_date=today, **values
            )
            self.db.add(snapshot)
        else:
            for column, value in values.items():
                setattr(existing_today, column, value)
            snapshot = existing_today

        await self.db.commit()
        return snapshot

    async def calculate_daily_returns(
        self,
        account_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperDailySnapshot).where(
            PaperDailySnapshot.account_id == account_id
        )
        if start_date is not None:
            stmt = stmt.where(PaperDailySnapshot.snapshot_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(PaperDailySnapshot.snapshot_date <= end_date)
        stmt = stmt.order_by(PaperDailySnapshot.snapshot_date.asc())

        result = await self.db.execute(stmt)
        snaps = list(result.scalars().all())
        return [
            {
                "date": s.snapshot_date.isoformat(),
                "currencies": {
                    currency: {
                        "total_equity": snapshot_equity(s, currency),
                        "daily_return_pct": getattr(
                            s, _SNAPSHOT_RETURN_COLUMNS[currency]
                        ),
                        "valuation_complete": snapshot_is_currency_safe(s, currency),
                    }
                    for currency in REPORTED_CURRENCIES
                },
            }
            for s in snaps
        ]

    # ------------------------------------------------------------------ #
    # Performance analytics helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_round_trips(trades: list[PaperTrade]) -> list[dict[str, Any]]:
        """Group raw trades into round trips per symbol until position is flat.
        Excludes open (unclosed) trips."""
        grouped: dict[str, list[tuple[int, PaperTrade]]] = defaultdict(list)
        for idx, t in enumerate(trades):
            grouped[t.symbol].append((idx, t))

        round_trips: list[dict[str, Any]] = []
        for symbol, indexed in grouped.items():
            indexed.sort(key=lambda item: (item[1].executed_at, item[0]))
            position_qty = Decimal("0")
            buy_cost = Decimal("0")
            total_pnl = Decimal("0")
            entry_date: datetime | None = None
            entry_reason = ""
            last_exit_date: datetime | None = None
            last_sell_reason = ""

            for _, t in indexed:
                qty = Decimal(t.quantity)
                if t.side == "buy":
                    if position_qty <= 0:
                        entry_date = t.executed_at
                        entry_reason = t.reason or ""
                        buy_cost = Decimal("0")
                        total_pnl = Decimal("0")
                    position_qty += qty
                    buy_cost += qty * Decimal(t.price) + Decimal(t.fee)
                elif t.side == "sell" and position_qty > 0:
                    position_qty -= qty
                    total_pnl += Decimal(t.realized_pnl or 0)
                    last_exit_date = t.executed_at
                    last_sell_reason = t.reason or ""

                    if (
                        position_qty <= 0
                        and entry_date is not None
                        and last_exit_date is not None
                    ):
                        holding_days = (last_exit_date.date() - entry_date.date()).days
                        return_pct = (
                            float(total_pnl / buy_cost * Decimal("100"))
                            if buy_cost > 0
                            else 0.0
                        )
                        round_trips.append(
                            {
                                "symbol": symbol,
                                "entry_date": entry_date.isoformat(),
                                "exit_date": last_exit_date.isoformat(),
                                "holding_days": max(holding_days, 0),
                                "pnl": float(total_pnl),
                                "return_pct": return_pct,
                                "entry_reason": entry_reason,
                                "exit_reason": last_sell_reason,
                            }
                        )
                        position_qty = Decimal("0")
                        buy_cost = Decimal("0")
                        total_pnl = Decimal("0")
                        entry_date = None
                        entry_reason = ""
                        last_exit_date = None
                        last_sell_reason = ""

        round_trips.sort(key=lambda trip: (trip["exit_date"], trip["symbol"]))
        return round_trips

    @staticmethod
    def _calc_max_drawdown_pct(equity_curve: list[Decimal]) -> float | None:
        if not equity_curve:
            return None
        peak = equity_curve[0]
        max_dd = Decimal("0")
        for v in equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        return float(max_dd * Decimal("100"))

    @staticmethod
    def _calc_sharpe_ratio(daily_returns_pct: list[Decimal]) -> float | None:
        """Annualised Sharpe ratio (252 trading days). Assumes 0% risk-free rate."""
        import math

        values = [float(r) for r in daily_returns_pct if r is not None]
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stdev = math.sqrt(variance)
        if stdev == 0:
            return None
        return (mean / stdev) * math.sqrt(252)

    @staticmethod
    def _total_return_pct(
        initial_capital: Decimal, total_equity: Decimal | None
    ) -> float | None:
        """Return against the currency's own opening capital.

        ``None`` when today's equity is not provable. A currency the account
        never funded and never used reports 0%, not a division by zero.
        """
        if total_equity is None:
            return None
        if initial_capital > 0:
            return float(
                (total_equity - initial_capital) / initial_capital * Decimal("100")
            )
        return 0.0 if total_equity == 0 else None

    @classmethod
    def _currency_performance(
        cls,
        *,
        currency: str,
        initial_capital: Decimal,
        cash: Decimal,
        valuation: CurrencyValuation,
        trades: list[PaperTrade],
        snapshots: list[PaperDailySnapshot],
    ) -> dict[str, Any]:
        """Performance metrics computed entirely within one currency."""
        complete = valuation.valuation_complete
        total_equity = _q_money(cash + valuation.positions_value) if complete else None

        realized = sum(
            (Decimal(t.realized_pnl) for t in trades if t.realized_pnl is not None),
            _ZERO,
        )

        # A symbol trades in exactly one currency, so grouping the currency's
        # own trades keeps every round trip single-currency by construction.
        round_trips = cls._build_round_trips(trades)
        total_trips = len(round_trips)
        wins = sum(1 for trip in round_trips if trip["pnl"] > 0)
        win_rate = (wins / total_trips * 100.0) if total_trips > 0 else 0.0
        avg_holding_days = (
            sum(trip["holding_days"] for trip in round_trips) / total_trips
            if total_trips > 0
            else 0.0
        )

        # Drawdown and Sharpe read only rows that carry this currency's own
        # equity and were fully valued that day; pre-P0 mixed rows are skipped.
        return_column = _SNAPSHOT_RETURN_COLUMNS[currency]
        equity_curve: list[Decimal] = []
        daily_returns: list[Decimal] = []
        for snapshot in snapshots:
            if not snapshot_is_currency_safe(snapshot, currency):
                continue
            equity = snapshot_equity(snapshot, currency)
            if equity is None:
                continue
            equity_curve.append(equity)
            daily_return = getattr(snapshot, return_column, None)
            if daily_return is not None:
                daily_returns.append(Decimal(str(daily_return)))

        max_dd = cls._calc_max_drawdown_pct(equity_curve) if equity_curve else None
        sharpe = cls._calc_sharpe_ratio(daily_returns) if daily_returns else None
        total_return_pct = cls._total_return_pct(initial_capital, total_equity)

        return {
            "currency": currency,
            "initial_capital": float(initial_capital),
            "cash": float(cash),
            "positions_count": valuation.positions_count,
            "positions_valued": valuation.positions_valued,
            "valuation_complete": complete,
            "positions_value": (float(valuation.positions_value) if complete else None),
            "total_equity": float(total_equity) if total_equity is not None else None,
            "total_return_pct": (
                round(total_return_pct, 4) if total_return_pct is not None else None
            ),
            "realized_pnl": float(realized),
            "unrealized_pnl": float(valuation.unrealized_pnl) if complete else None,
            "total_trades": total_trips,
            "win_rate": round(win_rate, 2),
            "avg_holding_days": round(avg_holding_days, 2),
            "max_drawdown_pct": round(max_dd, 4) if max_dd is not None else None,
            "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
            "best_trade": (
                max(round_trips, key=lambda t: t["return_pct"]) if round_trips else None
            ),
            "worst_trade": (
                min(round_trips, key=lambda t: t["return_pct"]) if round_trips else None
            ),
            "snapshots_used": len(equity_curve),
        }

    async def calculate_performance(
        self,
        account_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        """Account performance, partitioned by settlement currency.

        There is no combined figure: KRW and USD equity, PnL, drawdown, and
        Sharpe are reported separately because the paper ledger holds no FX
        rate, and a rate invented here would show returns that never happened.
        """
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        positions = await self.get_positions(account_id=account_id)
        valuations = await self._evaluate_positions_by_currency(
            account_id, positions=positions
        )

        # Trades in period
        trade_stmt = select(PaperTrade).where(PaperTrade.account_id == account_id)
        if start_date is not None:
            trade_stmt = trade_stmt.where(
                PaperTrade.executed_at
                >= datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
            )
        if end_date is not None:
            trade_stmt = trade_stmt.where(
                PaperTrade.executed_at
                <= datetime.combine(end_date, datetime.max.time(), tzinfo=UTC)
            )
        trade_stmt = trade_stmt.order_by(PaperTrade.executed_at.asc())
        trades = list((await self.db.execute(trade_stmt)).scalars().all())

        trades_by_currency: dict[str, list[PaperTrade]] = defaultdict(list)
        for trade in trades:
            trades_by_currency[(trade.currency or "KRW").upper()].append(trade)

        # Snapshots in period → per-currency sharpe + max drawdown
        snap_stmt = select(PaperDailySnapshot).where(
            PaperDailySnapshot.account_id == account_id
        )
        if start_date is not None:
            snap_stmt = snap_stmt.where(PaperDailySnapshot.snapshot_date >= start_date)
        if end_date is not None:
            snap_stmt = snap_stmt.where(PaperDailySnapshot.snapshot_date <= end_date)
        snap_stmt = snap_stmt.order_by(PaperDailySnapshot.snapshot_date.asc())
        snapshots = list((await self.db.execute(snap_stmt)).scalars().all())

        initial = {
            "KRW": Decimal(account.initial_capital),
            "USD": Decimal(account.initial_capital_usd or 0),
        }
        cash = {"KRW": account.cash_krw, "USD": account.cash_usd}

        return {
            "currencies": {
                currency: self._currency_performance(
                    currency=currency,
                    initial_capital=initial[currency],
                    cash=cash[currency],
                    valuation=valuations.get(currency, CurrencyValuation()),
                    trades=trades_by_currency.get(currency, []),
                    snapshots=snapshots,
                )
                for currency in REPORTED_CURRENCIES
            },
            "unsupported_currencies": unsupported_currency_evidence(
                valuations=valuations,
                trade_counts={
                    currency: len(rows) for currency, rows in trades_by_currency.items()
                },
            ),
        }


__all__ = [
    "FEE_RATES",
    "PAPER_QUOTE_STALE_AFTER",
    "REPORTED_CURRENCIES",
    "UPBIT_QUOTE_SOURCE",
    "VALUATION_ERROR_COST_BASIS_UNAVAILABLE",
    "VALUATION_ERROR_QUOTE_INVALID",
    "VALUATION_ERROR_QUOTE_UNAVAILABLE",
    "CurrencyValuation",
    "PaperTradingService",
    "PositionQuote",
    "calculate_fee",
    "parse_quote_as_of",
    "position_currency",
    "snapshot_equity",
    "snapshot_is_currency_safe",
    "unsupported_currency_evidence",
]
