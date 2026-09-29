"""NH PLUG 접근 토큰 파일 캐시.

NH 규약(``llms.txt`` 「공통 API」, SDK ``nhplug/auth.py``):

- 토큰은 24시간 유효하고 재발급마다 보안 알림이 간다. 파일 캐시로 프로세스 간
  공유하고 만료 전에는 다시 발급하지 않는다.
- 재발급은 만료(60초 여유) 때만 한다. 유량 초과(429)·종목·시세 권한 오류는
  토큰 문제가 아니므로 재발급 사유가 아니다.

서버에 이미 있는 캐시 형식 ``{"access_token": …, "expires_at": …}``을 그대로
읽고 쓴다(``expires_at``은 epoch 초 또는 ISO 8601). 캐시 파일이 있는데 읽거나
해석할 수 없으면 새로 발급하지 않고 실패한다 — 권한·마운트 오류가 재발급
반복으로 번지지 않게 하기 위해서다. 발급은 파일 잠금으로 프로세스 간
single-flight이며, 잠금을 얻은 뒤 캐시를 다시 읽어 다른 프로세스가 이미
갱신했으면 그 토큰을 쓴다. 토큰·앱키·시크릿은 로그와 예외 문구에 남기지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

AUTH_URL_DEFAULT: Final = "https://api.nhplug.com:8443"
ALLOWED_AUTH_HOSTS: Final = frozenset({"api.nhplug.com"})
TOKEN_CACHE_PATH_ENV: Final = "KASSET_NHPLUG_TOKEN_CACHE_PATH"
TOKEN_CACHE_PATH_DEFAULT: Final = "/var/lib/kasset-nhplug/token.json"
EXPIRY_MARGIN_SECONDS: Final = 60.0
DEFAULT_EXPIRES_IN: Final = 86400


class NhplugTokenError(RuntimeError):
    """토큰을 얻지 못했다. 메시지에 비밀값을 넣지 않는다."""


@dataclass(frozen=True, slots=True)
class NhplugCredentials:
    app_key: str
    app_secret: str
    auth_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> NhplugCredentials:
        env = os.environ if environ is None else environ
        app_key = (env.get("NHPLUG_APP_KEY") or "").strip()
        app_secret = (env.get("NHPLUG_APP_SECRET") or "").strip()
        if not app_key or not app_secret:
            raise NhplugTokenError(
                "NHPLUG_APP_KEY / NHPLUG_APP_SECRET environment is missing"
            )
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            auth_url=validate_auth_url(env.get("NHPLUG_AUTH_URL") or AUTH_URL_DEFAULT),
        )

    def __repr__(self) -> str:  # 비밀값이 repr로 새지 않게 한다.
        return f"NhplugCredentials(auth_url={self.auth_url!r})"


def validate_auth_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_AUTH_HOSTS:
        raise NhplugTokenError("NHPLUG_AUTH_URL must be https://api.nhplug.com")
    if parts.path or parts.query or parts.fragment:
        raise NhplugTokenError("NHPLUG_AUTH_URL must not carry a path")
    return url


@dataclass(frozen=True, slots=True)
class CachedToken:
    access_token: str
    expires_at: float
    #: 기존 파일의 ``expires_at`` 표기(epoch 숫자면 True). 갱신 때 그대로 쓴다.
    numeric_expiry: bool

    def valid_at(self, now: float) -> bool:
        return self.expires_at - EXPIRY_MARGIN_SECONDS > now


def _parse_expiry(raw: object) -> tuple[float, bool]:
    if isinstance(raw, bool):
        raise NhplugTokenError("token cache expires_at is invalid")
    if isinstance(raw, (int, float)):
        return float(raw), True
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return float(text), True
        except ValueError:
            pass
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NhplugTokenError("token cache expires_at is invalid") from exc
        if moment.tzinfo is None:
            raise NhplugTokenError("token cache expires_at must carry a timezone")
        return moment.timestamp(), False
    raise NhplugTokenError("token cache expires_at is invalid")


def read_token_cache(path: Path) -> CachedToken | None:
    """캐시를 읽는다. 파일이 없으면 ``None``, 있는데 못 읽으면 예외."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NhplugTokenError(
            f"token cache is not readable: {type(exc).__name__}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NhplugTokenError("token cache is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise NhplugTokenError("token cache must be an object")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise NhplugTokenError("token cache access_token is missing")
    expires_at, numeric = _parse_expiry(payload.get("expires_at"))
    return CachedToken(
        access_token=token.strip(), expires_at=expires_at, numeric_expiry=numeric
    )


def write_token_cache(path: Path, token: CachedToken) -> None:
    expires: object = (
        token.expires_at
        if token.numeric_expiry
        else datetime.fromtimestamp(token.expires_at, UTC).isoformat()
    )
    payload = json.dumps({"access_token": token.access_token, "expires_at": expires})
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp, path)


@contextlib.asynccontextmanager
async def _file_lock(path: Path) -> AsyncIterator[None]:
    """프로세스 간 배타 잠금. 발급 요청과 파일 쓰기가 끝날 때까지 유지한다."""

    import fcntl  # POSIX 전용. 운영 컨테이너와 격리 테스트는 Linux다.

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class NhplugTokenProvider:
    """만료 전에는 캐시만 쓰고, 만료 때만 발급하는 토큰 공급자."""

    def __init__(
        self,
        *,
        credentials: NhplugCredentials,
        cache_path: Path,
        http_client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._cache_path = cache_path
        self._http = http_client
        self._clock = clock
        self._lock = asyncio.Lock()
        self._memory: CachedToken | None = None
        self.issued_count = 0

    async def token(self) -> str:
        now = self._clock()
        memory = self._memory
        if memory is not None and memory.valid_at(now):
            return memory.access_token
        async with self._lock:
            now = self._clock()
            cached = read_token_cache(self._cache_path)
            if cached is not None and cached.valid_at(now):
                self._memory = cached
                return cached.access_token
            async with _file_lock(self._lock_path()):
                # 잠금을 기다리는 동안 다른 프로세스가 갱신했을 수 있다.
                cached = read_token_cache(self._cache_path)
                if cached is not None and cached.valid_at(self._clock()):
                    self._memory = cached
                    return cached.access_token
                issued = await self._issue(
                    numeric_expiry=(
                        cached.numeric_expiry if cached is not None else False
                    )
                )
                write_token_cache(self._cache_path, issued)
            self._memory = issued
            return issued.access_token

    def _lock_path(self) -> Path:
        return self._cache_path.with_name(f"{self._cache_path.name}.lock")

    async def _issue(self, *, numeric_expiry: bool) -> CachedToken:
        started = self._clock()
        try:
            response = await self._http.post(
                f"{self._credentials.auth_url}/oauth2/token",
                params={
                    "appkey": self._credentials.app_key,
                    "appsecretkey": self._credentials.app_secret,
                    "grant_type": "client_credentials",
                    "scope": "oob",
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise NhplugTokenError(
                f"token issue request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise NhplugTokenError(f"token issue rejected: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NhplugTokenError("token issue response is not JSON") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise NhplugTokenError("token issue response has no access_token")
        try:
            expires_in = int(payload.get("expires_in", DEFAULT_EXPIRES_IN))
        except (TypeError, ValueError):
            expires_in = DEFAULT_EXPIRES_IN
        self.issued_count += 1
        logger.warning(
            "NH PLUG access token issued (cache expired or absent); expires_in=%s",
            expires_in,
        )
        return CachedToken(
            access_token=token.strip(),
            expires_at=started + expires_in,
            numeric_expiry=numeric_expiry,
        )


def token_cache_path_from_env(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(TOKEN_CACHE_PATH_ENV) or TOKEN_CACHE_PATH_DEFAULT)


__all__ = [
    "CachedToken",
    "NhplugCredentials",
    "NhplugTokenError",
    "NhplugTokenProvider",
    "read_token_cache",
    "token_cache_path_from_env",
    "validate_auth_url",
    "write_token_cache",
]
