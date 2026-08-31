"""AI provider MCP sidecar 실행 설정.

sidecar는 API 프로세스보다 좁은 권한 경계에서 돈다. `app.core.config`의 전체
Settings를 읽지 않고 `os.environ`만 보므로 DB·broker·JWT 비밀을 컨테이너에
주입하지 않아도 기동한다.

환경변수 namespace 셋은 서로 다른 대상을 가리키며 섞이면 안 된다.

* `KASSET_AI_SIDECAR_*`: 이 sidecar 프로세스 자신의 설정(서버 쪽).
* `KASSET_AI_MCP_*`: API가 provider MCP를 가리키는 client 쪽 설정.
  `KASSET_AI_MCP_TOKEN`은 여기의 `KASSET_AI_SIDECAR_TOKEN`과 같아야 한다.
* `MCP_*`: 거래 도구 서버(`analysis_readonly`) 설정. sidecar와 무관하다.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

#: CLI 한 번 호출에 허용하는 벽시계 상한. 하한은 구독 CLI가 응답할 수 없는
#: 값을 배제하고, 상한은 event loop이 무한정 붙잡히지 않게 한다.
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 600.0
DEFAULT_TIMEOUT_SECONDS = 90.0

#: 동시에 처리하는 MCP POST 요청 수 상한. 세션 하나가 POST를 직렬로 보내므로
#: 동시에 살아 있는 CLI 프로세스 수 상한과 같다.
MIN_MAX_CONCURRENCY = 1
MAX_MAX_CONCURRENCY = 8
DEFAULT_MAX_CONCURRENCY = 2

#: compose가 포트를 발행하지 않으므로 bind 대상은 backend 네트워크뿐이다.
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8770
DEFAULT_PATH = "/mcp"


class SidecarConfigError(RuntimeError):
    """기동 시점에 확정되는 설정 오류다. sidecar는 이 상태로 서비스하지 않는다."""


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    """검증이 끝난 sidecar 실행 설정."""

    command: tuple[str, ...]
    token: str
    host: str
    port: int
    path: str
    timeout_seconds: float
    max_concurrency: int


def load_config(env: Mapping[str, str]) -> SidecarConfig:
    """환경변수에서 sidecar 설정을 읽고 검증한다.

    미설정·범위 위반은 모두 `SidecarConfigError`다. 기동을 실패시켜야 하며,
    잘못된 설정으로 서비스하다 호출자에게 계약 오류를 돌려주면 안 된다.
    """

    raw_command = env.get("KASSET_AI_SIDECAR_CMD", "").strip()
    if not raw_command:
        raise SidecarConfigError(
            "KASSET_AI_SIDECAR_CMD is required: the sidecar has no built-in "
            "model and must be given the operator's subscription CLI command"
        )
    command = tuple(shlex.split(raw_command))
    if not command:
        raise SidecarConfigError("KASSET_AI_SIDECAR_CMD parsed to an empty argv")

    token = env.get("KASSET_AI_SIDECAR_TOKEN", "").strip()
    if not token:
        raise SidecarConfigError(
            "KASSET_AI_SIDECAR_TOKEN is required: the sidecar refuses to serve "
            "an unauthenticated MCP endpoint"
        )

    host = env.get("KASSET_AI_SIDECAR_HOST", "").strip() or DEFAULT_HOST

    path = env.get("KASSET_AI_SIDECAR_PATH", "").strip() or DEFAULT_PATH
    if not path.startswith("/"):
        raise SidecarConfigError("KASSET_AI_SIDECAR_PATH must start with '/'")

    return SidecarConfig(
        command=command,
        token=token,
        host=host,
        port=_read_int(
            env,
            "KASSET_AI_SIDECAR_PORT",
            default=DEFAULT_PORT,
            minimum=1,
            maximum=65535,
        ),
        path=path,
        timeout_seconds=_read_float(
            env,
            "KASSET_AI_SIDECAR_TIMEOUT_SECONDS",
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=MIN_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        max_concurrency=_read_int(
            env,
            "KASSET_AI_SIDECAR_MAX_CONCURRENCY",
            default=DEFAULT_MAX_CONCURRENCY,
            minimum=MIN_MAX_CONCURRENCY,
            maximum=MAX_MAX_CONCURRENCY,
        ),
    )


def verify_command_executable(command: tuple[str, ...]) -> None:
    """설정된 CLI가 실제로 실행 가능한지 기동 시점에 확인한다.

    바이너리 mount 누락은 가용성 실패이며, 호출 중이 아니라 기동 시점에
    드러나야 한다. 그래야 sidecar가 뜨지 않고 호출자는 transport 오류를
    `AiProviderUnavailable`로 분류해 기존 direct/OpenRouter fallback으로 넘어간다.
    """

    if shutil.which(command[0]) is None:
        raise SidecarConfigError(
            f"configured subscription CLI is not executable: {command[0]}"
        )


def _read_int(
    env: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SidecarConfigError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SidecarConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _read_float(
    env: Mapping[str, str],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SidecarConfigError(f"{key} must be a number") from exc
    if not isfinite(value) or not minimum <= value <= maximum:
        raise SidecarConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_TIMEOUT_SECONDS",
    "SidecarConfig",
    "SidecarConfigError",
    "load_config",
    "verify_command_executable",
]
