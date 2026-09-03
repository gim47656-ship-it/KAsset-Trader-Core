from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kasset_intraday_rvol_shadow import KAssetIntradayRvolShadow

_RVOL_SHADOW_INSERT_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class RvolShadowObservation:
    observed_at: datetime
    cycle_trace_id: str | None
    owner_user_id: int
    symbol: str
    market: str
    direction: str
    bucket_start_kst: time
    completed_bars: int
    session_decision_status: str
    session_decision_reason: str | None
    same_time_baseline_median_5m: Decimal | None
    same_time_baseline_median_20m: Decimal | None
    session_rvol_5m: Decimal | None
    session_status_5m: str
    session_rvol_20m: Decimal | None
    session_status_20m: str
    same_time_rvol_5m: Decimal | None
    same_time_status_5m: str
    same_time_sample_days_5m: int
    same_time_rvol_20m: Decimal | None
    same_time_status_20m: str
    same_time_sample_days_20m: int


class RvolShadowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_many(self, rows: Sequence[RvolShadowObservation]) -> int:
        if not rows:
            return 0

        for start in range(0, len(rows), _RVOL_SHADOW_INSERT_CHUNK_SIZE):
            chunk = rows[start : start + _RVOL_SHADOW_INSERT_CHUNK_SIZE]
            values = [_as_insert_values(row) for row in chunk]
            statement = insert(KAssetIntradayRvolShadow).values(values)
            # 같은 cycle·symbol 재시도는 최신 shadow 계산값으로 교체하되,
            # 최초 적재 시각인 created_at은 보존한다.
            statement = statement.on_conflict_do_update(
                index_elements=["cycle_trace_id", "symbol"],
                index_where=text("cycle_trace_id IS NOT NULL"),
                set_={
                    "observed_at": statement.excluded.observed_at,
                    "owner_user_id": statement.excluded.owner_user_id,
                    "market": statement.excluded.market,
                    "direction": statement.excluded.direction,
                    "bucket_start_kst": statement.excluded.bucket_start_kst,
                    "completed_bars": statement.excluded.completed_bars,
                    "session_decision_status": (
                        statement.excluded.session_decision_status
                    ),
                    "session_decision_reason": (
                        statement.excluded.session_decision_reason
                    ),
                    "same_time_baseline_median_5m": (
                        statement.excluded.same_time_baseline_median_5m
                    ),
                    "same_time_baseline_median_20m": (
                        statement.excluded.same_time_baseline_median_20m
                    ),
                    "session_rvol_5m": statement.excluded.session_rvol_5m,
                    "session_status_5m": statement.excluded.session_status_5m,
                    "session_rvol_20m": statement.excluded.session_rvol_20m,
                    "session_status_20m": statement.excluded.session_status_20m,
                    "same_time_rvol_5m": statement.excluded.same_time_rvol_5m,
                    "same_time_status_5m": (statement.excluded.same_time_status_5m),
                    "same_time_sample_days_5m": (
                        statement.excluded.same_time_sample_days_5m
                    ),
                    "same_time_rvol_20m": statement.excluded.same_time_rvol_20m,
                    "same_time_status_20m": (statement.excluded.same_time_status_20m),
                    "same_time_sample_days_20m": (
                        statement.excluded.same_time_sample_days_20m
                    ),
                },
            )
            await self._session.execute(statement)
        return len(rows)


def _as_insert_values(row: RvolShadowObservation) -> dict[str, object]:
    return {
        "observed_at": row.observed_at,
        "cycle_trace_id": row.cycle_trace_id,
        "owner_user_id": row.owner_user_id,
        "symbol": row.symbol,
        "market": row.market,
        "direction": row.direction,
        "bucket_start_kst": row.bucket_start_kst,
        "completed_bars": row.completed_bars,
        "session_decision_status": row.session_decision_status,
        "session_decision_reason": row.session_decision_reason,
        "same_time_baseline_median_5m": row.same_time_baseline_median_5m,
        "same_time_baseline_median_20m": row.same_time_baseline_median_20m,
        "session_rvol_5m": row.session_rvol_5m,
        "session_status_5m": row.session_status_5m,
        "session_rvol_20m": row.session_rvol_20m,
        "session_status_20m": row.session_status_20m,
        "same_time_rvol_5m": row.same_time_rvol_5m,
        "same_time_status_5m": row.same_time_status_5m,
        "same_time_sample_days_5m": row.same_time_sample_days_5m,
        "same_time_rvol_20m": row.same_time_rvol_20m,
        "same_time_status_20m": row.same_time_status_20m,
        "same_time_sample_days_20m": row.same_time_sample_days_20m,
    }


__all__ = ["RvolShadowObservation", "RvolShadowRepository"]
