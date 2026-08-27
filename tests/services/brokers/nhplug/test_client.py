"""Offline transport tests for NHPLUG mock read-only host and account guards."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.client import (
    ACCOUNT_INFO_PATH,
    ALLOWED_READONLY_PATHS,
    BALANCE_PATH,
    MOCK_HOST,
    MOCK_PORT,
    QUOTE_PATH,
    NHPlugMockClient,
)
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockBrokerRejected,
    NHPlugMockConfigurationError,
    NHPlugMockDisabled,
    NHPlugMockEndpointError,
    NHPlugMockReadOnlyEndpointError,
)

pytestmark = pytest.mark.unit


class _TokenProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[tuple[bool, str | None]] = []

    async def __call__(
        self,
        *,
        force_refresh: bool = False,
        failed_token: str | None = None,
    ) -> str:
        self.calls += 1
        self.requests.append((force_refresh, failed_token))
        if force_refresh:
            return "unit-test-refreshed-token"
        return "unit-test-token"


def _account_payload(
    *, account_no: str = "mock-account", account_type: str = "03"
) -> dict[str, Any]:
    return {
        "rsp_cd": "00000",
        "Output_0": [{"acct_no": account_no, "acct_type": account_type}],
    }


def _allowlist(*, account_no: str = "mock-account") -> MockAccountAllowlist:
    return MockAccountAllowlist.from_acctinfo_response(
        payload=_account_payload(account_no=account_no),
        configured_account_no=account_no,
    )


def _client(
    transport: httpx.AsyncBaseTransport,
    token_provider: _TokenProvider | None = None,
) -> tuple[NHPlugMockClient, _TokenProvider]:
    provider = token_provider or _TokenProvider()
    return (
        NHPlugMockClient(
            app_key="test-key",
            app_secret="test-secret",
            token_provider=provider,
            transport=transport,
        ),
        provider,
    )


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


def test_readonly_allowlist_is_exactly_the_three_stage_one_paths() -> None:
    assert ALLOWED_READONLY_PATHS == frozenset(
        {
            "/n2/acctinfo",
            "/krstock/inquiry/v1/balance",
            "/krstock/quote/v1/currentPrice",
        }
    )


def test_live_or_wrong_host_is_rejected_at_construction() -> None:
    async def token(
        *,
        force_refresh: bool = False,
        failed_token: str | None = None,
    ) -> str:
        del force_refresh, failed_token
        return "unit-test-token"

    with pytest.raises(NHPlugMockEndpointError):
        NHPlugMockClient(
            app_key="test-key",
            app_secret="test-secret",
            token_provider=token,
            base_url="https://api.nhplug.com:8443",
        )


@pytest.mark.asyncio
async def test_disabled_gate_fails_closed_before_token_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_account_payload())

    client, token_provider = _client(httpx.MockTransport(handler))
    with pytest.raises(NHPlugMockDisabled):
        await client.list_accounts()

    assert token_provider.calls == 0
    assert calls == []


@pytest.mark.asyncio
async def test_non_allowlisted_path_is_refused_before_token_or_transport(
    armed: None,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"rsp_cd": "00000"})

    client, token_provider = _client(httpx.MockTransport(handler))
    with pytest.raises(NHPlugMockReadOnlyEndpointError):
        await client._post_readonly(path="/outside/read-only-allowlist", input_0={})

    assert token_provider.calls == 0
    assert calls == []


@pytest.mark.asyncio
async def test_account_scoped_dispatch_requires_a_bound_allowlist_before_token_or_transport(
    armed: None,
) -> None:
    """A direct dispatcher call cannot skip the broker-derived account boundary."""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"rsp_cd": "00166", "Output_0": {}})

    client, token_provider = _client(httpx.MockTransport(handler))
    with pytest.raises(NHPlugMockConfigurationError, match="broker-verified"):
        await client._post_readonly(
            path=BALANCE_PATH,
            input_0={"act_no": "LIVE-01-ACCOUNT"},
            act_no="LIVE-01-ACCOUNT",
        )

    assert token_provider.calls == 0
    assert calls == []


@pytest.mark.asyncio
async def test_mock_account_is_verified_then_balance_is_sent_to_only_mock_host(
    armed: None,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == ACCOUNT_INFO_PATH:
            return httpx.Response(200, json=_account_payload())
        return httpx.Response(200, json={"rsp_cd": "00166", "Output_0": {}})

    client, _ = _client(httpx.MockTransport(handler))
    account_payload = await client.list_accounts()
    allowlist = MockAccountAllowlist.from_acctinfo_response(
        payload=account_payload,
        configured_account_no="mock-account",
    )
    client.bind_account_allowlist(allowlist)
    result = await client.fetch_balance(act_no="mock-account")

    assert result["rsp_cd"] == "00166"
    assert [request.url.path for request in seen] == [ACCOUNT_INFO_PATH, BALANCE_PATH]
    balance_request = seen[-1]
    assert balance_request.url.host == MOCK_HOST
    assert balance_request.url.port == MOCK_PORT
    assert json.loads(balance_request.content) == {
        "Input_0": {
            "act_no": "mock-account",
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
        }
    }


@pytest.mark.parametrize("account_type", ("01", "02"))
def test_live_account_type_is_rejected_even_on_the_valid_mock_host(
    account_type: str,
) -> None:
    with pytest.raises(NHPlugMockAccountRejected):
        MockAccountAllowlist.from_acctinfo_response(
            payload=_account_payload(
                account_no="live-account", account_type=account_type
            ),
            configured_account_no="live-account",
        )


@pytest.mark.asyncio
async def test_account_recheck_immediately_before_send_blocks_a_changed_allowlist(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"rsp_cd": "00166", "Output_0": {}})

    client, token_provider = _client(httpx.MockTransport(handler))
    allowlist = _allowlist()
    client.bind_account_allowlist(allowlist)
    allowlist_calls = 0

    def assert_allowed_after_request_build(
        self: MockAccountAllowlist, act_no: str
    ) -> None:
        nonlocal allowlist_calls
        assert self is allowlist
        assert act_no == "mock-account"
        allowlist_calls += 1
        if allowlist_calls == 2:
            raise NHPlugMockAccountRejected("account changed after request build")

    monkeypatch.setattr(
        MockAccountAllowlist, "assert_allowed", assert_allowed_after_request_build
    )
    with pytest.raises(NHPlugMockAccountRejected):
        await client.fetch_balance(act_no="mock-account")

    assert allowlist_calls == 2
    assert token_provider.calls == 1
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered_url",
    (
        "https://unexpected.example.invalid:8443/n2/acctinfo",
        "https://moapi.nhplug.com:9443/n2/acctinfo",
        "http://moapi.nhplug.com:8443/n2/acctinfo",
    ),
)
async def test_resolved_host_or_port_mismatch_is_rejected_before_send(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
    tampered_url: str,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_account_payload())

    original_build_request = httpx.AsyncClient.build_request

    def build_wrong_port(
        self: httpx.AsyncClient, *args: Any, **kwargs: Any
    ) -> httpx.Request:
        request = original_build_request(self, *args, **kwargs)
        return httpx.Request(
            request.method,
            tampered_url,
            headers=request.headers,
            content=request.content,
        )

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build_wrong_port)
    client, token_provider = _client(httpx.MockTransport(handler))
    with pytest.raises(NHPlugMockEndpointError):
        await client.list_accounts()

    assert token_provider.calls == 1
    assert calls == []


@pytest.mark.asyncio
async def test_valid_mock_account_cannot_compensate_for_a_host_mismatch(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two discriminators are independent: a valid 03 still cannot send."""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"rsp_cd": "00166", "Output_0": {}})

    original_build_request = httpx.AsyncClient.build_request

    def build_production_host_request(
        self: httpx.AsyncClient, *args: Any, **kwargs: Any
    ) -> httpx.Request:
        request = original_build_request(self, *args, **kwargs)
        return httpx.Request(
            request.method,
            "https://api.nhplug.com:8443/krstock/inquiry/v1/balance",
            headers=request.headers,
            content=request.content,
        )

    monkeypatch.setattr(
        httpx.AsyncClient, "build_request", build_production_host_request
    )
    client, token_provider = _client(httpx.MockTransport(handler))
    client.bind_account_allowlist(_allowlist())

    with pytest.raises(NHPlugMockEndpointError):
        await client.fetch_balance(act_no="mock-account")

    assert token_provider.calls == 1
    assert calls == []


def test_conflicting_account_types_for_one_number_fail_closed() -> None:
    with pytest.raises(NHPlugMockAccountRejected):
        MockAccountAllowlist.from_acctinfo_response(
            payload={
                "rsp_cd": "00000",
                "Output_0": [
                    {"acct_no": "ambiguous-account", "acct_type": "01"},
                    {"acct_no": "ambiguous-account", "acct_type": "03"},
                ],
            },
            configured_account_no="ambiguous-account",
        )


@pytest.mark.asyncio
async def test_redirect_is_not_followed(armed: None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://unexpected.example.invalid/n2/acctinfo"},
        )

    client, _ = _client(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await client.list_accounts()

    assert len(seen) == 1
    assert seen[0].url.host == MOCK_HOST
    assert seen[0].url.port == MOCK_PORT


@pytest.mark.asyncio
async def test_quote_requires_the_same_verified_account_and_exact_symbol(
    armed: None,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rsp_cd": "00000", "Output_0": {}})

    client, _ = _client(httpx.MockTransport(handler))
    client.bind_account_allowlist(_allowlist())
    await client.fetch_quote(
        symbol="005930",
        market="KRX",
    )

    assert len(seen) == 1
    assert seen[0].url.path == QUOTE_PATH
    assert json.loads(seen[0].content) == {
        "Input_0": {"iem_cd": "005930", "market_cd": "KRX"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_response",
    (
        httpx.Response(401, json={"rsp_cd": "unauthorized"}),
        httpx.Response(200, json={"rsp_cd": "IGW40043"}),
    ),
)
async def test_invalid_token_response_refreshes_once_and_retries_same_request(
    armed: None,
    first_response: httpx.Response,
) -> None:
    seen: list[httpx.Request] = []
    responses = iter(
        (
            first_response,
            httpx.Response(200, json=_account_payload()),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)

    assert (await client.list_accounts())["rsp_cd"] == "00000"
    assert provider.requests == [
        (False, None),
        (True, "unit-test-token"),
    ]
    assert len(seen) == 2
    assert seen[0].url == seen[1].url
    assert seen[0].content == seen[1].content
    assert seen[0].headers["authorization"] == "Bearer unit-test-token"
    assert seen[1].headers["authorization"] == "Bearer unit-test-refreshed-token"


@pytest.mark.asyncio
async def test_http_429_never_refreshes_the_access_token(armed: None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, json={"rsp_cd": "IGW42902"})

    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)

    with pytest.raises(httpx.HTTPStatusError) as error:
        await client.list_accounts()

    assert error.value.response.status_code == 429
    assert provider.requests == [(False, None)]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_body_igw42902_never_refreshes_the_access_token(
    armed: None,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rsp_cd": "IGW42902"})

    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)

    with pytest.raises(NHPlugMockBrokerRejected) as error:
        await client.list_accounts()

    assert error.value.response_code == "IGW42902"
    assert provider.requests == [(False, None)]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_second_401_stops_without_a_third_send_or_refresh(
    armed: None,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"rsp_cd": "unauthorized"})

    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)

    with pytest.raises(httpx.HTTPStatusError) as error:
        await client.list_accounts()

    assert error.value.response.status_code == 401
    assert provider.requests == [
        (False, None),
        (True, "unit-test-token"),
    ]
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered_retry_url",
    (
        "https://unexpected.example.invalid:8443/n2/acctinfo",
        "https://moapi.nhplug.com:8443/outside/read-only-allowlist",
    ),
)
async def test_retry_rechecks_resolved_host_and_path_before_second_send(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
    tampered_retry_url: str,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"rsp_cd": "unauthorized"})

    original_build_request = httpx.AsyncClient.build_request
    build_count = 0

    def tamper_second_request(
        self: httpx.AsyncClient,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Request:
        nonlocal build_count
        request = original_build_request(self, *args, **kwargs)
        build_count += 1
        if build_count == 1:
            return request
        return httpx.Request(
            request.method,
            tampered_retry_url,
            headers=request.headers,
            content=request.content,
        )

    monkeypatch.setattr(
        httpx.AsyncClient,
        "build_request",
        tamper_second_request,
    )
    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)

    with pytest.raises((NHPlugMockEndpointError, NHPlugMockReadOnlyEndpointError)):
        await client.list_accounts()

    assert build_count == 2
    assert provider.requests == [
        (False, None),
        (True, "unit-test-token"),
    ]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_retry_rechecks_account_allowlist_before_second_send(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"rsp_cd": "unauthorized"})

    provider = _TokenProvider()
    client, _ = _client(httpx.MockTransport(handler), provider)
    allowlist = _allowlist()
    client.bind_account_allowlist(allowlist)
    allowlist_calls = 0

    def reject_before_retry_send(
        self: MockAccountAllowlist,
        act_no: str,
    ) -> None:
        nonlocal allowlist_calls
        assert self is allowlist
        assert act_no == "mock-account"
        allowlist_calls += 1
        if allowlist_calls == 3:
            raise NHPlugMockAccountRejected("account changed before retry dispatch")

    monkeypatch.setattr(
        MockAccountAllowlist,
        "assert_allowed",
        reject_before_retry_send,
    )

    with pytest.raises(NHPlugMockAccountRejected):
        await client.fetch_balance(act_no="mock-account")

    assert allowlist_calls == 3
    assert provider.requests == [
        (False, None),
        (True, "unit-test-token"),
    ]
    assert len(seen) == 1
