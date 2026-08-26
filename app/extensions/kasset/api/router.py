"""Android TraderApi-compatible routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.extensions.kasset.api.auth import (
    MobileSession,
    get_mobile_session,
    mobile_auth,
)
from app.extensions.kasset.api.schemas import (
    PairRequest,
    RefreshRequest,
    SessionTokens,
)

router = APIRouter(prefix="/api/v1", tags=["kasset-android"])


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
