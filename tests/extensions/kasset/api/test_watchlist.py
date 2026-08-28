from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.installation import install_android_compat_api
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)


@pytest_asyncio.fixture
async def watchlist_data(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    suffix = uuid4().hex[:10].upper()
    users = [
        User(
            username=f"watch-a-{suffix.lower()}",
            email=f"watch-a-{suffix.lower()}@example.com",
            role=UserRole.trader,
            is_active=True,
        ),
        User(
            username=f"watch-b-{suffix.lower()}",
            email=f"watch-b-{suffix.lower()}@example.com",
            role=UserRole.trader,
            is_active=True,
        ),
    ]
    exchanges = [
        Exchange(code=f"WK{suffix}", name="Watch KRX", tz="Asia/Seoul"),
        Exchange(code=f"WU{suffix}", name="Watch US", country="US", tz="America/New_York"),
        Exchange(code=f"WC{suffix}", name="Watch Crypto", tz="UTC"),
    ]
    db_session.add_all([*users, *exchanges])
    await db_session.flush()

    primary = Instrument(
        exchange_id=exchanges[0].id,
        symbol=f"K{suffix}",
        name="삼성전자 테스트",
        type=InstrumentType.equity_kr,
        base_currency="KRW",
        is_active=True,
    )
    search_instruments = [
        Instrument(
            exchange_id=exchanges[0].id,
            symbol=f"S{index:02d}{suffix}",
            name=f"검색대상 {index:02d}",
            type=InstrumentType.equity_kr,
            base_currency="KRW",
            is_active=True,
        )
        for index in range(22)
    ]
    inactive = Instrument(
        exchange_id=exchanges[0].id,
        symbol=f"ZINACTIVE{suffix}",
        name="검색대상 비활성",
        type=InstrumentType.equity_kr,
        base_currency="KRW",
        is_active=False,
    )
    us_instrument = Instrument(
        exchange_id=exchanges[1].id,
        symbol=f"US{suffix}",
        name="검색대상 미국",
        type=InstrumentType.equity_us,
        base_currency="USD",
        is_active=True,
    )
    crypto_instrument = Instrument(
        exchange_id=exchanges[2].id,
        symbol=f"KRW-{suffix}",
        name="검색대상 코인",
        type=InstrumentType.crypto,
        base_currency="KRW",
        is_active=True,
    )
    instruments = [
        primary,
        *search_instruments,
        inactive,
        us_instrument,
        crypto_instrument,
    ]
    db_session.add_all(instruments)
    await db_session.commit()

    user_ids = [user.id for user in users]
    instrument_ids = [instrument.id for instrument in instruments]
    exchange_ids = [exchange.id for exchange in exchanges]
    try:
        yield {
            "users": users,
            "primary": primary,
            "search_instruments": search_instruments,
            "inactive": inactive,
            "us_instrument": us_instrument,
            "crypto_instrument": crypto_instrument,
        }
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(UserWatchItem).where(
                UserWatchItem.user_id.in_(user_ids),
                UserWatchItem.instrument_id.in_(instrument_ids),
            )
        )
        await db_session.execute(delete(Instrument).where(Instrument.id.in_(instrument_ids)))
        await db_session.execute(delete(Exchange).where(Exchange.id.in_(exchange_ids)))
        await db_session.execute(delete(User).where(User.id.in_(user_ids)))
        await db_session.commit()


@pytest_asyncio.fixture
async def watchlist_client(
    db_session: AsyncSession,
    watchlist_data: dict[str, object],
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, object]]]:
    app = FastAPI()
    install_android_compat_api(app)
    state: dict[str, object] = {"user": watchlist_data["users"][0]}

    async def db_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def session_override() -> object:
        return SimpleNamespace(user=state["user"])

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_mobile_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://kasset.test",
    ) as client:
        yield client, state


@pytest.mark.asyncio
async def test_watchlist_crud_is_idempotent_and_reactivates_soft_deleted_item(
    db_session: AsyncSession,
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    primary = watchlist_data["primary"]
    payload = {"symbol": primary.symbol.lower(), "market": "KRX"}
    expected = {
        "symbol": primary.symbol,
        "name": primary.name,
        "market": "KRX",
        "instrumentId": primary.id,
    }

    assert (await client.get("/api/v1/watchlist")).json() == {"items": []}

    created = await client.post("/api/v1/watchlist", json=payload)
    assert created.status_code == 201
    assert created.json() == expected

    watch_item = await db_session.scalar(
        select(UserWatchItem).where(UserWatchItem.instrument_id == primary.id)
    )
    assert watch_item is not None
    watch_item_id = watch_item.id

    duplicate = await client.post("/api/v1/watchlist", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json() == expected
    assert await db_session.scalar(
        select(func.count())
        .select_from(UserWatchItem)
        .where(UserWatchItem.instrument_id == primary.id)
    ) == 1

    removed = await client.delete(
        f"/api/v1/watchlist/{primary.symbol.lower()}?market=KRX"
    )
    assert removed.status_code == 204
    assert removed.content == b""
    assert (await client.get("/api/v1/watchlist")).json() == {"items": []}

    reactivated = await client.post("/api/v1/watchlist", json=payload)
    assert reactivated.status_code == 201
    assert reactivated.json() == expected
    current = await db_session.scalar(
        select(UserWatchItem).where(UserWatchItem.instrument_id == primary.id)
    )
    assert current is not None
    assert current.id == watch_item_id
    assert current.is_active is True

    unknown = await client.post(
        "/api/v1/watchlist",
        json={"symbol": f"UNKNOWN-{uuid4().hex}", "market": "KRX"},
    )
    assert unknown.status_code == 404
    assert unknown.json() == {
        "error": {
            "code": "UNKNOWN_SYMBOL",
            "message": "등록되지 않았거나 비활성화된 종목입니다.",
        }
    }


@pytest.mark.asyncio
async def test_watchlist_is_isolated_by_authenticated_owner(
    db_session: AsyncSession,
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, state = watchlist_client
    users = watchlist_data["users"]
    primary = watchlist_data["primary"]
    payload = {"symbol": primary.symbol, "market": "KRX"}

    owner_a_created = await client.post("/api/v1/watchlist", json=payload)
    assert owner_a_created.status_code == 201

    state["user"] = users[1]
    assert (await client.get("/api/v1/watchlist")).json() == {"items": []}
    owner_b_created = await client.post("/api/v1/watchlist", json=payload)
    assert owner_b_created.status_code == 201

    state["user"] = users[0]
    owner_a_items = (await client.get("/api/v1/watchlist")).json()["items"]
    assert owner_a_items == [owner_a_created.json()]
    assert await db_session.scalar(
        select(func.count())
        .select_from(UserWatchItem)
        .where(UserWatchItem.instrument_id == primary.id)
    ) == 2


@pytest.mark.asyncio
async def test_instrument_search_matches_name_and_symbol_with_market_limit_and_active_filter(
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    inactive = watchlist_data["inactive"]
    search_instruments = watchlist_data["search_instruments"]

    kr = await client.get("/api/v1/instruments/search?q=검색대상&market=KRX")
    assert kr.status_code == 200
    kr_items = kr.json()["items"]
    assert len(kr_items) == 20
    assert all(item["market"] == "KRX" for item in kr_items)
    assert inactive.symbol not in {item["symbol"] for item in kr_items}

    symbol_match = await client.get(
        f"/api/v1/instruments/search?q={search_instruments[21].symbol}&market=KRX"
    )
    assert symbol_match.status_code == 200
    assert symbol_match.json() == {
        "items": [
            {
                "symbol": search_instruments[21].symbol,
                "name": search_instruments[21].name,
                "market": "KRX",
            }
        ]
    }

    us = await client.get("/api/v1/instruments/search?q=검색대상&market=US")
    crypto = await client.get("/api/v1/instruments/search?q=검색대상&market=CRYPTO")
    assert us.json() == {
        "items": [
            {
                "symbol": watchlist_data["us_instrument"].symbol,
                "name": "검색대상 미국",
                "market": "US",
            }
        ]
    }
    assert crypto.json() == {
        "items": [
            {
                "symbol": watchlist_data["crypto_instrument"].symbol,
                "name": "검색대상 코인",
                "market": "CRYPTO",
            }
        ]
    }
