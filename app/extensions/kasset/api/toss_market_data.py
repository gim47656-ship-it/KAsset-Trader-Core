"""서버 공용 KRX 실시간 시세 채널 (토스 인증 REST 배치).

토스 `GET /api/v1/prices`는 한 번의 호출로 여러 종목의 현재가를 내려주고
운영에서 0.15초 수준으로 응답한다. Android 시세 경로의 유일한 live provider이며,
실패하면 호출부가 저장 캔들로 failover한다.

계좌·주문과 무관한 읽기 전용 공용 데이터이므로 사용자 볼트 자격이 아니라
서버 env 자격(`TOSS_API_*`)만 사용한다. 이 모듈은 시세 조회만 하며 주문·자산
경로는 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import cast
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.brokers.toss.candles import TossCandleClient, fetch_toss_candles
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossCandlesPage
from app.services.brokers.toss.market_calendar import TossSessionWindow
from app.services.invest_price_fallback import TossPriceClient

logger = logging.getLogger(__name__)

# 응답 `source` 값. 공급자 구분만 노출하고 자격·엔드포인트·원문 예외는 절대
# 담지 않는다.
TOSS_QUOTE_SOURCE = "TOSS_API_PRICES"

# 일봉 폴백 실패를 캐시할 시간. 성공값은 거래일이 바뀔 때까지 유지하므로 TTL이
# 없고, 실패(값 없음)만 짧게 캐시해 장애 중 폴링이 토스를 두드리지 않게 한다.
_DAILY_CLOSE_MISS_TTL_SECONDS = 60.0
_REGULAR_CLOSE_MISS_TTL_SECONDS = 60.0
# previousClose 하나를 찾는 데 필요한 최소 일봉 수는 2다(당일 + 직전 거래일).
# 같은 날짜가 중복 저장된 응답에서도 직전 거래일을 찾도록 1행 더 읽는다.
_DAILY_CLOSE_LOOKBACK = 3
# 일봉 폴백은 관심종목 수만큼 동시에 발생할 수 있다. 토스 차트 그룹
# (`MARKET_DATA_CHART`)을 한꺼번에 두드리지 않도록 동시 실행을 묶는다.
_DAILY_CLOSE_CONCURRENCY = 4
# 거래일 경계는 저장 일봉 경로와 같은 KST 날짜를 쓴다.
_KST = ZoneInfo("Asia/Seoul")
# 차트 일봉 캐시. 일봉은 세션 중 마지막 봉만 움직이므로 짧게만 잡아도 충분하고,
# 같은 종목 상세 화면을 여러 번 열어도 토스 차트 그룹을 반복 호출하지 않는다.
_DAILY_BARS_TTL_SECONDS = 60.0

_MIN_PLAUSIBLE_YEAR = 2000
_MAX_PLAUSIBLE_YEAR = 2100

# Toss 사양의 요청당 캔들 상한(`count.maximum`). 페이지 수 계산의 분모다.
_TOSS_CANDLE_PAGE_LIMIT = 200
# 닫힌 세션은 최근 128개 종목·세션만 프로세스 안에 보관한다. 390봉 세션 기준
# 약 5만 봉으로 상한을 고정해, US 전체 종목을 조회해도 캐시가 무한히 자라지 않는다.
_INTRADAY_BARS_CACHE_MAX_ENTRIES = 128

type _IntradayCacheKey = tuple[str, str, datetime, datetime]
type _IntradayInflightKey = tuple[_IntradayCacheKey, bool]


@dataclass(frozen=True, slots=True)
class TossQuotePoint:
    """토스 배치 시세 한 종목. `as_of`는 항상 tz-aware UTC다."""

    symbol: str
    price: Decimal
    currency: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class TossDailyBar:
    """토스 일봉 한 개. `time_utc`는 항상 tz-aware UTC다."""

    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class TossIndicatorPoint:
    """토스 시장지표 한 건.

    지표 응답에는 통화가 없고, 장 마감 상태에서는 `timestamp`가 `null`로 온다.
    시각을 신뢰할 수 없으면 서버 시각으로 대체하지 않고 `as_of`를 `None`으로
    두어, 호출부가 그 항목을 `stale`로 표시할 수 있게 한다.
    """

    symbol: str
    last_price: Decimal
    as_of: datetime | None


def _normalized_as_of(value: object) -> datetime | None:
    """공급자 시각을 tz-aware UTC로 정규화한다. 신뢰할 수 없으면 `None`.

    시각이 없거나 오프셋이 없는 응답을 서버 현재 시각으로 대체하지 않는다.
    그렇게 하면 오래된 값이 실시간처럼 보이므로, 해당 종목은 이 채널에서
    제외하고 호출부가 다음 폴백으로 내려가게 한다.
    """
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int | float):
        with suppress(OSError, OverflowError, ValueError):
            parsed = datetime.fromtimestamp(float(value), UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in {"z", "Z"}:
            text = f"{text[:-1]}+00:00"
        with suppress(ValueError):
            parsed = datetime.fromisoformat(text)
    if parsed is None or parsed.tzinfo is None:
        return None
    if not _MIN_PLAUSIBLE_YEAR <= parsed.year <= _MAX_PLAUSIBLE_YEAR:
        return None
    return parsed.astimezone(UTC)


def _quote_point(row: object) -> TossQuotePoint | None:
    """`TossPrice` 한 건을 검증해 시세 포인트로 바꾼다. 이상하면 `None`."""
    try:
        symbol = str(getattr(row, "symbol", "")).strip().upper()
        price = getattr(row, "last_price", None)
        currency = str(getattr(row, "currency", "") or "").strip().upper()
        as_of = _normalized_as_of(getattr(row, "timestamp", None))
    except Exception:  # noqa: BLE001 — 공급자 응답 변형은 폴백 사유일 뿐이다
        return None
    if not symbol or as_of is None:
        return None
    if not isinstance(price, Decimal):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return TossQuotePoint(
        symbol=symbol,
        price=price,
        currency=currency or "KRW",
        as_of=as_of,
    )


def _indicator_point(row: object) -> TossIndicatorPoint | None:
    """`TossMarketIndicatorPrice` 한 건을 지표 포인트로 바꾼다. 이상하면 `None`.

    시세 포인트와 달리 `as_of`가 `None`이어도 값은 살린다. 지표 응답의
    `timestamp`는 장 마감 상태에서 `null`로 오는 것이 정상이고, 값 자체는
    유효하기 때문이다. 음수 금리를 배제하지 않기 위해 부호는 검사하지 않는다.
    """
    try:
        symbol = str(getattr(row, "symbol", "")).strip().upper()
        last_price = getattr(row, "last_price", None)
        as_of = _normalized_as_of(getattr(row, "timestamp", None))
    except Exception:  # noqa: BLE001 — 공급자 응답 변형은 폴백 사유일 뿐이다
        return None
    if not symbol:
        return None
    if not isinstance(last_price, Decimal) or not last_price.is_finite():
        return None
    return TossIndicatorPoint(symbol=symbol, last_price=last_price, as_of=as_of)


def _previous_daily_close(page: object, *, boundary: date) -> Decimal | None:
    """일봉 페이지에서 `boundary` 직전 거래일의 종가를 고른다.

    봉의 거래일은 라벨 타임스탬프의 KST 날짜로 읽는다. 토스는 국내 봉을
    `00:00+09:00`, 미국 봉을 `13:00+09:00`(= ET 자정)으로 라벨하므로 두 시장
    모두 라벨의 KST 날짜가 곧 그 시장의 거래일이다.

    `boundary`는 호출부가 **시장의 거래일 기준**으로 넘겨야 한다. 미국 시세를
    KST 날짜로 넘기면 정규장(22:30~05:00 KST)이 자정을 넘는 순간 boundary가
    하루 앞서가고, 그러면 진행 중인 당일 봉이 "직전 거래일"로 잡혀 전일 종가가
    위조된다. `boundary` 당일 봉은 진행 중일 수 있어 항상 제외한다.
    """
    rows = getattr(page, "candles", None)
    if not rows:
        return None
    dated: list[tuple[date, Decimal]] = []
    for row in rows:
        as_of = _normalized_as_of(getattr(row, "timestamp", None))
        close = getattr(row, "close_price", None)
        if as_of is None or not isinstance(close, Decimal):
            continue
        if not close.is_finite() or close <= 0:
            continue
        dated.append((as_of.astimezone(_KST).date(), close))
    if not dated:
        return None
    dated.sort(key=lambda item: item[0])
    for trading_date, close in reversed(dated):
        if trading_date < boundary:
            return close
    return None


def _regular_close(page: object, *, window: TossSessionWindow) -> Decimal | None:
    """정규장 구간 안의 마지막 1분봉 종가만 채택한다."""

    rows = getattr(page, "candles", None)
    if not rows:
        return None
    candidates: list[tuple[datetime, Decimal]] = []
    for row in rows:
        as_of = _normalized_as_of(getattr(row, "timestamp", None))
        close = getattr(row, "close_price", None)
        if as_of is None or not isinstance(close, Decimal):
            continue
        if not close.is_finite() or close <= 0:
            continue
        if window.start <= as_of < window.end:
            candidates.append((as_of, close))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _daily_bars(page: object) -> list[TossDailyBar]:
    """일봉 페이지를 오래된 순 목록으로 바꾼다. 이상한 봉은 버린다."""
    rows = getattr(page, "candles", None)
    if not rows:
        return []
    bars: list[TossDailyBar] = []
    for row in rows:
        as_of = _normalized_as_of(getattr(row, "timestamp", None))
        if as_of is None:
            continue
        values: list[Decimal] = []
        for field in ("open_price", "high_price", "low_price", "close_price"):
            value = getattr(row, field, None)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                break
            values.append(value)
        if len(values) != 4:
            continue
        volume = getattr(row, "volume", None)
        if not isinstance(volume, Decimal) or not volume.is_finite() or volume < 0:
            volume = Decimal(0)
        bars.append(
            TossDailyBar(
                time_utc=as_of,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=volume,
            )
        )
    bars.sort(key=lambda bar: bar.time_utc)
    return bars


def aggregate_intraday_bars(
    bars: Sequence[TossDailyBar],
    *,
    window: TossSessionWindow,
    interval_minutes: int,
) -> list[TossDailyBar]:
    """정규장 시작을 기준으로 분봉을 고정 간격 OHLCV 봉으로 집계한다.

    거래가 있는 버킷만 만들며, 정규장 끝의 짧은 버킷도 그대로 보존한다.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    bucket_seconds = interval_minutes * 60
    aggregated: list[TossDailyBar] = []
    current_index: int | None = None
    current_open = Decimal(0)
    current_high = Decimal(0)
    current_low = Decimal(0)
    current_close = Decimal(0)
    current_volume = Decimal(0)

    for bar in sorted(bars, key=lambda item: item.time_utc):
        if not window.contains(bar.time_utc):
            continue
        bucket_index = int(
            (bar.time_utc - window.start).total_seconds() // bucket_seconds
        )
        if current_index != bucket_index:
            if current_index is not None:
                aggregated.append(
                    TossDailyBar(
                        time_utc=window.start
                        + timedelta(minutes=current_index * interval_minutes),
                        open=current_open,
                        high=current_high,
                        low=current_low,
                        close=current_close,
                        volume=current_volume,
                    )
                )
            current_index = bucket_index
            current_open = bar.open
            current_high = bar.high
            current_low = bar.low
            current_close = bar.close
            current_volume = bar.volume
            continue
        current_high = max(current_high, bar.high)
        current_low = min(current_low, bar.low)
        current_close = bar.close
        current_volume += bar.volume

    if current_index is not None:
        aggregated.append(
            TossDailyBar(
                time_utc=window.start
                + timedelta(minutes=current_index * interval_minutes),
                open=current_open,
                high=current_high,
                low=current_low,
                close=current_close,
                volume=current_volume,
            )
        )
    return aggregated


class TossSharedMarketData:
    """토스 배치 시세 채널. 클라이언트 재사용 + 짧은 캐시 + 단일비행.

    운영 rate limit 보호가 이 클래스의 존재 이유다.

    - `TossReadClient`를 프로세스에서 한 번만 만들고 lifespan에서 닫는다.
      매 요청마다 생성·종료하면 OAuth 토큰과 커넥션을 계속 새로 만든다.
    - 종목별 2초 캐시: 15초 폴링 사용자가 늘어도 같은 종목 호출량은
      최대 2초당 1회다.
    - 종목별 단일비행: 동시에 들어온 요청들이 같은 종목을 중복 호출하지 않고
      하나의 배치 호출로 합쳐진다.
    - 실패 직후 2초 냉각: 장애 중 폴링이 그대로 토스를 두드리지 않게 한다.
    - `previous_closes`는 저장 일봉이 없는 종목의 전일 종가를 토스 일봉에서
      받아 거래일 단위로 캐시한다. 저장 일봉 유니버스에 없는 종목이
      등락률·차트 없이 보이던 결함을 이 경로가 덮는다.
    - `regular_closes`는 끝난 정규장의 마지막 1분봉만 읽고 정규장 종료
      시각별로 캐시해, 연장장 시세의 정규장 기준 등락을 만든다.
    - `daily_bars`는 저장 일봉이 없거나 부족한 종목의 차트를 토스 일봉으로 채운다.
    - `intraday_bars`는 진행 중인 세션은 캐시하지 않고, 닫힌 정규장만
      `(market, symbol, start, end)` 단위의 제한된 LRU에 보관한다.
    """

    _CACHE_TTL_SECONDS = 2.0
    _FAILURE_COOLDOWN_SECONDS = 2.0

    def __init__(
        self,
        *,
        client_factory: Callable[[], TossPriceClient] | None = None,
    ) -> None:
        self._client_factory: Callable[[], TossPriceClient] = (
            client_factory or TossReadClient.from_settings
        )
        self._client: TossPriceClient | None = None
        self._client_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cache: dict[str, tuple[float, TossQuotePoint]] = {}
        self._inflight: dict[str, asyncio.Future[TossQuotePoint | None]] = {}
        self._cooldown_until = 0.0
        # symbol -> (기준 거래일, 전일 종가). 값이 `None`이면 "일봉 없음"이고
        # `_daily_close_miss`의 만료 시각까지만 유효하다.
        self._daily_close: dict[str, tuple[date, Decimal | None]] = {}
        self._daily_close_miss: dict[str, float] = {}
        # 단일비행 키는 `(symbol, 기준 거래일)`이다. symbol만 키로 쓰면 거래일이
        # 바뀌는 순간 다른 기준일을 기다리던 호출자가 남의 결과를 받는다.
        self._daily_close_inflight: dict[
            tuple[str, date], asyncio.Future[Decimal | None]
        ] = {}
        self._daily_close_gate = asyncio.Semaphore(_DAILY_CLOSE_CONCURRENCY)
        # symbol -> (정규장 종료 시각, 정규장 종가). 정규장 종료 시각이
        # 달라질 때만 새로 조회한다.
        self._regular_close: dict[str, tuple[datetime, Decimal | None]] = {}
        self._regular_close_miss: dict[str, float] = {}
        self._regular_close_inflight: dict[
            tuple[str, datetime], asyncio.Future[Decimal | None]
        ] = {}
        # (symbol, count) -> (만료 시각, 일봉). 차트 응답 전용 캐시다.
        self._daily_bars: dict[tuple[str, int], tuple[float, list[TossDailyBar]]] = {}
        self._closed_intraday_bars: OrderedDict[
            _IntradayCacheKey, list[TossDailyBar]
        ] = OrderedDict()
        self._intraday_inflight: dict[
            _IntradayInflightKey, asyncio.Future[list[TossDailyBar] | None]
        ] = {}

    async def prices(self, symbols: Sequence[str]) -> dict[str, TossQuotePoint]:
        """요청 종목의 실시간 시세를 조회한다. 실패 종목은 결과에서 빠진다."""
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return {}
        requested = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        if not requested:
            return {}
        self._reset_if_loop_changed()

        now = time.monotonic()
        resolved: dict[str, TossQuotePoint] = {}
        pending: dict[str, asyncio.Future[TossQuotePoint | None]] = {}
        owned: dict[str, asyncio.Future[TossQuotePoint | None]] = {}
        loop = asyncio.get_running_loop()
        for symbol in requested:
            cached = self._cache.get(symbol)
            if cached is not None and now - cached[0] < self._CACHE_TTL_SECONDS:
                resolved[symbol] = cached[1]
                continue
            inflight = self._inflight.get(symbol)
            if inflight is not None:
                pending[symbol] = inflight
                continue
            if now < self._cooldown_until:
                continue
            future: asyncio.Future[TossQuotePoint | None] = loop.create_future()
            self._inflight[symbol] = future
            owned[symbol] = future

        if owned:
            await self._fetch(list(owned))
        for symbol, future in (*owned.items(), *pending.items()):
            point = await future
            if point is not None:
                resolved[symbol] = point
        return resolved

    async def _fetch(self, symbols: list[str]) -> None:
        points: dict[str, TossQuotePoint] = {}
        failed = False
        try:
            client = await self._ensure_client()
            for row in await client.prices(symbols):
                point = _quote_point(row)
                if point is not None:
                    points[point.symbol] = point
        except Exception as exc:  # noqa: BLE001 — 폴백으로 강등하고 계속한다
            failed = True
            # 자격·응답 원문을 로그로 흘리지 않는다. 공급자 구분과 예외 종류만
            # 남기고 나머지는 토스 어댑터 자체 헬스 신호가 담당한다.
            logger.warning(
                "kasset toss quote batch unavailable (%s): falling back",
                type(exc).__name__,
            )
        finally:
            now = time.monotonic()
            if failed:
                self._cooldown_until = now + self._FAILURE_COOLDOWN_SECONDS
            for symbol in symbols:
                point = points.get(symbol)
                if point is not None:
                    self._cache[symbol] = (now, point)
                future = self._inflight.pop(symbol, None)
                if future is not None and not future.done():
                    future.set_result(point)

    async def previous_closes(
        self, symbols: Sequence[str], *, boundary: date
    ) -> dict[str, Decimal]:
        """`boundary` 거래일 직전 거래일의 토스 일봉 종가를 모은다.

        저장 일봉 유니버스에 없는 종목의 `previousClose`를 채우는 폴백이다.
        값은 기준 거래일이 바뀔 때까지 캐시하므로 종목당 하루 1회만 호출한다.
        조회에 실패한 종목은 결과에서 빠지고, 호출부는 `previousClose`를
        `null`로 두면 된다. 당일 종가를 전일 종가로 재사용하지 않는다.
        """
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return {}
        requested = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        if not requested:
            return {}
        self._reset_if_loop_changed()

        now = time.monotonic()
        resolved: dict[str, Decimal] = {}
        pending: dict[str, asyncio.Future[Decimal | None]] = {}
        owned: dict[str, asyncio.Future[Decimal | None]] = {}
        loop = asyncio.get_running_loop()
        for symbol in requested:
            cached = self._daily_close.get(symbol)
            if cached is not None and cached[0] == boundary:
                if cached[1] is not None:
                    resolved[symbol] = cached[1]
                    continue
                if now < self._daily_close_miss.get(symbol, 0.0):
                    continue
            inflight = self._daily_close_inflight.get((symbol, boundary))
            if inflight is not None:
                pending[symbol] = inflight
                continue
            future: asyncio.Future[Decimal | None] = loop.create_future()
            self._daily_close_inflight[(symbol, boundary)] = future
            owned[symbol] = future

        if owned:
            await asyncio.gather(
                *(
                    self._fetch_daily_close(symbol, boundary=boundary)
                    for symbol in owned
                )
            )
        for symbol, future in (*owned.items(), *pending.items()):
            close = await future
            if close is not None:
                resolved[symbol] = close
        return resolved

    async def _fetch_daily_close(self, symbol: str, *, boundary: date) -> None:
        close: Decimal | None = None
        try:
            async with self._daily_close_gate:
                client = await self._ensure_client()
                candles = getattr(client, "candles", None)
                if candles is not None:
                    page = await candles(
                        symbol,
                        interval="1d",
                        count=_DAILY_CLOSE_LOOKBACK,
                        adjusted=True,
                    )
                    close = _previous_daily_close(page, boundary=boundary)
        except Exception as exc:  # noqa: BLE001 — previousClose는 없으면 null이다
            logger.warning(
                "kasset toss daily close unavailable (%s): previousClose omitted",
                type(exc).__name__,
            )
        finally:
            now = time.monotonic()
            self._daily_close[symbol] = (boundary, close)
            if close is None:
                self._daily_close_miss[symbol] = now + _DAILY_CLOSE_MISS_TTL_SECONDS
            else:
                self._daily_close_miss.pop(symbol, None)
            future = self._daily_close_inflight.pop((symbol, boundary), None)
            if future is not None and not future.done():
                future.set_result(close)

    async def regular_closes(
        self, symbols: Sequence[str], *, window: TossSessionWindow
    ) -> dict[str, Decimal]:
        """완료된 정규장의 마지막 1분봉 종가를 종목별로 모은다."""

        if not bool(getattr(settings, "toss_api_enabled", False)):
            return {}
        requested = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        if not requested:
            return {}
        self._reset_if_loop_changed()

        now = time.monotonic()
        resolved: dict[str, Decimal] = {}
        pending: dict[str, asyncio.Future[Decimal | None]] = {}
        owned: dict[str, asyncio.Future[Decimal | None]] = {}
        loop = asyncio.get_running_loop()
        for symbol in requested:
            cached = self._regular_close.get(symbol)
            if cached is not None and cached[0] == window.end:
                if cached[1] is not None:
                    resolved[symbol] = cached[1]
                    continue
                if now < self._regular_close_miss.get(symbol, 0.0):
                    continue
            key = (symbol, window.end)
            inflight = self._regular_close_inflight.get(key)
            if inflight is not None:
                pending[symbol] = inflight
                continue
            future: asyncio.Future[Decimal | None] = loop.create_future()
            self._regular_close_inflight[key] = future
            owned[symbol] = future

        if owned:
            await asyncio.gather(
                *(self._fetch_regular_close(symbol, window=window) for symbol in owned)
            )
        for symbol, future in (*owned.items(), *pending.items()):
            close = await future
            if close is not None:
                resolved[symbol] = close
        return resolved

    async def _fetch_regular_close(
        self, symbol: str, *, window: TossSessionWindow
    ) -> None:
        close: Decimal | None = None
        try:
            async with self._daily_close_gate:
                client = await self._ensure_client()
                candles = getattr(client, "candles", None)
                if candles is not None:
                    page = await candles(
                        symbol,
                        interval="1m",
                        count=1,
                        before=(window.end - timedelta(microseconds=1)).isoformat(),
                        adjusted=True,
                    )
                    close = _regular_close(page, window=window)
        except Exception as exc:  # noqa: BLE001 — 정규장 종가는 없으면 null이다
            logger.warning(
                "kasset toss regular close unavailable (%s): regularClose omitted",
                type(exc).__name__,
            )
        finally:
            now = time.monotonic()
            self._regular_close[symbol] = (window.end, close)
            if close is None:
                self._regular_close_miss[symbol] = now + _REGULAR_CLOSE_MISS_TTL_SECONDS
            else:
                self._regular_close_miss.pop(symbol, None)
            future = self._regular_close_inflight.pop((symbol, window.end), None)
            if future is not None and not future.done():
                future.set_result(close)

    async def intraday_bars(
        self,
        symbol: str,
        *,
        count: int,
        market: str,
        window: TossSessionWindow,
        moment: datetime,
    ) -> list[TossDailyBar]:
        """정규장 1분봉을 오래된 순으로 돌려준다.

        진행 중인 정규장은 마지막 봉이 계속 변하므로 캐시하지 않되, 동시 요청은
        한 번의 조회로 합친다. 닫힌 정규장은 종료 직전 시각을 `before`로 주어
        시간외 봉을 건너뛴 뒤, 시장·종목·정규장 창 단위로 캐시한다.
        """
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return []
        normalized = symbol.strip().upper()
        normalized_market = market.strip().lower()
        if not normalized or not normalized_market or count <= 0:
            return []
        self._reset_if_loop_changed()

        key = (normalized_market, normalized, window.start, window.end)
        is_closed = moment >= window.end
        inflight_key = (key, is_closed)
        if is_closed:
            cached = self._closed_intraday_bars.get(key)
            if cached is not None:
                self._closed_intraday_bars.move_to_end(key)
                return cached

        future = self._intraday_inflight.get(inflight_key)
        if future is not None:
            bars = await future
            return bars if bars is not None else []

        future = asyncio.get_running_loop().create_future()
        self._intraday_inflight[inflight_key] = future
        bars: list[TossDailyBar] | None = None
        try:
            before = (
                (window.end - timedelta(microseconds=1)).isoformat()
                if is_closed
                else None
            )
            bars = await self._fetch_intraday_bars(
                normalized,
                count=count,
                before=before,
                window=window,
            )
            if is_closed and bars is not None:
                self._closed_intraday_bars[key] = bars
                self._closed_intraday_bars.move_to_end(key)
                while (
                    len(self._closed_intraday_bars) > _INTRADAY_BARS_CACHE_MAX_ENTRIES
                ):
                    self._closed_intraday_bars.popitem(last=False)
        finally:
            pending = self._intraday_inflight.pop(inflight_key, None)
            if pending is not None and not pending.done():
                pending.set_result(bars)
        return bars if bars is not None else []

    async def _fetch_intraday_bars(
        self,
        symbol: str,
        *,
        count: int,
        before: str | None,
        window: TossSessionWindow,
    ) -> list[TossDailyBar] | None:
        """한 세션의 실제 분봉을 읽는다. 공급자 실패는 캐시하지 않는다."""
        try:
            async with self._daily_close_gate:
                client = await self._ensure_client()
                candles = getattr(client, "candles", None)
                if candles is None:
                    return None
                rows = await fetch_toss_candles(
                    client=cast(TossCandleClient, client),
                    symbol=symbol,
                    interval="1m",
                    count=count,
                    before=before,
                    adjusted=True,
                    max_pages=ceil(count / _TOSS_CANDLE_PAGE_LIMIT),
                )
                return [
                    bar
                    for bar in _daily_bars(
                        TossCandlesPage(candles=rows, next_before=None)
                    )
                    if window.contains(bar.time_utc)
                ]
        except Exception as exc:  # noqa: BLE001 — 분봉은 없으면 빈 목록이다
            logger.warning(
                "kasset toss intraday bars unavailable (%s): chart omitted",
                type(exc).__name__,
            )
            return None

    async def daily_bars(self, symbol: str, *, count: int) -> list[TossDailyBar]:
        """토스 일봉을 오래된 순으로 돌려준다. 실패하면 빈 목록이다.

        저장 일봉이 없거나 요청 수보다 부족한 종목의 차트를 채우는 용도다.
        같은 종목·요청 수는 기존 짧은 TTL 캐시로 반복 호출을 흡수한다.
        """
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return []
        normalized = symbol.strip().upper()
        if not normalized or count <= 0:
            return []
        self._reset_if_loop_changed()

        key = (normalized, count)
        now = time.monotonic()
        cached = self._daily_bars.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]

        bars: list[TossDailyBar] = []
        try:
            async with self._daily_close_gate:
                client = await self._ensure_client()
                candles = getattr(client, "candles", None)
                if candles is not None:
                    page = await candles(
                        normalized,
                        interval="1d",
                        count=count,
                        adjusted=True,
                    )
                    bars = _daily_bars(page)
        except Exception as exc:  # noqa: BLE001 — 차트는 없으면 빈 목록이다
            logger.warning(
                "kasset toss daily bars unavailable (%s): chart omitted",
                type(exc).__name__,
            )
            return []
        self._daily_bars[key] = (time.monotonic() + _DAILY_BARS_TTL_SECONDS, bars)
        return bars

    async def market_indicators(
        self, symbols: Sequence[str]
    ) -> dict[str, TossIndicatorPoint]:
        """토스 시장지표 현재가를 한 번의 배치 호출로 모은다.

        호출부(`/market/overview`)가 이미 15초 캐시 + 단일비행이라 여기에 별도
        캐시 층을 두지 않는다. 실패하면 빈 사전을 돌려주고 호출부가 해당 지표를
        `unavailable`로 표시한다. 값을 만들어내지 않는다.
        """
        if not bool(getattr(settings, "toss_api_enabled", False)):
            return {}
        requested = list(
            dict.fromkeys(
                symbol.strip().upper()
                for symbol in symbols
                if symbol and symbol.strip()
            )
        )
        if not requested:
            return {}
        self._reset_if_loop_changed()

        points: dict[str, TossIndicatorPoint] = {}
        try:
            client = await self._ensure_client()
            # 좁은 `TossPriceClient` 프로토콜을 넓히지 않고 기존 `candles`와 같은
            # 방식으로 선택적 메서드를 읽는다(테스트 대역도 그대로 동작한다).
            fetch = getattr(client, "market_indicator_prices", None)
            if fetch is not None:
                for row in await fetch(requested):
                    point = _indicator_point(row)
                    if point is not None and point.symbol in requested:
                        points[point.symbol] = point
        except Exception as exc:  # noqa: BLE001 — 지표는 없으면 unavailable이다
            # 자격·응답 원문은 로그로 흘리지 않는다.
            logger.warning(
                "kasset toss market indicators unavailable (%s): indicators omitted",
                type(exc).__name__,
            )
            return {}
        return points

    async def _ensure_client(self) -> TossPriceClient:
        client = self._client
        if client is not None:
            return client
        async with self._client_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _reset_if_loop_changed(self) -> None:
        """루프가 바뀌면 루프에 묶인 상태를 버린다.

        `httpx.AsyncClient`와 `asyncio.Lock`은 만들어진 루프에만 유효하다.
        운영에서는 루프가 하나지만, 죽은 루프의 클라이언트를 재사용하면
        조용히 깨지므로 참조를 버리고 새로 만든다.
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._client = None
        self._client_lock = asyncio.Lock()
        self._cache.clear()
        self._inflight.clear()
        self._cooldown_until = 0.0
        self._daily_close.clear()
        self._daily_close_miss.clear()
        self._daily_close_inflight.clear()
        self._regular_close.clear()
        self._regular_close_miss.clear()
        self._regular_close_inflight.clear()
        self._daily_close_gate = asyncio.Semaphore(_DAILY_CLOSE_CONCURRENCY)
        self._daily_bars.clear()
        self._closed_intraday_bars.clear()
        self._intraday_inflight.clear()

    async def aclose(self) -> None:
        """lifespan 종료 시 공용 클라이언트를 닫는다."""
        client = self._client
        self._client = None
        if client is None:
            return
        aclose = getattr(client, "aclose", None)
        if aclose is None:
            return
        with suppress(Exception):
            await aclose()

    def reset(self) -> None:
        """테스트용: 캐시·단일비행·클라이언트 참조를 모두 버린다."""
        self._client = None
        self._loop = None
        self._cache.clear()
        self._inflight.clear()
        self._cooldown_until = 0.0
        self._daily_close.clear()
        self._daily_close_miss.clear()
        self._daily_close_inflight.clear()
        self._regular_close.clear()
        self._regular_close_miss.clear()
        self._regular_close_inflight.clear()
        self._daily_bars.clear()
        self._closed_intraday_bars.clear()
        self._intraday_inflight.clear()


toss_market_data = TossSharedMarketData()
