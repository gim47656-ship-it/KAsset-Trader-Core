"""KRX 실시간 시세 계약: 배치 엔드포인트와 토스 우선 폴백 체인.

토스 HTTP는 실제로 호출하지 않는다. 테스트 환경은 오프라인 계약이므로
`TossSharedMarketData`에 스텁 클라이언트 팩토리를 주입해 배치 응답을 모사한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.toss_market_data import (
    TossQuotePoint,
    TossSharedMarketData,
    _regular_close,
)
from app.middleware.auth import AuthMiddleware
from app.services.brokers.toss.dto import TossPrice
from app.services.brokers.toss.market_calendar import TossSessionWindow
from app.services.nxt_preflight import NxtTradability


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeDb:
    """`kr_candles_1d` 배치 읽기와 종목명 조회만 모사하는 세션 대역."""

    def __init__(
        self,
        *,
        candles: dict[str, list[dict[str, Any]]] | None = None,
        names: dict[str, str] | None = None,
    ) -> None:
        self.candles = candles or {}
        self.names = names or {}
        self.candle_reads = 0
        self.name_reads = 0

    async def execute(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> _FakeResult:
        if "kr_candles_1d" in str(statement):
            self.candle_reads += 1
            requested = list(params["symbols"]) if params else []
            rows: list[dict[str, Any]] = []
            for symbol in requested:
                rows.extend(self.candles.get(symbol, []))
            return _FakeResult(rows)
        self.name_reads += 1
        return _FakeResult(list(self.names.items()))


class _StubTossClient:
    def __init__(
        self,
        prices: dict[str, TossPrice] | None = None,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._prices = prices or {}
        self._error = error
        self._gate = gate
        self.calls: list[list[str]] = []
        self.closed = False

    async def prices(self, symbols: Sequence[str]) -> list[TossPrice]:
        self.calls.append(list(symbols))
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error
        return [self._prices[symbol] for symbol in symbols if symbol in self._prices]

    async def aclose(self) -> None:
        self.closed = True


def _toss_price(
    symbol: str,
    *,
    price: str,
    timestamp: object = "2026-08-28T18:44:26+09:00",
    currency: str = "KRW",
) -> TossPrice:
    return TossPrice(
        symbol=symbol,
        timestamp=timestamp,  # type: ignore[arg-type]
        last_price=Decimal(price),
        currency=currency,
    )


def _candle_row(*, symbol: str, day: str, close: float) -> dict[str, Any]:
    # 저장 일봉의 `time`은 거래일 자정 UTC다. 실제 SQL이 time DESC로 주므로
    # 픽스처도 최신 행을 먼저 넣는다.
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


class _StubNh:
    def __init__(self, quote: Quote | None = None) -> None:
        self._quote = quote
        self.symbols: list[str] = []

    async def quote(self, *, market: str, symbol: str) -> Quote:
        self.symbols.append(symbol)
        if self._quote is None:
            raise MobileApiError(502, "NH_QUOTE_FAILED", "조회하지 못했습니다.")
        return self._quote.model_copy(update={"symbol": symbol})


def _nh_quote(symbol: str) -> Quote:
    return Quote(
        broker="NH",
        market="KRX",
        symbol=symbol,
        name="엔에이치",
        currency="KRW",
        price="71500",
        previous_close="71000",
        change_amount="500",
        change_rate="0.7",
        as_of="2026-08-28T09:44:00Z",
        source="NH_PLUG_MOCK",
    )


@pytest.fixture
def toss_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)


@pytest.fixture(autouse=True)
def regular_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def context(
        db: object, *, market: str, symbols: Sequence[str]
    ) -> tuple[dict[str, str], None]:
        del db, market
        return (dict.fromkeys(symbols, "REGULAR"), None)

    monkeypatch.setattr(krx_quotes, "_quote_session_context", context)


def _window(start: datetime, end: datetime) -> TossSessionWindow:
    return TossSessionWindow(start=start, end=end)


def _install_toss(
    monkeypatch: pytest.MonkeyPatch, client: _StubTossClient
) -> TossSharedMarketData:
    service = TossSharedMarketData(client_factory=lambda: client)
    monkeypatch.setattr(krx_quotes, "toss_market_data", service)
    return service


def _client(db: object) -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[object]:
        yield db

    async def session_override() -> object:
        return SimpleNamespace(user=SimpleNamespace(id=101, role="trader"))

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    return TestClient(app)


def test_batch_quotes_serve_many_symbols_from_one_toss_call(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient(
        {
            "005930": _toss_price("005930", price="256500"),
            "000660": _toss_price("000660", price="180000"),
        }
    )
    _install_toss(monkeypatch, toss)
    db = _FakeDb(
        candles={
            # 당일 행(08-28)과 직전 거래일 행(08-27)이 함께 있다.
            "005930": [
                _candle_row(symbol="005930", day="2026-08-28", close=256000.0),
                _candle_row(symbol="005930", day="2026-08-27", close=250000.0),
            ]
        },
        names={"005930": "삼성전자"},
    )

    with _client(db) as client:
        response = client.get("/api/v1/market/quotes?market=KRX&symbols=005930,000660")

    assert response.status_code == 200
    assert response.json() == {
        "quotes": [
            {
                "broker": "PAPER",
                "market": "KRX",
                "symbol": "005930",
                "name": "삼성전자",
                "currency": "KRW",
                "price": "256500",
                # 당일 종가(256000)가 아니라 직전 거래일 종가를 쓴다.
                "previousClose": "250000",
                "changeAmount": "6500",
                "changeRate": "2.60",
                "session": "REGULAR",
                "regularClose": None,
                "sessionChangeAmount": None,
                "sessionChangeRate": None,
                # +09:00 공급자 시각은 UTC `Z`로 정규화된다.
                "asOf": "2026-08-28T09:44:26Z",
                "source": "TOSS_API_PRICES",
            },
            {
                "broker": "PAPER",
                "market": "KRX",
                "symbol": "000660",
                "name": None,
                "currency": "KRW",
                "price": "180000",
                # 저장 일봉이 없으면 previousClose를 만들어내지 않는다.
                "previousClose": None,
                "changeAmount": None,
                "changeRate": None,
                "session": "REGULAR",
                "regularClose": None,
                "sessionChangeAmount": None,
                "sessionChangeRate": None,
                "asOf": "2026-08-28T09:44:26Z",
                "source": "TOSS_API_PRICES",
            },
        ]
    }
    assert toss.calls == [["005930", "000660"]]
    assert db.candle_reads == 1


def test_batch_quotes_deduplicate_symbols_before_calling_toss(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient({"005930": _toss_price("005930", price="256500")})
    _install_toss(monkeypatch, toss)

    with _client(_FakeDb()) as client:
        response = client.get(
            "/api/v1/market/quotes?market=KRX&symbols=005930,005930,005930"
        )

    assert response.status_code == 200
    assert [quote["symbol"] for quote in response.json()["quotes"]] == ["005930"]
    assert toss.calls == [["005930"]]


def test_batch_quotes_accept_us_interest_symbols_in_one_toss_call(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient(
        {
            "TQQQ": _toss_price("TQQQ", price="71.89", currency="USD"),
            "AAPL": _toss_price("AAPL", price="111.62", currency="USD"),
        }
    )
    _install_toss(monkeypatch, toss)

    with _client(_FakeDb()) as client:
        response = client.get("/api/v1/market/quotes?market=US&symbols=tqqq,AAPL")

    assert response.status_code == 200
    assert [
        (quote["market"], quote["symbol"], quote["session"])
        for quote in response.json()["quotes"]
    ] == [
        ("US", "TQQQ", "REGULAR"),
        ("US", "AAPL", "REGULAR"),
    ]
    assert toss.calls == [["TQQQ", "AAPL"]]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("market=KRX&symbols=,", "조회할 종목 코드를 입력해 주세요."),
        ("market=KRX&symbols=00593", "KRX 6자리 종목코드만 조회할 수 있습니다."),
        ("market=KRX&symbols=005930,AAPL", "KRX 6자리 종목코드만 조회할 수 있습니다."),
        (
            "market=KRX&symbols=" + ",".join(f"{index:06d}" for index in range(51)),
            "한 번에 최대 50종목까지 조회할 수 있습니다.",
        ),
        ("market=LSE&symbols=005930", "지원하지 않는 시장입니다."),
    ],
)
def test_batch_quotes_reject_contract_violations(query: str, message: str) -> None:
    with _client(_FakeDb()) as client:
        response = client.get(f"/api/v1/market/quotes?{query}")

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": message}
    }


def test_batch_quotes_degrade_to_nh_then_stored_candles_when_toss_fails(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    _install_toss(monkeypatch, _StubTossClient(error=RuntimeError("toss unavailable")))

    class _PartialNh(_StubNh):
        """005930만 응답하는 NH 공용 채널."""

        async def quote(self, *, market: str, symbol: str) -> Quote:
            self.symbols.append(symbol)
            if symbol == "005930":
                return _nh_quote(symbol)
            raise MobileApiError(502, "NH_QUOTE_FAILED", "조회하지 못했습니다.")

    partial = _PartialNh()
    monkeypatch.setattr(krx_quotes, "nh_market_data", partial)
    db = _FakeDb(
        candles={
            "000660": [
                _candle_row(symbol="000660", day="2026-08-27", close=180000.0),
                _candle_row(symbol="000660", day="2026-08-26", close=175000.0),
            ]
        }
    )

    with _client(db) as client:
        response = client.get("/api/v1/market/quotes?market=KRX&symbols=005930,000660")

    assert response.status_code == 200
    quotes = response.json()["quotes"]
    # 우선순위: 토스 실패 → NH 공용 → 저장 일봉.
    assert partial.symbols == ["005930", "000660"]
    assert [(quote["symbol"], quote["source"]) for quote in quotes] == [
        ("005930", "NH_PLUG_MOCK"),
        ("000660", "PAPER_CANDLES"),
    ]
    assert quotes[0]["broker"] == "PAPER"
    assert quotes[1] == {
        "broker": "PAPER",
        "market": "KRX",
        "symbol": "000660",
        "name": None,
        "currency": "KRW",
        "price": "180000",
        "previousClose": "175000",
        "changeAmount": "5000",
        "changeRate": "2.86",
        "session": "REGULAR",
        "regularClose": None,
        "sessionChangeAmount": None,
        "sessionChangeRate": None,
        "asOf": "2026-08-27T00:00:00Z",
        "source": "PAPER_CANDLES",
    }


def test_batch_quotes_omit_symbols_no_channel_can_serve(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    _install_toss(monkeypatch, _StubTossClient(error=RuntimeError("down")))
    monkeypatch.setattr(krx_quotes, "nh_market_data", _StubNh())

    with _client(_FakeDb()) as client:
        response = client.get("/api/v1/market/quotes?market=KRX&symbols=005930,000660")

    assert response.status_code == 200
    assert response.json() == {"quotes": []}


def test_batch_quotes_never_leak_provider_internals(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    secret = "toss-client-secret-do-not-leak"
    _install_toss(monkeypatch, _StubTossClient(error=RuntimeError(secret)))
    monkeypatch.setattr(krx_quotes, "nh_market_data", _StubNh())
    db = _FakeDb(
        candles={
            "005930": [_candle_row(symbol="005930", day="2026-08-27", close=250000.0)]
        }
    )

    with _client(db) as client:
        response = client.get("/api/v1/market/quotes?market=KRX&symbols=005930")

    assert response.status_code == 200
    assert secret not in response.text
    assert "RuntimeError" not in response.text
    assert response.json()["quotes"][0]["source"] == "PAPER_CANDLES"


def test_batch_quotes_require_authentication() -> None:
    app = FastAPI()
    install_android_compat_api(app)
    app.add_middleware(AuthMiddleware)

    async def db_override() -> AsyncIterator[object]:
        yield _FakeDb()

    app.dependency_overrides[get_db] = db_override

    with TestClient(app) as client:
        response = client.get("/api/v1/market/quotes?market=KRX&symbols=005930")

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "UNAUTHORIZED", "message": "인증 토큰이 필요합니다."}
    }


def test_single_krx_quote_prefers_toss_over_shared_nh_channel(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient({"005930": _toss_price("005930", price="256500")})
    _install_toss(monkeypatch, toss)
    nh = _StubNh(_nh_quote("005930"))
    monkeypatch.setattr(krx_quotes, "nh_market_data", nh)
    db = _FakeDb(
        candles={
            "005930": [_candle_row(symbol="005930", day="2026-08-27", close=250000.0)]
        },
        names={"005930": "삼성전자"},
    )

    with _client(db) as client:
        response = client.get(
            "/api/v1/market/quote?broker=PAPER&market=KRX&symbol=005930"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "TOSS_API_PRICES"
    assert body["price"] == "256500"
    assert body["previousClose"] == "250000"
    assert body["asOf"] == "2026-08-28T09:44:26Z"
    assert nh.symbols == []


def test_single_krx_quote_degrades_to_existing_paper_path_when_channels_fail(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    _install_toss(monkeypatch, _StubTossClient(error=RuntimeError("down")))
    monkeypatch.setattr(krx_quotes, "nh_market_data", _StubNh())
    paper_quote = AsyncMock(
        return_value=Quote(
            broker="PAPER",
            market="KRX",
            symbol="005930",
            name=None,
            currency="KRW",
            price="250000",
            previous_close=None,
            change_amount=None,
            change_rate=None,
            as_of="2026-08-27T00:00:00Z",
            source="PAPER_CANDLES",
        )
    )
    monkeypatch.setattr(
        krx_quotes.paper_account_adapter, "quote", paper_quote, raising=True
    )

    with _client(_FakeDb()) as client:
        response = client.get(
            "/api/v1/market/quote?broker=PAPER&market=KRX&symbol=005930"
        )

    assert response.status_code == 200
    assert response.json()["source"] == "PAPER_CANDLES"
    paper_quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_toss_channel_merges_concurrent_requests_into_one_batch_call(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    gate = asyncio.Event()
    toss = _StubTossClient(
        {
            "005930": _toss_price("005930", price="256500"),
            "000660": _toss_price("000660", price="180000"),
        },
        gate=gate,
    )
    service = TossSharedMarketData(client_factory=lambda: toss)

    tasks = [
        asyncio.create_task(service.prices(["005930", "000660"])) for _ in range(3)
    ]
    # 세 요청이 모두 같은 배치를 기다리는 상태를 만든 뒤 응답을 흘려보낸다.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert toss.calls == [["005930", "000660"]]
    assert all(set(result) == {"005930", "000660"} for result in results)

    # 2초 캐시가 직후 재조회를 흡수한다.
    again = await service.prices(["005930", "000660"])
    assert set(again) == {"005930", "000660"}
    assert toss.calls == [["005930", "000660"]]


def test_us_day_market_uses_latest_regular_close_as_previous_close() -> None:
    quote = krx_quotes._toss_quote(
        TossQuotePoint(
            symbol="TQQQ",
            price=Decimal("73.45"),
            currency="USD",
            as_of=datetime.fromisoformat("2026-08-28T09:30:00+09:00"),
        ),
        market="US",
        name=None,
        rows=(),
        previous_close_fallback=Decimal("72.00"),
        session="DAY_MARKET",
        regular_close=Decimal("73.30"),
    )

    assert quote.previous_close == "73.30"
    assert quote.change_amount == "0.00"
    assert quote.change_rate == "0.00"
    assert quote.session_change_amount == "0.15"
    assert quote.session_change_rate == "0.20"


def test_us_after_market_quote_separates_regular_and_session_changes(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient(
        {
            "TQQQ": _toss_price(
                "TQQQ",
                price="71.6995",
                timestamp="2026-08-28T08:20:00+09:00",
                currency="USD",
            )
        }
    )
    _install_toss(monkeypatch, toss)
    regular_window = _window(
        datetime.fromisoformat("2026-08-27T22:30:00+09:00"),
        datetime.fromisoformat("2026-08-28T05:00:00+09:00"),
    )

    async def after_context(
        db: object, *, market: str, symbols: Sequence[str]
    ) -> tuple[dict[str, str], TossSessionWindow]:
        del db
        assert market == "US"
        return (dict.fromkeys(symbols, "AFTER_MARKET"), regular_window)

    monkeypatch.setattr(krx_quotes, "_quote_session_context", after_context)
    monkeypatch.setattr(krx_quotes, "_candle_rows", AsyncMock(return_value={}))
    monkeypatch.setattr(krx_quotes, "_instrument_names", AsyncMock(return_value={}))
    monkeypatch.setattr(
        krx_quotes,
        "_previous_close_fallback",
        AsyncMock(return_value={"TQQQ": Decimal("73.3")}),
    )
    monkeypatch.setattr(
        krx_quotes,
        "_regular_closes",
        AsyncMock(return_value={"TQQQ": Decimal("71.89")}),
    )

    with _client(_FakeDb()) as client:
        response = client.get("/api/v1/market/quote?broker=PAPER&market=US&symbol=TQQQ")

    assert response.status_code == 200
    assert response.json() == {
        "broker": "PAPER",
        "market": "US",
        "symbol": "TQQQ",
        "name": None,
        "currency": "USD",
        "price": "71.6995",
        "previousClose": "73.3",
        "changeAmount": "-1.41",
        "changeRate": "-1.92",
        "session": "AFTER_MARKET",
        "regularClose": "71.89",
        "sessionChangeAmount": "-0.1905",
        "sessionChangeRate": "-0.26",
        "asOf": "2026-08-27T23:20:00Z",
        "source": "TOSS_API_PRICES",
    }


@pytest.mark.parametrize(
    ("market", "state", "tradability", "expected"),
    [
        (
            "KRX",
            "PRE_MARKET",
            NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=datetime(2026, 8, 28, tzinfo=UTC),
            ),
            "PRE_MARKET",
        ),
        (
            "KRX",
            "AFTER_MARKET",
            NxtTradability(
                nxt_eligible=False,
                nxt_trading_suspended=False,
                asof=datetime(2026, 8, 28, tzinfo=UTC),
            ),
            "CLOSED",
        ),
        ("KRX", "PRE_MARKET", None, None),
        ("US", "DAY_MARKET", None, "DAY_MARKET"),
    ],
)
def test_symbol_session_never_leaks_nxt_only_windows(
    market: str,
    state: str,
    tradability: NxtTradability | None,
    expected: str | None,
) -> None:
    assert (
        krx_quotes._symbol_session_state(
            market,
            state,  # type: ignore[arg-type]
            tradability,
            moment=datetime(2026, 8, 28, 1, tzinfo=UTC),
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "provider_session", "expected"),
    [
        ("US", "day", "DAY_MARKET"),
        ("US", "pre", "PRE_MARKET"),
        ("US", "regular", "REGULAR"),
        ("US", "post", "AFTER_MARKET"),
        ("US", "closed", "CLOSED"),
        ("US", None, None),
        ("KRX", "nxt_premarket", "PRE_MARKET"),
        ("KRX", "regular", "REGULAR"),
        ("KRX", "nxt_after", "AFTER_MARKET"),
        ("KRX", "closed", "CLOSED"),
        ("KRX", None, None),
    ],
)
async def test_public_session_vocabulary_maps_each_provider_window_once(
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    provider_session: str | None,
    expected: str | None,
) -> None:
    target = (
        "get_us_toss_session_from_toss"
        if market == "US"
        else "get_kr_toss_session_from_toss"
    )
    monkeypatch.setattr(
        krx_quotes,
        target,
        AsyncMock(return_value=provider_session),
    )

    assert (
        await krx_quotes.resolve_market_session_state(
            market,
            moment=datetime(2026, 8, 28, 1, tzinfo=UTC),
        )
        == expected
    )


def test_regular_close_rejects_a_candle_at_the_after_market_boundary() -> None:
    window = _window(
        datetime.fromisoformat("2026-08-27T22:30:00+09:00"),
        datetime.fromisoformat("2026-08-28T05:00:00+09:00"),
    )
    page = SimpleNamespace(
        candles=[
            SimpleNamespace(
                timestamp="2026-08-28T05:00:00+09:00",
                close_price=Decimal("71.6995"),
            )
        ]
    )

    assert _regular_close(page, window=window) is None


@pytest.mark.asyncio
async def test_regular_close_fetches_one_candle_per_symbol_then_caches(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    class Client(_StubTossClient):
        def __init__(self) -> None:
            super().__init__()
            self.candle_calls: list[tuple[str, str, int, str, bool]] = []

        async def candles(
            self,
            symbol: str,
            *,
            interval: str,
            count: int,
            before: str,
            adjusted: bool,
        ) -> object:
            self.candle_calls.append((symbol, interval, count, before, adjusted))
            return SimpleNamespace(
                candles=[
                    SimpleNamespace(
                        timestamp="2026-08-28T04:59:00+09:00",
                        close_price=Decimal("71.89"),
                    )
                ]
            )

    client = Client()
    service = TossSharedMarketData(client_factory=lambda: client)
    window = _window(
        datetime.fromisoformat("2026-08-27T22:30:00+09:00"),
        datetime.fromisoformat("2026-08-28T05:00:00+09:00"),
    )
    symbols = [f"T{index:02d}" for index in range(20)]

    first = await service.regular_closes(symbols, window=window)
    second = await service.regular_closes(symbols, window=window)

    assert first == second == {symbol: Decimal("71.89") for symbol in symbols}
    assert len(client.candle_calls) == 20
    assert all(call[1:3] == ("1m", 1) for call in client.candle_calls)
    assert all(
        call[3] == "2026-08-28T04:59:59.999999+09:00" for call in client.candle_calls
    )


@pytest.mark.asyncio
async def test_toss_channel_skips_rows_without_trustworthy_timestamp(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    toss = _StubTossClient(
        {
            "005930": _toss_price("005930", price="256500"),
            "000660": _toss_price("000660", price="180000", timestamp=None),
            # 오프셋 없는 시각은 서버 시각으로 대체하지 않고 버린다.
            "035420": _toss_price(
                "035420", price="200000", timestamp="2026-08-28T18:44:26"
            ),
            "000270": _toss_price("000270", price="0"),
        }
    )
    service = TossSharedMarketData(client_factory=lambda: toss)

    points = await service.prices(["005930", "000660", "035420", "000270"])

    assert set(points) == {"005930"}
    assert points["005930"].as_of == datetime(2026, 8, 28, 9, 44, 26, tzinfo=UTC)


@pytest.mark.asyncio
async def test_toss_channel_stays_idle_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "toss_api_enabled", False, raising=False)
    toss = _StubTossClient({"005930": _toss_price("005930", price="256500")})
    service = TossSharedMarketData(client_factory=lambda: toss)

    assert await service.prices(["005930"]) == {}
    assert toss.calls == []


@pytest.mark.asyncio
async def test_toss_channel_reuses_one_client_and_closes_it_once(
    monkeypatch: pytest.MonkeyPatch, toss_enabled: None
) -> None:
    built: list[_StubTossClient] = []

    def factory() -> _StubTossClient:
        client = _StubTossClient({"005930": _toss_price("005930", price="256500")})
        built.append(client)
        return client

    service = TossSharedMarketData(client_factory=factory)

    await service.prices(["005930"])
    await asyncio.sleep(0.01)
    service._cache.clear()  # 캐시 만료를 모사해 두 번째 호출을 강제한다.
    await service.prices(["005930"])

    assert len(built) == 1
    assert built[0].calls == [["005930"], ["005930"]]

    await service.aclose()
    assert built[0].closed is True
