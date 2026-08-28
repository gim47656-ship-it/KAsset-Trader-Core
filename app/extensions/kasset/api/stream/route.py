"""앱이 붙는 WebSocket 엔드포인트.

**인증은 기존 신원 계약을 그대로 재사용한다.** `get_mobile_session`이 쓰는
`mobile_auth.authenticate` 하나만 호출하므로, 새 인증 경로가 생기지 않고 세션
폐기·기기 세션 검증이 REST와 동일하게 적용된다. 토큰을 싣는 방법만 두 가지다.

1. handshake 헤더 `Authorization: Bearer {accessToken}` — 정식 경로다.
2. 헤더를 못 싣는 클라이언트만, 연결 후 첫 프레임
   `{"type":"auth","accessToken":"..."}` — 5초 안에 와야 한다.

쿼리스트링(`?access_token=`)은 받지 않는다. Caddy 액세스 로그와 프록시 로그에
토큰이 그대로 남는다.

인증 실패 시 handshake를 거절하지 않고 **accept 후 4401로 닫는다.** 그래야 앱이
"토큰 만료 → 갱신 후 재접속"과 "서버 장애 → 백오프 재접속"을 구분할 수 있다.
handshake 거절은 close code를 실을 수 없다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, WebSocket, WebSocketDisconnect

from app.core.db import AsyncSessionLocal
from app.extensions.kasset.api.auth import MobileSession, mobile_auth
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.stream import contract
from app.extensions.kasset.api.stream.runtime import (
    MarketStreamRuntime,
    get_stream_runtime,
    market_stream_runtime,
)
from app.extensions.kasset.api.stream.session import SlowConsumer, StreamSession

logger = logging.getLogger(__name__)

# 첫 프레임 인증을 기다리는 시간. 이 안에 오지 않으면 닫는다.
AUTH_FRAME_TIMEOUT_SECONDS: float = 5.0

STREAM_PATH: str = "/market/stream"


@asynccontextmanager
async def _stream_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """런타임은 첫 연결에서 게으르게 뜨고, 종료 시 여기서 정리한다.

    시작 시점에 뜨우지 않는 이유는 두 가지다. 구독자가 없으면 상향 연결도 필요
    없고, 테스트 환경은 Redis를 건드리지 않아야 한다.
    """

    try:
        yield
    finally:
        await market_stream_runtime.aclose()


stream_router = APIRouter(
    prefix="/api/v1", tags=["kasset-android"], lifespan=_stream_lifespan
)


async def authenticate_stream_token(token: str) -> MobileSession:
    """REST와 동일한 신원 게이트. DB 세션은 인증 순간에만 짧게 연다."""

    async with AsyncSessionLocal() as db:
        return await mobile_auth.authenticate(db, token)


@stream_router.websocket(STREAM_PATH)
async def market_stream(
    websocket: WebSocket,
    runtime: Annotated[MarketStreamRuntime, Depends(get_stream_runtime)],
) -> None:
    await websocket.accept()

    session_identity = await _authenticate(websocket)
    if session_identity is None:
        return

    await runtime.ensure_started()
    session = StreamSession(send=websocket.send_text)
    runtime.register(session)
    sender = asyncio.create_task(
        session.run(), name=f"kasset-stream-send-{session_identity.user.id}"
    )
    session.push_control(contract.ready_message(upstream=runtime.upstream_state))
    try:
        await _serve(websocket, runtime, session, sender)
    finally:
        runtime.unregister(session)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender


async def _authenticate(websocket: WebSocket) -> MobileSession | None:
    token = _header_token(websocket)
    if token is None:
        token = await _first_frame_token(websocket)
        if token is None:
            return None
    try:
        return await authenticate_stream_token(token)
    except MobileApiError as exc:
        await _close(
            websocket,
            contract.CLOSE_UNAUTHORIZED,
            exc.message,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — 자격 원문을 로그로 흘리지 않는다
        logger.warning("kasset stream auth failed (%s)", type(exc).__name__)
        await _close(websocket, contract.CLOSE_UNAUTHORIZED, "인증에 실패했습니다.")
        return None


def _header_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


async def _first_frame_token(websocket: WebSocket) -> str | None:
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=AUTH_FRAME_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await _close(
            websocket, contract.CLOSE_AUTH_TIMEOUT, "인증 프레임이 오지 않았습니다."
        )
        return None
    except WebSocketDisconnect:
        return None
    frame = contract.parse_client_frame(raw)
    if isinstance(frame, contract.AuthRequest):
        return frame.access_token
    await _close(
        websocket, contract.CLOSE_UNAUTHORIZED, "첫 프레임은 인증이어야 합니다."
    )
    return None


async def _serve(
    websocket: WebSocket,
    runtime: MarketStreamRuntime,
    session: StreamSession,
    sender: asyncio.Task[None],
) -> None:
    while True:
        if sender.done():
            # 송신 루프가 죽었다. 느린 클라이언트가 유일한 정상 원인이다.
            await _close_for_sender(websocket, sender)
            return
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except RuntimeError:
            # 이미 닫힌 소켓에서 읽었다. 정상 종료로 취급한다.
            return

        frame = contract.parse_client_frame(raw)
        if isinstance(frame, contract.SubscribeRequest):
            accepted, rejected = await runtime.declare(session, frame.topics)
            session.push_control(
                contract.subscribed_message(accepted=accepted, rejected=rejected)
            )
            continue
        if isinstance(frame, contract.PingRequest):
            session.push_control(contract.pong_message())
            continue
        if isinstance(frame, contract.AuthRequest):
            # 이미 인증된 연결이다. 재인증 경로를 만들지 않는다.
            session.push_control(
                contract.error_message(
                    contract.ERROR_UNKNOWN_TYPE, "이미 인증된 연결입니다."
                )
            )
            continue
        session.push_control(contract.error_message(frame.code, frame.message))


async def _close_for_sender(websocket: WebSocket, sender: asyncio.Task[None]) -> None:
    reason = "전송이 지연되어 연결을 닫습니다."
    code = contract.CLOSE_SLOW_CONSUMER
    exception = sender.exception() if not sender.cancelled() else None
    if exception is not None and not isinstance(exception, SlowConsumer):
        logger.warning("kasset stream sender failed (%s)", type(exception).__name__)
        code = contract.CLOSE_SERVER_SHUTDOWN
        reason = "서버 전송 오류로 연결을 닫습니다."
    await _close(websocket, code, reason)


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code, reason=reason)
