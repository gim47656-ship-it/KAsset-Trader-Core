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
from app.models.symbol_master import SymbolMaster
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
    search_term = f"검색대상{suffix}"
    english_term = f"Samsung{suffix}"
    alias_term = f"젬스{suffix}"
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
        aliases=["삼성", "samsung", alias_term],
        type=InstrumentType.equity_kr,
        base_currency="KRW",
        is_active=True,
    )
    search_instruments = [
        Instrument(
            exchange_id=exchanges[0].id,
            symbol=f"S{index:02d}{suffix}",
            name=f"{search_term} {index:02d}",
            type=InstrumentType.equity_kr,
            base_currency="KRW",
            is_active=True,
        )
        for index in range(22)
    ]
    inactive = Instrument(
        exchange_id=exchanges[0].id,
        symbol=f"ZINACTIVE{suffix}",
        name=f"{search_term} 비활성",
        type=InstrumentType.equity_kr,
        base_currency="KRW",
        is_active=False,
    )
    us_instrument = Instrument(
        exchange_id=exchanges[1].id,
        symbol=f"US{suffix}",
        name=f"{search_term} 미국",
        type=InstrumentType.equity_us,
        base_currency="USD",
        is_active=True,
    )
    crypto_instrument = Instrument(
        exchange_id=exchanges[2].id,
        symbol=f"KRW-{suffix}",
        name=f"{search_term} 코인",
        type=InstrumentType.crypto,
        base_currency="KRW",
        is_active=True,
    )
    master_only = SymbolMaster(
        market="KRX",
        symbol=f"M{suffix}",
        name="마스터 전용 종목",
        name_en="Master Only Security",
        security_type="COMMON_STOCK",
        is_active=True,
    )
    symbol_master_rows = [
        SymbolMaster(
            market="KRX",
            symbol=primary.symbol,
            name=primary.name,
            name_en=f"{english_term} Test",
            security_type="COMMON_STOCK",
            is_active=True,
        ),
        *[
            SymbolMaster(
                market="KRX",
                symbol=instrument.symbol,
                name=instrument.name,
                name_en=f"Search Target {index:02d}",
                security_type="COMMON_STOCK",
                is_active=True,
            )
            for index, instrument in enumerate(search_instruments)
        ],
        SymbolMaster(
            market="KRX",
            symbol=inactive.symbol,
            name=inactive.name,
            security_type="COMMON_STOCK",
            is_active=False,
        ),
        SymbolMaster(
            market="US",
            symbol=us_instrument.symbol,
            name=us_instrument.name,
            name_en=f"{english_term} America",
            security_type="COMMON_STOCK",
            is_active=True,
        ),
        master_only,
    ]
    instruments = [
        primary,
        *search_instruments,
        inactive,
        us_instrument,
        crypto_instrument,
    ]
    db_session.add_all([*instruments, *symbol_master_rows])
    await db_session.commit()
    instrument_symbols = [instrument.symbol for instrument in instruments] + [
        master_only.symbol
    ]
    master_symbols = [row.symbol for row in symbol_master_rows]

    user_ids = [user.id for user in users]
    exchange_ids = [exchange.id for exchange in exchanges]
    try:
        yield {
            "users": users,
            "primary": primary,
            "search_instruments": search_instruments,
            "inactive": inactive,
            "us_instrument": us_instrument,
            "crypto_instrument": crypto_instrument,
            "master_only": master_only,
            "search_term": search_term,
            "english_term": english_term,
            "alias_term": alias_term,
        }
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(UserWatchItem).where(UserWatchItem.user_id.in_(user_ids))
        )
        await db_session.execute(
            delete(Instrument).where(
                Instrument.symbol.in_(instrument_symbols)
            )
        )
        await db_session.execute(
            delete(SymbolMaster).where(SymbolMaster.symbol.in_(master_symbols))
        )
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

    assert (await client.get("/api/v1/watchlist")).json() == {
        "items": [],
        "maxItems": 20,
    }

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
    assert (await client.get("/api/v1/watchlist")).json() == {
        "items": [],
        "maxItems": 20,
    }

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
async def test_watchlist_enforces_twenty_item_limit_but_keeps_duplicate_idempotent(
    db_session: AsyncSession,
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    owner_user_id = watchlist_data["users"][0].id
    instruments = watchlist_data["search_instruments"]

    for instrument in instruments[:20]:
        response = await client.post(
            "/api/v1/watchlist",
            json={"symbol": instrument.symbol, "market": "KRX"},
        )
        assert response.status_code == 201

    listed = await client.get("/api/v1/watchlist")
    assert listed.status_code == 200
    assert listed.json()["maxItems"] == 20
    assert len(listed.json()["items"]) == 20

    duplicate = await client.post(
        "/api/v1/watchlist",
        json={"symbol": instruments[0].symbol, "market": "KRX"},
    )
    assert duplicate.status_code == 200

    twenty_first_symbol = instruments[20].symbol
    over_limit = await client.post(
        "/api/v1/watchlist",
        json={"symbol": twenty_first_symbol, "market": "KRX"},
    )
    assert over_limit.status_code == 409
    assert over_limit.json() == {
        "error": {
            "code": "WATCHLIST_LIMIT_REACHED",
            "message": "관심종목은 최대 20개까지 등록할 수 있습니다.",
        }
    }
    assert await db_session.scalar(
        select(func.count())
        .select_from(UserWatchItem)
        .where(
            UserWatchItem.user_id == owner_user_id,
            UserWatchItem.is_active.is_(True),
        )
    ) == 20


@pytest.mark.asyncio
async def test_watchlist_add_materializes_master_only_instrument(
    db_session: AsyncSession,
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    master = watchlist_data["master_only"]

    response = await client.post(
        "/api/v1/watchlist",
        json={"symbol": master.symbol.lower(), "market": "KRX"},
    )

    assert response.status_code == 201
    instrument = await db_session.scalar(
        select(Instrument).where(
            Instrument.symbol == master.symbol,
            Instrument.type == InstrumentType.equity_kr,
        )
    )
    assert instrument is not None
    assert response.json() == {
        "symbol": master.symbol,
        "name": master.name,
        "market": "KRX",
        "instrumentId": instrument.id,
    }
    watch_item = await db_session.scalar(
        select(UserWatchItem).where(UserWatchItem.instrument_id == instrument.id)
    )
    assert watch_item is not None


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
    assert (await client.get("/api/v1/watchlist")).json() == {
        "items": [],
        "maxItems": 20,
    }
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
async def test_instrument_search_integrates_markets_and_supports_filters(
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    inactive = watchlist_data["inactive"]
    search_instruments = watchlist_data["search_instruments"]

    search_term = watchlist_data["search_term"]
    integrated = await client.get(
        f"/api/v1/instruments/search?q={search_term}&limit=100"
    )
    assert integrated.status_code == 200
    integrated_items = integrated.json()["items"]
    assert len(integrated_items) == 23
    assert [item["market"] for item in integrated_items] == ["KRX"] * 22 + ["US"]
    assert inactive.symbol not in {item["symbol"] for item in integrated_items}

    explicit_all = await client.get(
        f"/api/v1/instruments/search?q={search_term}&market=ALL&limit=100"
    )
    assert explicit_all.json() == integrated.json()

    kr = await client.get(
        f"/api/v1/instruments/search?q={search_term}&market=KRX"
    )
    assert kr.status_code == 200
    assert len(kr.json()["items"]) == 20
    assert all(item["market"] == "KRX" for item in kr.json()["items"])

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

    us = await client.get(
        f"/api/v1/instruments/search?q={search_term}&market=US"
    )
    assert us.json() == {
        "items": [
            {
                "symbol": watchlist_data["us_instrument"].symbol,
                "name": watchlist_data["us_instrument"].name,
                "market": "US",
            }
        ]
    }
    english = await client.get(
        f"/api/v1/instruments/search?q={watchlist_data['english_term']}"
    )
    assert [item["market"] for item in english.json()["items"]] == ["KRX", "US"]

    invalid_market = await client.get(
        f"/api/v1/instruments/search?q={search_term}&market=CRYPTO"
    )
    assert invalid_market.status_code == 422


@pytest.mark.asyncio
async def test_instrument_search_matches_alias(
    watchlist_client: tuple[httpx.AsyncClient, dict[str, object]],
    watchlist_data: dict[str, object],
) -> None:
    client, _state = watchlist_client
    primary = watchlist_data["primary"]

    response = await client.get(
        f"/api/v1/instruments/search?q={watchlist_data['alias_term']}&market=KRX"
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "symbol": primary.symbol,
                "name": primary.name,
                "market": "KRX",
            }
        ]
    }
