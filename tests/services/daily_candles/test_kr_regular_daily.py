from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daily_candles.converters import aggregate_kr_regular_daily_row
from app.services.daily_candles.kr_regular_daily import (
    KR_REGULAR_VALUE_SEMANTICS,
    KrTossMinuteCandle,
    latest_completed_kr_session_date,
)
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.daily_candles.sync_service import DailyCandleSyncService

KST = ZoneInfo("Asia/Seoul")
SESSION_DATE = date(2026, 9, 1)
SYMBOL = "000660"


def _time_utc(day: date, hour: int, minute: int) -> datetime:
    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=KST,
    ).astimezone(UTC)


def _minute(
    *,
    day: date = SESSION_DATE,
    hour: int,
    minute: int,
    close: int,
    volume: int,
    open_price: int | None = None,
    high: int | None = None,
    low: int | None = None,
    segment: str | None = None,
    is_padding: bool | None = None,
    value_semantics: str = KR_REGULAR_VALUE_SEMANTICS,
) -> KrTossMinuteCandle:
    close_decimal = Decimal(close)
    volume_decimal = Decimal(volume)
    return KrTossMinuteCandle(
        time_utc=_time_utc(day, hour, minute),
        session_date_kst=day,
        symbol=SYMBOL,
        session_segment=(
            segment
            if segment is not None
            else "NXT_POST"
            if (hour, minute) == (15, 31)
            else "KRX_REGULAR"
        ),
        open=Decimal(open_price if open_price is not None else close),
        high=Decimal(high if high is not None else close),
        low=Decimal(low if low is not None else close),
        close=close_decimal,
        volume=volume_decimal,
        value=close_decimal * volume_decimal,
        value_semantics=value_semantics,
        is_padding=volume == 0 if is_padding is None else is_padding,
    )


def _opening_trade_rows(*, count: int) -> list[KrTossMinuteCandle]:
    start = datetime(2026, 9, 1, 9, 1)
    rows: list[KrTossMinuteCandle] = []
    for offset in range(count):
        tick = start + timedelta(minutes=offset)
        close = 1_650_000 + offset * 100
        rows.append(
            _minute(
                hour=tick.hour,
                minute=tick.minute,
                open_price=1_649_000 if offset == 0 else close - 100,
                high=close + 500,
                low=1_648_500 if offset == 0 else close - 500,
                close=close,
                volume=100 + offset,
            )
        )
    return rows


def _sync_service_with_rows(
    rows: list[KrTossMinuteCandle],
) -> tuple[DailyCandleSyncService, SimpleNamespace, SimpleNamespace]:
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    repository = SimpleNamespace(
        session=session,
        fetch_kr_toss_minutes=AsyncMock(return_value={SYMBOL: rows}),
        upsert_kr_regular_rows=AsyncMock(return_value=1),
    )
    unused_fetcher = AsyncMock()
    service = DailyCandleSyncService(
        repository=repository,
        toss_kr_fetcher=unused_fetcher,
        toss_us_fetcher=unused_fetcher,
        yahoo_us_fetcher=unused_fetcher,
        upbit_crypto_fetcher=unused_fetcher,
    )
    return service, repository, session


def test_hynix_closing_auction_bar_is_included_in_regular_daily_row() -> None:
    rows = _opening_trade_rows(count=60)
    rows.append(
        _minute(
            hour=15,
            minute=20,
            close=1_697_000,
            volume=500,
            high=1_700_000,
            low=1_696_000,
        )
    )
    rows.extend(
        _minute(
            hour=15,
            minute=minute,
            close=1_697_000,
            volume=0,
            high=9_999_999 if minute == 21 else 1_697_000,
            low=1 if minute == 21 else 1_697_000,
        )
        for minute in range(21, 31)
    )
    rows.append(
        _minute(
            hour=15,
            minute=31,
            close=1_693_000,
            volume=170_732,
            high=1_693_000,
            low=1_693_000,
            segment="NXT_POST",
        )
    )

    result = aggregate_kr_regular_daily_row(
        rows,
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.skip_reason is None
    assert result.trade_bar_count == 62
    assert result.row is not None
    assert result.row.open == 1_649_000
    assert result.row.high == 1_700_000
    assert result.row.low == 1_648_500
    assert result.row.close == 1_693_000
    assert result.row.volume == float(
        sum(row.volume for row in rows if not row.is_padding and row.volume > 0)
    )
    assert result.row.value == float(
        sum(row.value for row in rows if not row.is_padding and row.volume > 0)
    )
    assert result.row.source == "toss_regular"


def test_regular_daily_row_uses_last_trade_when_auction_bar_is_missing() -> None:
    rows = _opening_trade_rows(count=60)
    rows.append(
        _minute(
            hour=15,
            minute=20,
            close=1_697_000,
            volume=500,
        )
    )
    rows.extend(
        _minute(hour=15, minute=minute, close=1_697_000, volume=0)
        for minute in range(21, 31)
    )

    result = aggregate_kr_regular_daily_row(
        rows,
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.row is not None
    assert result.row.close == 1_697_000
    assert result.trade_bar_count == 61


def test_regular_window_excludes_0900_and_after_1531() -> None:
    rows = _opening_trade_rows(count=60)
    rows.extend(
        [
            _minute(
                hour=9,
                minute=0,
                close=9_000_000,
                volume=999_999,
                high=9_000_000,
                low=1,
            ),
            _minute(hour=15, minute=20, close=1_697_000, volume=500),
            _minute(
                hour=15,
                minute=32,
                close=8_000_000,
                volume=888_888,
                high=8_000_000,
                low=1,
                segment="NXT_POST",
            ),
        ]
    )

    result = aggregate_kr_regular_daily_row(
        rows,
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.row is not None
    assert result.trade_bar_count == 61
    assert result.row.high == 1_697_000
    assert result.row.low == 1_648_500
    assert result.row.close == 1_697_000


def test_regular_daily_row_skips_when_trade_bars_are_short() -> None:
    result = aggregate_kr_regular_daily_row(
        _opening_trade_rows(count=59),
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.row is None
    assert result.skip_reason == "regular_trade_rows_short"
    assert result.trade_bar_count == 59


def test_partial_first_day_skips_when_first_trade_is_after_0910() -> None:
    start = datetime(2026, 9, 1, 13, 47)
    rows = [
        _minute(
            hour=(start + timedelta(minutes=offset)).hour,
            minute=(start + timedelta(minutes=offset)).minute,
            close=1_697_000,
            volume=100,
        )
        for offset in range(60)
    ]

    result = aggregate_kr_regular_daily_row(
        rows,
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.row is None
    assert result.skip_reason == "regular_first_trade_late"


def test_regular_daily_row_skips_unknown_value_semantics() -> None:
    rows = _opening_trade_rows(count=60)
    rows[10] = replace(rows[10], value_semantics="EXCHANGE_REPORTED")

    result = aggregate_kr_regular_daily_row(
        rows,
        symbol=SYMBOL,
        session_date=SESSION_DATE,
    )

    assert result.row is None
    assert result.skip_reason == "minute_value_semantics_mismatch"


def test_intraday_target_is_previous_trading_session() -> None:
    assert latest_completed_kr_session_date(
        datetime(2026, 9, 2, 15, 30, 59, tzinfo=KST)
    ) == date(2026, 9, 1)
    assert latest_completed_kr_session_date(
        datetime(2026, 9, 2, 15, 31, tzinfo=KST)
    ) == date(2026, 9, 2)


@pytest.mark.asyncio
async def test_sync_service_skips_when_regular_tail_is_missing() -> None:
    service, repository, session = _sync_service_with_rows(
        _opening_trade_rows(count=60)
    )

    result = await service.override_kr_regular_daily(
        symbols=[SYMBOL],
        session_date=SESSION_DATE,
    )

    assert result.status == "skipped"
    assert result.skip_reasons == {"regular_tail_missing": 1}
    repository.upsert_kr_regular_rows.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_service_upserts_regular_row_with_session_tail() -> None:
    rows = [
        *_opening_trade_rows(count=60),
        _minute(hour=15, minute=20, close=1_697_000, volume=500),
    ]
    service, repository, session = _sync_service_with_rows(rows)

    result = await service.override_kr_regular_daily(
        symbols=[SYMBOL],
        session_date=SESSION_DATE,
    )

    assert result.status == "completed"
    assert result.rows_upserted == 1
    saved_rows = repository.upsert_kr_regular_rows.await_args.kwargs["rows"]
    assert len(saved_rows) == 1
    assert saved_rows[0].source == "toss_regular"
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_service_skips_closed_session_without_querying_minutes() -> None:
    service, repository, session = _sync_service_with_rows([])

    result = await service.override_kr_regular_daily(
        symbols=[SYMBOL],
        session_date=date(2026, 9, 5),
    )

    assert result.status == "skipped"
    assert result.skip_reasons == {"session_not_trading_or_unknown": 1}
    repository.fetch_kr_toss_minutes.assert_not_awaited()
    repository.upsert_kr_regular_rows.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_manual_override_cli_accepts_session_date() -> None:
    from scripts.override_kr_regular_daily import _build_parser

    args = _build_parser().parse_args(["--date", "2026-09-02"])

    assert args.date == date(2026, 9, 2)
    assert args.symbols is None


@pytest.mark.asyncio
async def test_manual_override_cli_rejects_unfinished_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import override_kr_regular_daily as cli

    service_factory = AsyncMock()
    monkeypatch.setattr(cli, "setup_logging_and_sentry", lambda **_: None)
    monkeypatch.setattr(cli, "_build_default_service", service_factory)

    exit_code = await cli.main(
        ["--date", "2026-09-02", "--symbols", SYMBOL],
        now=datetime(2026, 9, 2, 11, 0, tzinfo=KST),
    )

    assert exit_code == 1
    service_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_reads_requested_toss_minute_session(
    db_session: AsyncSession,
) -> None:
    symbol = "KRREG-FETCH-20260902"
    candle_time = _time_utc(date(2026, 9, 2), 9, 1)
    await db_session.execute(
        text(
            """
            INSERT INTO research.kr_candles_1m_toss (
                time_utc, session_date_kst, symbol, session_segment, source,
                open, high, low, close, volume, value, value_semantics,
                is_padding, pre_nxt, retrieved_at, batch_id
            ) VALUES (
                :time_utc, :session_date, :symbol, 'KRX_REGULAR', 'TOSS',
                100, 110, 90, 105, 10, 1050, :value_semantics,
                FALSE, NULL, :retrieved_at, 'kr-regular-fetch-test'
            )
            """
        ),
        {
            "time_utc": candle_time,
            "session_date": date(2026, 9, 2),
            "symbol": symbol,
            "value_semantics": KR_REGULAR_VALUE_SEMANTICS,
            "retrieved_at": datetime(2026, 9, 2, 0, 2, tzinfo=UTC),
        },
    )

    fetched = await DailyCandlesRepository(session=db_session).fetch_kr_toss_minutes(
        session_date=date(2026, 9, 2),
        symbols=[symbol, "MISSING-KRREG"],
    )

    assert fetched["MISSING-KRREG"] == []
    assert len(fetched[symbol]) == 1
    assert fetched[symbol][0].time_utc == candle_time
    assert fetched[symbol][0].volume == Decimal(10)


@pytest.mark.asyncio
async def test_toss_regular_overwrites_toss_and_is_not_reverted(
    db_session: AsyncSession,
) -> None:
    repository = DailyCandlesRepository(session=db_session)
    candle_time = datetime(2026, 9, 2, tzinfo=UTC)
    symbol = "KRREG-UPSERT-20260902"
    toss_row = DailyCandleRow(
        time_utc=candle_time,
        symbol=symbol,
        partition="KRX",
        open=1_650_000,
        high=1_700_000,
        low=1_640_000,
        close=1_652_000,
        adj_close=None,
        volume=1_000,
        value=1_652_000_000,
        source="toss",
    )
    await repository.upsert_rows(market=MarketKey.KR, rows=[toss_row])
    regular_row = replace(
        toss_row,
        close=1_693_000,
        volume=2_000,
        value=3_386_000_000,
        source="toss_regular",
    )

    assert await repository.upsert_kr_regular_rows(rows=[regular_row]) == 1
    stored = (
        await db_session.execute(
            text(
                "SELECT close, volume, source FROM public.kr_candles_1d "
                "WHERE time = :time AND symbol = :symbol AND venue = 'KRX'"
            ),
            {"time": candle_time, "symbol": symbol},
        )
    ).one()
    assert float(stored.close) == 1_693_000
    assert float(stored.volume) == 2_000
    assert stored.source == "toss_regular"

    await repository.upsert_rows(
        market=MarketKey.KR,
        rows=[replace(toss_row, close=1_600_000)],
    )
    protected = (
        await db_session.execute(
            text(
                "SELECT open, close, volume, source FROM public.kr_candles_1d "
                "WHERE time = :time AND symbol = :symbol AND venue = 'KRX'"
            ),
            {"time": candle_time, "symbol": symbol},
        )
    ).one()
    assert float(protected.open) == 1_650_000
    assert float(protected.close) == 1_693_000
    assert float(protected.volume) == 2_000
    assert protected.source == "toss_regular"

    adjusted_toss_row = replace(
        toss_row,
        open=825_000,
        high=850_000,
        low=800_000,
        close=810_000,
        volume=3_000,
        value=2_430_000_000,
    )
    await repository.upsert_rows(
        market=MarketKey.KR,
        rows=[adjusted_toss_row],
    )
    adjusted = (
        await db_session.execute(
            text(
                "SELECT open, close, source FROM public.kr_candles_1d "
                "WHERE time = :time AND symbol = :symbol AND venue = 'KRX'"
            ),
            {"time": candle_time, "symbol": symbol},
        )
    ).one()
    assert float(adjusted.open) == 825_000
    assert float(adjusted.close) == 810_000
    assert adjusted.source == "toss"


@pytest.mark.asyncio
async def test_toss_regular_does_not_overwrite_other_daily_sources(
    db_session: AsyncSession,
) -> None:
    repository = DailyCandlesRepository(session=db_session)
    candle_time = datetime(2026, 9, 2, tzinfo=UTC)
    original = DailyCandleRow(
        time_utc=candle_time,
        symbol="KRREG-KIS-PROTECTED-20260902",
        partition="KRX",
        open=1_650_000,
        high=1_700_000,
        low=1_640_000,
        close=1_680_000,
        adj_close=None,
        volume=1_000,
        value=1_680_000_000,
        source="kis",
    )
    await repository.upsert_rows(market=MarketKey.KR, rows=[original])

    assert (
        await repository.upsert_kr_regular_rows(
            rows=[replace(original, close=1_693_000, source="toss_regular")]
        )
        == 0
    )

    stored = (
        await db_session.execute(
            text(
                "SELECT close, source FROM public.kr_candles_1d "
                "WHERE time = :time AND symbol = :symbol AND venue = 'KRX'"
            ),
            {"time": candle_time, "symbol": original.symbol},
        )
    ).one()
    assert float(stored.close) == 1_680_000
    assert stored.source == "kis"
