"""AI provider 전용 Streamable HTTP MCP sidecar.

`run_skill` 도구 하나만 노출한다. broker·account·order 도구는 물론
market data 도구도 등록하지 않으며, DB·Redis·broker 연결을 열지 않는다.
거래 도구 서버인 `app/mcp_server`(profile `analysis_readonly`)와는 별개
프로세스이고, 그 서버의 도구 registry나 profile을 재사용하지 않는다.

bearer 토큰 검증만 `app.mcp_server.auth.build_auth_provider`를 그대로 쓴다.
`hmac.compare_digest` 기반 FastMCP 토큰 검증을 두 번 구현하지 않기 위한
재사용이며, 거래 도구 registry와는 무관한 모듈이다.

가용성 신호는 HTTP 계층에만 있다. `McpStructuredJsonClient`는 HTTP
408/429/5xx와 transport 오류만 `AiProviderUnavailable`로 분류하고 나머지는
fail-closed하므로 sidecar는

* 설정·바이너리 문제로 서비스할 수 없으면 기동을 실패시켜(연결 오류),
* 동시 처리 상한을 넘으면 HTTP 429로,

가용성을 알린다. 호출이 시작된 뒤 발생한 CLI 실패는 MCP 규격상 같은 응답
안에서 상태 코드를 바꿀 수 없어 `isError`로 나가고 호출자는 fail-closed한다.
그래서 sidecar timeout은 호출자의 `KASSET_AI_MCP_TIMEOUT_SECONDS`보다 넉넉히
크게 두어, 느린 CLI가 호출자 timeout(=가용성 실패)으로 먼저 드러나게 한다.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any, Literal

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan
from pydantic import Field
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.extensions.kasset.ai_mcp_sidecar.config import (
    SidecarConfig,
    load_config,
    verify_command_executable,
)
from app.extensions.kasset.ai_mcp_sidecar.runner import (
    MAX_INSTRUCTION_CHARS,
    MAX_SKILL_CHARS,
    SidecarContractError,
    SidecarUnavailable,
    SkillRunner,
    build_invocation,
)
from app.mcp_server.auth import build_auth_provider

SERVICE_NAME = "kasset-ai-mcp-sidecar"
SERVICE_VERSION = "1.0"

#: 이 서버가 노출하는 유일한 도구 이름. API 쪽 `KASSET_AI_MCP_TOOL_NAME`의 기본값과
#: 같아야 한다.
TOOL_NAME = "run_skill"

#: 임의 `response_schema`를 받는 계약이므로 도구 outputSchema는 객체라는 사실만
#: 선언한다. 실제 형태 검증은 호출자가 넘긴 스키마로 runner가 수행한다.
_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}

_SERVER_INSTRUCTIONS = (
    "Read-only analysis provider for KAsset Core. The only tool is "
    f"{TOOL_NAME}, which runs one analysis prompt through the operator's "
    "subscription CLI and returns one JSON object matching the caller's "
    "response_schema. No broker, account, holdings, or order tool exists here."
)

_RUN_SKILL_DESCRIPTION = (
    "Run one read-only KAsset analysis skill and return a single JSON object "
    "that validates against response_schema. Analysis output only: never "
    "returns broker, account, credential, or order-execution data."
)

logger = logging.getLogger(__name__)

_STARTED_MONOTONIC = time.monotonic()

_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug"})


class InFlightLimitMiddleware:
    """동시에 처리 중인 POST 요청 수를 제한하고 포화 시 즉시 429로 거절한다.

    `McpStructuredJsonClient`는 429를 `AiProviderUnavailable`로 분류하므로
    호출자는 대기 없이 기존 direct/OpenRouter fallback으로 넘어간다. 세션
    하나가 POST를 직렬로 보내므로 이 상한은 동시에 살아 있는 CLI 프로세스 수
    상한이기도 하다. GET/DELETE는 세지 않는다. 장수 SSE 스트림이 슬롯을 물고
    있으면 CLI를 한 번도 못 부르고 막히기 때문이다.

    FastMCP는 bearer 인증을 `/mcp` route endpoint에만 감싸므로 이 middleware가
    인증보다 먼저 돈다. 미인증 요청은 auth에서 즉시 실패해 슬롯을 곧바로
    반납하며, sidecar 자체가 backend 네트워크 전용이라 외부 유입 경로는 없다.
    """

    __slots__ = ("_app", "_in_flight", "_limit")

    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self._app = app
        self._limit = limit
        self._in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        # 단일 event loop이고 검사와 증가 사이에 await가 없어 lock이 필요 없다.
        if self._in_flight >= self._limit:
            logger.warning(
                "ai_mcp_sidecar.rejected class=availability reason=in_flight_limit "
                "limit=%d",
                self._limit,
            )
            await _too_many_requests(scope, receive, send)
            return
        self._in_flight += 1
        try:
            await self._app(scope, receive, send)
        finally:
            self._in_flight -= 1


def build_server(config: SidecarConfig) -> FastMCP:
    """`run_skill` 하나만 등록된 FastMCP 서버를 만든다."""

    runner = SkillRunner(
        command=config.command,
        timeout_seconds=config.timeout_seconds,
    )
    mcp = FastMCP(
        name=SERVICE_NAME,
        instructions=_SERVER_INSTRUCTIONS,
        version=SERVICE_VERSION,
        auth=build_auth_provider(config.token),
        # 예상 밖 예외 본문이 호출자에게 새지 않게 한다. 명시적으로 올리는
        # ToolError 메시지는 masking 대상이 아니다.
        mask_error_details=True,
    )

    @mcp.tool(
        name=TOOL_NAME,
        description=_RUN_SKILL_DESCRIPTION,
        output_schema=_OUTPUT_SCHEMA,
        annotations={
            "title": "Run KAsset analysis skill",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def run_skill(
        skill: Annotated[
            str,
            Field(
                description="Analysis skill identifier, e.g. kasset_tier_verdict.",
                min_length=1,
                max_length=MAX_SKILL_CHARS,
            ),
        ],
        instruction: Annotated[
            str,
            Field(
                description="System instruction for this analysis call.",
                min_length=1,
                max_length=MAX_INSTRUCTION_CHARS,
            ),
        ],
        context: Annotated[
            dict[str, Any],
            Field(description="Read-only evidence object supplied by the caller."),
        ],
        response_schema: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "JSON Schema of type object that the returned result must "
                    "validate against."
                )
            ),
        ],
        reasoning_effort: Annotated[
            Literal["low", "medium", "high"] | None,
            Field(description="Advisory analysis depth for this call."),
        ] = None,
    ) -> dict[str, Any]:
        try:
            invocation = build_invocation(
                skill=skill,
                instruction=instruction,
                context=context,
                response_schema=response_schema,
                reasoning_effort=reasoning_effort,
            )
            return await runner.run(invocation)
        except SidecarContractError as exc:
            logger.warning(
                "ai_mcp_sidecar.rejected class=contract skill=%s reason=%s",
                skill[:MAX_SKILL_CHARS],
                exc,
            )
            raise ToolError(f"contract: {exc}") from None
        except SidecarUnavailable as exc:
            logger.warning(
                "ai_mcp_sidecar.rejected class=availability skill=%s reason=%s",
                skill[:MAX_SKILL_CHARS],
                exc,
            )
            raise ToolError(f"provider_unavailable: {exc}") from None

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> JSONResponse:  # noqa: ARG001
        # 의존성 없는 event loop liveness probe다. CLI를 부르지 않는다.
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "uptime_s": round(time.monotonic() - _STARTED_MONOTONIC, 1),
            }
        )

    return mcp


def build_http_app(config: SidecarConfig) -> StarletteWithLifespan:
    """sidecar ASGI app을 조립한다. 실행 경로와 테스트가 같은 조립을 쓴다.

    stateless로 돌린다. 호출 하나가 initialize → notifications/initialized →
    tools/call로 끝나므로 세션 상태를 남길 이유가 없고, 서버가 세션을 들고
    있지 않으면 호출자 timeout 뒤에 고아 세션이 쌓이지 않는다.
    """

    return build_server(config).http_app(
        path=config.path,
        transport="streamable-http",
        stateless_http=True,
        middleware=[Middleware(InFlightLimitMiddleware, limit=config.max_concurrency)],
    )


def main() -> None:
    log_level = _log_level()
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(os.environ)
    verify_command_executable(config.command)

    logger.info(
        "ai_mcp_sidecar.starting host=%s port=%d path=%s tool=%s timeout_s=%s "
        "max_concurrency=%d argv0=%s",
        config.host,
        config.port,
        config.path,
        TOOL_NAME,
        config.timeout_seconds,
        config.max_concurrency,
        config.command[0],
    )
    uvicorn.run(
        build_http_app(config),
        host=config.host,
        port=config.port,
        lifespan="on",
        log_level=log_level,
        timeout_graceful_shutdown=5,
    )


async def _too_many_requests(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        {"error": "sidecar in-flight limit reached"},
        status_code=429,
        headers={"Retry-After": "1"},
    )
    await response(scope, receive, send)


def _log_level() -> str:
    level = os.environ.get("LOG_LEVEL", "").strip().lower()
    return level if level in _LOG_LEVELS else "info"


if __name__ == "__main__":
    main()
