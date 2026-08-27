"""Offline tests for the physically isolated NHPLUG OAuth client."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.brokers.nhplug import auth as auth_module
from app.services.brokers.nhplug.auth import (
    AUTH_ALLOWED_PATHS,
    AUTH_BASE_URL,
    AUTH_HOST,
    AUTH_PORT,
    AUTH_REVOKE_PATH,
    AUTH_TOKEN_PATH,
    NHPlugAuthClient,
)
from app.services.brokers.nhplug.auth import (
    _assert_mock_enabled as auth_dispatch_gate,
)
from app.services.brokers.nhplug.client import (
    _assert_mock_enabled as data_dispatch_gate,
)
from app.services.brokers.nhplug.errors import (
    NHPlugMockDisabled,
    NHPlugMockEndpointError,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled

pytestmark = pytest.mark.unit


def _transport(
    payload: dict[str, Any] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=payload or {"access_token": "test-token", "expires_in": 86_400},
        )

    return httpx.MockTransport(handler), seen


def _cache_payload(
    *,
    app_key: str = "test-key",
    token: str = "cached-token",
    base: str = AUTH_BASE_URL,
    expires_at: float | None = None,
) -> dict[str, Any]:
    return {
        "base": base,
        "exp": time.time() + 3_600 if expires_at is None else expires_at,
        "owner_fingerprint": hashlib.sha256(
            f"{app_key}|{AUTH_BASE_URL}".encode()
        ).hexdigest(),
        "token": token,
    }


@pytest.fixture(autouse=True)
def isolated_default_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_module,
        "DEFAULT_TOKEN_CACHE_PATH",
        tmp_path / ".nhplug" / "token_cache.json",
    )


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


def test_auth_allowlist_is_exactly_the_two_stage_one_oauth_paths() -> None:
    """Pin OAuth constants to literals, not the constants' own definitions."""

    assert AUTH_TOKEN_PATH == "/oauth2/token"
    assert AUTH_REVOKE_PATH == "/oauth2/revoke"
    assert AUTH_ALLOWED_PATHS == frozenset({"/oauth2/token", "/oauth2/revoke"})


def test_auth_and_data_dispatch_share_the_neutral_gate() -> None:
    assert auth_dispatch_gate is _assert_mock_enabled
    assert data_dispatch_gate is _assert_mock_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_value", (None, "false"))
async def test_auth_dispatch_gate_blocks_unset_or_false_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    gate_value: str | None,
) -> None:
    """The production-host exception has the same dispatch-time master gate."""

    if gate_value is None:
        monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("NHPLUG_MOCK_ENABLED", gate_value)
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    error: Exception | None = None
    try:
        await client.get_access_token()
    except Exception as exc:  # The assertion below fixes both type and dispatch count.
        error = exc

    assert seen == []
    assert isinstance(error, NHPlugMockDisabled)


@pytest.mark.asyncio
async def test_token_request_uses_only_pinned_oauth_host_port_and_path(
    armed: None,
) -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.get_access_token() == "test-token"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == AUTH_HOST
    assert request.url.port == AUTH_PORT
    assert request.url.path == AUTH_TOKEN_PATH
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"appkey=test-key" in request.content
    assert b"appsecretkey=test-secret" in request.content


@pytest.mark.asyncio
async def test_auth_token_is_reused_without_a_second_dispatch(armed: None) -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.get_access_token() == "test-token"
    assert await client.get_access_token() == "test-token"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_auth_revoke_is_the_second_and_only_other_allowed_path(
    armed: None,
) -> None:
    transport, seen = _transport(payload={"rsp_cd": "00000"})
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.revoke_access_token(access_token="test-token") == {
        "rsp_cd": "00000"
    }
    assert len(seen) == 1
    assert seen[0].url.path == AUTH_REVOKE_PATH


@pytest.mark.asyncio
async def test_non_oauth_path_is_refused_before_any_transport_or_token_work(
    armed: None,
) -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    with pytest.raises(NHPlugMockEndpointError):
        await client._post_form(path="/n2/acctinfo", form={})

    assert seen == []


@pytest.mark.parametrize(
    "base_url",
    (
        "https://moapi.nhplug.com:8443",
        "https://api.nhplug.com",
        "http://api.nhplug.com:8443",
        "https://api.nhplug.com:8443/not-allowed",
    ),
)
def test_auth_constructor_rejects_every_non_exact_endpoint(base_url: str) -> None:
    with pytest.raises(NHPlugMockEndpointError):
        NHPlugAuthClient(
            app_key="test-key",
            app_secret="test-secret",
            base_url=base_url,
        )


@pytest.mark.asyncio
async def test_auth_does_not_follow_redirects(armed: None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://unexpected.example.invalid/oauth2/token"},
        )

    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_access_token()

    assert len(seen) == 1
    assert seen[0].url.host == AUTH_HOST


@pytest.mark.asyncio
async def test_auth_postbuild_http_scheme_tamper_is_rejected_before_send(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, seen = _transport()
    original_build_request = httpx.AsyncClient.build_request

    def build_http_request(
        self: httpx.AsyncClient, *args: Any, **kwargs: Any
    ) -> httpx.Request:
        request = original_build_request(self, *args, **kwargs)
        return httpx.Request(
            request.method,
            "http://api.nhplug.com:8443/oauth2/token",
            headers=request.headers,
            content=request.content,
        )

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build_http_request)
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    with pytest.raises(NHPlugMockEndpointError):
        await client.get_access_token()

    assert seen == []


@pytest.mark.asyncio
async def test_file_cache_is_reused_by_a_different_client_instance(
    armed: None,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / ".nhplug" / "token_cache.json"
    first_transport, first_seen = _transport(
        {"access_token": "process-shared-token", "expires_in": 3_600}
    )
    first = NHPlugAuthClient(
        app_key="owner-key-sentinel",
        app_secret="owner-secret-sentinel",
        cache_path=cache_path,
        transport=first_transport,
    )

    assert await first.get_access_token() == "process-shared-token"
    assert len(first_seen) == 1
    cache_text = cache_path.read_text(encoding="utf-8")
    cache_payload = json.loads(cache_text)
    assert cache_payload["base"] == AUTH_BASE_URL
    assert cache_payload["token"] == "process-shared-token"
    assert cache_payload["exp"] > time.time() + 60
    assert (
        cache_payload["owner_fingerprint"]
        == hashlib.sha256(f"owner-key-sentinel|{AUTH_BASE_URL}".encode()).hexdigest()
    )
    assert "owner-key-sentinel" not in cache_text
    assert "owner-secret-sentinel" not in cache_text

    second_transport, second_seen = _transport(
        {"access_token": "must-not-be-issued", "expires_in": 3_600}
    )
    second = NHPlugAuthClient(
        app_key="owner-key-sentinel",
        app_secret="owner-secret-sentinel",
        cache_path=cache_path,
        transport=second_transport,
    )

    assert await second.get_access_token() == "process-shared-token"
    assert second_seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_cache",
    ("owner", "base", "expiry", "corruption"),
)
async def test_invalid_file_cache_is_never_reused(
    armed: None,
    tmp_path: Path,
    invalid_cache: str,
) -> None:
    cache_path = tmp_path / invalid_cache / "token_cache.json"
    cache_path.parent.mkdir()
    payload = _cache_payload(token="must-not-be-reused")
    if invalid_cache == "owner":
        payload["owner_fingerprint"] = _cache_payload(app_key="other-key")[
            "owner_fingerprint"
        ]
    elif invalid_cache == "base":
        payload["base"] = "https://wrong-auth-host.invalid:8443"
    elif invalid_cache == "expiry":
        payload["exp"] = time.time() + 60

    cache_path.write_text(
        "{corrupt-json" if invalid_cache == "corruption" else json.dumps(payload),
        encoding="utf-8",
    )
    transport, seen = _transport(
        {"access_token": "newly-issued-token", "expires_in": 3_600}
    )
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )

    assert await client.get_access_token() == "newly-issued-token"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_force_refresh_reuses_a_newer_token_after_failed_token_race(
    armed: None,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / ".nhplug" / "token_cache.json"
    issued_tokens = iter(("failed-token", "fresh-token"))
    issue_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        issue_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"access_token": next(issued_tokens), "expires_in": 3_600},
        )

    transport = httpx.MockTransport(handler)
    first = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )
    second = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )

    assert await first.get_access_token() == "failed-token"
    assert (
        await second.get_access_token(
            force_refresh=True,
            failed_token="failed-token",
        )
        == "fresh-token"
    )

    unused_transport, unused_seen = _transport(
        {"access_token": "duplicate-refresh", "expires_in": 3_600}
    )
    racing_client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=unused_transport,
    )
    assert (
        await racing_client.get_access_token(
            force_refresh=True,
            failed_token="failed-token",
        )
        == "fresh-token"
    )
    assert issue_paths == [AUTH_TOKEN_PATH, AUTH_TOKEN_PATH]
    assert unused_seen == []


@pytest.mark.asyncio
async def test_revoke_invalidates_only_the_revoked_cached_token(
    armed: None,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / ".nhplug" / "token_cache.json"
    issued_tokens = iter(("revoked-token", "replacement-token"))
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == AUTH_REVOKE_PATH:
            return httpx.Response(200, json={"rsp_cd": "00000"})
        return httpx.Response(
            200,
            json={"access_token": next(issued_tokens), "expires_in": 3_600},
        )

    transport = httpx.MockTransport(handler)
    first = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )
    assert await first.get_access_token() == "revoked-token"
    assert await first.revoke_access_token(access_token="revoked-token") == {
        "rsp_cd": "00000"
    }
    assert not cache_path.exists()

    second = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )
    assert await second.get_access_token() == "replacement-token"
    assert paths == [AUTH_TOKEN_PATH, AUTH_REVOKE_PATH, AUTH_TOKEN_PATH]


@pytest.mark.asyncio
async def test_cache_uses_secure_permissions_and_unique_atomic_replacements(
    armed: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / ".nhplug" / "token_cache.json"
    replace_sources: list[Path] = []
    replace_targets: list[Path] = []
    real_replace = auth_module.os.replace

    def record_replace(source: Any, destination: Any) -> None:
        replace_sources.append(Path(source))
        replace_targets.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(auth_module.os, "replace", record_replace)
    transport, seen = _transport(
        {"access_token": "permission-test-token", "expires_in": 3_600}
    )
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        cache_path=cache_path,
        transport=transport,
    )

    await client.get_access_token()
    await client.get_access_token(force_refresh=True)

    assert len(seen) == 2
    assert len(replace_sources) == 2
    assert replace_sources[0] != replace_sources[1]
    assert all(source.parent == cache_path.parent for source in replace_sources)
    assert replace_targets == [cache_path, cache_path]
    assert all(not source.exists() for source in replace_sources)
    if os.name != "nt":
        assert stat.S_IMODE(cache_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_auth_repr_error_and_logs_never_render_token_or_secret(
    armed: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_token = "TOKEN_MUST_NOT_APPEAR_123"
    sentinel_secret = "SECRET_MUST_NOT_APPEAR_456"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text=f"{sentinel_token}:{sentinel_secret}",
        )

    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret=sentinel_secret,
        cache_path=tmp_path / ".nhplug" / "token_cache.json",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError) as error:
        await client.get_access_token()

    rendered = f"{client!r}\n{error.value!r}\n{error.value}\n{caplog.text}"
    assert sentinel_token not in rendered
    assert sentinel_secret not in rendered
