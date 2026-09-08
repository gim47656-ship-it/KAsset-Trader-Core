"""Market events repository — only place that writes the three market_events tables (ROB-128).

Idempotency strategy:
* When `source_event_id` is provided, upsert keyed by (source, category, market, source_event_id).
* Otherwise upsert keyed by (source, category, market, symbol, event_date, fiscal_year, fiscal_quarter).
Both keys are enforced by partial unique indexes (see migration).

Values are upserted by (event_id, metric_name, period).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)

# 삭제된 ``app.services.alpaca_paper_ledger_service``에 있던 마스킹 규칙을 값과
# 정규식 그대로 옮겨왔다. raw_payload_json은 외부 응답 원문이므로 영속화 전에
# 반드시 이 규칙을 통과해야 한다(규칙 완화 금지).
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|authorization|token|account[_-]?no|"
    r"account[_-]?number|account[_-]?id|account[_-]?identifier|"
    r"email|passwd|password|credential)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_VALUE_RE = re.compile(
    r"(?P<prefix>\b(api[_-]?key|secret|token|account[_-]?no|"
    r"account[_-]?number|account[_-]?id|account[_-]?identifier|"
    r"email|passwd|password|credential)\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_TEXT_VALUE_RE = re.compile(
    r"(?P<prefix>\b(authorization|bearer)\b\s*[:=]?\s*)"
    r"(?P<value>(?:bearer\s+)?\S+)",
    re.IGNORECASE,
)
_JSON_SENSITIVE_RE = re.compile(
    r"(?P<prefix>\"(?:api[_-]?key|secret|token|account[_-]?no|"
    r"account[_-]?number|account[_-]?id|account[_-]?identifier|"
    r"email|passwd|password|credential)\"\s*:\s*\")"
    r"(?P<value>[^\"]*)"
    r"(?P<suffix>\")",
    re.IGNORECASE,
)


def _redact_sensitive_text(text: str | None) -> str | None:
    """Redact key=value/key: value secrets from operator narrative text."""
    if text is None:
        return None
    redacted = _AUTHORIZATION_TEXT_VALUE_RE.sub(r"\g<prefix>[REDACTED]", text)
    redacted = _JSON_SENSITIVE_RE.sub(r"\g<prefix>[REDACTED]\g<suffix>", redacted)
    return _SENSITIVE_TEXT_VALUE_RE.sub(r"\g<prefix>[REDACTED]", redacted)


def _redact_sensitive_keys(payload: Any) -> Any:
    """Recursively redact sensitive keys from dicts/lists before persistence."""
    if isinstance(payload, dict):
        return {
            k: "[REDACTED]"
            if _SENSITIVE_KEY_RE.search(str(k))
            else _redact_sensitive_keys(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_sensitive_keys(item) for item in payload]
    if isinstance(payload, str):
        return _redact_sensitive_text(payload)
    return payload


class MarketEventsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_event_with_values(
        self,
        event_data: dict[str, Any],
        values: list[dict[str, Any]],
    ) -> MarketEvent:
        payload = dict(event_data)
        if payload.get("raw_payload_json") is not None:
            payload["raw_payload_json"] = _redact_sensitive_keys(
                payload["raw_payload_json"]
            )

        update_columns = {
            k: payload.get(k)
            for k in (
                "country",
                "currency",
                "company_name",
                "title",
                "release_time_utc",
                "release_time_local",
                "source_timezone",
                "time_hint",
                "importance",
                "status",
                "source_url",
                "raw_payload_json",
                "fetched_at",
            )
            if k in payload
        }
        update_columns["updated_at"] = func.now()

        if payload.get("source_event_id"):
            stmt = (
                pg_insert(MarketEvent.__table__)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=["source", "category", "market", "source_event_id"],
                    index_where=MarketEvent.__table__.c.source_event_id.isnot(None),
                    set_=update_columns,
                )
                .returning(MarketEvent.__table__.c.id)
            )
            result = await self.db.execute(stmt)
            event_id = result.scalar_one()
        else:
            event = await self._find_event_by_natural_key(payload)
            if event is None:
                event = MarketEvent(**payload)
                self.db.add(event)
                await self.db.flush()
            else:
                for key, value in update_columns.items():
                    if key != "updated_at":
                        setattr(event, key, value)
                event.updated_at = datetime.now(UTC)
                await self.db.flush()
            event_id = event.id

        for value in values:
            await self._upsert_value(event_id, value)

        await self.db.flush()
        event = (
            await self.db.execute(select(MarketEvent).where(MarketEvent.id == event_id))
        ).scalar_one()
        return event

    async def _find_event_by_natural_key(
        self, payload: dict[str, Any]
    ) -> MarketEvent | None:
        stmt = select(MarketEvent).where(
            MarketEvent.source == payload.get("source"),
            MarketEvent.category == payload.get("category"),
            MarketEvent.market == payload.get("market"),
            MarketEvent.event_date == payload.get("event_date"),
            MarketEvent.source_event_id.is_(None),
        )
        for attr in ("symbol", "fiscal_year", "fiscal_quarter"):
            column = getattr(MarketEvent, attr)
            value = payload.get(attr)
            stmt = stmt.where(column.is_(None) if value is None else column == value)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _upsert_value(self, event_id: int, value: dict[str, Any]) -> None:
        payload = {**value, "event_id": event_id}
        update_columns = {
            k: payload.get(k)
            for k in (
                "actual",
                "forecast",
                "previous",
                "revised_previous",
                "unit",
                "surprise",
                "surprise_pct",
                "released_at",
            )
            if k in payload
        }
        update_columns["updated_at"] = func.now()
        stmt = select(MarketEventValue).where(
            MarketEventValue.event_id == event_id,
            MarketEventValue.metric_name == payload.get("metric_name"),
        )
        period = payload.get("period")
        stmt = stmt.where(
            MarketEventValue.period.is_(None)
            if period is None
            else MarketEventValue.period == period
        )
        existing = (await self.db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            self.db.add(MarketEventValue(**payload))
        else:
            for key, value_ in update_columns.items():
                if key != "updated_at":
                    setattr(existing, key, value_)
            existing.updated_at = datetime.now(UTC)
        await self.db.flush()

    # -- partition state ----------------------------------------------------

    async def get_or_create_partition(
        self,
        *,
        source: str,
        category: str,
        market: str,
        partition_date: date,
    ) -> MarketEventIngestionPartition:
        existing = (
            await self.db.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == source,
                    MarketEventIngestionPartition.category == category,
                    MarketEventIngestionPartition.market == market,
                    MarketEventIngestionPartition.partition_date == partition_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = MarketEventIngestionPartition(
            source=source,
            category=category,
            market=market,
            partition_date=partition_date,
            status="pending",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def mark_partition_running(
        self, partition: MarketEventIngestionPartition
    ) -> None:
        partition.status = "running"
        partition.started_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_succeeded(
        self,
        partition: MarketEventIngestionPartition,
        *,
        event_count: int,
    ) -> None:
        partition.status = "succeeded"
        partition.event_count = event_count
        partition.finished_at = datetime.now(UTC)
        partition.last_error = None
        await self.db.flush()

    async def mark_partition_failed(
        self,
        partition: MarketEventIngestionPartition,
        *,
        error: str,
    ) -> None:
        partition.status = "failed"
        partition.finished_at = datetime.now(UTC)
        partition.last_error = error[:2000]
        partition.retry_count = (partition.retry_count or 0) + 1
        await self.db.flush()

    async def list_succeeded_partitions_in_range(
        self,
        *,
        source: str,
        category: str,
        market: str,
        from_date: date,
        to_date: date,
    ) -> set[date]:
        """Return the set of `partition_date`s with status='succeeded' in the
        inclusive window.

        Used to skip already-succeeded partitions during rolling-window ingestion
        and avoid unnecessarily reconsuming external API quota on a rerun.

        Note: callers MUST NOT use `event_count > 0` as a substitute for
        `status='succeeded'`. A failed partition can still carry a non-zero
        `event_count` from an earlier partial run.
        """
        stmt = select(MarketEventIngestionPartition.partition_date).where(
            MarketEventIngestionPartition.source == source,
            MarketEventIngestionPartition.category == category,
            MarketEventIngestionPartition.market == market,
            MarketEventIngestionPartition.status == "succeeded",
            MarketEventIngestionPartition.partition_date >= from_date,
            MarketEventIngestionPartition.partition_date <= to_date,
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return set(rows)
