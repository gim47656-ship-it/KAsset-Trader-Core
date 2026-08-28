"""Android TraderApi-compatible routes."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.web_router import limiter
from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api import market_overview as market_overview_service
from app.extensions.kasset.api.ai_briefing import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    build_mobile_ai_briefing,
)
from app.extensions.kasset.api.ai_schemas import AiBriefingResponse
from app.extensions.kasset.api.auth import (
    MobileSession,
    get_mobile_session,
    mobile_auth,
)
from app.extensions.kasset.api.broker_registry import broker_registry
from app.extensions.kasset.api.credential_vault import credential_vault
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.nh_adapter import nh_adapter
from app.extensions.kasset.api.orderbook_store import (
    NHOrderbookSnapshotStore,
    get_orderbook_store,
    nh_orderbook_store,
    normalize_orderbook_key,
)
from app.extensions.kasset.api.paper import decimal_text, iso_z, paper_account_adapter
from app.extensions.kasset.api.paper_orders import paper_orders
from app.extensions.kasset.api.paper_schemas import (
    AiStatus,
    AmendRequest,
    Balance,
    FillsResponse,
    KillSwitchRequest,
    OrderDetail,
    OrderEnvelope,
    OrderRequest,
    OrdersResponse,
    PositionsResponse,
    Quote,
    QuotesResponse,
    RiskAssessment,
    RiskPolicy,
    RiskPolicyUpdate,
    SymbolsResponse,
    TradingModeRequest,
)
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.api.schemas import (
    AiRelayStatus,
    Broker,
    BrokersResponse,
    BrokerVerifyResponse,
    CandleRange,
    CredentialRequest,
    CurrentUserResponse,
    DailyCandle,
    DailyCandlesResponse,
    DatabaseStatus,
    GoogleLoginRequest,
    HealthResponse,
    InstrumentSearchMarket,
    InstrumentSearchResponse,
    LoginRequest,
    MarketIndexDetailResponse,
    MarketIndexRange,
    MarketOverviewResponse,
    NicknameUpdateRequest,
    OrderbookResponse,
    RefreshRequest,
    RegisterRequest,
    SessionTokens,
    SystemBrokerStatus,
    SystemStatus,
    WatchlistCreateRequest,
    WatchlistItem,
    WatchlistMarket,
    WatchlistResponse,
)
from app.extensions.kasset.api.toss_market_data import toss_market_data
from app.extensions.kasset.api.watchlist import watchlist_service
from app.extensions.kasset.automation.market_pipeline import _market_route
from app.models.trading import UserRole
from app.services.brokers.toss.market_calendar import (
    TossKrMarketDay,
    TossSessionWindow,
    TossUsMarketDay,
    get_toss_market_day,
)
from app.services.daily_candles.repository import DailyCandlesRepository

public_router = APIRouter(tags=["kasset-android"])


@asynccontextmanager
async def _kasset_api_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The first market read in a fresh process pays a one-time ~6s provider
    # client warmup (measured on the deployed API), which the app sees as a
    # stalled home screen right after a deploy. Pay it in the background at
    # startup so the first real request hits the warm overview cache. The test
    # environment stays offline by contract, so it never warms.
    warmup: asyncio.Task[None] | None = None
    if settings.ENVIRONMENT != "test":
        warmup = asyncio.create_task(market_overview_service.warm_market_sources())
    try:
        yield
    finally:
        if warmup is not None:
            warmup.cancel()
            with suppress(asyncio.CancelledError):
                await warmup
        await nh_orderbook_store.close()
        # 토스 공용 시세 클라이언트는 프로세스에서 재사용하므로 여기서만 닫는다.
        await toss_market_data.aclose()


router = APIRouter(
    prefix="/api/v1",
    tags=["kasset-android"],
    lifespan=_kasset_api_lifespan,
)

_CANDLE_RANGE_COUNTS: dict[CandleRange, int] = {
    "1W": 5,
    "1M": 20,
    "3M": 60,
    "6M": 120,
}
# 분봉 예산의 상한. 정규장 390분에 시간외 전체를 더해도 남는 여유다.
_TOSS_INTRADAY_MAX_CANDLE_COUNT = 1200


def _intraday_candle_budget(window: TossSessionWindow, moment: datetime) -> int:
    """정규장 분수 + 정규장 종료 후 경과 분.

    Toss는 최신 봉부터 거꾸로 주고 `fetch_toss_candles` 는 최신 `count` 개만 남긴다.
    장이 끝난 뒤에는 **시간외 봉이 최신 슬롯을 차지**하므로 정규장 분수만 요청하면 세션의
    앞부분이 잘려 나간다. 실측(2026-08-28 금요일 정규장 종료 후): 390분 중 앞 150분 누락으로
    시가 `72.95`→`72.355`, 고가 `74.18`→`72.56`, 거래량 `5359548`→`2840957`.

    종료 후 경과분만큼 예산을 늘려 그 침식을 정확히 상쇄한다. 장중에는 경과분이 0이라
    요청량이 그대로이므로 흔한 경로의 호출 수가 늘지 않는다.
    """
    regular_minutes = ceil((window.end - window.start).total_seconds() / 60)
    elapsed_after = 0
    if moment > window.end:
        elapsed_after = ceil((moment - window.end).total_seconds() / 60)
    return min(regular_minutes + elapsed_after, _TOSS_INTRADAY_MAX_CANDLE_COUNT)


def _current_market_trading_date(market: str) -> date:
    return krx_quotes._market_trading_date(market, datetime.now(UTC))


async def _regular_market_window(
    market: str, boundary: date
) -> TossSessionWindow | None:
    if market == "kr":
        source_market = "kr"
    elif market == "us":
        source_market = "us"
    else:
        return None
    market_day = await get_toss_market_day(source_market, boundary)
    if not isinstance(market_day, TossKrMarketDay | TossUsMarketDay):
        return None
    return market_day.regular_market


@public_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.post(
    "/auth/register",
    response_model=SessionTokens,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def register(
    request: Request,
    payload: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionTokens:
    return await mobile_auth.register(db, payload)


@router.post("/auth/login", response_model=SessionTokens)
@limiter.limit("5/minute")
async def login(
    request: Request,
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionTokens:
    return await mobile_auth.login(db, payload)


@router.post("/auth/google", response_model=SessionTokens)
@limiter.limit("5/minute")
async def google_login(
    request: Request,
    payload: GoogleLoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionTokens:
    return await mobile_auth.google_login(db, payload)


@router.get("/auth/me", response_model=CurrentUserResponse)
async def me(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUserResponse:
    return await mobile_auth.current_user(db, session)


@router.patch("/auth/me", response_model=CurrentUserResponse)
async def update_nickname(
    payload: NicknameUpdateRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUserResponse:
    return await mobile_auth.update_nickname(db, session, payload)


@router.post("/auth/refresh", response_model=SessionTokens)
async def refresh(
    request: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionTokens:
    return await mobile_auth.refresh(db, request.refresh_token)


@router.post("/auth/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    await mobile_auth.revoke(db, session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/brokers", response_model=BrokersResponse)
async def brokers(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrokersResponse:
    return BrokersResponse(
        brokers=await broker_registry.list_brokers(db, session.user.id)
    )


@router.post("/brokers/{provider}/credential", response_model=Broker)
@router.post("/brokers/{provider}/credentials", response_model=Broker)
async def register_broker_credential(
    provider: str,
    request: CredentialRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Broker:
    _require_trader(session)
    _require_nh(provider)
    credential = await credential_vault.store_nh(
        db,
        session.user.id,
        app_key=request.app_key,
        app_secret=request.app_secret,
        account_no=request.account_no,
    )
    await nh_adapter.invalidate_auth_cache(credential.id)
    return await broker_registry.get_broker(db, session.user.id, "NH")


@router.delete("/brokers/{provider}/credential", status_code=status.HTTP_204_NO_CONTENT)
@router.delete(
    "/brokers/{provider}/credentials", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_broker_credential(
    provider: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    _require_trader(session)
    _require_nh(provider)
    credential_id = await credential_vault.delete_nh(db, session.user.id)
    await nh_adapter.invalidate_auth_cache(credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/brokers/{provider}/verify", response_model=BrokerVerifyResponse)
async def verify_broker(
    provider: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrokerVerifyResponse:
    _require_trader(session)
    _require_nh(provider)
    checked_at = await nh_adapter.verify(db, session.user.id)
    return BrokerVerifyResponse(
        connected=True,
        checked_at=checked_at.isoformat().replace("+00:00", "Z"),
        message="NH PLUG 모의투자 계좌 연결을 확인했습니다.",
    )


@router.get("/system/status", response_model=SystemStatus)
async def system_status(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    return await _build_system_status(db, session.user.id)


async def _build_system_status(
    db: AsyncSession,
    owner_user_id: int,
) -> SystemStatus:
    registered = await broker_registry.list_brokers(db, owner_user_id)
    state = await runtime_state.get(db, owner_user_id)
    global_state = await runtime_state.get_global(db)
    return SystemStatus(
        server_version=settings.KASSET_SERVER_VERSION,
        server_time=(
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ),
        database=DatabaseStatus(status="ok"),
        trading_mode=state.trading_mode,
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        kill_switch_enabled=(
            global_state.kill_switch_enabled or state.kill_switch_enabled
        ),
        brokers=[
            SystemBrokerStatus(
                provider=broker.provider,
                connected=broker.connected,
                last_verified_at=broker.last_verified_at,
            )
            for broker in registered
        ],
        ai_relay=AiRelayStatus(
            configured=False,
            reachable=False,
            message="AI Relay는 이번 통합 단계에서 확장하지 않습니다.",
        ),
    )


@router.post("/system/kill-switch", response_model=SystemStatus)
async def set_kill_switch(
    request: KillSwitchRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    _require_trader(session)
    await runtime_state.set_user_kill_switch(
        db, session.user.id, enabled=request.enabled
    )
    return await _build_system_status(db, session.user.id)


@router.post("/admin/system/kill-switch", response_model=SystemStatus)
async def set_global_kill_switch(
    request: KillSwitchRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    _require_admin(session)
    await runtime_state.set_global_kill_switch(db, enabled=request.enabled)
    return await _build_system_status(db, session.user.id)


@router.post("/system/trading-mode", response_model=SystemStatus)
async def set_trading_mode(
    request: TradingModeRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    _require_trader(session)
    await runtime_state.set_trading_mode(db, session.user.id, mode=request.mode)
    return await _build_system_status(db, session.user.id)


@router.get("/watchlist", response_model=WatchlistResponse)
async def list_watchlist(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WatchlistResponse:
    return await watchlist_service.list_items(db, session.user.id)


@router.post(
    "/watchlist",
    response_model=WatchlistItem,
    status_code=status.HTTP_201_CREATED,
)
async def add_watchlist_item(
    request: WatchlistCreateRequest,
    response: Response,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WatchlistItem:
    _require_trader(session)
    item, created = await watchlist_service.add_item(
        db,
        session.user.id,
        symbol=request.symbol,
        market=request.market,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return item


@router.delete("/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist_item(
    symbol: str,
    market: WatchlistMarket,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    _require_trader(session)
    await watchlist_service.remove_item(
        db,
        session.user.id,
        symbol=symbol,
        market=market,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/instruments/search", response_model=InstrumentSearchResponse)
async def search_instruments(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[InstrumentSearchMarket, Query()] = "ALL",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InstrumentSearchResponse:
    return await watchlist_service.search_instruments(
        db,
        query=q,
        market=market,
        limit=limit,
    )


@router.get("/account/balance", response_model=Balance)
async def account_balance(
    broker: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Balance:
    if broker.strip().upper() == "NH":
        return await nh_adapter.balance(db, session.user.id)
    _require_paper(broker)
    return await paper_account_adapter.balance(db, session.user.id)


@router.get("/positions", response_model=PositionsResponse)
async def positions(
    broker: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PositionsResponse:
    if broker.strip().upper() == "NH":
        return await nh_adapter.positions(db, session.user.id)
    _require_paper(broker)
    return await paper_account_adapter.positions(db, session.user.id)


@router.get("/market/overview", response_model=MarketOverviewResponse)
async def market_overview(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
) -> MarketOverviewResponse:
    return await market_overview_service.get_market_overview()


@router.get(
    "/market/indices/{symbol}",
    response_model=MarketIndexDetailResponse,
)
async def market_index_detail(
    symbol: str,
    range_: Annotated[MarketIndexRange, Query(alias="range")],
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
) -> MarketIndexDetailResponse:
    return await market_overview_service.get_market_index_detail(symbol, range_)


@router.get("/market/quote", response_model=Quote)
async def market_quote(
    broker: str,
    market: str,
    symbol: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Quote:
    if broker.strip().upper() == "NH":
        return await nh_adapter.quote(db, session.user.id, market=market, symbol=symbol)
    _require_paper(broker)
    # 시세는 계좌 연동과 무관한 공용 데이터다. 토스 실시간을 우선 쓰고, 실패하면
    # KRX는 서버 공용 NH PLUG 채널 → 저장 캔들 기반 PAPER 시세로 강등한다. 미국은
    # NH 경로가 없어 곧바로 PAPER로 내려간다. 주문 기준가(`paper_orders`)도 같은
    # 진입점을 쓰므로 화면 가격과 체결 기준가가 갈라지지 않는다.
    return await krx_quotes.quote_for_market(db, market=market, symbol=symbol)


@router.get("/market/quotes", response_model=QuotesResponse)
async def market_quotes(
    market: Annotated[str, Query(min_length=1, max_length=20)],
    symbols: Annotated[str, Query(min_length=1, max_length=512)],
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QuotesResponse:
    normalized_market = krx_quotes.normalize_market(market)
    normalized_symbols = krx_quotes.normalize_symbols(symbols)
    return QuotesResponse(
        quotes=await krx_quotes.resolve_quotes(
            db,
            market=normalized_market,
            symbols=normalized_symbols,
        )
    )


@router.get("/market/orderbook", response_model=OrderbookResponse)
async def market_orderbook(
    market: str,
    symbol: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    store: Annotated[NHOrderbookSnapshotStore, Depends(get_orderbook_store)],
) -> OrderbookResponse:
    normalized_market, normalized_symbol = normalize_orderbook_key(
        market=market,
        symbol=symbol,
    )
    snapshot = await store.get_snapshot(
        market=normalized_market,
        symbol=normalized_symbol,
    )
    return OrderbookResponse.model_validate(snapshot)


@router.get("/market/candles", response_model=DailyCandlesResponse)
async def market_candles(
    market: Annotated[str, Query(min_length=1, max_length=20)],
    symbol: Annotated[str, Query(min_length=1, max_length=64)],
    range_: Annotated[CandleRange, Query(alias="range")],
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DailyCandlesResponse:
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise MobileApiError(422, "VALIDATION_ERROR", "종목 코드를 입력해 주세요.")
    try:
        candle_market, partition, _recommendation_market = _market_route(market)
    except ValueError as err:
        raise MobileApiError(
            422, "VALIDATION_ERROR", "지원하지 않는 시장입니다."
        ) from err

    if range_ == "1D":
        boundary = _current_market_trading_date(market)
        regular_market = await _regular_market_window(candle_market.value, boundary)
        if regular_market is None:
            return DailyCandlesResponse(interval="1m", candles=[])
        bars = await toss_market_data.intraday_bars(
            normalized_symbol,
            count=_intraday_candle_budget(regular_market, datetime.now(UTC)),
        )
        return DailyCandlesResponse(
            interval="1m",
            candles=[
                DailyCandle(
                    time=iso_z(bar.time_utc),
                    open=decimal_text(bar.open),
                    high=decimal_text(bar.high),
                    low=decimal_text(bar.low),
                    close=decimal_text(bar.close),
                    volume=decimal_text(bar.volume),
                )
                for bar in bars
                if krx_quotes._market_trading_date(market, bar.time_utc) == boundary
                and regular_market.contains(bar.time_utc)
            ],
        )

    limit = _CANDLE_RANGE_COUNTS[range_]
    rows = await DailyCandlesRepository(session=db).fetch_recent(
        market=candle_market,
        symbol=normalized_symbol,
        partition=partition,
        count=limit,
    )
    if rows:
        return DailyCandlesResponse(
            interval="1d",
            candles=[
                DailyCandle(
                    time=iso_z(row.time_utc),
                    open=decimal_text(str(row.open)),
                    high=decimal_text(str(row.high)),
                    low=decimal_text(str(row.low)),
                    close=decimal_text(str(row.close)),
                    volume=decimal_text(str(row.volume)),
                )
                for row in rows
            ],
        )
    # 저장 일봉 유니버스는 관심종목 전체를 담고 있지 않아, 새로 추가한 종목의
    # 차트가 계속 빈 배열이었다. 저장 값이 없을 때만 토스 일봉으로 채운다.
    bars = await toss_market_data.daily_bars(normalized_symbol, count=limit)
    return DailyCandlesResponse(
        interval="1d",
        candles=[
            DailyCandle(
                time=iso_z(bar.time_utc),
                open=decimal_text(bar.open),
                high=decimal_text(bar.high),
                low=decimal_text(bar.low),
                close=decimal_text(bar.close),
                volume=decimal_text(bar.volume),
            )
            for bar in bars
        ],
    )


@router.get("/market/symbols", response_model=SymbolsResponse)
async def market_symbols(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SymbolsResponse:
    _require_paper(broker)
    return await paper_account_adapter.symbols(db)


@router.post("/orders/preview", response_model=RiskAssessment)
async def preview_order(
    request: OrderRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskAssessment:
    _require_trader(session)
    _require_order_capable(request.broker)
    return await paper_orders.preview(db, session.user.id, request)


@router.post("/orders", response_model=OrderEnvelope)
async def submit_order(
    request: OrderRequest,
    response: Response,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderEnvelope:
    _require_trader(session)
    _require_order_capable(request.broker)
    envelope, replay = await paper_orders.submit(db, session.user.id, request)
    response.status_code = status.HTTP_200_OK if replay else status.HTTP_201_CREATED
    return envelope


@router.get("/orders", response_model=OrdersResponse)
async def list_orders(
    broker: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> OrdersResponse:
    if broker.strip().upper() == "NH":
        return OrdersResponse(orders=[])
    _require_paper(broker)
    statuses = (
        {item.strip().upper() for item in status_filter.split(",") if item.strip()}
        if status_filter
        else None
    )
    return await paper_orders.list_orders(
        db, session.user.id, statuses=statuses, limit=limit
    )


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def order_detail(
    order_id: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderDetail:
    return await paper_orders.detail(db, session.user.id, order_id)


@router.post("/orders/{order_id}/cancel", response_model=OrderEnvelope)
async def cancel_order(
    order_id: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[str | None, Query()] = None,
) -> OrderEnvelope:
    _require_trader(session)
    if broker is not None:
        _require_order_capable(broker)
    return await paper_orders.cancel(db, session.user.id, order_id)


@router.post("/orders/{order_id}/amend", response_model=OrderEnvelope)
async def amend_order(
    order_id: str,
    request: AmendRequest,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[str | None, Query()] = None,
) -> OrderEnvelope:
    _require_trader(session)
    if broker is not None:
        _require_order_capable(broker)
    return await paper_orders.amend(db, session.user.id, order_id, request)


@router.get("/fills", response_model=FillsResponse)
async def fills(
    broker: str,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> FillsResponse:
    if broker.strip().upper() == "NH":
        return FillsResponse(fills=[])
    _require_paper(broker)
    return await paper_orders.list_fills(db, session.user.id, limit=limit)


@router.get("/risk/policy", response_model=RiskPolicy)
async def risk_policy(
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskPolicy:
    state = await runtime_state.get(db, session.user.id)
    return RiskPolicy(
        max_order_ratio=format(state.max_order_ratio, "f"),
        max_symbol_ratio=format(state.max_symbol_ratio, "f"),
        allow_short_sell=False,
        updated_at=iso_z(state.updated_at),
    )


@router.put("/risk/policy", response_model=RiskPolicy)
async def update_risk_policy(
    request: RiskPolicyUpdate,
    session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskPolicy:
    _require_trader(session)
    await runtime_state.update_policy(
        db,
        session.user.id,
        max_order_ratio=request.max_order_ratio,
        max_symbol_ratio=request.max_symbol_ratio,
    )
    return await risk_policy(session, db)


@router.get("/ai/status", response_model=AiStatus)
async def ai_status(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
) -> AiStatus:
    return AiStatus(
        relay_configured=False,
        reachable=False,
        message="AI 기능은 이번 통합 단계에서 확장하지 않습니다.",
    )


@router.get("/ai/briefing", response_model=AiBriefingResponse)
async def ai_briefing(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: str,
    symbol: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=MIN_LIMIT, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AiBriefingResponse:
    return await build_mobile_ai_briefing(db, market=market, symbol=symbol, limit=limit)


def _require_trader(session: MobileSession) -> None:
    if session.user.role not in {UserRole.trader, UserRole.admin}:
        raise MobileApiError(403, "FORBIDDEN", "이 작업을 수행할 권한이 없습니다.")


def _require_admin(session: MobileSession) -> None:
    if session.user.role != UserRole.admin:
        raise MobileApiError(403, "FORBIDDEN", "관리자 권한이 필요합니다.")


def _require_paper(provider: str) -> None:
    if provider.strip().upper() != "PAPER":
        raise MobileApiError(
            409, "BROKER_NOT_CONNECTED", "선택한 브로커가 연결되지 않았습니다."
        )


def _require_order_capable(provider: str) -> None:
    if provider.strip().upper() == "NH":
        raise MobileApiError(
            409, "BROKER_READ_ONLY", "NH PLUG는 현재 모의 Read-Only 단계입니다."
        )
    _require_paper(provider)


def _require_nh(provider: str) -> None:
    if provider.strip().upper() != "NH":
        raise MobileApiError(
            501, "BROKER_NOT_IMPLEMENTED", "해당 브로커 연결은 아직 지원하지 않습니다."
        )
