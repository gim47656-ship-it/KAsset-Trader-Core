"""서버 공용 KRX 실시간 시세 채널 (토스 인증 REST 배치).

토스 `GET /api/v1/prices`는 한 번의 호출로 여러 종목의 현재가를 내려주고
운영에서 0.15초 수준으로 응답한다. Android 시세 경로가 이 채널을 1순위로 쓰고
실패하면 호출부가 기존 NH 공용 채널 → 저장 캔들 순서로 조용히 강등한다.

계좌·주문과 무관한 읽기 전용 공용 데이터이므로 사용자 볼트 자격이 아니라
서버 env 자격(`TOSS_API_*`)만 사용한다. 이 모듈은 시세 조회만 하며 주문·자산
경로는 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.core.config import settings
from app.services.brokers.toss.client import TossReadClient
from app.services.invest_price_fallback import TossPriceClient

logger = logging.getLogger(__name__)

# 응답 `source` 값. 공급자 구분만 노출하고 자격·엔드포인트·원문 예외는 절대
# 담지 않는다.
TOSS_QUOTE_SOURCE = "TOSS_API_PRICES"

_MIN_PLAUSIBLE_YEAR = 2000
_MAX_PLAUSIBLE_YEAR = 2100


@dataclass(frozen=True, slots=True)
class TossQuotePoint:
    """토스 배치 시세 한 종목. `as_of`는 항상 tz-aware UTC다."""

    symbol: str
    price: Decimal
    currency: str
    as_of: datetime


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


toss_market_data = TossSharedMarketData()
