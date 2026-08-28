"""Owner-scoped watchlist and instrument search for the mobile API."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import (
    InstrumentSearchMarket,
    InstrumentSearchItem,
    InstrumentSearchResponse,
    WatchlistItem,
    WatchlistMarket,
    WatchlistResponse,
)
from app.models.trading import Exchange, Instrument, InstrumentType, UserWatchItem
from app.models.symbol_master import SymbolMaster

_MARKET_TYPES: dict[WatchlistMarket, InstrumentType] = {
    "KRX": InstrumentType.equity_kr,
    "US": InstrumentType.equity_us,
    "CRYPTO": InstrumentType.crypto,
}


class MobileWatchlistService:
    async def list_items(
        self,
        db: AsyncSession,
        owner_user_id: int,
    ) -> WatchlistResponse:
        rows = await db.execute(
            select(UserWatchItem, Instrument, Exchange.code)
            .join(Instrument, Instrument.id == UserWatchItem.instrument_id)
            .outerjoin(Exchange, Exchange.id == Instrument.exchange_id)
            .where(
                UserWatchItem.user_id == owner_user_id,
                UserWatchItem.is_active.is_(True),
                Instrument.is_active.is_(True),
                Instrument.type.in_(tuple(_MARKET_TYPES.values())),
            )
            .order_by(UserWatchItem.id)
        )
        return WatchlistResponse(
            items=[
                self._watchlist_item(instrument, exchange_code)
                for _watch_item, instrument, exchange_code in rows.all()
            ]
        )

    async def add_item(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        symbol: str,
        market: WatchlistMarket,
    ) -> tuple[WatchlistItem, bool]:
        normalized_symbol = self._normalize_symbol(symbol)
        row = await self._find_instrument(
            db,
            normalized_symbol,
            market,
            active_only=True,
        )
        if row is None:
            row = await self._materialize_instrument_from_master(
                db,
                normalized_symbol,
                market,
            )

        instrument, exchange_code = row
        watch_item = await db.scalar(
            select(UserWatchItem)
            .where(
                UserWatchItem.user_id == owner_user_id,
                UserWatchItem.instrument_id == instrument.id,
            )
            .order_by(UserWatchItem.id)
            .limit(1)
            .with_for_update()
        )
        response_item = self._watchlist_item(instrument, exchange_code)
        if watch_item is not None:
            if watch_item.is_active:
                await db.commit()
                return response_item, False
            watch_item.is_active = True
            await db.commit()
            return response_item, True

        db.add(
            UserWatchItem(
                user_id=owner_user_id,
                instrument_id=instrument.id,
                notify_cooldown=timedelta(hours=1),
                is_active=True,
            )
        )
        await db.commit()
        return response_item, True

    async def _find_instrument(
        self,
        db: AsyncSession,
        symbol: str,
        market: WatchlistMarket,
        *,
        active_only: bool,
    ) -> tuple[Instrument, str | None] | None:
        statement = (
            select(Instrument, Exchange.code)
            .outerjoin(Exchange, Exchange.id == Instrument.exchange_id)
            .where(
                func.upper(Instrument.symbol) == symbol,
                Instrument.type == _MARKET_TYPES[market],
            )
            .order_by(Instrument.id)
            .limit(1)
            .with_for_update(of=Instrument)
        )
        if active_only:
            statement = statement.where(Instrument.is_active.is_(True))
        row = (await db.execute(statement)).first()
        return None if row is None else (row[0], row[1])

    async def _materialize_instrument_from_master(
        self,
        db: AsyncSession,
        symbol: str,
        market: WatchlistMarket,
    ) -> tuple[Instrument, str | None]:
        master = None
        if market in {"KRX", "US"}:
            master = await db.scalar(
                select(SymbolMaster)
                .where(
                    SymbolMaster.market == market,
                    func.upper(SymbolMaster.symbol) == symbol,
                    SymbolMaster.is_active.is_(True),
                )
                .with_for_update()
            )
        if master is None:
            raise MobileApiError(
                404,
                "UNKNOWN_SYMBOL",
                "등록되지 않았거나 비활성화된 종목입니다.",
            )

        existing = await self._find_instrument(
            db,
            symbol,
            market,
            active_only=False,
        )
        if existing is not None:
            instrument, exchange_code = existing
            instrument.is_active = True
            if not instrument.name:
                instrument.name = master.name
            await db.flush()
            return instrument, exchange_code

        instrument = Instrument(
            symbol=master.symbol,
            full_symbol=master.symbol,
            name=master.name,
            type=_MARKET_TYPES[market],
            base_currency="KRW" if market == "KRX" else "USD",
            is_active=True,
        )
        db.add(instrument)
        await db.flush()
        return instrument, None

    async def remove_item(
        self,
        db: AsyncSession,
        owner_user_id: int,
        *,
        symbol: str,
        market: WatchlistMarket,
    ) -> None:
        normalized_symbol = self._normalize_symbol(symbol)
        rows = await db.scalars(
            select(UserWatchItem)
            .join(Instrument, Instrument.id == UserWatchItem.instrument_id)
            .where(
                UserWatchItem.user_id == owner_user_id,
                UserWatchItem.is_active.is_(True),
                func.upper(Instrument.symbol) == normalized_symbol,
                Instrument.type == _MARKET_TYPES[market],
            )
            .order_by(UserWatchItem.id)
            .with_for_update(of=UserWatchItem)
        )
        for watch_item in rows.all():
            watch_item.is_active = False
        await db.commit()

    async def search_instruments(
        self,
        db: AsyncSession,
        *,
        query: str,
        market: InstrumentSearchMarket,
        limit: int = 20,
    ) -> InstrumentSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise MobileApiError(422, "VALIDATION_ERROR", "검색어를 입력해 주세요.")

        escaped_query = self._escape_like(normalized_query)
        prefix_pattern = f"{escaped_query}%"
        contains_pattern = f"%{escaped_query}%"
        alias_match = exists(
            select(Instrument.id).where(
                Instrument.is_active.is_(True),
                func.upper(Instrument.symbol) == func.upper(SymbolMaster.symbol),
                or_(
                    and_(
                        SymbolMaster.market == "KRX",
                        Instrument.type == InstrumentType.equity_kr,
                    ),
                    and_(
                        SymbolMaster.market == "US",
                        Instrument.type == InstrumentType.equity_us,
                    ),
                ),
                func.array_to_string(Instrument.aliases, " ").ilike(
                    contains_pattern,
                    escape="\\",
                ),
            )
        )
        matches = or_(
            SymbolMaster.symbol.ilike(prefix_pattern, escape="\\"),
            SymbolMaster.name.ilike(contains_pattern, escape="\\"),
            SymbolMaster.name_en.ilike(contains_pattern, escape="\\"),
            alias_match,
        )
        statement = select(SymbolMaster).where(
            SymbolMaster.is_active.is_(True),
            matches,
        )
        if market != "ALL":
            statement = statement.where(SymbolMaster.market == market)

        prefix_rank = case(
            (
                or_(
                    SymbolMaster.symbol.ilike(prefix_pattern, escape="\\"),
                    SymbolMaster.name.ilike(prefix_pattern, escape="\\"),
                    SymbolMaster.name_en.ilike(prefix_pattern, escape="\\"),
                ),
                0,
            ),
            else_=1,
        )
        market_rank = case((SymbolMaster.market == "KRX", 0), else_=1)
        capped_limit = min(max(limit, 1), 100)
        rows = await db.scalars(
            statement.order_by(
                prefix_rank,
                market_rank,
                SymbolMaster.symbol,
            ).limit(capped_limit)
        )
        return InstrumentSearchResponse(
            items=[
                InstrumentSearchItem(
                    symbol=row.symbol,
                    name=row.name,
                    market=row.market,
                )
                for row in rows.all()
            ]
        )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise MobileApiError(422, "VALIDATION_ERROR", "종목 코드를 입력해 주세요.")
        return normalized

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @classmethod
    def _watchlist_item(
        cls,
        instrument: Instrument,
        exchange_code: str | None,
    ) -> WatchlistItem:
        return WatchlistItem(
            symbol=instrument.symbol,
            name=instrument.name or instrument.symbol,
            market=cls._wire_market(instrument.type, exchange_code),
            instrument_id=instrument.id,
        )

    @staticmethod
    def _wire_market(
        instrument_type: InstrumentType,
        exchange_code: str | None,
    ) -> WatchlistMarket:
        normalized_exchange = (exchange_code or "").upper()
        if instrument_type == InstrumentType.equity_kr:
            return "KRX"
        if instrument_type == InstrumentType.equity_us:
            return "US"
        if instrument_type == InstrumentType.crypto:
            return "CRYPTO"
        raise ValueError(
            f"unsupported instrument market: {instrument_type.value}/{normalized_exchange}"
        )


watchlist_service = MobileWatchlistService()

__all__ = ["MobileWatchlistService", "watchlist_service"]
