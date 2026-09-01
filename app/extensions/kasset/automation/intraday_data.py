"""장중 Trigger가 쓰는 완료 정규장 bar 적재기.

공용 :func:`app.services.market_data.service.get_ohlcv`만 쓴다. KR 개별 종목은 그
서비스의 Toss-first 경로를 따르고, KOSPI/KOSDAQ은 Toss 시장지표 전용 경로를
따른다. 지수 공급 실패는 종목/KIS proxy로 대체하지 않고 unavailable로 남긴다.

이 모듈이 책임지는 것은 딱 셋이다.

1. 정규장 세션 달력으로 "지금이 장중인가"와 세션 경계를 확정한다.
2. 완료된 bucket만 남긴다. 부분 bucket은 값을 섞지 않고 버린다.
3. 최신 완료 bar가 신선한지 검증한다. 낡으면 값을 쓰지 않고 사유를 남긴다.

세 검증 중 하나라도 실패하면 개별 trigger가 아니라 판정 전체를 막는 사유
(``blocked_reason``)를 돌려준다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal
from zoneinfo import ZoneInfo

from app.extensions.kasset.automation.contracts import PriceBar
from app.extensions.kasset.automation.intraday_triggers import (
    INDEX_INTRADAY_UNAVAILABLE,
)
from app.extensions.kasset.automation.market_session import (
    RegularSession,
    current_regular_session,
)

logger = logging.getLogger(__name__)

#: 공용 OHLCV 서비스에 보내는 시장 키.
_OHLCV_MARKET: Final[dict[str, str]] = {"KRX": "equity_kr", "US": "equity_us"}

#: KR 분봉 DB/KIS 경로는 시간대 없는 KST를 돌려준다는 기존 계약이 있다.
#: 그 규약을 그대로 적용하고, 다른 시장은 시간대 없는 값을 받지 않는다.
_NAIVE_TIMEZONE: Final[dict[str, ZoneInfo]] = {"KRX": ZoneInfo("Asia/Seoul")}

#: 진입 판정에 쓰는 완료 bucket 길이. 5m 하나로 5분·20분 창을 모두 만든다.
INTRADAY_BAR_PERIOD: Final = "5m"
INTRADAY_BAR_INTERVAL: Final = timedelta(minutes=5)

#: 최신 완료 bar가 이보다 오래되면 stale로 보고 판정을 막는다. 완료 시각
#: 기준이므로 bucket 하나가 막 닫힌 직후에도 통과할 수 있어야 한다.
INTRADAY_MAX_BAR_AGE: Final = timedelta(minutes=12)

#: KRX/XNYS 정규장 390분 전체(78 bucket)와 공급자 경계 여유를 함께 읽는다.
#: 최근 bar만 반환하는 공급자에서도 개장 bucket을 잃지 않아 ORB와 세션-reset
#: VWAP이 장 마감까지 같은 세션 기준을 유지해야 한다.
INTRADAY_BAR_COUNT: Final = 84


@dataclass(frozen=True, slots=True)
class CompletedIntradayBars:
    """한 심볼의 완료 정규장 bar와 그 출처."""

    symbol: str
    market: Literal["KRX", "US"]
    period: str
    bar_interval: timedelta
    session: RegularSession
    bars: tuple[PriceBar, ...]
    source: str
    data_as_of: datetime
    blocked_reason: None = None


@dataclass(frozen=True, slots=True)
class IntradayBarsUnavailable:
    """완료 bar를 쓸 수 없는 이유. 판정 전체를 막는다."""

    symbol: str
    market: Literal["KRX", "US"]
    period: str
    blocked_reason: str
    detail: str
    session: RegularSession | None = None
    bars: tuple[PriceBar, ...] = ()
    source: str | None = None
    data_as_of: datetime | None = None


IntradayBarsResult = CompletedIntradayBars | IntradayBarsUnavailable


async def load_completed_session_bars(
    *,
    symbol: str,
    market: Literal["KRX", "US"],
    as_of: datetime,
    period: str = INTRADAY_BAR_PERIOD,
    bar_interval: timedelta = INTRADAY_BAR_INTERVAL,
    count: int = INTRADAY_BAR_COUNT,
    maximum_bar_age: timedelta = INTRADAY_MAX_BAR_AGE,
    session: RegularSession | None = None,
) -> IntradayBarsResult:
    """정규장 완료 bar만 적재하고 신선도까지 검증한다."""

    moment = _aware_utc(as_of, "as_of")
    resolved_session = session or current_regular_session(market, moment)
    if resolved_session is None:
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="regular_session_closed",
            detail=(
                "the shared session calendar does not place as_of inside a "
                "regular session"
            ),
        )
    ohlcv_market = _OHLCV_MARKET.get(market)
    if ohlcv_market is None:
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="unsupported_market",
            detail=f"{market} has no shared intraday OHLCV route",
            session=resolved_session,
        )

    from app.services.market_data.service import get_ohlcv

    try:
        candles = await get_ohlcv(
            symbol=symbol,
            market=ohlcv_market,
            period=period,
            count=count,
        )
    except Exception as exc:  # noqa: BLE001 - bounded per-symbol failure
        logger.info(
            "kasset intraday OHLCV unavailable symbol=%s market=%s period=%s error=%s",
            symbol,
            market,
            period,
            type(exc).__name__,
        )
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="intraday_provider_unavailable",
            detail=f"shared get_ohlcv raised {type(exc).__name__}",
            session=resolved_session,
        )
    if not candles:
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="intraday_bars_empty",
            detail="the shared market-data path returned no intraday candles",
            session=resolved_session,
        )

    sources = {str(candle.source) for candle in candles if candle.source}
    source = ",".join(sorted(sources)) if sources else "unknown"
    bars: list[PriceBar] = []
    for candle in candles:
        timestamp = _normalized_timestamp(candle.timestamp, market=market)
        if timestamp is None:
            return IntradayBarsUnavailable(
                symbol=symbol,
                market=market,
                period=period,
                blocked_reason="intraday_timestamp_unusable",
                detail="an intraday candle timestamp had no provable instant",
                session=resolved_session,
                source=source,
            )
        closes_at = timestamp + bar_interval
        if timestamp < resolved_session.opens_at or closes_at > (
            resolved_session.closes_at
        ):
            # 정규장 밖(시간외/전일 잔여) bucket은 세션 리셋 계산에 섞지 않는다.
            continue
        if closes_at > moment:
            # 아직 닫히지 않은 부분 bucket.
            continue
        bar = _price_bar(candle, timestamp=timestamp)
        if bar is None:
            return IntradayBarsUnavailable(
                symbol=symbol,
                market=market,
                period=period,
                blocked_reason="intraday_bar_invalid",
                detail="an intraday candle had non-finite or inconsistent OHLCV",
                session=resolved_session,
                source=source,
            )
        bars.append(bar)
    bars.sort(key=lambda item: item.timestamp)
    if not bars:
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="no_completed_session_bars",
            detail=(
                "the regular session has produced no completed bucket for this "
                "symbol yet"
            ),
            session=resolved_session,
            source=source,
        )
    data_as_of = bars[-1].timestamp + bar_interval
    if moment - data_as_of > maximum_bar_age:
        return IntradayBarsUnavailable(
            symbol=symbol,
            market=market,
            period=period,
            blocked_reason="intraday_bars_stale",
            detail=(
                f"the newest completed bar closed at {_timestamp_text(data_as_of)}, "
                f"older than {maximum_bar_age}"
            ),
            session=resolved_session,
            bars=tuple(bars),
            source=source,
            data_as_of=data_as_of,
        )
    return CompletedIntradayBars(
        symbol=symbol,
        market=market,
        period=period,
        bar_interval=bar_interval,
        session=resolved_session,
        bars=tuple(bars),
        source=source,
        data_as_of=data_as_of,
    )


async def load_index_session_bars(
    *,
    index_symbol: str,
    market: Literal["KRX", "US"],
    as_of: datetime,
    session: RegularSession | None = None,
    period: str = INTRADAY_BAR_PERIOD,
    bar_interval: timedelta = INTRADAY_BAR_INTERVAL,
    count: int = INTRADAY_BAR_COUNT,
    maximum_bar_age: timedelta = INTRADAY_MAX_BAR_AGE,
) -> IntradayBarsResult:
    """지수 완료 분봉을 같은 공용 경로로 시도한다.

    공용 경로는 KOSPI/KOSDAQ을 Toss 시장지표 전용 endpoint로 보낸다. 실제 지수
    분봉을 받지 못하면 값을 추정하거나 종목·ETF·일봉으로 대체하지 않고
    ``index_intraday_unavailable`` 근거를 남긴다.
    """

    result = await load_completed_session_bars(
        symbol=index_symbol,
        market=market,
        as_of=as_of,
        period=period,
        bar_interval=bar_interval,
        count=count,
        maximum_bar_age=maximum_bar_age,
        session=session,
    )
    if isinstance(result, IntradayBarsUnavailable) and result.blocked_reason in {
        "intraday_bars_empty",
        "intraday_provider_unavailable",
        "no_completed_session_bars",
    }:
        return IntradayBarsUnavailable(
            symbol=index_symbol,
            market=market,
            period=period,
            blocked_reason=INDEX_INTRADAY_UNAVAILABLE,
            detail=(
                f"{result.blocked_reason}: {result.detail}. Index intraday bars "
                "are not synthesized from daily data."
            ),
            session=result.session,
            source=result.source,
        )
    return result


def _normalized_timestamp(
    value: datetime,
    *,
    market: Literal["KRX", "US"],
) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    zone = _NAIVE_TIMEZONE.get(market)
    if zone is None:
        return None
    return value.replace(tzinfo=zone).astimezone(UTC)


def _price_bar(candle: object, *, timestamp: datetime) -> PriceBar | None:
    try:
        open_price = _decimal(candle.open)
        high = _decimal(candle.high)
        low = _decimal(candle.low)
        close = _decimal(candle.close)
        volume = _decimal(candle.volume)
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return None
    prices = (open_price, high, low, close)
    if any(not value.is_finite() or value <= 0 for value in prices):
        return None
    if not volume.is_finite() or volume < 0:
        return None
    if high < max(open_price, close) or low > min(open_price, close):
        return None
    return PriceBar(
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "INTRADAY_BAR_COUNT",
    "INTRADAY_BAR_INTERVAL",
    "INTRADAY_BAR_PERIOD",
    "INTRADAY_MAX_BAR_AGE",
    "CompletedIntradayBars",
    "IntradayBarsResult",
    "IntradayBarsUnavailable",
    "load_completed_session_bars",
    "load_index_session_bars",
]
