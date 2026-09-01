from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_candles_1m_toss import KRTossMinuteCandle
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.research_candles.toss_minute_source import TossMinuteCandleRow

TOSS_MINUTE_BATCH_SIZE = 20
TOSS_MINUTE_UPSERT_CHUNK_SIZE = 500


class TossMinuteCandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_symbol_count(self) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(KRSymbolUniverse)
            .where(KRSymbolUniverse.is_active.is_(True))
        )
        return int(result.scalar_one())

    async def active_symbol_batch(
        self,
        *,
        total: int,
        offset: int,
        limit: int = TOSS_MINUTE_BATCH_SIZE,
    ) -> list[str]:
        if total <= 0 or limit <= 0:
            return []
        batch_limit = min(int(limit), total)
        normalized_offset = int(offset) % total
        first = await self._select_active_symbols(
            offset=normalized_offset,
            limit=batch_limit,
        )
        if len(first) == batch_limit:
            return first
        wrapped = await self._select_active_symbols(
            offset=0,
            limit=batch_limit - len(first),
        )
        return [*first, *wrapped]

    async def _select_active_symbols(self, *, offset: int, limit: int) -> list[str]:
        result = await self._session.execute(
            select(KRSymbolUniverse.symbol)
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

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
