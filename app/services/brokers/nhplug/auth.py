"""The sole NHPLUG production-host owner, limited to two OAuth paths.

The production OAuth host is an unavoidable vendor exception: tokens are issued
there even when every subsequent data request uses the mock host.  This module
therefore owns that host physically and permits only token issuance and token
revocation.  No data client imports this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Final

import httpx

from app.services.brokers.nhplug.errors import (
    NHPlugMockConfigurationError,
    NHPlugMockEndpointError,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled

# Keep the production hostname in this file only.  The data client has an exact
# mock allowlist and deliberately does not import this module.
AUTH_BASE_URL: Final[str] = "https://api.nhplug.com:8443"
AUTH_HOST: Final[str] = "api.nhplug.com"
AUTH_PORT: Final[int] = 8443
AUTH_TOKEN_PATH: Final[str] = "/oauth2/token"
AUTH_REVOKE_PATH: Final[str] = "/oauth2/revoke"
AUTH_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    {AUTH_TOKEN_PATH, AUTH_REVOKE_PATH}
)

_DEFAULT_TOKEN_TTL_SECONDS: Final[float] = 86_400.0
_TOKEN_REFRESH_LEEWAY_SECONDS: Final[float] = 60.0
DEFAULT_TOKEN_CACHE_PATH: Final[Path] = Path.home() / ".nhplug" / "token_cache.json"
_CACHE_PARENT_MODE: Final[int] = 0o700
_CACHE_FILE_MODE: Final[int] = 0o600


def _assert_auth_base_url(base_url: str) -> str:
    """Accept only the exact HTTPS production host and required port."""

    url = httpx.URL(base_url)
    if (
        url.scheme != "https"
        or url.host != AUTH_HOST
        or url.port != AUTH_PORT
        or url.path not in {"", "/"}
        or url.query
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG auth client only accepts its pinned OAuth endpoint"
        )
    return AUTH_BASE_URL


def _assert_auth_path(path: str) -> None:
    if path not in AUTH_ALLOWED_PATHS:
        raise NHPlugMockEndpointError("NHPLUG auth path is not allowlisted")


def _assert_resolved_auth_request(request: httpx.Request) -> None:
    """Last-moment host, port, and path check immediately before send."""

    if (
        request.url.scheme != "https"
        or request.url.host != AUTH_HOST
        or request.url.port != AUTH_PORT
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG OAuth request resolved outside the pinned HTTPS endpoint"
        )
    _assert_auth_path(request.url.path)


class NHPlugAuthClient:
    """OAuth-only client with memory and process-independent file token reuse."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = AUTH_BASE_URL,
        cache_path: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(app_key, str) or not app_key.strip():
            raise NHPlugMockConfigurationError("NHPLUG_APP_KEY is required")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise NHPlugMockConfigurationError("NHPLUG_APP_SECRET is required")
        self._base_url = _assert_auth_base_url(base_url)
        self._app_key = app_key
        self._app_secret = app_secret
        self._cache_path = (
            DEFAULT_TOKEN_CACHE_PATH
            if cache_path is None
            else Path(cache_path).expanduser()
        )
        self._owner_fingerprint = hashlib.sha256(
            f"{self._app_key}|{self._base_url}".encode()
        ).hexdigest()
        self._transport = transport
        self._timeout = timeout
        self._cached_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def get_access_token(
        self,
        *,
        force_refresh: bool = False,
        failed_token: str | None = None,
    ) -> str:
        """Resolve memory, then file, issuing only when no usable token remains."""

        if failed_token is not None and (
            not isinstance(failed_token, str) or not failed_token.strip()
        ):
            raise NHPlugMockConfigurationError(
                "failed_token must be a non-empty access token"
            )
        if failed_token is not None:
            failed_token = failed_token.strip()

        now = time.time()
        if not force_refresh:
            memory_token = self._valid_memory_token(now)
            if memory_token is not None:
                return memory_token[0]

        async with self._refresh_lock:
            now = time.time()
            if not force_refresh:
                memory_token = self._valid_memory_token(now)
                if memory_token is not None:
                    return memory_token[0]
                file_token = self._read_file_cache(now=now)
                if file_token is not None:
                    self._remember_token(*file_token)
                    return file_token[0]
            elif failed_token is not None:
                replacement_token = self._different_token_after_failure(
                    now,
                    failed_token,
                )
                if replacement_token is not None:
                    self._remember_token(*replacement_token)
                    return replacement_token[0]
                self._invalidate_cached_token(expected_token=failed_token)
            else:
                self._invalidate_cached_token()

            return await self._issue_access_token()

    async def revoke_access_token(self, *, access_token: str) -> dict[str, Any]:
        """Revoke through the allowlisted path, then invalidate only that token."""

        if not isinstance(access_token, str) or not access_token.strip():
            raise NHPlugMockConfigurationError(
                "an access token is required for OAuth revocation"
            )
        normalized_token = access_token.strip()
        payload = await self._post_form(
            path=AUTH_REVOKE_PATH,
            form={"access_token": normalized_token},
        )
        self._invalidate_cached_token(expected_token=normalized_token)
        return payload

    def _valid_memory_token(self, now: float) -> tuple[str, float] | None:
        token = self._cached_token
        if (
            token is None
            or self._token_expires_at <= now + _TOKEN_REFRESH_LEEWAY_SECONDS
        ):
            return None
        return token, self._token_expires_at

    def _different_token_after_failure(
        self,
        now: float,
        failed_token: str,
    ) -> tuple[str, float] | None:
        file_token = self._read_file_cache(now=now)
        if file_token is not None and not hmac.compare_digest(
            file_token[0], failed_token
        ):
            return file_token
        memory_token = self._valid_memory_token(now)
        if memory_token is not None and not hmac.compare_digest(
            memory_token[0], failed_token
        ):
            return memory_token
        return None

    def _read_file_cache(self, *, now: float | None) -> tuple[str, float] | None:
        try:
            if self._cache_path.is_symlink() or not self._cache_path.is_file():
                return None
            os.chmod(self._cache_path.parent, _CACHE_PARENT_MODE)
            os.chmod(self._cache_path, _CACHE_FILE_MODE)
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        base = payload.get("base")
        owner_fingerprint = payload.get("owner_fingerprint")
        if (
            not isinstance(base, str)
            or base != self._base_url
            or not isinstance(owner_fingerprint, str)
            or not hmac.compare_digest(owner_fingerprint, self._owner_fingerprint)
        ):
            return None

        token = payload.get("token")
        raw_expires_at = payload.get("exp")
        if (
            not isinstance(token, str)
            or not token
            or token.strip() != token
            or isinstance(raw_expires_at, bool)
            or not isinstance(raw_expires_at, int | float)
        ):
            return None
        try:
            expires_at = float(raw_expires_at)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(expires_at):
            return None
        if now is not None and expires_at <= now + _TOKEN_REFRESH_LEEWAY_SECONDS:
            return None
        return token, expires_at

    def _write_file_cache(self, *, token: str, expires_at: float) -> None:
        parent = self._cache_path.parent
        temporary_path: Path | None = None
        file_descriptor = -1
        try:
            if parent.is_symlink():
                return
            parent.mkdir(mode=_CACHE_PARENT_MODE, parents=True, exist_ok=True)
            os.chmod(parent, _CACHE_PARENT_MODE)
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._cache_path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, _CACHE_FILE_MODE)
            cache_file = os.fdopen(file_descriptor, "w", encoding="utf-8")
            file_descriptor = -1
            with cache_file:
                json.dump(
                    {
                        "base": self._base_url,
                        "exp": expires_at,
                        "owner_fingerprint": self._owner_fingerprint,
                        "token": token,
                    },
                    cache_file,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                cache_file.flush()
                os.fsync(cache_file.fileno())
            os.replace(temporary_path, self._cache_path)
            temporary_path = None
        except (OSError, TypeError, ValueError):
            return
        finally:
            if file_descriptor >= 0:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _invalidate_cached_token(self, *, expected_token: str | None = None) -> None:
        memory_token = self._cached_token
        if expected_token is None or (
            memory_token is not None
            and hmac.compare_digest(memory_token, expected_token)
        ):
            self._cached_token = None
            self._token_expires_at = 0.0

        file_token = self._read_file_cache(now=None)
        if file_token is None:
            return
        if expected_token is not None and not hmac.compare_digest(
            file_token[0], expected_token
        ):
            return
        try:
            self._cache_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _remember_token(self, token: str, expires_at: float) -> None:
        self._cached_token = token
        self._token_expires_at = expires_at

    async def _issue_access_token(self) -> str:
        payload = await self._post_form(
            path=AUTH_TOKEN_PATH,
            form={
                "appkey": self._app_key,
                "appsecretkey": self._app_secret,
                "grant_type": "client_credentials",
                "scope": "oob",
            },
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response did not contain an access token"
            )
        expires_in = payload.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS)
        try:
            ttl = float(expires_in)
        except (OverflowError, TypeError, ValueError):
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response has an invalid expiry"
            ) from None
        if not math.isfinite(ttl) or ttl <= 0:
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response has a non-positive expiry"
            )
        normalized_token = token.strip()
        expires_at = time.time() + ttl
        self._remember_token(normalized_token, expires_at)
        self._write_file_cache(token=normalized_token, expires_at=expires_at)
        return normalized_token

    async def _post_form(self, *, path: str, form: dict[str, str]) -> dict[str, Any]:
        """Check the path before client creation, then recheck just before send."""

        # This is deliberately a dispatch-time gate: direct construction must
        # never make the production OAuth exception reachable while mock reads
        # are disabled.
        _assert_mock_enabled()
        _assert_auth_path(path)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            # A 307/308 redirect could forward this credential-bearing form
            # to another host, so this is an APP KEY/SECRET boundary as well
            # as a host-boundary control.
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST",
                path,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            _assert_resolved_auth_request(request)
            response = await client.send(request)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NHPlugMockResponseError("NHPLUG OAuth response was not JSON") from exc
        if not isinstance(payload, dict):
            raise NHPlugMockResponseError("NHPLUG OAuth response was not an object")
        return dict(payload)
