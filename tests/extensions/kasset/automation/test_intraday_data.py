"""공용 OHLCV 경로가 주는 시간대 없는 분봉을 시장별로 해석하는 계약.

US Toss 분봉은 시간대 없는 ET를, KR 경로는 시간대 없는 KST를 돌려준다.
US 매핑이 없던 동안 미국 후보 전원이 ``intraday_timestamp_unusable``로
탈락해 미국장에서 추천과 주문이 한 건도 나오지 못했다. 이 파일은 그 회귀를
재현해 방어한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions.kasset.automation import intraday_data
from app.extensions.kasset.automation.intraday_data import (
    CompletedIntradayBars,
    IntradayBarsUnavailable,
    load_completed_session_bars,
)


def _candle(naive_timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=naive_timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
        source="toss",
    )


def _install_candles(
    monkeypatch: pytest.MonkeyPatch,
    candles: list[SimpleNamespace],
) -> None:
    async def _get_ohlcv(**_kwargs: object) -> list[SimpleNamespace]:
        return candles

    monkeypatch.setattr(
        "app.services.market_data.service.get_ohlcv",
        _get_ohlcv,
    )


@pytest.mark.asyncio
async def test_us_naive_eastern_bars_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ET-naive 09:30/09:35/09:40 봉이 정규장 완료 bar로 적재된다."""
    # 2026-09-02 13:46Z == 09:46 EDT. 09:40 봉이 09:45에 닫힌 직후다.
    as_of = datetime(2026, 9, 2, 13, 46, tzinfo=UTC)
    candles = [
        _candle(datetime(2026, 9, 2, 9, 30)),
        _candle(datetime(2026, 9, 2, 9, 35)),
        _candle(datetime(2026, 9, 2, 9, 40)),
    ]
    _install_candles(monkeypatch, candles)

    result = await load_completed_session_bars(
        symbol="AAPL",
        market="US",
        as_of=as_of,
    )

    assert isinstance(result, CompletedIntradayBars)
    assert [bar.timestamp for bar in result.bars] == [
        datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
        datetime(2026, 9, 2, 13, 35, tzinfo=UTC),
        datetime(2026, 9, 2, 13, 40, tzinfo=UTC),
    ]
    assert result.data_as_of == datetime(2026, 9, 2, 13, 45, tzinfo=UTC)


@pytest.mark.asyncio
async def test_us_naive_bars_are_not_read_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ET-naive 값을 UTC로 읽으면 정규장 밖으로 밀려 bar가 사라진다."""
    as_of = datetime(2026, 9, 2, 13, 41, tzinfo=UTC)
    candles = [_candle(datetime(2026, 9, 2, 9, 35))]
    _install_candles(monkeypatch, candles)

    result = await load_completed_session_bars(
        symbol="AAPL",
        market="US",
        as_of=as_of,
    )

    assert isinstance(result, CompletedIntradayBars)
    # UTC 해석이면 09:35Z는 개장(13:30Z) 전이라 버려진다. ET 해석이라야 남는다.
    assert result.bars[0].timestamp == datetime(2026, 9, 2, 13, 35, tzinfo=UTC)


@pytest.mark.asyncio
async def test_unmapped_market_keeps_naive_timestamps_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """매핑 없는 시장은 시간대 없는 값을 받지 않고 fail-closed로 막는다."""
    as_of = datetime(2026, 9, 2, 13, 41, tzinfo=UTC)
    _install_candles(monkeypatch, [_candle(datetime(2026, 9, 2, 9, 35))])
    monkeypatch.delitem(intraday_data._NAIVE_TIMEZONE, "US")

    result = await load_completed_session_bars(
        symbol="AAPL",
        market="US",
        as_of=as_of,
    )

    assert isinstance(result, IntradayBarsUnavailable)
    assert result.blocked_reason == "intraday_timestamp_unusable"


@pytest.mark.asyncio
async def test_aware_bars_are_converted_without_market_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간대가 붙은 봉은 시장 매핑과 무관하게 UTC로 변환된다."""
    as_of = datetime(2026, 9, 2, 13, 41, tzinfo=UTC)
    aware = _candle(datetime(2026, 9, 2, 13, 35, tzinfo=UTC))
    _install_candles(monkeypatch, [aware])

    result = await load_completed_session_bars(
        symbol="AAPL",
        market="US",
        as_of=as_of,
    )

    assert isinstance(result, CompletedIntradayBars)
    assert result.bars[0].timestamp == datetime(2026, 9, 2, 13, 35, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stale_us_bars_still_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간대 수용이 신선도 검증을 무력화하지 않는다."""
    as_of = datetime(2026, 9, 2, 17, 0, tzinfo=UTC)
    _install_candles(monkeypatch, [_candle(datetime(2026, 9, 2, 9, 30))])

    result = await load_completed_session_bars(
        symbol="AAPL",
        market="US",
        as_of=as_of,
        maximum_bar_age=timedelta(minutes=12),
    )

    assert isinstance(result, IntradayBarsUnavailable)
    assert result.blocked_reason == "intraday_bars_stale"
