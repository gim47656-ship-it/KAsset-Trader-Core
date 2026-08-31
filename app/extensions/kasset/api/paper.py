"""Thin Android adapter over the existing Core PaperTradingService."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper_schemas import (
    Balance,
    CashBalance,
    Position,
    PositionsResponse,
    Quote,
    SymbolItem,
    SymbolsResponse,
)
from app.extensions.kasset.models import AndroidPaperAccount
from app.models.paper_trading import PaperAccount, PaperTrade
from app.models.trading import Instrument, InstrumentType
from app.services.exchange_rate_service import (
    UsdKrwExchangeRateQuote,
    get_usd_krw_rate_details,
)
from app.services.paper_trading_service import PaperTradingService

_DEFAULT_ACCOUNT_NAME_PREFIX = "KAsset Android PAPER"
_FX_QUOTE_UNAVAILABLE = "FX_QUOTE_UNAVAILABLE"
_FX_QUOTE_PAIR_MISMATCH = "FX_QUOTE_PAIR_MISMATCH"
_FX_QUOTE_INVALID = "FX_QUOTE_INVALID"
_FX_QUOTE_INCOMPLETE = "FX_QUOTE_INCOMPLETE"
_FX_QUOTE_STALE = "FX_QUOTE_STALE"

logger = logging.getLogger(__name__)


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
        names = await self._instrument_names(
            db, [item["symbol"] for item in raw_positions]
        )
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
        return PositionsResponse(
            positions=[
                Position(
                    broker="PAPER",
                    account_id=self.account_id(account),
                    market=self.market_name(item["instrument_type"]),
                    symbol=item["symbol"],
                    name=names.get(item["symbol"]),
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
        )

    async def _quote_from_candles(
        self,
        db: AsyncSession,
        symbol: str,
    ) -> dict[str, object]:
        """KIS 실시세가 막힌 서버에서 Toss 수집 일봉 종가로 시세를 만든다."""
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

    async def quote(self, db: AsyncSession, *, market: str, symbol: str) -> Quote:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip().upper()
        try:
            if normalized_market in {"KRX", "KR"}:
                # KIS는 미연결 브로커라 토큰 시도 자체가 수 초를 태운다.
                # KRX PAPER 시세는 곧장 저장 캔들 종가로 만든다(상위에서 NH 공용
                # 채널이 먼저 시도된 뒤에만 이 경로에 온다).
                raw = await self._quote_from_candles(db, normalized_symbol)
                market_name = "KRX"
                currency = "KRW"
            elif normalized_market in {"US", "NYSE", "NASDAQ"}:
                from app.mcp_server.tooling.market_data_quotes import (
                    _fetch_quote_equity_us,
                )

                raw = await _fetch_quote_equity_us(normalized_symbol)
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
        name = (await self._instrument_names(db, [normalized_symbol])).get(
            normalized_symbol
        )
        as_of_raw = raw.get("price_as_of") or raw.get("quote_asof")
        as_of = iso_z(as_of_raw) if isinstance(as_of_raw, datetime) else iso_z()
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
    async def _instrument_names(db: AsyncSession, symbols: list[str]) -> dict[str, str]:
        if not symbols:
            return {}
        result = await db.execute(
            select(Instrument.symbol, Instrument.name).where(
                Instrument.symbol.in_(set(symbols))
            )
        )
        return {symbol: name for symbol, name in result.all() if name}

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
