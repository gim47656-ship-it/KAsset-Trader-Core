from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_candles_1m_toss import KRTossMinuteCandle
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.research_candles.toss_minute_source import TossMinuteCandleRow

TOSS_MINUTE_BATCH_SIZE = 20
TOSS_MINUTE_UPSERT_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class TossMinuteCoverage:
    """Newest stored bar of one active symbol inside the lookback window.

    ``retrieved_at`` is when that bar was last fetched, so it is the symbol's
    last successful collection time even after its session closed. Both fields
    are ``None`` when nothing was stored inside the window.
    """

    symbol: str
    latest_time_utc: datetime | None
    retrieved_at: datetime | None


class TossMinuteCandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stalest_active_symbols(
        self,
        *,
        limit: int,
        since: datetime,
        exclude: Collection[str] = (),
    ) -> list[TossMinuteCoverage]:
        """Active symbols whose last successful fetch is oldest, never-fetched first.

        One ``(symbol, time_utc DESC)`` index probe per active symbol, bounded
        to chunks newer than ``since``; no scan of the hypertable's rows.
        """

        if limit <= 0:
            return []
        candle = KRTossMinuteCandle
        latest = (
            select(candle.time_utc, candle.retrieved_at)
            .where(
                candle.symbol == KRSymbolUniverse.symbol,
                candle.time_utc >= since,
            )
            .order_by(candle.time_utc.desc())
            .limit(1)
            .correlate(KRSymbolUniverse)
            .lateral("latest")
        )
        statement = (
            select(
                KRSymbolUniverse.symbol,
                latest.c.time_utc,
                latest.c.retrieved_at,
            )
            .select_from(KRSymbolUniverse)
            .outerjoin(latest, true())
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(
                latest.c.retrieved_at.asc().nulls_first(),
                KRSymbolUniverse.symbol.asc(),
            )
            .limit(int(limit))
        )
        if exclude:
            statement = statement.where(KRSymbolUniverse.symbol.not_in(sorted(exclude)))
        result = await self._session.execute(statement)
        return [
            TossMinuteCoverage(
                symbol=symbol,
                latest_time_utc=latest_time_utc,
                retrieved_at=retrieved_at,
            )
            for symbol, latest_time_utc, retrieved_at in result.all()
        ]

    async def neighbour_session_dates(
        self, *, session_date: date
    ) -> tuple[date | None, date | None]:
        """Nearest stored session dates before and after ``session_date``."""

        session_day = KRTossMinuteCandle.session_date_kst
        previous = await self._session.scalar(
            select(func.max(session_day)).where(session_day < session_date)
        )
        following = await self._session.scalar(
            select(func.min(session_day)).where(session_day > session_date)
        )
        return previous, following

    async def session_segment_counts(
        self, *, session_date: date
    ) -> list[tuple[str, str, int]]:
        """``(symbol, segment, stored bars)`` for one KST session date."""

        candle = KRTossMinuteCandle
        result = await self._session.execute(
            select(candle.symbol, candle.session_segment, func.count())
            .where(candle.session_date_kst == session_date)
            .group_by(candle.symbol, candle.session_segment)
        )
        return [
            (symbol, segment, int(count)) for symbol, segment, count in result.all()
        ]

    async def session_minute_presence(
        self, *, session_date: date, symbols: Sequence[str] | None = None
    ) -> list[tuple[str, datetime, int]]:
        """``(segment, minute, symbols with that bar)`` for one session date."""

        candle = KRTossMinuteCandle
        statement = (
            select(candle.session_segment, candle.time_utc, func.count())
            .where(candle.session_date_kst == session_date)
            .group_by(candle.session_segment, candle.time_utc)
        )
        if symbols is not None:
            statement = statement.where(candle.symbol.in_(list(symbols)))
        result = await self._session.execute(statement)
        return [
            (segment, minute, int(count)) for segment, minute, count in result.all()
        ]

    async def session_minutes(
        self, *, session_date: date, symbols: Sequence[str]
    ) -> list[tuple[str, datetime]]:
        """Stored ``(symbol, minute)`` pairs of the given symbols on one date."""

        if not symbols:
            return []
        candle = KRTossMinuteCandle
        result = await self._session.execute(
            select(candle.symbol, candle.time_utc).where(
                candle.session_date_kst == session_date,
                candle.symbol.in_(list(symbols)),
            )
        )
        return [(symbol, minute) for symbol, minute in result.all()]

    async def upsert(self, rows: Sequence[TossMinuteCandleRow]) -> int:
        deduped = {(row.time_utc, row.symbol): row for row in rows}
        if not deduped:
            return 0

        values = [row.as_insert_values() for row in deduped.values()]
        for start in range(0, len(values), TOSS_MINUTE_UPSERT_CHUNK_SIZE):
            chunk = values[start : start + TOSS_MINUTE_UPSERT_CHUNK_SIZE]
            statement = insert(KRTossMinuteCandle).values(chunk)
            statement = statement.on_conflict_do_update(
                constraint="uq_research_kr_candles_1m_toss_time_symbol",
                set_={
                    "session_date_kst": statement.excluded.session_date_kst,
                    "session_segment": statement.excluded.session_segment,
                    "source": statement.excluded.source,
                    "open": statement.excluded.open,
                    "high": statement.excluded.high,
                    "low": statement.excluded.low,
                    "close": statement.excluded.close,
                    "volume": statement.excluded.volume,
                    "value": statement.excluded.value,
                    "value_semantics": statement.excluded.value_semantics,
                    "is_padding": statement.excluded.is_padding,
                    "pre_nxt": statement.excluded.pre_nxt,
                    "retrieved_at": statement.excluded.retrieved_at,
                    "batch_id": statement.excluded.batch_id,
                },
            )
            await self._session.execute(statement)
        return len(values)
