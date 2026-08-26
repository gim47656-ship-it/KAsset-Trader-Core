"""Android TraderApi-compatible routes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.auth import (
    MobileSession,
    get_mobile_session,
    mobile_auth,
)
from app.extensions.kasset.api.broker_registry import broker_registry
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
) -> SystemStatus:
    registered = broker_registry.list_brokers()
    return SystemStatus(
        server_version=settings.KASSET_SERVER_VERSION,
        server_time=(
            datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        database=DatabaseStatus(status="ok"),
        trading_mode="PAPER",
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        kill_switch_enabled=False,
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

