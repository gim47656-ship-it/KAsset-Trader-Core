"""외부 일봉 frame과 KR 1분봉을 저장소 일봉 행으로 바꾸는 순수 변환."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pandas as pd

from app.services.daily_candles.kr_regular_daily import (
    KR_REGULAR_FIRST_TRADE_LATEST,
    KR_REGULAR_LAST_TRADE_EARLIEST,
    KR_REGULAR_MIN_TRADE_BARS,
    KR_REGULAR_PARTITION,
    KR_REGULAR_SEGMENT_END,
    KR_REGULAR_SOURCE,
    KR_REGULAR_VALUE_SEMANTICS,
    KR_REGULAR_WINDOW_END,
    KR_REGULAR_WINDOW_START,
    KST,
    KrTossMinuteCandle,
)
from app.services.daily_candles.repository import DailyCandleRow


def frame_to_rows(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str,
    source: str,
) -> list[DailyCandleRow]:
    """Convert a pandas DataFrame with date/OHLCV columns to ``DailyCandleRow``s.

    The DataFrame is expected to have a ``date`` (or ``datetime``) column and
    ``close`` column at minimum. Missing OHLC values default to ``close``. A
    missing ``value`` column is computed as ``close * volume``. Times are
    normalized to UTC.

    Empty frames or frames without a ``close`` column return ``[]``.
    """
    if frame is None or frame.empty or "close" not in frame.columns:
        return []

    out: list[DailyCandleRow] = []
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        if raw_date is None:
            raw_date = record.get("datetime")
        if raw_date is None:
            continue
        ts = pd.Timestamp(raw_date)
        if ts.tzinfo is None:
            ts = ts.tz_localize(UTC)
        else:
            ts = ts.tz_convert(UTC)

        close = float(record["close"])
        # Explicit None check (not truthiness) preserves legitimate 0.0 values.
        volume = float(record["volume"]) if record.get("volume") is not None else 0.0
        open_value = float(record["open"]) if record.get("open") is not None else close
        high_value = float(record["high"]) if record.get("high") is not None else close
        low_value = float(record["low"]) if record.get("low") is not None else close
        raw_value = record.get("value")
        computed_value = float(raw_value) if raw_value is not None else close * volume

        adj_close_raw = record.get("adj_close")
        adj_close: float | None = (
            float(adj_close_raw) if adj_close_raw is not None else None
        )

        out.append(
            DailyCandleRow(
                time_utc=ts.to_pydatetime(),
                symbol=symbol,
                partition=partition,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close,
                adj_close=adj_close,
                volume=volume,
                value=computed_value,
                source=source,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class KrRegularDailyAggregation:
    """KRX 정규장 1분봉 집계 결과와 fail-closed 사유."""

    row: DailyCandleRow | None
    skip_reason: str | None
    trade_bar_count: int
    first_trade_time_kst: time | None


def aggregate_kr_regular_daily_row(
    rows: Sequence[KrTossMinuteCandle],
    *,
    symbol: str,
    session_date: date,
) -> KrRegularDailyAggregation:
    """한 심볼의 Toss 1분봉을 KRX 정규장 일봉 한 행으로 집계한다."""

    normalized_symbol = str(symbol).strip().upper()
    if not rows:
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="minute_rows_missing",
            trade_bar_count=0,
            first_trade_time_kst=None,
        )

    trade_rows: list[tuple[datetime, datetime, KrTossMinuteCandle]] = []
    for row in rows:
        time_utc = (
            row.time_utc.replace(tzinfo=UTC)
            if row.time_utc.tzinfo is None
            else row.time_utc.astimezone(UTC)
        )
        time_kst = time_utc.astimezone(KST)
        if (
            row.session_date_kst != session_date
            or time_kst.date() != session_date
            or str(row.symbol).strip().upper() != normalized_symbol
        ):
            return KrRegularDailyAggregation(
                row=None,
                skip_reason="minute_row_identity_mismatch",
                trade_bar_count=0,
                first_trade_time_kst=None,
            )

        clock = time_kst.time().replace(tzinfo=None)
        in_regular_segment = (
            row.session_segment == "KRX_REGULAR"
            and KR_REGULAR_WINDOW_START < clock <= KR_REGULAR_SEGMENT_END
        )
        # 운영 실측상 15:31 NXT_POST는 마감 동시호가다. NXT 시간표가 바뀌면
        # 이 가정과 ``pre_nxt``/segment 분류를 함께 재검토해야 한다.
        is_closing_auction_bar = (
            row.session_segment == "NXT_POST" and clock == KR_REGULAR_WINDOW_END
        )
        if (
            (in_regular_segment or is_closing_auction_bar)
            and not row.is_padding
            and row.volume > Decimal(0)
        ):
            trade_rows.append((time_utc, time_kst, row))

    trade_rows.sort(key=lambda item: item[0])
    trade_bar_count = len(trade_rows)
    if not trade_rows:
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="regular_trade_rows_missing",
            trade_bar_count=0,
            first_trade_time_kst=None,
        )

    first_trade_time = trade_rows[0][1].time().replace(tzinfo=None)
    if any(
        row.value_semantics != KR_REGULAR_VALUE_SEMANTICS for _, _, row in trade_rows
    ):
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="minute_value_semantics_mismatch",
            trade_bar_count=trade_bar_count,
            first_trade_time_kst=first_trade_time,
        )
    if first_trade_time > KR_REGULAR_FIRST_TRADE_LATEST:
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="regular_first_trade_late",
            trade_bar_count=trade_bar_count,
            first_trade_time_kst=first_trade_time,
        )
    if trade_bar_count < KR_REGULAR_MIN_TRADE_BARS:
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="regular_trade_rows_short",
            trade_bar_count=trade_bar_count,
            first_trade_time_kst=first_trade_time,
        )
    last_trade_time = trade_rows[-1][1].time().replace(tzinfo=None)
    has_closing_auction_bar = last_trade_time == KR_REGULAR_WINDOW_END
    if not has_closing_auction_bar and last_trade_time < KR_REGULAR_LAST_TRADE_EARLIEST:
        return KrRegularDailyAggregation(
            row=None,
            skip_reason="regular_tail_missing",
            trade_bar_count=trade_bar_count,
            first_trade_time_kst=first_trade_time,
        )

    first_row = trade_rows[0][2]
    last_row = trade_rows[-1][2]
    return KrRegularDailyAggregation(
        row=DailyCandleRow(
            time_utc=datetime.combine(session_date, time.min, tzinfo=UTC),
            symbol=normalized_symbol,
            partition=KR_REGULAR_PARTITION,
            open=float(first_row.open),
            high=float(max(item[2].high for item in trade_rows)),
            low=float(min(item[2].low for item in trade_rows)),
            close=float(last_row.close),
            adj_close=None,
            volume=float(sum((item[2].volume for item in trade_rows), Decimal(0))),
            value=float(sum((item[2].value for item in trade_rows), Decimal(0))),
            source=KR_REGULAR_SOURCE,
        ),
        skip_reason=None,
        trade_bar_count=trade_bar_count,
        first_trade_time_kst=first_trade_time,
    )
