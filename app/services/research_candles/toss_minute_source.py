from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from app.models.kr_candles_1m_toss import (
    TOSS_MINUTE_SOURCE,
    TOSS_MINUTE_VALUE_SEMANTICS,
)
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage

KST = ZoneInfo("Asia/Seoul")
TOSS_MINUTE_FETCH_COUNT = 200
logger = logging.getLogger(__name__)


class TossMinuteClient(Protocol):
    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage: ...

    async def aclose(self) -> None: ...


class UnclassifiableTossMinute(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TossMinuteCandleRow:
    time_utc: datetime
    session_date_kst: date
    symbol: str
    session_segment: str
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal
    value_semantics: str
    is_padding: bool
    pre_nxt: bool | None
    retrieved_at: datetime
    batch_id: str

    def as_insert_values(self) -> dict[str, object]:
        return {
            "time_utc": self.time_utc,
            "session_date_kst": self.session_date_kst,
            "symbol": self.symbol,
            "session_segment": self.session_segment,
            "source": self.source,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "value": self.value,
            "value_semantics": self.value_semantics,
            "is_padding": self.is_padding,
            "pre_nxt": self.pre_nxt,
            "retrieved_at": self.retrieved_at,
            "batch_id": self.batch_id,
        }


def normalize_toss_minute_timestamp(raw: str) -> tuple[datetime, datetime]:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    timestamp_kst = parsed.astimezone(KST).replace(second=0, microsecond=0)
    return timestamp_kst.astimezone(UTC), timestamp_kst


def classify_toss_minute_segment(timestamp_kst: datetime) -> str:
    clock = timestamp_kst.astimezone(KST).time().replace(tzinfo=None)
    if time(8, 0) <= clock < time(9, 0):
        return "NXT_PRE"
    if time(9, 0) <= clock <= time(15, 30):
        return "KRX_REGULAR"
    if time(15, 30) < clock <= time(20, 0):
        return "NXT_POST"
    raise UnclassifiableTossMinute(
        "session_segment_unclassifiable:" + timestamp_kst.astimezone(KST).isoformat()
    )


def _normalize_candle(
    *,
    candle: TossCandle,
    symbol: str,
    retrieved_at: datetime,
    batch_id: str,
) -> TossMinuteCandleRow:
    time_utc, timestamp_kst = normalize_toss_minute_timestamp(candle.timestamp)
    close = candle.close_price
    volume = candle.volume
    return TossMinuteCandleRow(
        time_utc=time_utc,
        session_date_kst=timestamp_kst.date(),
        symbol=symbol,
        session_segment=classify_toss_minute_segment(timestamp_kst),
        source=TOSS_MINUTE_SOURCE,
        open=candle.open_price,
        high=candle.high_price,
        low=candle.low_price,
        close=close,
        volume=volume,
        value=close * volume,
        value_semantics=TOSS_MINUTE_VALUE_SEMANTICS,
        is_padding=volume == Decimal(0),
        # The table contract keeps UNKNOWN as NULL until an exact sourced NXT
        # launch date is introduced. Runtime collection must not invent one.
        pre_nxt=None,
        retrieved_at=retrieved_at,
        batch_id=batch_id,
    )


class TossMinuteCandleSource:
    def __init__(
        self,
        client: TossMinuteClient,
        *,
        fetch_count: int = TOSS_MINUTE_FETCH_COUNT,
    ) -> None:
        self._client = client
        self._fetch_count = max(1, min(int(fetch_count), 200))

    @classmethod
    def from_settings(cls) -> TossMinuteCandleSource:
        return cls(TossReadClient.from_settings())

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        *,
        symbol: str,
        retrieved_at: datetime,
        batch_id: str,
    ) -> list[TossMinuteCandleRow]:
        page = await self._client.candles(
            symbol,
            interval="1m",
            count=self._fetch_count,
            before=None,
            adjusted=None,
        )
        retrieved_utc = (
            retrieved_at.replace(tzinfo=UTC)
            if retrieved_at.tzinfo is None
            else retrieved_at.astimezone(UTC)
        )
        current_minute_utc = retrieved_utc.replace(second=0, microsecond=0)
        # A bounded batch can cross a minute boundary while Toss requests are
        # in flight. Accept only that immediately following minute; anything
        # farther ahead is still clock/data corruption and fails the symbol.
        latest_acceptable_minute = current_minute_utc + timedelta(minutes=1)
        rows: dict[datetime, TossMinuteCandleRow] = {}
        for candle in page.candles:
            try:
                row = _normalize_candle(
                    candle=candle,
                    symbol=symbol,
                    retrieved_at=retrieved_utc,
                    batch_id=batch_id,
                )
            except UnclassifiableTossMinute:
                logger.warning(
                    "Rejected an unclassifiable Toss minute: symbol=%s timestamp=%s",
                    symbol,
                    candle.timestamp,
                )
                continue
            if row.time_utc > latest_acceptable_minute:
                raise UnclassifiableTossMinute(
                    f"future_minute:{symbol}:{row.time_utc.isoformat()}"
                )
            rows.setdefault(row.time_utc, row)
        return [rows[key] for key in sorted(rows)]
