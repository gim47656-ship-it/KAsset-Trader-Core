"""Owner-scoped watchlist and instrument search for the mobile API."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import (
    InstrumentSearchItem,
    InstrumentSearchResponse,
    WatchlistItem,
    WatchlistMarket,
    WatchlistResponse,
)
from app.models.trading import Exchange, Instrument, InstrumentType, UserWatchItem

_MARKET_TYPES: dict[WatchlistMarket, InstrumentType] = {
    "KRX": InstrumentType.equity_kr,
    "US": InstrumentType.equity_us,
    "CRYPTO": InstrumentType.crypto,
}
_MAX_SEARCH_RESULTS = 20


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
        row = (
            await db.execute(
                select(Instrument, Exchange.code)
                .outerjoin(Exchange, Exchange.id == Instrument.exchange_id)
                .where(
                    func.upper(Instrument.symbol) == normalized_symbol,
                    Instrument.type == _MARKET_TYPES[market],
                    Instrument.is_active.is_(True),
                )
                .order_by(Instrument.id)
                .limit(1)
                .with_for_update(of=Instrument)
            )
        ).first()
        if row is None:
            raise MobileApiError(
                404,
                "UNKNOWN_SYMBOL",
                "등록되지 않았거나 비활성화된 종목입니다.",
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
        market: WatchlistMarket,
    ) -> InstrumentSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise MobileApiError(422, "VALIDATION_ERROR", "검색어를 입력해 주세요.")
        pattern = f"%{self._escape_like(normalized_query)}%"
        rows = await db.execute(
            select(Instrument, Exchange.code)
            .outerjoin(Exchange, Exchange.id == Instrument.exchange_id)
            .where(
                Instrument.type == _MARKET_TYPES[market],
                Instrument.is_active.is_(True),
                or_(
                    Instrument.symbol.ilike(pattern, escape="\\"),
                    Instrument.name.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(Instrument.symbol, Instrument.id)
            .limit(_MAX_SEARCH_RESULTS)
        )
        return InstrumentSearchResponse(
            items=[
                InstrumentSearchItem(
                    symbol=instrument.symbol,
                    name=instrument.name or instrument.symbol,
                    market=self._wire_market(instrument.type, exchange_code),
                )
                for instrument, exchange_code in rows.all()
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
