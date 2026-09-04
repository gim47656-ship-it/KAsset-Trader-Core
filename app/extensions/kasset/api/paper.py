"""Thin Android adapter over the existing Core PaperTradingService."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import (
    Balance,
    CashBalance,
    ClosedTrade,
    ClosedTradesResponse,
    ClosedTradeTotal,
    Position,
    PositionsResponse,
    Quote,
    SymbolItem,
    SymbolsResponse,
)
from app.extensions.kasset.api.toss_market_data import toss_market_data
from app.extensions.kasset.models import AndroidPaperAccount
from app.models.paper_trading import PaperAccount, PaperTrade
from app.models.symbol_master import SymbolMaster
from app.models.trading import Instrument, InstrumentType
from app.services.exchange_rate_service import (
    UsdKrwExchangeRateQuote,
    get_usd_krw_rate_details,
)
from app.services.market_data.toss_ohlcv import fetch_daily_toss_frame
from app.services.paper_trading_service import PaperTradingService
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

_DEFAULT_ACCOUNT_NAME_PREFIX = "KAsset Android PAPER"
_FX_QUOTE_UNAVAILABLE = "FX_QUOTE_UNAVAILABLE"
_FX_QUOTE_PAIR_MISMATCH = "FX_QUOTE_PAIR_MISMATCH"
_FX_QUOTE_INVALID = "FX_QUOTE_INVALID"
_FX_QUOTE_INCOMPLETE = "FX_QUOTE_INCOMPLETE"
_FX_QUOTE_STALE = "FX_QUOTE_STALE"

logger = logging.getLogger(__name__)


class _PositionIdentity(Protocol):
    market: str
    symbol: str


def decimal_text(value: Decimal | int | str) -> str:
    return format(Decimal(value), "f")


def iso_z(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (
        current.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class PaperAccountAdapter:
    async def default_account(
        self,
        db: AsyncSession,
        owner_user_id: int,
    ) -> PaperAccount:
        result = await db.execute(
            select(PaperAccount)
            .join(
                AndroidPaperAccount,
                AndroidPaperAccount.paper_account_id == PaperAccount.id,
            )
            .where(AndroidPaperAccount.owner_user_id == owner_user_id)
            .order_by(AndroidPaperAccount.created_at, PaperAccount.id)
            .limit(1)
        )
        account = result.scalar_one_or_none()
        if account is not None:
            return account

        account = PaperAccount(
            name=f"{_DEFAULT_ACCOUNT_NAME_PREFIX} {owner_user_id}",
            initial_capital=Decimal("10000000"),
            initial_capital_usd=Decimal("10000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("10000"),
            description="KAsset Android user PAPER account",
            strategy_name=None,
            is_active=True,
        )
        try:
            db.add(account)
            await db.flush()
            db.add(
                AndroidPaperAccount(
                    owner_user_id=owner_user_id,
                    paper_account_id=account.id,
                )
            )
            await db.commit()
            await db.refresh(account)
            return account
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(PaperAccount)
                .join(
                    AndroidPaperAccount,
                    AndroidPaperAccount.paper_account_id == PaperAccount.id,
                )
                .where(AndroidPaperAccount.owner_user_id == owner_user_id)
                .order_by(AndroidPaperAccount.created_at, PaperAccount.id)
                .limit(1)
            )
            account = result.scalar_one_or_none()
            if account is None:
                raise
            return account

    async def resolve_account(
        self,
        db: AsyncSession,
        owner_user_id: int,
        account_id: str | None,
    ) -> PaperAccount:
        if account_id in {None, ""}:
            return await self.default_account(db, owner_user_id)
        prefix = "PAPER-"
        raw_id = account_id.removeprefix(prefix)
        if not account_id.startswith(prefix) or not raw_id.isdecimal():
            raise MobileApiError(404, "NOT_FOUND", "PAPER 계좌를 찾을 수 없습니다.")
        result = await db.execute(
            select(PaperAccount)
            .join(
                AndroidPaperAccount,
                AndroidPaperAccount.paper_account_id == PaperAccount.id,
            )
            .where(
                AndroidPaperAccount.owner_user_id == owner_user_id,
                PaperAccount.id == int(raw_id),
            )
        )
        account = result.scalar_one_or_none()
        if account is None:
            raise MobileApiError(404, "NOT_FOUND", "PAPER 계좌를 찾을 수 없습니다.")
        return account

    async def balance(self, db: AsyncSession, owner_user_id: int) -> Balance:
        account = await self.default_account(db, owner_user_id)
        service = PaperTradingService(db)
        positions = await service.get_positions(account.id)
        observed_at = datetime.now(UTC)
        has_usd_exposure = Decimal(account.cash_usd) != 0 or any(
            item["instrument_type"] == "equity_us" for item in positions
        )
        fx_snapshot = (
            await self._load_krw_reference_snapshot(
                observed_at=observed_at,
                purpose="PAPER 자산 합산용",
            )
            if has_usd_exposure
            else self._empty_krw_reference()
        )
        fx_rate = fx_snapshot.get("_rate_decimal")
        kr_positions = [
            item for item in positions if item["instrument_type"] == "equity_kr"
        ]
        valuation_complete = all(
            item["evaluation_amount"] is not None and item["unrealized_pnl"] is not None
            for item in kr_positions
        )
        evaluation = (
            sum(
                (Decimal(str(item["evaluation_amount"])) for item in kr_positions),
                Decimal("0"),
            )
            if valuation_complete
            else None
        )
        unrealized = (
            sum(
                (Decimal(str(item["unrealized_pnl"])) for item in kr_positions),
                Decimal("0"),
            )
            if valuation_complete
            else None
        )
        realized_result = await db.execute(
            select(PaperTrade.realized_pnl).where(
                PaperTrade.account_id == account.id,
                PaperTrade.instrument_type == InstrumentType.equity_kr,
                PaperTrade.realized_pnl.is_not(None),
            )
        )
        realized = sum(
            (Decimal(str(value)) for value in realized_result.scalars().all()),
            Decimal("0"),
        )
        return Balance(
            broker="PAPER",
            account_id=self.account_id(account),
            base_currency="KRW",
            cash=[
                CashBalance(
                    currency="KRW",
                    cash=decimal_text(account.cash_krw),
                    available=decimal_text(account.cash_krw),
                ),
                CashBalance(
                    currency="USD",
                    cash=decimal_text(account.cash_usd),
                    available=decimal_text(account.cash_usd),
                ),
            ],
            evaluation_amount=(
                decimal_text(evaluation) if evaluation is not None else None
            ),
            total_assets=(
                decimal_text(Decimal(account.cash_krw) + evaluation)
                if evaluation is not None
                else None
            ),
            unrealized_pnl=(
                decimal_text(unrealized) if unrealized is not None else None
            ),
            realized_pnl=decimal_text(realized),
            fx_rate=decimal_text(fx_rate) if isinstance(fx_rate, Decimal) else None,
            updated_at=iso_z(observed_at),
        )

    @staticmethod
    def _quote_provenance(item: dict[str, object]) -> dict[str, object | None]:
        """Wire-side quote provenance for one position row.

        Passed through from the service verbatim. ``quoteAsOf`` stays absent
        when the provider gave no timestamp — filling in the server clock would
        make an undated quote look freshly observed.
        """
        as_of = item.get("quote_as_of")
        return {
            "quote_source": item.get("quote_source"),
            "quote_as_of": iso_z(as_of) if isinstance(as_of, datetime) else None,
            "quote_session": item.get("quote_session"),
            "quote_is_stale": item.get("quote_is_stale"),
            "valuation_error": item.get("valuation_error"),
        }

    @staticmethod
    def _empty_krw_reference(
        error: str | None = None,
    ) -> dict[str, object | None]:
        return {
            "market_value_krw_reference": None,
            "market_value_krw_fx_rate": None,
            "market_value_krw_fx_source": None,
            "market_value_krw_fx_as_of": None,
            "market_value_krw_fx_valid_until": None,
            "market_value_krw_fx_is_stale": None,
            "market_value_krw_reference_error": error,
        }

    @classmethod
    def _krw_reference_snapshot(
        cls,
        quote: UsdKrwExchangeRateQuote,
        *,
        now: datetime,
    ) -> dict[str, object | None]:
        if not isinstance(quote, UsdKrwExchangeRateQuote):
            return cls._empty_krw_reference(_FX_QUOTE_INVALID)
        if quote.base_currency != "USD" or quote.quote_currency != "KRW":
            return cls._empty_krw_reference(_FX_QUOTE_PAIR_MISMATCH)

        try:
            rate = quote.default_rate_decimal
        except (InvalidOperation, TypeError, ValueError):
            return cls._empty_krw_reference(_FX_QUOTE_INVALID)
        if quote.mid_rate_decimal is None:
            return cls._empty_krw_reference(_FX_QUOTE_INCOMPLETE)
        if quote.source not in {"toss", "open_er_api"}:
            return cls._empty_krw_reference(_FX_QUOTE_INVALID)

        as_of = quote.valid_from
        valid_until = quote.valid_until
        snapshot = cls._empty_krw_reference()
        snapshot.update(
            {
                "market_value_krw_fx_rate": decimal_text(rate),
                "market_value_krw_fx_source": quote.source,
                "market_value_krw_fx_as_of": (
                    iso_z(as_of) if isinstance(as_of, datetime) else None
                ),
                "market_value_krw_fx_valid_until": (
                    iso_z(valid_until) if isinstance(valid_until, datetime) else None
                ),
            }
        )
        if not isinstance(as_of, datetime) or not isinstance(valid_until, datetime):
            snapshot["market_value_krw_reference_error"] = _FX_QUOTE_INCOMPLETE
            return snapshot
        if (
            as_of.tzinfo is None
            or as_of.utcoffset() is None
            or valid_until.tzinfo is None
            or valid_until.utcoffset() is None
            or valid_until <= as_of
            or as_of > now
        ):
            snapshot["market_value_krw_reference_error"] = _FX_QUOTE_INVALID
            return snapshot
        if (
            snapshot["market_value_krw_fx_as_of"]
            == snapshot["market_value_krw_fx_valid_until"]
        ):
            snapshot["market_value_krw_reference_error"] = _FX_QUOTE_INCOMPLETE
            return snapshot
        if valid_until <= now:
            snapshot["market_value_krw_fx_is_stale"] = True
            snapshot["market_value_krw_reference_error"] = _FX_QUOTE_STALE
            return snapshot

        snapshot["market_value_krw_fx_is_stale"] = False
        snapshot["_rate_decimal"] = rate
        return snapshot

    async def _load_krw_reference_snapshot(
        self,
        *,
        observed_at: datetime,
        purpose: str,
    ) -> dict[str, object | None]:
        try:
            quote = await get_usd_krw_rate_details()
        except Exception as exc:
            logger.warning("%s USD/KRW 환율을 가져오지 못했습니다: %s", purpose, exc)
            return self._empty_krw_reference(_FX_QUOTE_UNAVAILABLE)
        return self._krw_reference_snapshot(quote, now=observed_at)

    @classmethod
    def _position_krw_reference(
        cls,
        item: dict[str, object],
        snapshot: dict[str, object | None],
    ) -> dict[str, object | None]:
        if str(item.get("currency", "")).upper() != "USD":
            return cls._empty_krw_reference()
        raw_market_value = item.get("evaluation_amount")
        if raw_market_value is None:
            return cls._empty_krw_reference()

        reference = {
            key: value for key, value in snapshot.items() if key != "_rate_decimal"
        }
        raw_market_value = item["evaluation_amount"]
        rate = snapshot.get("_rate_decimal")
        if not isinstance(rate, Decimal):
            return reference
        try:
            market_value = Decimal(str(raw_market_value))
            converted = market_value * rate
        except (DecimalException, InvalidOperation, TypeError, ValueError):
            reference["market_value_krw_reference_error"] = _FX_QUOTE_INVALID
            return reference
        if (
            not market_value.is_finite()
            or market_value < 0
            or not converted.is_finite()
        ):
            reference["market_value_krw_reference_error"] = _FX_QUOTE_INVALID
            return reference
        reference["market_value_krw_reference"] = decimal_text(converted)
        return reference

    async def positions(
        self, db: AsyncSession, owner_user_id: int
    ) -> PositionsResponse:
        account = await self.default_account(db, owner_user_id)
        service = PaperTradingService(db)
        raw_positions = await service.get_positions(account.id)
        observed_at = datetime.now(UTC)
        fx_snapshot = self._empty_krw_reference()
        if any(
            str(item.get("currency", "")).upper() == "USD"
            and item.get("evaluation_amount") is not None
            for item in raw_positions
        ):
            fx_snapshot = await self._load_krw_reference_snapshot(
                observed_at=observed_at,
                purpose="PAPER 원화 참고용",
            )

        now = iso_z(observed_at)
        positions = [
            Position(
                broker="PAPER",
                account_id=self.account_id(account),
                market=self.market_name(item["instrument_type"]),
                symbol=item["symbol"],
                name=None,
                # Settlement currency as resolved by the service, the same
                # value its trades and per-currency metrics are keyed by.
                currency=str(item["currency"]),
                quantity=decimal_text(item["quantity"]),
                average_price=decimal_text(item["avg_price"]),
                current_price=(
                    decimal_text(item["current_price"])
                    if item["current_price"] is not None
                    else None
                ),
                market_value=(
                    decimal_text(item["evaluation_amount"])
                    if item["evaluation_amount"] is not None
                    else None
                ),
                **self._position_krw_reference(item, fx_snapshot),  # type: ignore[arg-type]
                unrealized_pnl=(
                    decimal_text(item["unrealized_pnl"])
                    if item["unrealized_pnl"] is not None
                    else None
                ),
                unrealized_pnl_rate=(
                    decimal_text(item["pnl_pct"])
                    if item["pnl_pct"] is not None
                    else None
                ),
                **self._quote_provenance(item),  # type: ignore[arg-type]
                updated_at=now,
            )
            for item in raw_positions
        ]
        resolved_names = await self._position_names(db, positions)
        for position in positions:
            position.name = resolved_names.get((position.market, position.symbol))
        return PositionsResponse(positions=positions)

    async def closed_trades(
        self, db: AsyncSession, owner_user_id: int, *, limit: int = 100
    ) -> ClosedTradesResponse:
        """청산이 끝난 매매의 확정 수익률. 통화별로만 합계를 낸다."""
        account = await self.default_account(db, owner_user_id)
        service = PaperTradingService(db)
        rows, total_rows = await service.list_closed_trades(account.id, limit=limit)
        trades = [
            ClosedTrade(
                market=self.market_name(str(row["instrument_type"])),
                symbol=str(row["symbol"]),
                name=None,
                currency=str(row["currency"]),
                quantity=decimal_text(row["quantity"]),
                cost_basis=decimal_text(row["cost_basis"]),
                realized_pnl=decimal_text(row["pnl_amount"]),
                return_rate=decimal_text(row["return_rate_pct"]),
                holding_days=int(row["holding_days"]),
                entry_at=iso_z(row["entry_date"]),
                exit_at=iso_z(row["exit_date"]),
            )
            for row in rows
        ]
        resolved_names = await self._position_names(db, trades)
        for trade in trades:
            trade.name = resolved_names.get((trade.market, trade.symbol))
        totals = [
            ClosedTradeTotal(
                currency=str(row["currency"]),
                trade_count=int(row["trade_count"]),
                win_count=int(row["win_count"]),
                realized_pnl=decimal_text(row["realized_pnl"]),
                cost_basis=decimal_text(row["cost_basis"]),
                return_rate=decimal_text(row["return_rate"]),
            )
            for row in total_rows
        ]
        return ClosedTradesResponse(trades=trades, totals=totals)

    async def _quote_from_candles(
        self,
        db: AsyncSession,
        symbol: str,
    ) -> dict[str, object]:
        """저장된 KR 일봉 종가로 PAPER 시세 snapshot을 만든다."""
        from sqlalchemy import text as sql_text

        rows = (
            await db.execute(
                sql_text(
                    "SELECT time, close FROM kr_candles_1d "
                    "WHERE symbol = :symbol AND venue = 'KRX' "
                    "ORDER BY time DESC LIMIT 2"
                ),
                {"symbol": symbol},
            )
        ).all()
        if not rows:
            raise ValueError(f"no stored candles for {symbol}")
        latest_time, latest_close = rows[0]
        previous_close = rows[1][1] if len(rows) > 1 else None
        return {
            "price": latest_close,
            "previous_close": previous_close,
            "price_as_of": latest_time,
            "source": "CANDLES",
        }

    async def _quote_us_toss_or_candles(
        self,
        db: AsyncSession,
        symbol: str,
    ) -> dict[str, object]:
        """활성 US 종목을 Toss로 읽고, 미가용 시 저장 일봉으로 내린다."""
        from sqlalchemy import text as sql_text

        exchange = await get_us_exchange_by_symbol(symbol, db=db)
        try:
            points = await toss_market_data.prices([symbol])
        except Exception:
            points = {}
        point = points.get(symbol)
        try:
            frame = await fetch_daily_toss_frame(symbol=symbol, count=2)
        except Exception:
            frame = None
        if point is not None or (frame is not None and not frame.empty):
            latest = frame.iloc[-1] if frame is not None and not frame.empty else None
            previous_close = (
                frame.iloc[-2].get("close")
                if frame is not None and len(frame) >= 2
                else None
            )
            price = point.price if point is not None else latest.get("close")
            price_as_of = (
                point.as_of
                if point is not None
                else latest.get("datetime", latest.get("date"))
            )
            return {
                "price": price,
                "previous_close": previous_close,
                "price_as_of": price_as_of,
                "source": "TOSS",
            }

        rows = (
            await db.execute(
                sql_text(
                    "SELECT time, close FROM us_candles_1d "
                    "WHERE symbol = :symbol AND exchange = :exchange "
                    "ORDER BY time DESC LIMIT 2"
                ),
                {"symbol": symbol, "exchange": exchange},
            )
        ).all()
        if not rows:
            raise ValueError(f"no Toss or stored candles for {symbol}")
        return {
            "price": rows[0][1],
            "previous_close": rows[1][1] if len(rows) > 1 else None,
            "price_as_of": rows[0][0],
            "source": "CANDLES",
        }

    async def quote(self, db: AsyncSession, *, market: str, symbol: str) -> Quote:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip().upper()
        try:
            if normalized_market in {"KRX", "KR"}:
                raw = await self._quote_from_candles(db, normalized_symbol)
                market_name = "KRX"
                currency = "KRW"
            elif normalized_market in {"US", "NYSE", "NASDAQ"}:
                raw = await self._quote_us_toss_or_candles(db, normalized_symbol)
                market_name = "US"
                currency = "USD"
            else:
                raise MobileApiError(
                    422, "VALIDATION_ERROR", "지원하지 않는 PAPER 시장입니다."
                )
        except MobileApiError:
            raise
        except ValueError as err:
            raise MobileApiError(
                404, "NOT_FOUND", "종목 시세를 찾을 수 없습니다."
            ) from err
        except Exception as err:
            raise MobileApiError(
                502, "BROKER_ERROR", "PAPER 시세를 가져오지 못했습니다."
            ) from err

        price = self._required_decimal(raw.get("price"))
        previous_close = self._optional_decimal(raw.get("previous_close"))
        change_amount = price - previous_close if previous_close is not None else None
        change_rate = (
            (change_amount / previous_close * Decimal("100"))
            if previous_close not in {None, Decimal("0")}
            else None
        )
        from app.extensions.kasset.api import krx_quotes

        name = (
            await krx_quotes._instrument_names(db, market_name, [normalized_symbol])
        ).get(normalized_symbol)
        as_of_raw = raw.get("price_as_of")
        if isinstance(as_of_raw, datetime):
            as_of = iso_z(as_of_raw)
        elif isinstance(as_of_raw, str) and as_of_raw.strip():
            as_of = iso_z(datetime.fromisoformat(as_of_raw.replace("Z", "+00:00")))
        else:
            raise MobileApiError(
                502,
                "BROKER_ERROR",
                "PAPER 시세의 공급자 시각을 확인하지 못했습니다.",
            )
        return Quote(
            broker="PAPER",
            market=market_name,
            symbol=normalized_symbol,
            name=name,
            currency=currency,
            price=decimal_text(price),
            previous_close=(
                decimal_text(previous_close) if previous_close is not None else None
            ),
            change_amount=(
                decimal_text(change_amount) if change_amount is not None else None
            ),
            change_rate=decimal_text(change_rate) if change_rate is not None else None,
            as_of=as_of,
            source=f"PAPER_{str(raw.get('source') or 'CORE').upper()}",
        )

    async def symbols(self, db: AsyncSession) -> SymbolsResponse:
        result = await db.execute(
            select(Instrument)
            .where(
                Instrument.is_active == True,  # noqa: E712
                Instrument.type.in_(
                    [InstrumentType.equity_kr, InstrumentType.equity_us]
                ),
            )
            .order_by(Instrument.symbol)
            .limit(500)
        )
        return SymbolsResponse(
            symbols=[
                SymbolItem(
                    market=self.market_name(instrument.type.value),
                    symbol=instrument.symbol,
                    name=instrument.name,
                    currency=instrument.base_currency,
                )
                for instrument in result.scalars().all()
            ]
        )

    @classmethod
    async def _position_names(
        cls,
        db: AsyncSession,
        positions: Sequence[_PositionIdentity],
    ) -> dict[tuple[str, str], str]:
        """주식은 SymbolMaster, 비주식은 기존 Instrument에서 이름을 찾는다."""
        equity_keys = tuple(
            dict.fromkeys(
                (position.market, position.symbol)
                for position in positions
                if position.market in {"KRX", "US"}
            )
        )
        names: dict[tuple[str, str], str] = {}
        if equity_keys:
            master_rows = (
                await db.execute(
                    select(
                        SymbolMaster.market,
                        SymbolMaster.symbol,
                        SymbolMaster.name,
                    ).where(
                        tuple_(
                            SymbolMaster.market,
                            SymbolMaster.symbol,
                        ).in_(equity_keys)
                    )
                )
            ).all()
            names.update(
                {
                    (market, symbol): name.strip()
                    for market, symbol, name in master_rows
                    if name and name.strip() and name.strip() != symbol
                }
            )

        legacy_types = {
            "CRYPTO": InstrumentType.crypto,
            "FOREX": InstrumentType.forex,
            "INDEX": InstrumentType.index,
        }
        requested_legacy_types = {
            legacy_types[position.market]
            for position in positions
            if position.market in legacy_types
        }
        legacy_symbols = {
            position.symbol for position in positions if position.market in legacy_types
        }
        if requested_legacy_types and legacy_symbols:
            instrument_rows = (
                await db.execute(
                    select(
                        Instrument.type,
                        Instrument.symbol,
                        Instrument.name,
                    ).where(
                        Instrument.type.in_(requested_legacy_types),
                        Instrument.symbol.in_(legacy_symbols),
                    )
                )
            ).all()
            names.update(
                {
                    (cls.market_name(instrument_type.value), symbol): name.strip()
                    for instrument_type, symbol, name in instrument_rows
                    if name and name.strip() and name.strip() != symbol
                }
            )
        return names

    @staticmethod
    def account_id(account: PaperAccount) -> str:
        return f"PAPER-{account.id}"

    @staticmethod
    def market_name(instrument_type: str) -> str:
        return {
            "equity_kr": "KRX",
            "equity_us": "US",
            "crypto": "CRYPTO",
        }.get(instrument_type, instrument_type.upper())

    @staticmethod
    def _required_decimal(value: object) -> Decimal:
        parsed = PaperAccountAdapter._optional_decimal(value)
        if parsed is None or parsed <= 0:
            raise MobileApiError(502, "BROKER_ERROR", "유효한 시세가 없습니다.")
        return parsed

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None


paper_account_adapter = PaperAccountAdapter()
