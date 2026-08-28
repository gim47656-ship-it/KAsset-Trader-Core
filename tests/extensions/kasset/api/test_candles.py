from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)


def _expected_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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
        params={"market": "KRX", "symbol": symbols["recent"]},
    )

    assert response.status_code == 200
    assert response.json() == {
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
        ]
    }


@pytest.mark.asyncio
async def test_candles_clamp_count_to_120(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={"market": "KRX", "symbol": symbols["clamped"], "count": 999},
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
        params={"market": "KRX", "symbol": f"NONE{uuid4().hex[:10]}"},
    )

    assert response.status_code == 200
    assert response.json() == {"candles": []}


@pytest.mark.asyncio
async def test_candles_accept_nxt_display_spelling_for_ntx_partition(
    candle_client: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    client, symbols = candle_client

    response = await client.get(
        "/api/v1/market/candles",
        params={"market": "NXT", "symbol": symbols["nxt"], "count": 1},
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
        response = await client.get("/api/v1/market/candles?market=KRX&symbol=005930")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "인증 토큰이 필요합니다.",
        }
    }
