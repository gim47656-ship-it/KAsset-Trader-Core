from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.extensions.kasset.api.router as router_module
from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.toss_market_data import (
    TossDailyBar,
    TossSharedMarketData,
)
from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage
from app.services.brokers.toss.market_calendar import TossSessionWindow
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)


def _expected_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@asynccontextmanager
async def _isolated_candle_client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[object]:
        yield object()

    async def session_override() -> object:
        return SimpleNamespace(user=SimpleNamespace(id=101, role="trader"))

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://kasset.test",
    ) as client:
        yield client


def _bar(timestamp: datetime, close: str) -> TossDailyBar:
    price = Decimal(close)
    return TossDailyBar(
        time_utc=timestamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("10"),
    )


def _regular_window() -> TossSessionWindow:
    return TossSessionWindow(
        start=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 6, 30, tzinfo=UTC),
    )


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(
        self,
        *,
        force_reissue: bool = False,
        failed_token: str | None = None,
    ) -> str:
        del force_reissue, failed_token
        return "token"


@pytest_asyncio.fixture
async def candle_client(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    suffix = uuid4().hex[:10].upper()
    symbols = {
        "recent": f"KR{suffix}",
        "clamped": f"KC{suffix}",
        "nxt": f"KN{suffix}",
    }
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_start = today - timedelta(days=3)
    clamped_start = today - timedelta(days=125)
    nxt_time = today - timedelta(days=1)
    seed = {
        **symbols,
        "recent_0_time": _expected_time(recent_start),
        "recent_1_time": _expected_time(recent_start + timedelta(days=1)),
        "recent_2_time": _expected_time(recent_start + timedelta(days=2)),
        "clamped_first_time": _expected_time(clamped_start + timedelta(days=5)),
        "clamped_last_time": _expected_time(clamped_start + timedelta(days=124)),
        "nxt_time": _expected_time(nxt_time),
    }
    rows = [
        DailyCandleRow(
            time_utc=recent_start + timedelta(days=index),
            symbol=symbols["recent"],
            partition="KRX",
            open=100.25 + index,
            high=101.5 + index,
            low=99.75 + index,
            close=101.0 + index,
            adj_close=None,
            volume=1000.0 + index,
            value=0.0,
            source="test",
        )
        for index in range(3)
    ]
    rows.extend(
        DailyCandleRow(
            time_utc=clamped_start + timedelta(days=index),
            symbol=symbols["clamped"],
            partition="KRX",
            open=float(index),
            high=float(index + 2),
            low=float(index),
            close=float(index + 1),
            adj_close=None,
            volume=float(index + 100),
            value=0.0,
            source="test",
        )
        for index in range(125)
    )
    rows.append(
        DailyCandleRow(
            time_utc=nxt_time,
            symbol=symbols["nxt"],
            partition="NTX",
            open=10.0,
            high=12.0,
            low=9.0,
            close=11.0,
            adj_close=None,
            volume=500.0,
            value=0.0,
            source="test",
        )
    )
    repository = DailyCandlesRepository(session=db_session)
    await repository.upsert_rows(market=MarketKey.KR, rows=rows)
    await db_session.commit()

    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def session_override() -> object:
        return SimpleNamespace(user=SimpleNamespace(id=101, role="trader"))

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://kasset.test",
        ) as client:
            yield client, seed
    finally:
        await db_session.rollback()
        await db_session.execute(
            text(
                "DELETE FROM public.kr_candles_1d "
                "WHERE symbol IN (:recent, :clamped, :nxt)"
            ),
            {
                "recent": symbols["recent"],
                "clamped": symbols["clamped"],
                "nxt": symbols["nxt"],
            },
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_candles_return_seeded_rows_in_ascending_string_contract(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={"market": "KRX", "symbol": symbols["recent"], "range": "1M"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "interval": "1d",
        "candles": [
            {
                "time": symbols["recent_0_time"],
                "open": "100.25",
                "high": "101.5",
                "low": "99.75",
                "close": "101.0",
                "volume": "1000.0",
            },
            {
                "time": symbols["recent_1_time"],
                "open": "101.25",
                "high": "102.5",
                "low": "100.75",
                "close": "102.0",
                "volume": "1001.0",
            },
            {
                "time": symbols["recent_2_time"],
                "open": "102.25",
                "high": "103.5",
                "low": "101.75",
                "close": "103.0",
                "volume": "1002.0",
            },
        ],
    }


@pytest.mark.asyncio
async def test_candles_map_six_month_range_to_120_rows(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={"market": "KRX", "symbol": symbols["clamped"], "range": "6M"},
    )

    assert response.status_code == 200
    candles = response.json()["candles"]
    assert len(candles) == 120
    assert candles[0]["time"] == symbols["clamped_first_time"]
    assert candles[-1]["time"] == symbols["clamped_last_time"]


@pytest.mark.asyncio
async def test_candles_return_empty_array_when_no_data_exists(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, _symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={
            "market": "KRX",
            "symbol": f"NONE{uuid4().hex[:10]}",
            "range": "1M",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"interval": "1d", "candles": []}


@pytest.mark.asyncio
async def test_candles_accept_nxt_display_spelling_for_ntx_partition(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={"market": "NXT", "symbol": symbols["nxt"], "range": "1W"},
    )

    assert response.status_code == 200
    assert response.json()["candles"] == [
        {
            "time": symbols["nxt_time"],
            "open": "10.0",
            "high": "12.0",
            "low": "9.0",
            "close": "11.0",
            "volume": "500.0",
        }
    ]


@pytest.mark.asyncio
async def test_candles_require_authentication(db_session: AsyncSession) -> None:
    app = FastAPI()
    install_android_compat_api(app)

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = db_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://kasset.test",
    ) as client:
        response = await client.get(
            "/api/v1/market/candles?market=KRX&symbol=005930&range=1M"
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "인증 토큰이 필요합니다.",
        }
    }


@pytest.mark.asyncio
async def test_toss_client_uses_candle_query_path() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            path=request.url.path,
            params=dict(request.url.params),
        )
        return httpx.Response(
            200,
            json={"result": {"candles": [], "nextBefore": None}},
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        page = await client.candles(
            "005930",
            interval="1m",
            count=200,
            adjusted=True,
        )
    finally:
        await client.aclose()

    assert page.candles == []
    assert seen == {
        "method": "GET",
        "path": "/api/v1/candles",
        "params": {
            "symbol": "005930",
            "interval": "1m",
            "count": "200",
            "adjusted": "true",
        },
    }


@pytest.mark.asyncio
async def test_intraday_service_pages_full_regular_session_with_minimum_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def candle(index: int) -> TossCandle:
        timestamp = datetime(2026, 8, 29, tzinfo=UTC) + timedelta(minutes=index)
        price = Decimal(index + 1)
        return TossCandle(
            timestamp=timestamp.isoformat(),
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=Decimal("1"),
            currency="KRW",
        )

    class FakeClient:
        async def candles(
            self,
            symbol: str,
            *,
            interval: str,
            count: int,
            before: str | None = None,
            adjusted: bool | None = None,
        ) -> TossCandlesPage:
            calls.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "count": count,
                    "before": before,
                    "adjusted": adjusted,
                }
            )
            if before is None:
                return TossCandlesPage(
                    candles=[candle(index) for index in range(190, 390)],
                    next_before="older-page",
                )
            return TossCandlesPage(
                candles=[candle(index) for index in range(190)],
                next_before=None,
            )

    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    service = TossSharedMarketData(client_factory=FakeClient)

    bars = await service.intraday_bars("005930", count=390)

    assert calls == [
        {
            "symbol": "005930",
            "interval": "1m",
            "count": 200,
            "before": None,
            "adjusted": True,
        },
        {
            "symbol": "005930",
            "interval": "1m",
            "count": 190,
            "before": "older-page",
            "adjusted": True,
        },
    ]
    assert len(bars) == 390
    assert bars[0].close == Decimal("1")
    assert bars[-1].close == Decimal("390")


@pytest.mark.asyncio
async def test_intraday_service_returns_available_partial_page_in_time_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def candles(
            self,
            _symbol: str,
            *,
            interval: str,
            count: int,
            before: str | None = None,
            adjusted: bool | None = None,
        ) -> TossCandlesPage:
            assert (interval, count, before, adjusted) == ("1m", 200, None, True)
            return TossCandlesPage(
                candles=[
                    TossCandle(
                        timestamp="2026-08-29T09:01:00+09:00",
                        open_price=Decimal("2"),
                        high_price=Decimal("2"),
                        low_price=Decimal("2"),
                        close_price=Decimal("2"),
                        volume=Decimal("1"),
                        currency="KRW",
                    ),
                    TossCandle(
                        timestamp="2026-08-29T09:00:00+09:00",
                        open_price=Decimal("1"),
                        high_price=Decimal("1"),
                        low_price=Decimal("1"),
                        close_price=Decimal("1"),
                        volume=Decimal("1"),
                        currency="KRW",
                    ),
                ],
                next_before=None,
            )

    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    service = TossSharedMarketData(client_factory=FakeClient)

    bars = await service.intraday_bars("005930", count=390)

    assert [bar.close for bar in bars] == [Decimal("1"), Decimal("2")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("range_", "expected_count"),
    [("1W", 5), ("1M", 20), ("3M", 60), ("6M", 120)],
)
async def test_daily_ranges_keep_existing_counts_and_emit_daily_interval(
    monkeypatch: pytest.MonkeyPatch,
    range_: str,
    expected_count: int,
) -> None:
    counts: list[int] = []

    class EmptyRepository:
        def __init__(self, *, session: object) -> None:
            del session

        async def fetch_recent(self, **kwargs: object) -> list[object]:
            counts.append(int(kwargs["count"]))
            return []

    daily_bars = AsyncMock(return_value=[])
    provider = SimpleNamespace(
        intraday_bars=AsyncMock(side_effect=AssertionError("unexpected intraday")),
        daily_bars=daily_bars,
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(router_module, "DailyCandlesRepository", EmptyRepository)
    monkeypatch.setattr(router_module, "toss_market_data", provider)

    async with _isolated_candle_client() as client:
        response = await client.get(
            "/api/v1/market/candles",
            params={"market": "KRX", "symbol": "005930", "range": range_},
        )

    assert response.status_code == 200
    assert response.json() == {"interval": "1d", "candles": []}
    assert counts == [expected_count]
    daily_bars.assert_awaited_once_with("005930", count=expected_count)


@pytest.mark.asyncio
async def test_empty_intraday_does_not_fall_back_to_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_bars = AsyncMock(side_effect=AssertionError("daily fallback is forbidden"))
    provider = SimpleNamespace(
        intraday_bars=AsyncMock(return_value=[]),
        daily_bars=daily_bars,
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(router_module, "toss_market_data", provider)
    monkeypatch.setattr(
        router_module,
        "_regular_market_window",
        AsyncMock(return_value=_regular_window()),
    )

    async with _isolated_candle_client() as client:
        response = await client.get(
            "/api/v1/market/candles",
            params={"market": "KRX", "symbol": "005930", "range": "1D"},
        )

    assert response.status_code == 200
    assert response.json() == {"interval": "1m", "candles": []}
    provider.intraday_bars.assert_awaited_once_with("005930", count=390)
    daily_bars.assert_not_awaited()


@pytest.mark.asyncio
async def test_intraday_response_keeps_only_current_regular_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(
        intraday_bars=AsyncMock(
            return_value=[
                _bar(datetime(2026, 8, 28, 1, 0, tzinfo=UTC), "100"),
                _bar(datetime(2026, 8, 29, 1, 0, tzinfo=UTC), "200"),
                _bar(datetime(2026, 8, 29, 11, 0, tzinfo=UTC), "300"),
            ]
        ),
        daily_bars=AsyncMock(side_effect=AssertionError("daily fallback is forbidden")),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(router_module, "toss_market_data", provider)
    monkeypatch.setattr(
        router_module,
        "_current_market_trading_date",
        lambda _market: date(2026, 8, 29),
    )
    monkeypatch.setattr(
        router_module,
        "_regular_market_window",
        AsyncMock(return_value=_regular_window()),
    )

    async with _isolated_candle_client() as client:
        response = await client.get(
            "/api/v1/market/candles",
            params={"market": "KRX", "symbol": "005930", "range": "1D"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "interval": "1m",
        "candles": [
            {
                "time": "2026-08-29T01:00:00Z",
                "open": "200",
                "high": "200",
                "low": "200",
                "close": "200",
                "volume": "10",
            }
        ],
    }


def test_intraday_budget_grows_after_session_close_to_survive_after_hours_bars() -> (
    None
):
    """장 종료 후 시간외 봉이 세션 앞부분을 밀어내지 않도록 예산이 늘어난다.

    Toss는 최신 봉부터 주고 pager는 최신 `count` 개만 남긴다. 정규장 분수만 요청하면
    종료 후 생긴 시간외 봉이 그만큼 세션의 시작을 잘라 낸다(실측: 390분 중 앞 150분 누락).
    """
    start = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
    end = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    window = TossSessionWindow(start=start, end=end)

    # 장중: 경과분이 없으므로 정규장 분수 그대로 — 흔한 경로의 호출 수가 늘지 않는다.
    assert router_module._intraday_candle_budget(window, start) == 390
    assert (
        router_module._intraday_candle_budget(window, end - timedelta(minutes=1)) == 390
    )

    # 종료 후: 경과분만큼 정확히 늘어난다.
    assert (
        router_module._intraday_candle_budget(window, end + timedelta(minutes=150))
        == 540
    )

    # 며칠이 지나도 시간외 봉 분량 이상으로는 늘지 않는다. 휴장일에는 봉이 생기지 않으므로
    # 경과 시간을 그대로 더하면 페이지만 늘고 얻는 것이 없다.
    assert (
        router_module._intraday_candle_budget(window, end + timedelta(days=7))
        == 390 + router_module._TOSS_INTRADAY_POST_SESSION_ALLOWANCE
    )


@pytest.mark.asyncio
async def test_intraday_service_pages_beyond_two_pages_when_budget_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """페이지 수는 요청량에서 유도한다. 2페이지로 묶으면 늘린 예산이 다시 잘린다."""
    requested: list[int] = []

    def candle(index: int) -> TossCandle:
        timestamp = datetime(2026, 8, 28, tzinfo=UTC) + timedelta(minutes=index)
        price = Decimal(index + 1)
        return TossCandle(
            timestamp=timestamp.isoformat(),
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=Decimal("1"),
            currency="USD",
        )

    class FakeClient:
        def __init__(self) -> None:
            self._served = 0

        async def candles(
            self,
            symbol: str,
            *,
            interval: str,
            count: int,
            before: str | None = None,
            adjusted: bool | None = None,
        ) -> TossCandlesPage:
            requested.append(count)
            newest = 540 - self._served
            self._served += count
            oldest = newest - count
            return TossCandlesPage(
                candles=[candle(index) for index in range(oldest, newest)],
                next_before=None if oldest <= 0 else f"before-{oldest}",
            )

    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    service = TossSharedMarketData(client_factory=FakeClient)

    bars = await service.intraday_bars("TQQQ", count=540)

    # 200+200+140 — 세 페이지를 돌아야 540봉이 채워진다.
    assert requested == [200, 200, 140]
    assert len(bars) == 540
    assert bars[0].close == Decimal("1")
    assert bars[-1].close == Decimal("540")


@pytest.mark.asyncio
async def test_recent_session_walks_back_to_the_last_trading_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """휴장일에는 최근 거래일로 되짚는다.

    실측(2026-08-29 토요일): KRX 는 `boundary=08-29` 로 창이 없어 빈 배열을 냈고 US 는
    시차 때문에 금요일 창이 잡혀 분봉이 나왔다. 같은 `1D` 가 시장에 따라 다르게 동작했다.
    """
    friday = date(2026, 8, 28)
    saturday = date(2026, 8, 29)
    window = TossSessionWindow(
        start=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 28, 15, 30, tzinfo=UTC),
    )
    asked: list[date] = []

    async def fake_window(market: str, boundary: date) -> TossSessionWindow | None:
        asked.append(boundary)
        return window if boundary == friday else None

    monkeypatch.setattr(router_module, "_regular_market_window", fake_window)

    resolved = await router_module._recent_session("kr", saturday)

    assert resolved == (friday, window)
    # 토요일부터 하루씩만 되짚는다. 건너뛰거나 미래를 보지 않는다.
    assert asked == [saturday, friday]


@pytest.mark.asyncio
async def test_recent_session_returns_none_when_no_trading_day_is_within_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되짚기는 무한하지 않다. 상한까지 못 찾으면 빈 응답으로 떨어진다."""
    asked: list[date] = []

    async def fake_window(market: str, boundary: date) -> TossSessionWindow | None:
        asked.append(boundary)
        return None

    monkeypatch.setattr(router_module, "_regular_market_window", fake_window)

    resolved = await router_module._recent_session("us", date(2026, 8, 29))

    assert resolved is None
    assert len(asked) == router_module._MAX_TRADING_DATE_LOOKBACK_DAYS
