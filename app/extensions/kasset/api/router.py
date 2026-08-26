"""Android TraderApi-compatible routes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.auth import (
    MobileSession,
    get_mobile_session,
    mobile_auth,
)
from app.extensions.kasset.api.broker_registry import broker_registry
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import paper_account_adapter
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
    RiskAssessment,
    RiskPolicy,
    RiskPolicyUpdate,
    SymbolsResponse,
    TradingModeRequest,
)
from app.extensions.kasset.api.runtime_state import runtime_state
from app.extensions.kasset.api.schemas import (
    AiRelayStatus,
    BrokersResponse,
    DatabaseStatus,
    HealthResponse,
    PairRequest,
    RefreshRequest,
    SessionTokens,
    SystemBrokerStatus,
    SystemStatus,
)

public_router = APIRouter(tags=["kasset-android"])
router = APIRouter(prefix="/api/v1", tags=["kasset-android"])


@public_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()



@router.post("/auth/pair", response_model=SessionTokens)
async def pair(
    request: PairRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionTokens:
    return await mobile_auth.pair(
        db,
        pairing_code=request.pairing_code,
        device_id=request.device_id,
        device_name=request.device_name,
    )


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
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
) -> BrokersResponse:
    return BrokersResponse(brokers=broker_registry.list_brokers())


@router.get("/system/status", response_model=SystemStatus)
async def system_status(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    return await _build_system_status(db)


async def _build_system_status(db: AsyncSession) -> SystemStatus:
    registered = broker_registry.list_brokers()
    state = await runtime_state.get(db)
    return SystemStatus(
        server_version=settings.KASSET_SERVER_VERSION,
        server_time=(
            datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        database=DatabaseStatus(status="ok"),
        trading_mode=state.trading_mode,
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        kill_switch_enabled=state.kill_switch_enabled,
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
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    await runtime_state.set_kill_switch(db, enabled=request.enabled)
    return await _build_system_status(db)


@router.post("/system/trading-mode", response_model=SystemStatus)
async def set_trading_mode(
    request: TradingModeRequest,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    await runtime_state.set_trading_mode(db, mode=request.mode)
    return await _build_system_status(db)


@router.get("/account/balance", response_model=Balance)
async def account_balance(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Balance:
    _require_paper(broker)
    return await paper_account_adapter.balance(db)


@router.get("/positions", response_model=PositionsResponse)
async def positions(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PositionsResponse:
    _require_paper(broker)
    return await paper_account_adapter.positions(db)


@router.get("/market/quote", response_model=Quote)
async def market_quote(
    broker: str,
    market: str,
    symbol: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Quote:
    _require_paper(broker)
    return await paper_account_adapter.quote(db, market=market, symbol=symbol)


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
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskAssessment:
    return await paper_orders.preview(db, request)


@router.post("/orders", response_model=OrderEnvelope)
async def submit_order(
    request: OrderRequest,
    response: Response,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderEnvelope:
    envelope, replay = await paper_orders.submit(db, request)
    response.status_code = status.HTTP_200_OK if replay else status.HTTP_201_CREATED
    return envelope


@router.get("/orders", response_model=OrdersResponse)
async def list_orders(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> OrdersResponse:
    _require_paper(broker)
    statuses = (
        {item.strip().upper() for item in status_filter.split(",") if item.strip()}
        if status_filter
        else None
    )
    return await paper_orders.list_orders(db, statuses=statuses, limit=limit)


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def order_detail(
    order_id: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderDetail:
    return await paper_orders.detail(db, order_id)


@router.post("/orders/{order_id}/cancel", response_model=OrderEnvelope)
async def cancel_order(
    order_id: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderEnvelope:
    return await paper_orders.cancel(db, order_id)


@router.post("/orders/{order_id}/amend", response_model=OrderEnvelope)
async def amend_order(
    order_id: str,
    request: AmendRequest,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderEnvelope:
    return await paper_orders.amend(db, order_id, request)


@router.get("/fills", response_model=FillsResponse)
async def fills(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> FillsResponse:
    _require_paper(broker)
    return await paper_orders.list_fills(db, limit=limit)


@router.get("/risk/policy", response_model=RiskPolicy)
async def risk_policy(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskPolicy:
    state = await runtime_state.get(db)
    return RiskPolicy(
        max_order_ratio=format(state.max_order_ratio, "f"),
        max_symbol_ratio=format(state.max_symbol_ratio, "f"),
        allow_short_sell=False,
        updated_at=state.updated_at.isoformat(),
    )


@router.put("/risk/policy", response_model=RiskPolicy)
async def update_risk_policy(
    request: RiskPolicyUpdate,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RiskPolicy:
    await runtime_state.update_policy(
        db,
        max_order_ratio=request.max_order_ratio,
        max_symbol_ratio=request.max_symbol_ratio,
    )
    return await risk_policy(_session, db)


@router.get("/ai/status", response_model=AiStatus)
async def ai_status(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
) -> AiStatus:
    return AiStatus(
        relay_configured=False,
        reachable=False,
        message="AI 기능은 이번 통합 단계에서 확장하지 않습니다.",
    )


def _require_paper(provider: str) -> None:
    if provider.strip().upper() != "PAPER":
        raise MobileApiError(
            409, "BROKER_NOT_CONNECTED", "선택한 브로커가 연결되지 않았습니다."
        )

