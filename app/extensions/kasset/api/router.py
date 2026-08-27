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
from app.extensions.kasset.api.credential_vault import credential_vault
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.nh_adapter import nh_adapter
from app.extensions.kasset.api.paper import iso_z, paper_account_adapter
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
    AiBriefingResponse,
    AiBriefingSection,
    AiBriefingSummary,
    AiRelayStatus,
    Broker,
    BrokersResponse,
    BrokerVerifyResponse,
    CredentialRequest,
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
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrokersResponse:
    return BrokersResponse(brokers=await broker_registry.list_brokers(db))


@router.post("/brokers/{provider}/credential", response_model=Broker)
@router.post("/brokers/{provider}/credentials", response_model=Broker)
async def register_broker_credential(
    provider: str,
    request: CredentialRequest,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Broker:
    _require_nh(provider)
    credential = await credential_vault.store_nh(
        db,
        app_key=request.app_key,
        app_secret=request.app_secret,
        account_no=request.account_no,
    )
    await nh_adapter.invalidate_auth_cache(credential.id)
    return await broker_registry.get_broker(db, "NH")


@router.delete("/brokers/{provider}/credential", status_code=status.HTTP_204_NO_CONTENT)
@router.delete(
    "/brokers/{provider}/credentials", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_broker_credential(
    provider: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    _require_nh(provider)
    credential_id = await credential_vault.delete_nh(db)
    await nh_adapter.invalidate_auth_cache(credential_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/brokers/{provider}/verify", response_model=BrokerVerifyResponse)
async def verify_broker(
    provider: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrokerVerifyResponse:
    _require_nh(provider)
    checked_at = await nh_adapter.verify(db)
    return BrokerVerifyResponse(
        connected=True,
        checked_at=checked_at.isoformat().replace("+00:00", "Z"),
        message="NH PLUG 모의투자 계좌 연결을 확인했습니다.",
    )


@router.get("/system/status", response_model=SystemStatus)
async def system_status(
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatus:
    return await _build_system_status(db)


async def _build_system_status(db: AsyncSession) -> SystemStatus:
    registered = await broker_registry.list_brokers(db)
    state = await runtime_state.get(db)
    return SystemStatus(
        server_version=settings.KASSET_SERVER_VERSION,
        server_time=(
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
    if broker.strip().upper() == "NH":
        return await nh_adapter.balance(db)
    _require_paper(broker)
    return await paper_account_adapter.balance(db)


@router.get("/positions", response_model=PositionsResponse)
async def positions(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PositionsResponse:
    if broker.strip().upper() == "NH":
        return await nh_adapter.positions(db)
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
    if broker.strip().upper() == "NH":
        return await nh_adapter.quote(db, market=market, symbol=symbol)
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
    _require_order_capable(request.broker)
    return await paper_orders.preview(db, request)


@router.post("/orders", response_model=OrderEnvelope)
async def submit_order(
    request: OrderRequest,
    response: Response,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderEnvelope:
    _require_order_capable(request.broker)
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
    if broker.strip().upper() == "NH":
        return OrdersResponse(orders=[])
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
    broker: Annotated[str | None, Query()] = None,
) -> OrderEnvelope:
    if broker is not None:
        _require_order_capable(broker)
    return await paper_orders.cancel(db, order_id)


@router.post("/orders/{order_id}/amend", response_model=OrderEnvelope)
async def amend_order(
    order_id: str,
    request: AmendRequest,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[str | None, Query()] = None,
) -> OrderEnvelope:
    if broker is not None:
        _require_order_capable(broker)
    return await paper_orders.amend(db, order_id, request)


@router.get("/fills", response_model=FillsResponse)
async def fills(
    broker: str,
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> FillsResponse:
    if broker.strip().upper() == "NH":
        return FillsResponse(fills=[])
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
        updated_at=iso_z(state.updated_at),
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


@router.get("/ai/briefing", response_model=AiBriefingResponse)
async def ai_briefing(
    _market: Annotated[
        str,
        Query(
            alias="market",
            min_length=1,
            max_length=16,
            pattern=r"^(kr|us|crypto)$",
        ),
    ],
    _limit: Annotated[int, Query(alias="limit", ge=1, le=100)],
    _session: Annotated[MobileSession, Depends(get_mobile_session)],
    _symbol: Annotated[
        str | None,
        Query(
            alias="symbol",
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$",
        ),
    ] = None,
) -> AiBriefingResponse:
    return AiBriefingResponse(
        status="empty",
        as_of=None,
        news=AiBriefingSection(status="empty", refreshed_at=None, items=[]),
        research=AiBriefingSection(status="empty", refreshed_at=None, items=[]),
        briefing=AiBriefingSummary(
            status="unavailable",
            id=None,
            title=None,
            summary=None,
            provider=None,
            market=None,
            as_of=None,
            valid_until=None,
            data_status="unknown",
            unavailable_reason="저장된 AI 브리핑 제공자가 아직 연결되지 않았습니다.",
        ),
    )


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
