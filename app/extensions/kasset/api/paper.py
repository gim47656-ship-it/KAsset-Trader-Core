"""Thin Android adapter over the existing Core PaperTradingService."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from app.models.paper_trading import PaperAccount, PaperTrade
from app.models.trading import Instrument, InstrumentType
from app.services.paper_trading_service import PaperTradingService

_DEFAULT_ACCOUNT_NAME = "KAsset Android PAPER"


def decimal_text(value: Decimal | int | str) -> str:
    return format(Decimal(value), "f")


def iso_z(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class PaperAccountAdapter:
    async def default_account(self, db: AsyncSession) -> PaperAccount:
        result = await db.execute(
            select(PaperAccount).where(PaperAccount.name == _DEFAULT_ACCOUNT_NAME)
        )
        account = result.scalar_one_or_none()
        if account is not None:
            return account
        service = PaperTradingService(db)
        try:
            return await service.create_account(
                name=_DEFAULT_ACCOUNT_NAME,
                initial_capital_krw=Decimal("10000000"),
                description="KAsset Android compatibility account",
            )
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(PaperAccount).where(PaperAccount.name == _DEFAULT_ACCOUNT_NAME)
            )
            account = result.scalar_one_or_none()
            if account is None:
                raise
            return account

    async def resolve_account(
        self, db: AsyncSession, account_id: str | None
    ) -> PaperAccount:
        account = await self.default_account(db)
        expected = self.account_id(account)
        if account_id not in {None, "", expected}:
            raise MobileApiError(404, "NOT_FOUND", "PAPER 계좌를 찾을 수 없습니다.")
        return account

    async def balance(self, db: AsyncSession) -> Balance:
        account = await self.default_account(db)
        service = PaperTradingService(db)
        positions = await service.get_positions(account.id)
        evaluation = sum(
            (
                Decimal(str(item["evaluation_amount"]))
                for item in positions
                if item["instrument_type"] == "equity_kr"
                and item["evaluation_amount"] is not None
            ),
            Decimal("0"),
        )
        unrealized = sum(
            (
                Decimal(str(item["unrealized_pnl"]))
                for item in positions
                if item["instrument_type"] == "equity_kr"
                and item["unrealized_pnl"] is not None
            ),
            Decimal("0"),
        )
        realized_result = await db.execute(
            select(PaperTrade.realized_pnl).where(
                PaperTrade.account_id == account.id,
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
            evaluation_amount=decimal_text(evaluation),
            total_assets=decimal_text(Decimal(account.cash_krw) + evaluation),
            unrealized_pnl=decimal_text(unrealized),
            realized_pnl=decimal_text(realized),
            updated_at=iso_z(),
        )

    async def positions(self, db: AsyncSession) -> PositionsResponse:
        account = await self.default_account(db)
        service = PaperTradingService(db)
        raw_positions = await service.get_positions(account.id)
        names = await self._instrument_names(db, [item["symbol"] for item in raw_positions])
        now = iso_z()
        return PositionsResponse(
            positions=[
                Position(
                    broker="PAPER",
                    account_id=self.account_id(account),
                    market=self.market_name(item["instrument_type"]),
                    symbol=item["symbol"],
                    name=names.get(item["symbol"]),
                    currency=self.currency(item["instrument_type"]),
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
                    updated_at=now,
                )
                for item in raw_positions
            ]
        )

    async def quote(
        self, db: AsyncSession, *, market: str, symbol: str
    ) -> Quote:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip().upper()
        try:
            if normalized_market in {"KRX", "KR"}:
                from app.mcp_server.tooling.market_data_quotes import (
                    _fetch_quote_equity_kr,
                )

                raw = await _fetch_quote_equity_kr(normalized_symbol)
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
            raise MobileApiError(404, "NOT_FOUND", "종목 시세를 찾을 수 없습니다.") from err
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
    def currency(instrument_type: str) -> str:
        return "USD" if instrument_type == "equity_us" else "KRW"

    @staticmethod
    async def _instrument_names(
        db: AsyncSession, symbols: list[str]
    ) -> dict[str, str]:
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
