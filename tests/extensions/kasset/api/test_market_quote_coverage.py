"""저장 일봉이 없는 종목의 시세 커버리지 계약.

실측 결함(2026-08-28): `kr_symbol_universe`가 0행이라 일봉 동기화가 대상 0건으로
공전했고, 저장 일봉은 수동 시드된 3종목뿐이었다. 그 결과

- KRX: 관심종목에 새로 넣은 종목의 `previousClose`가 계속 `null`이라
  등락률이 `-`로 표시되고 상세 차트가 빈 배열이었다.
- 미국: `/market/quote`가 토스가 아닌 Yahoo 폴백으로 내려가 **한 세션 지연된**
  종가를 현재가로 보여주고, `changeRate`가 양자화되지 않아 27자리 문자열이
  앱으로 나갔다.

토스는 국내 6자리 코드와 미국 티커를 같은 `GET /api/v1/prices`로 받고
`GET /api/v1/candles`(interval=1d)로 두 시장의 일봉을 모두 준다. 이 파일은 그
폴백이 붙어 있는지, 그리고 저장 일봉이 있을 때는 폴백을 타지 않는지를 고정한다.

토스 HTTP는 호출하지 않는다. 테스트 환경은 오프라인 계약이므로 스텁
클라이언트 팩토리를 주입한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api import router as router_module
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.toss_market_data import (
    TossSharedMarketData,
    _previous_daily_close,
)
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage, TossPrice


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeDb:
    """일봉 배치 읽기와 종목명 조회만 모사하는 세션 대역."""

    def __init__(
        self, *, candles: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        self.candles = candles or {}

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> _FakeResult:
        text = str(statement)
        if "candles_1d" in text:
            if not params:
                requested: list[str] = []
            elif "symbols" in params:
                requested = list(params["symbols"])
            else:
                # 단일 조회(`fetch_recent`)는 `symbol` 하나만 바인딩한다.
                requested = [params["symbol"]]
            rows: list[dict[str, Any]] = []
            for symbol in requested:
                rows.extend(self.candles.get(symbol, []))
            return _FakeResult(rows)
        return _FakeResult([])


class _StubTossClient:
    """`prices`와 `candles`를 모두 모사한다. 호출 횟수를 세어 캐시를 검증한다."""

    def __init__(
        self,
        *,
        prices: dict[str, TossPrice] | None = None,
        candles: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        self._prices = prices or {}
        self._candles = candles or {}
        self.price_calls: list[list[str]] = []
        self.candle_calls: list[str] = []

    async def prices(self, symbols: Sequence[str]) -> list[TossPrice]:
        self.price_calls.append(list(symbols))
        return [self._prices[symbol] for symbol in symbols if symbol in self._prices]

    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage:
        assert interval == "1d"
        self.candle_calls.append(symbol)
        rows = self._candles.get(symbol, [])
        return TossCandlesPage(
            candles=[
                TossCandle(
                    timestamp=timestamp,
                    open_price=Decimal(close),
                    high_price=Decimal(close),
                    low_price=Decimal(close),
                    close_price=Decimal(close),
                    volume=Decimal(1000),
                    currency="KRW",
                )
                for timestamp, close in rows
            ],
            next_before=None,
        )

    async def aclose(self) -> None:
        return None


def _toss_price(
    symbol: str,
    *,
    price: str,
    timestamp: str = "2026-08-28T18:44:26+09:00",
    currency: str = "KRW",
) -> TossPrice:
    return TossPrice(
        symbol=symbol,
        timestamp=timestamp,  # type: ignore[arg-type]
        last_price=Decimal(price),
        currency=currency,
    )


def _stored_candle(*, symbol: str, day: str, close: float) -> dict[str, Any]:
    return {
        "time": datetime.fromisoformat(f"{day}T00:00:00+00:00"),
        "symbol": symbol,
        "partition": "KRX",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": None,
        "volume": 1000.0,
        "value": 0.0,
        "source": "test",
    }


@pytest.fixture
def toss_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)


def _install(
    monkeypatch: pytest.MonkeyPatch, client: _StubTossClient
) -> TossSharedMarketData:
    service = TossSharedMarketData(client_factory=lambda: client)
    monkeypatch.setattr(krx_quotes, "toss_market_data", service)
    monkeypatch.setattr(router_module, "toss_market_data", service)
    return service


def _client(db: object) -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[object]:
        yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = lambda: _session()
    return TestClient(app)


def _session() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(user=SimpleNamespace(id=101, role="trader", is_active=True))


def test_previous_daily_close_skips_the_boundary_day_bar() -> None:
    """당일 봉을 전일 종가로 재사용하면 등락이 0으로 위조된다."""

    page = TossCandlesPage(
        candles=[
            TossCandle(
                timestamp="2026-08-27T00:00:00+09:00",
                open_price=Decimal(100),
                high_price=Decimal(100),
                low_price=Decimal(100),
                close_price=Decimal("73.30"),
                volume=Decimal(1),
                currency="USD",
            ),
            TossCandle(
                timestamp="2026-08-28T00:00:00+09:00",
                open_price=Decimal(100),
                high_price=Decimal(100),
                low_price=Decimal(100),
                close_price=Decimal("72.96"),
                volume=Decimal(1),
                currency="USD",
            ),
        ],
        next_before=None,
    )
    assert _previous_daily_close(page, boundary=date(2026, 8, 28)) == Decimal("73.30")
    # 기준일이 더 과거면 그보다 앞선 봉이 없으므로 값이 없다.
    assert _previous_daily_close(page, boundary=date(2026, 8, 27)) is None


@pytest.mark.usefixtures("toss_enabled")
def test_krx_change_rate_survives_missing_stored_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장 일봉이 없어도 토스 일봉으로 등락률을 채운다."""

    client = _StubTossClient(
        prices={"005380": _toss_price("005380", price="398500")},
        candles={
            "005380": [
                ("2026-08-27T00:00:00+09:00", "401000"),
                ("2026-08-28T00:00:00+09:00", "398500"),
            ]
        },
    )
    _install(monkeypatch, client)

    with _client(_FakeDb()) as http:
        response = http.get("/api/v1/market/quotes?market=KRX&symbols=005380")

    assert response.status_code == 200
    quote = response.json()["quotes"][0]
    assert quote["previousClose"] == "401000"
    assert quote["changeAmount"] == "-2500"
    assert quote["changeRate"] == "-0.62"
    assert quote["source"] == "TOSS_API_PRICES"
    assert client.candle_calls == ["005380"]


@pytest.mark.usefixtures("toss_enabled")
def test_stored_candles_win_and_skip_the_daily_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장 일봉으로 전일 종가가 나오면 토스 일봉을 호출하지 않는다."""

    client = _StubTossClient(
        prices={"005930": _toss_price("005930", price="256500")},
        candles={"005930": [("2026-08-27T00:00:00+09:00", "999999")]},
    )
    _install(monkeypatch, client)
    db = _FakeDb(
        candles={
            "005930": [
                _stored_candle(symbol="005930", day="2026-08-28", close=256500.0),
                _stored_candle(symbol="005930", day="2026-08-27", close=267000.0),
            ]
        }
    )

    with _client(db) as http:
        response = http.get("/api/v1/market/quotes?market=KRX&symbols=005930")

    quote = response.json()["quotes"][0]
    assert quote["previousClose"] == "267000"
    assert quote["changeRate"] == "-3.93"
    assert client.candle_calls == []


@pytest.mark.usefixtures("toss_enabled")
def test_us_quote_uses_toss_and_quantizes_change_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """미국 종목이 Yahoo 폴백이 아니라 토스로 해석되고 등락률이 양자화된다."""

    client = _StubTossClient(
        prices={
            "TQQQ": _toss_price(
                "TQQQ",
                price="73.05",
                timestamp="2026-08-28T21:23:04+09:00",
                currency="USD",
            )
        },
        candles={
            "TQQQ": [
                ("2026-08-27T13:00:00+09:00", "73.30"),
                ("2026-08-28T13:00:00+09:00", "72.96"),
            ]
        },
    )
    _install(monkeypatch, client)

    with _client(_FakeDb()) as http:
        response = http.get("/api/v1/market/quote?broker=PAPER&market=US&symbol=TQQQ")

    assert response.status_code == 200
    quote = response.json()
    assert quote["market"] == "US"
    assert quote["currency"] == "USD"
    assert quote["price"] == "73.05"
    assert quote["previousClose"] == "73.30"
    assert quote["source"] == "TOSS_API_PRICES"
    # 27자리 미양자화 문자열이 앱으로 나가던 결함을 고정한다.
    assert quote["changeRate"] == "-0.34"


@pytest.mark.usefixtures("toss_enabled")
def test_candles_fall_back_to_toss_when_store_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장 일봉이 비면 차트를 토스 일봉으로 채우고 오래된 순으로 준다."""

    client = _StubTossClient(
        candles={
            "005380": [
                ("2026-08-28T00:00:00+09:00", "398500"),
                ("2026-08-27T00:00:00+09:00", "401000"),
            ]
        }
    )
    _install(monkeypatch, client)

    with _client(_FakeDb()) as http:
        response = http.get("/api/v1/market/candles?market=KRX&symbol=005380&count=5")

    assert response.status_code == 200
    candles = response.json()["candles"]
    assert [candle["close"] for candle in candles] == ["401000", "398500"]
    assert candles[0]["time"] < candles[1]["time"]


@pytest.mark.usefixtures("toss_enabled")
def test_daily_bars_are_cached_per_symbol_and_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 차트를 반복 열어도 토스 차트 그룹을 다시 호출하지 않는다."""

    client = _StubTossClient(
        candles={"005380": [("2026-08-28T00:00:00+09:00", "398500")]}
    )
    service = _install(monkeypatch, client)

    async def scenario() -> None:
        first = await service.daily_bars("005380", count=5)
        second = await service.daily_bars("005380", count=5)
        assert first == second
        assert client.candle_calls == ["005380"]
        # count가 다르면 다른 응답이므로 캐시를 공유하지 않는다.
        await service.daily_bars("005380", count=60)
        assert client.candle_calls == ["005380", "005380"]

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_previous_close_miss_is_not_refetched_within_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """일봉이 없는 종목을 폴링이 반복 조회하지 않는다."""

    client = _StubTossClient(prices={}, candles={})
    service = _install(monkeypatch, client)

    async def scenario() -> None:
        boundary = date(2026, 8, 28)
        assert await service.previous_closes(["999999"], boundary=boundary) == {}
        assert await service.previous_closes(["999999"], boundary=boundary) == {}
        assert client.candle_calls == ["999999"]

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_quote_for_market_resolves_supported_markets_through_toss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """주문 기준가 진입점이 표시 경로와 같은 토스 시세를 준다.

    실측 결함(2026-08-28): 표시는 토스 실시간(TQQQ 73.06)인데 주문 미리보기는
    Yahoo 하루 지연값(73.30000305175781)을 기준가로 썼다.
    """

    client = _StubTossClient(
        prices={
            "TQQQ": _toss_price(
                "TQQQ",
                price="73.05",
                timestamp="2026-08-28T21:23:04+09:00",
                currency="USD",
            )
        },
        candles={"TQQQ": [("2026-08-27T13:00:00+09:00", "73.30")]},
    )
    _install(monkeypatch, client)

    async def scenario() -> None:
        quote = await krx_quotes.quote_for_market(_FakeDb(), market="US", symbol="TQQQ")
        assert quote.source == "TOSS_API_PRICES"
        assert quote.price == "73.05"
        assert quote.currency == "USD"
        assert quote.change_rate == "-0.34"

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_quote_for_market_falls_back_for_unsupported_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """토스가 못 받는 시장은 기존 PAPER 어댑터로 내려간다."""

    _install(monkeypatch, _StubTossClient())
    calls: list[str] = []

    async def fake_quote(db: object, *, market: str, symbol: str) -> object:
        calls.append(market)
        return "paper-quote"

    monkeypatch.setattr(
        krx_quotes.paper_account_adapter, "quote", fake_quote, raising=True
    )

    async def scenario() -> None:
        result = await krx_quotes.quote_for_market(
            _FakeDb(), market="CRYPTO", symbol="KRW-BTC"
        )
        assert result == "paper-quote"
        assert calls == ["CRYPTO"]

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_us_previous_close_uses_et_trading_date_across_kst_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """미국 정규장이 KST 자정을 넘어도 진행 중 봉을 전일종가로 쓰지 않는다.

    검수 지적(2026-08-28): 기준일을 KST 날짜로 잡으면 01:00 KST 시세가
    boundary를 하루 앞세워 진행 중인 당일 봉을 "직전 거래일"로 집었다.
    토스는 미국 일봉을 ET 자정(`13:00+09:00`)으로 라벨한다.
    """

    client = _StubTossClient(
        # 01:00 KST 2026-08-29 = 12:00 ET 2026-08-28 (정규장 진행 중)
        prices={
            "TQQQ": _toss_price(
                "TQQQ",
                price="80",
                timestamp="2026-08-29T01:00:00+09:00",
                currency="USD",
            )
        },
        candles={
            "TQQQ": [
                ("2026-08-27T13:00:00+09:00", "70"),
                # 진행 중인 08-28 세션 봉. 전일종가로 쓰면 안 된다.
                ("2026-08-28T13:00:00+09:00", "75"),
            ]
        },
    )
    _install(monkeypatch, client)

    async def scenario() -> None:
        quote = await krx_quotes.resolve_quote(
            _FakeDb(), market="NASDAQ", symbol="TQQQ"
        )
        # 08-27 종가 70이어야 한다. 75(진행 중 봉)면 등락률이 위조된다.
        assert quote.previous_close == "70"
        assert quote.change_amount == "10"

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_daily_close_single_flight_does_not_leak_across_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단일비행이 기준 거래일을 무시해 남의 결과를 주지 않는다.

    검수 지적(2026-08-28): 키가 `symbol`만이면 거래일이 바뀌는 순간 다른
    기준일을 기다리던 호출자가 앞선 기준일의 종가를 받았다.
    """

    client = _StubTossClient(
        candles={
            "005930": [
                ("2026-08-26T00:00:00+09:00", "100"),
                ("2026-08-27T00:00:00+09:00", "200"),
                ("2026-08-28T00:00:00+09:00", "300"),
            ]
        }
    )
    service = _install(monkeypatch, client)

    async def scenario() -> None:
        first, second = await asyncio.gather(
            service.previous_closes(["005930"], boundary=date(2026, 8, 28)),
            service.previous_closes(["005930"], boundary=date(2026, 8, 27)),
        )
        # 각 기준일의 직전 거래일 종가를 각각 받아야 한다.
        assert first == {"005930": Decimal("200")}
        assert second == {"005930": Decimal("100")}

    import asyncio

    asyncio.run(scenario())


@pytest.mark.usefixtures("toss_enabled")
def test_us_quote_currency_comes_from_market_not_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """통화는 시장이 결정한다. 공급자가 KRW를 줘도 미국은 USD다.

    검수 지적(2026-08-28): 공급자 필드를 그대로 신뢰하면 수수료 자산군이
    `equity_kr`로 잘못 키잉될 수 있다.
    """

    client = _StubTossClient(
        prices={
            "TQQQ": _toss_price(
                "TQQQ",
                price="80",
                timestamp="2026-08-28T23:30:00+09:00",
                currency="KRW",  # 공급자 오표기
            )
        }
    )
    _install(monkeypatch, client)

    async def scenario() -> None:
        quote = await krx_quotes.resolve_quote(
            _FakeDb(), market="NASDAQ", symbol="TQQQ"
        )
        assert quote.currency == "USD"

    import asyncio

    asyncio.run(scenario())
