"""AI route catalog, 정책 검증, 그리고 실행 경로에 정책이 실제로 적용되는지.

DB가 필요 없는 순수 계약만 다룬다. singleton 저장/동시성/관리자 API는
``tests/test_admin_ai_routes.py``가 담당한다.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, ModuleType

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.extensions.kasset.ai import factory
from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.factory import (
    build_api_provider_chain,
    build_model_router,
    build_summary_json_client,
)
from app.extensions.kasset.ai.model_router import AnalysisKind
from app.extensions.kasset.ai.runtime_config import (
    AI_APP_LANES,
    AI_REVIEW_LANES,
    DEFAULT_ROUTE_POLICY,
    LANE_ROUTE_IDS,
    REASON_NO_ACTIVE_ROUTE,
    REASON_POLICY_UNREADABLE,
    REASON_ROUTES_UNAVAILABLE,
    AiLane,
    AiRouteId,
    AiRoutePolicyError,
    AiRuntimeSnapshot,
    build_ai_availability,
    build_ai_route_catalog,
    default_snapshot,
    fail_closed_snapshot,
    freeze_route_policy,
    lane_telemetry_covered,
    normalize_route_policy,
    serialize_route_policy,
)

_SECRETS = (
    "direct-secret-key",
    "openrouter-secret-key",
    "mcp-secret-token",
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[4]
    / "alembic"
    / "versions"
    / "20260830_kasset_ai_runtime_config.py"
)


def _load_migration_module() -> ModuleType:
    """숫자로 시작하는 모듈명이라 파일 경로로 직접 읽는다."""

    spec = importlib.util.spec_from_file_location(
        "kasset_ai_runtime_config_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 provider slot이 채워진 상태."""

    monkeypatch.setattr(settings, "KASSET_AI_API_BASE_URL", "https://direct.invalid/v1")
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr(_SECRETS[0]))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_LUNA", "model-luna")
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "model-terra")
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_SOL", "model-sol")
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_BASE_URL", "https://router.invalid/api/v1"
    )
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr(_SECRETS[1])
    )
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_FLASH", "model-flash")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "model-pro")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "https://mcp.invalid")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOKEN", SecretStr(_SECRETS[2]))
    monkeypatch.setattr(settings, "KASSET_AI_MCP_TOOL_NAME", "review_market")
    monkeypatch.setattr(
        settings, "KASSET_AI_SUBSCRIPTION_CMD", "codex exec --sandbox 'read only' -"
    )


def _snapshot(lanes: dict[AiLane, tuple[AiRouteId, ...]]) -> AiRuntimeSnapshot:
    merged = {lane: lanes.get(lane, ()) for lane in AiLane}
    return AiRuntimeSnapshot(
        revision=7,
        updated_at=None,
        updated_by_user_id=1,
        source="persisted",
        lanes=freeze_route_policy(merged),
    )


# ---- 유효 AI 가용성 ----


def test_availability_is_true_when_direct_api_works_and_the_relay_is_absent(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """운영에서 실제로 벌어진 상황: MCP만 미설정, direct/OpenRouter는 정상.

    선택 사항인 relay 하나가 빠졌다고 AI 전체를 사용 불가로 보고하면 안 된다.
    """

    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")

    availability = build_ai_availability(default_snapshot(), build_ai_route_catalog())

    assert availability.available is True
    assert availability.configured is True
    assert availability.unavailable_reason is None
    assert availability.message == "AI를 사용할 수 있습니다."
    assert availability.usable_lanes == frozenset(AI_APP_LANES)
    assert availability.any_lane_usable(AI_REVIEW_LANES) is True


def test_availability_ignores_the_compat_skill_lane(configured_ai) -> None:
    """``compat_skill``은 운영 caller가 없으므로 판정에 들어오지 않는다."""

    availability = build_ai_availability(default_snapshot(), build_ai_route_catalog())

    assert AiLane.COMPAT_SKILL not in availability.usable_lanes


def test_availability_fails_closed_when_no_route_is_usable(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")

    availability = build_ai_availability(default_snapshot(), build_ai_route_catalog())

    assert availability.available is False
    assert availability.configured is True
    assert availability.unavailable_reason == REASON_ROUTES_UNAVAILABLE
    assert availability.usable_lanes == frozenset()
    assert availability.any_lane_usable(AI_REVIEW_LANES) is False
    assert availability.message.startswith("설정된 AI 경로를 지금 사용할 수 없습니다.")
    assert "서버에 API 키가 설정되어 있지 않습니다." in availability.message


@pytest.mark.parametrize("source", ["invalid", "unavailable"])
def test_availability_fails_closed_when_the_policy_cannot_be_trusted(
    configured_ai, source: str
) -> None:
    availability = build_ai_availability(
        fail_closed_snapshot(source),  # type: ignore[arg-type]
        build_ai_route_catalog(),
    )

    assert availability.available is False
    assert availability.configured is False
    assert availability.unavailable_reason == REASON_POLICY_UNREADABLE
    assert availability.message == (
        "AI 경로 설정을 읽을 수 없어 AI 기능을 사용할 수 없습니다."
    )


def test_availability_reports_an_explicitly_disabled_policy_separately(
    configured_ai,
) -> None:
    """빈 lane은 손상이 아니라 운영자의 명시적 비활성화다. 사유를 구분해서 말한다."""

    availability = build_ai_availability(_snapshot({}), build_ai_route_catalog())

    assert availability.available is False
    assert availability.configured is False
    assert availability.unavailable_reason == REASON_NO_ACTIVE_ROUTE
    assert availability.message == "사용할 AI 경로가 설정되어 있지 않습니다."


def test_availability_names_the_lanes_it_cannot_serve(configured_ai) -> None:
    """요약만 살아 있으면 "사용 가능"이지만 무엇이 빠졌는지 함께 말한다."""

    availability = build_ai_availability(
        _snapshot({AiLane.SUMMARY_LUNA: (AiRouteId.DIRECT_LUNA,)}),
        build_ai_route_catalog(),
    )

    assert availability.available is True
    assert availability.usable_lanes == frozenset({AiLane.SUMMARY_LUNA})
    assert availability.any_lane_usable(AI_REVIEW_LANES) is False
    assert availability.message.startswith("AI 일부 기능만 사용할 수 있습니다.")
    assert "1차 검토 (빠른 판단)" in availability.message


def test_availability_never_leaks_credentials_or_urls(configured_ai) -> None:
    catalog = build_ai_route_catalog()
    snapshots = (
        default_snapshot(),
        _snapshot({}),
        fail_closed_snapshot("invalid"),
    )
    messages = [build_ai_availability(item, catalog).message for item in snapshots]

    blob = " ".join(messages)
    for secret in _SECRETS:
        assert secret not in blob
    assert "invalid" not in blob
    assert "model-" not in blob


# ---- catalog: secret-free 계약 ----


def test_catalog_never_serializes_credentials_or_urls(configured_ai) -> None:
    catalog = build_ai_route_catalog()

    serialized = json.dumps(
        [
            {
                "routeId": entry.route_id.value,
                "provider": entry.provider,
                "label": entry.label,
                "model": entry.model,
                "configured": entry.configured,
                "available": entry.available,
                "unavailableReason": entry.unavailable_reason,
            }
            for entry in catalog.values()
        ],
        ensure_ascii=False,
    )

    for secret in _SECRETS:
        assert secret not in serialized
    assert "direct.invalid" not in serialized
    assert "router.invalid" not in serialized
    assert "mcp.invalid" not in serialized
    assert "codex" not in serialized
    assert "read only" not in serialized


def test_catalog_exposes_resolved_models_not_stored_strings(configured_ai) -> None:
    catalog = build_ai_route_catalog()

    assert catalog[AiRouteId.DIRECT_LUNA].model == "model-luna"
    assert catalog[AiRouteId.DIRECT_TERRA].model == "model-terra"
    assert catalog[AiRouteId.DIRECT_SOL].model == "model-sol"
    assert catalog[AiRouteId.OPENROUTER_FLASH].model == "model-flash"
    assert catalog[AiRouteId.OPENROUTER_PRO].model == "model-pro"
    # 원장이 기록하는 표현과 동일해야 latest success를 맞출 수 있다.
    assert catalog[AiRouteId.MCP_TOOL].model == "tool:review_market"
    assert catalog[AiRouteId.SUBSCRIPTION_CLI].model == "subscription-agent"


def test_catalog_marks_routes_unavailable_without_credentials(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")
    monkeypatch.setattr(settings, "KASSET_AI_SUBSCRIPTION_CMD", "  ")

    catalog = build_ai_route_catalog()

    direct = catalog[AiRouteId.DIRECT_TERRA]
    assert direct.configured is True
    assert direct.available is False
    assert direct.unavailable_reason == "missing_api_key"

    pro = catalog[AiRouteId.OPENROUTER_PRO]
    assert pro.configured is False
    assert pro.unavailable_reason == "missing_model"

    assert catalog[AiRouteId.MCP_TOOL].unavailable_reason == "missing_mcp_url"
    assert (
        catalog[AiRouteId.SUBSCRIPTION_CLI].unavailable_reason
        == "missing_subscription_command"
    )
    # 사용 가능한 route가 남아 있어도 credential 없는 route는 살아나지 않는다.
    assert catalog[AiRouteId.OPENROUTER_FLASH].available is False


def test_compat_lane_is_not_covered_by_the_call_ledger() -> None:
    assert lane_telemetry_covered(AiLane.SUMMARY_LUNA) is True
    assert lane_telemetry_covered(AiLane.REVIEW_TERRA) is True
    assert lane_telemetry_covered(AiLane.COMPAT_SKILL) is False


# ---- 기본 정책: 마이그레이션 직후 순서 보존 ----


def test_default_policy_matches_the_migration_literal() -> None:
    """마이그레이션이 삽입하는 리터럴과 코드 기본값이 어긋나지 않게 고정한다.

    마이그레이션은 상수를 import하지 않고 리터럴을 적는다(적용된 이력의 의미가
    나중에 조용히 바뀌면 안 되므로). 그래서 드리프트는 이 테스트가 잡는다.
    """

    module = _load_migration_module()

    assert module.DEFAULT_ROUTE_POLICY == serialize_route_policy(DEFAULT_ROUTE_POLICY)
    assert module.revision == "20260830_ai_runtime_config"
    assert module.down_revision == "20260830_admin_recovery"


def test_default_policy_only_uses_lane_compatible_routes() -> None:
    assert set(DEFAULT_ROUTE_POLICY) == set(AiLane)
    for lane, routes in DEFAULT_ROUTE_POLICY.items():
        assert len(set(routes)) == len(routes)
        for route_id in routes:
            assert route_id in LANE_ROUTE_IDS[lane]


def test_default_snapshot_is_read_only_and_env_equivalent() -> None:
    snapshot = default_snapshot()

    assert snapshot.source == "default"
    assert snapshot.revision == 0
    assert snapshot.routes(AiLane.REVIEW_TERRA) == (
        AiRouteId.MCP_TOOL,
        AiRouteId.DIRECT_TERRA,
        AiRouteId.OPENROUTER_PRO,
    )
    with pytest.raises(TypeError):
        snapshot.lanes[AiLane.REVIEW_TERRA] = ()  # type: ignore[index]


# ---- 정책 검증 ----


def _valid_payload() -> dict[str, list[str]]:
    return serialize_route_policy(DEFAULT_ROUTE_POLICY)


def test_normalize_accepts_the_default_payload() -> None:
    normalized = normalize_route_policy(_valid_payload())

    assert normalized == dict(DEFAULT_ROUTE_POLICY)


def test_normalize_accepts_mcp_only_summary_policy() -> None:
    payload = _valid_payload()
    payload["summary_luna"] = ["mcp_tool"]

    normalized = normalize_route_policy(payload)

    assert normalized[AiLane.SUMMARY_LUNA] == (AiRouteId.MCP_TOOL,)


def test_normalize_rejects_unknown_route() -> None:
    payload = _valid_payload()
    payload["review_terra"] = ["direct_terra", "gpt-4o-mini"]

    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)

    assert exc.value.code == "unknown_route"


def test_normalize_rejects_arbitrary_model_or_url_as_route() -> None:
    for injected in (
        "https://evil.invalid/v1",
        "sk-live-000",
        "codex exec",
        "direct-api",
    ):
        payload = _valid_payload()
        payload["summary_luna"] = [injected]
        with pytest.raises(AiRoutePolicyError) as exc:
            normalize_route_policy(payload)
        assert exc.value.code == "unknown_route"


def test_normalize_rejects_cross_lane_route() -> None:
    payload = _valid_payload()
    payload["summary_luna"] = ["direct_luna", "openrouter_pro"]

    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)

    assert exc.value.code == "lane_route_mismatch"


def test_normalize_rejects_duplicate_route() -> None:
    payload = _valid_payload()
    payload["review_sol"] = ["direct_sol", "direct_sol"]

    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)

    assert exc.value.code == "duplicate_route"


def test_normalize_rejects_missing_and_unknown_lane() -> None:
    payload = _valid_payload()
    del payload["review_luna"]
    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)
    assert exc.value.code == "lane_missing"

    payload = _valid_payload()
    payload["review_pluto"] = ["direct_luna"]
    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)
    assert exc.value.code == "unknown_lane"


def test_normalize_rejects_non_list_lane_value() -> None:
    payload = _valid_payload()
    payload["summary_luna"] = "direct_luna"  # type: ignore[assignment]

    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload)

    assert exc.value.code == "route_list_shape"


def test_normalize_allows_empty_lane_as_explicit_disable() -> None:
    payload = _valid_payload()
    payload["compat_skill"] = []

    normalized = normalize_route_policy(payload)

    assert normalized[AiLane.COMPAT_SKILL] == ()


def test_normalize_rejects_unavailable_route_only_on_the_write_path(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")
    payload = _valid_payload()

    # 읽기 경로: credential이 빠졌다는 이유로 저장된 정책을 손상 처리하지 않는다.
    assert normalize_route_policy(payload)[AiLane.REVIEW_TERRA][0] is AiRouteId.MCP_TOOL

    with pytest.raises(AiRoutePolicyError) as exc:
        normalize_route_policy(payload, catalog=build_ai_route_catalog())
    assert exc.value.code == "route_unavailable"


def test_fail_closed_snapshot_blocks_every_lane() -> None:
    snapshot = fail_closed_snapshot("invalid", revision=4)

    assert snapshot.source == "invalid"
    assert snapshot.revision == 4
    for lane in AiLane:
        assert snapshot.routes(lane) == ()


# ---- 검토 lane: 정책 순서가 실제 호출에 반영되는지 ----


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(self._payload)}
                        ],
                    }
                ]
            },
        )


_TERMINAL_VERDICT: dict[str, object] = {
    "action": "HOLD",
    "confidence": 0.95,
    "risk": "LOW",
    "bullish_score": 50,
    "bearish_score": 50,
    "escalate": False,
    "rationale_tags": ["momentum_stable"],
}


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
async def test_review_lane_policy_selects_openrouter_first(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정책이 OpenRouter를 1순위로 두면 direct API보다 먼저 호출된다."""

    transport = _RecordingTransport(_TERMINAL_VERDICT)
    _install_transport(monkeypatch, transport)
    snapshot = _snapshot(
        {
            AiLane.REVIEW_LUNA: (
                AiRouteId.OPENROUTER_FLASH,
                AiRouteId.DIRECT_LUNA,
            ),
        }
    )

    router = build_model_router(snapshot=snapshot)
    verdict = await router.analyze(AnalysisKind.NEWS_TRIAGE, {"headline": "샘플"})

    assert verdict.provider == "openrouter"
    assert verdict.model_id == "model-flash"
    assert len(transport.requests) == 1
    assert transport.requests[0].url.host == "router.invalid"


@pytest.mark.asyncio
async def test_review_lane_policy_can_drop_mcp_without_touching_settings(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP가 설정되어 있어도 정책에서 빠지면 호출되지 않는다."""

    transport = _RecordingTransport(_TERMINAL_VERDICT)
    _install_transport(monkeypatch, transport)
    snapshot = _snapshot({AiLane.REVIEW_SOL: (AiRouteId.DIRECT_SOL,)})

    router = build_model_router(snapshot=snapshot)
    verdict = await router.analyze(AnalysisKind.CRITICAL_REVIEW, {"headline": "샘플"})

    assert verdict.provider == "direct-api"
    assert [request.url.host for request in transport.requests] == ["direct.invalid"]


@pytest.mark.asyncio
async def test_empty_review_lane_fails_closed_without_env_fallback(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _RecordingTransport(_TERMINAL_VERDICT)
    _install_transport(monkeypatch, transport)
    snapshot = _snapshot({})

    router = build_model_router(snapshot=snapshot)

    with pytest.raises(AiProviderUnavailable):
        await router.analyze(AnalysisKind.NEWS_TRIAGE, {"headline": "샘플"})
    assert transport.requests == []


@pytest.mark.asyncio
async def test_review_lane_skips_unavailable_route_and_keeps_next(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정책에 남아 있어도 credential이 없는 route는 조용히 빠진다."""

    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    transport = _RecordingTransport(_TERMINAL_VERDICT)
    _install_transport(monkeypatch, transport)
    snapshot = _snapshot(
        {
            AiLane.REVIEW_LUNA: (
                AiRouteId.DIRECT_LUNA,
                AiRouteId.OPENROUTER_FLASH,
            ),
        }
    )

    router = build_model_router(snapshot=snapshot)
    verdict = await router.analyze(AnalysisKind.NEWS_TRIAGE, {"headline": "샘플"})

    assert verdict.provider == "openrouter"


def test_model_router_without_snapshot_keeps_env_equivalent_order(
    configured_ai,
) -> None:
    router = build_model_router()

    assert router._route_policy is DEFAULT_ROUTE_POLICY


# ---- 요약 lane ----


def test_summary_lane_policy_orders_routes(configured_ai) -> None:
    reversed_snapshot = _snapshot(
        {
            AiLane.SUMMARY_LUNA: (
                AiRouteId.OPENROUTER_FLASH,
                AiRouteId.DIRECT_LUNA,
            ),
        }
    )

    client = build_summary_json_client(
        name="news-summary",
        direct_model="model-luna",
        fallback_model="model-flash",
        snapshot=reversed_snapshot,
    )

    assert client is not None
    assert [route.client.name for route in client._routes] == [
        "openrouter",
        "direct-api",
    ]


def test_summary_lane_default_keeps_direct_first(configured_ai) -> None:
    client = build_summary_json_client(
        name="news-summary",
        direct_model="model-luna",
        fallback_model="model-flash",
    )

    assert client is not None
    assert [route.client.name for route in client._routes] == [
        "direct-api",
        "openrouter",
    ]


def test_empty_summary_lane_reports_unconfigured(configured_ai) -> None:
    client = build_summary_json_client(
        name="news-summary",
        direct_model="model-luna",
        fallback_model="model-flash",
        snapshot=_snapshot({}),
    )

    assert client is None


# ---- 호환 skill lane ----


def test_compat_lane_policy_orders_api_chain(configured_ai) -> None:
    chain = build_api_provider_chain(
        snapshot=_snapshot(
            {
                AiLane.COMPAT_SKILL: (
                    AiRouteId.OPENROUTER_PRO,
                    AiRouteId.DIRECT_TERRA,
                ),
            }
        )
    )

    assert chain is not None
    assert [provider.name for provider in chain._providers] == [
        "openrouter",
        "primary-api",
    ]


def test_compat_lane_default_keeps_terra_before_openrouter(configured_ai) -> None:
    chain = build_api_provider_chain()

    assert chain is not None
    assert [provider.name for provider in chain._providers] == [
        "primary-api",
        "openrouter",
    ]


def test_compat_lane_without_subscription_route_disables_the_bridge(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정책에서 구독 CLI를 빼면 명령이 설정되어 있어도 bridge를 만들지 않는다."""

    def unexpected_builder(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("정책에서 제외된 구독 CLI를 만들면 안 된다")

    monkeypatch.setattr(factory, "build_cli_invoker", unexpected_builder)

    router = factory.build_ai_provider_router(
        snapshot=_snapshot(
            {AiLane.COMPAT_SKILL: (AiRouteId.DIRECT_TERRA, AiRouteId.OPENROUTER_PRO)}
        )
    )

    assert router._subscription._invoke_agent is None


def test_compat_lane_default_still_builds_the_configured_bridge(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], float]] = []

    def fake_build_cli_invoker(argv, timeout):  # type: ignore[no-untyped-def]
        calls.append((list(argv), timeout))
        return lambda *args, **kwargs: None

    monkeypatch.setattr(factory, "build_cli_invoker", fake_build_cli_invoker)

    factory.build_ai_provider_router()

    assert calls
    assert calls[0][0][0] == "codex"


def test_serialize_route_policy_stores_only_route_ids() -> None:
    serialized = serialize_route_policy(DEFAULT_ROUTE_POLICY)

    assert serialized == {
        "summary_luna": ["direct_luna", "openrouter_flash"],
        "review_luna": ["mcp_tool", "direct_luna", "openrouter_flash"],
        "review_terra": ["mcp_tool", "direct_terra", "openrouter_pro"],
        "review_sol": ["mcp_tool", "direct_sol", "openrouter_pro"],
        "compat_skill": ["subscription_cli", "direct_terra", "openrouter_pro"],
    }
    blob = json.dumps(serialized)
    assert "http" not in blob
    assert "model-" not in blob


def test_freeze_route_policy_returns_a_read_only_mapping() -> None:
    frozen = freeze_route_policy({AiLane.SUMMARY_LUNA: [AiRouteId.DIRECT_LUNA]})

    assert isinstance(frozen, MappingProxyType)
    assert frozen[AiLane.SUMMARY_LUNA] == (AiRouteId.DIRECT_LUNA,)


# ---- 한 cycle/batch당 snapshot 하나 ----


def _reversed_summary_snapshot() -> AiRuntimeSnapshot:
    return _snapshot(
        {
            **dict(DEFAULT_ROUTE_POLICY),
            AiLane.SUMMARY_LUNA: (
                AiRouteId.OPENROUTER_FLASH,
                AiRouteId.DIRECT_LUNA,
            ),
        }
    )


@pytest.mark.asyncio
async def test_news_batch_reads_one_snapshot_and_applies_it(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import news_summary_service

    reads: list[object] = []

    async def counting_snapshot(db):  # type: ignore[no-untyped-def]
        reads.append(db)
        return _reversed_summary_snapshot()

    captured: list[object] = []

    async def fake_run_batch(db, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs["generator"])
        return "batch-done"

    monkeypatch.setattr(
        news_summary_service, "get_ai_runtime_snapshot", counting_snapshot
    )
    monkeypatch.setattr(news_summary_service, "_run_batch", fake_run_batch)

    result = await news_summary_service.summarize_pending_news(object())

    assert result == "batch-done"
    # 기사마다 다시 읽지 않는다.
    assert len(reads) == 1
    assert [route.client.name for route in captured[0]._client._routes] == [
        "openrouter",
        "direct-api",
    ]


@pytest.mark.asyncio
async def test_news_batch_with_injected_generator_reads_no_policy(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    """호출자가 generator를 넘기면 정책 조회 자체가 없다."""

    from app.services import news_summary_service

    async def forbidden_snapshot(db):  # type: ignore[no-untyped-def]
        raise AssertionError("주입된 generator에는 정책을 읽지 않아야 한다")

    async def fake_run_batch(db, **kwargs):  # type: ignore[no-untyped-def]
        return "batch-done"

    monkeypatch.setattr(
        news_summary_service, "get_ai_runtime_snapshot", forbidden_snapshot
    )
    monkeypatch.setattr(news_summary_service, "_run_batch", fake_run_batch)

    result = await news_summary_service.summarize_pending_news(
        object(), generator=object()
    )

    assert result == "batch-done"


@pytest.mark.asyncio
async def test_disclosure_batch_reads_one_snapshot_and_applies_it(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.disclosures import summary_service

    reads: list[object] = []

    async def counting_snapshot(db):  # type: ignore[no-untyped-def]
        reads.append(db)
        return _reversed_summary_snapshot()

    captured: list[object] = []

    async def fake_run_batch(db, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs["generator"])
        return "batch-done"

    monkeypatch.setattr(summary_service, "get_ai_runtime_snapshot", counting_snapshot)
    monkeypatch.setattr(summary_service, "_run_batch", fake_run_batch)

    result = await summary_service.summarize_pending_disclosures(
        object(), fetcher=object()
    )

    assert result == "batch-done"
    assert len(reads) == 1
    assert [route.client.name for route in captured[0]._client._routes] == [
        "openrouter",
        "direct-api",
    ]


@pytest.mark.asyncio
async def test_automation_cycle_reads_one_snapshot_and_passes_it_to_the_router(
    configured_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    monkeypatch.setattr(
        vertical_slice,
        "is_market_open",
        lambda market, *, now=None: market == "kr",
    )

    session = AsyncMock()
    owner_result = MagicMock()
    owner_result.all.return_value = []
    session.scalars = AsyncMock(return_value=owner_result)

    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr(vertical_slice, "_session", fake_session)

    expected = _reversed_summary_snapshot()
    reads: list[object] = []

    async def counting_snapshot(db):  # type: ignore[no-untyped-def]
        reads.append(db)
        return expected

    monkeypatch.setattr(
        runtime_config_service, "get_ai_runtime_snapshot", counting_snapshot
    )

    seen: list[object] = []

    def fake_build_model_router(*, snapshot=None):  # type: ignore[no-untyped-def]
        seen.append(snapshot)
        return None

    monkeypatch.setattr(ai_factory, "build_model_router", fake_build_model_router)

    result = await vertical_slice.run_ai_recommendation_cycle_once()

    assert result["enabled"] is True
    assert result["owners"] == []
    assert len(reads) == 1
    assert seen == [expected]


def _cycle_session(monkeypatch: pytest.MonkeyPatch, owner_ids: list[int]):  # type: ignore[no-untyped-def]
    """owner 목록만 돌려주고 독립 audit 쓰기는 관찰 가능한 mock으로 막는다."""

    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from app.extensions.kasset.automation import vertical_slice

    session = AsyncMock()
    owner_result = MagicMock()
    owner_result.all.return_value = owner_ids
    session.scalars = AsyncMock(return_value=owner_result)

    @asynccontextmanager
    async def fake_session():
        yield session

    audit = AsyncMock()
    monkeypatch.setattr(vertical_slice, "_session", fake_session)
    monkeypatch.setattr(vertical_slice, "record_automation_cycle_event", audit)
    monkeypatch.setattr(
        vertical_slice,
        "is_market_open",
        lambda market, *, now=None: market == "kr",
    )
    return audit


@pytest.mark.asyncio
async def test_automation_cycle_closed_markets_skip_every_owner_before_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    audit = _cycle_session(monkeypatch, [4, 8])
    now = datetime(2026, 12, 25, 15, 0, tzinfo=UTC)
    market_calls: list[tuple[str, datetime | None]] = []

    def closed_market(market: str, *, now: datetime | None = None) -> bool:
        market_calls.append((market, now))
        return False

    def never_build_router(**kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not build router")

    class NeverConstructSlice:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("must not construct candidate slice")

    monkeypatch.setattr(vertical_slice, "is_market_open", closed_market)
    monkeypatch.setattr(
        runtime_config_service,
        "get_ai_runtime_snapshot",
        AsyncMock(side_effect=AssertionError("must not read AI policy")),
    )
    monkeypatch.setattr(ai_factory, "build_model_router", never_build_router)
    monkeypatch.setattr(
        vertical_slice,
        "AIRecommendationVerticalSlice",
        NeverConstructSlice,
    )

    result = await vertical_slice.run_ai_recommendation_cycle_once(now=now)

    assert market_calls == [("kr", now), ("us", now)]
    assert result["openMarkets"] == []
    assert result["candidateCount"] == 0
    assert result["recommendationCount"] == 0
    owners = result["owners"]
    assert isinstance(owners, list)
    assert [owner["ownerUserId"] for owner in owners] == [4, 8]
    assert {owner["skipped"] for owner in owners} == {"no_regular_market_open"}
    assert all(owner["candidateCount"] == 0 for owner in owners)
    assert all(owner["aiReviewedCount"] == 0 for owner in owners)
    traces = [owner["cycleTraceId"] for owner in owners]
    assert len(set(traces)) == 2
    assert audit.await_count == 2
    assert [
        call.kwargs["result"]["cycleTraceId"] for call in audit.await_args_list
    ] == traces


@pytest.mark.asyncio
async def test_automation_cycle_passes_only_the_open_us_market_to_each_owner(
    configured_ai,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    _cycle_session(monkeypatch, [4])
    monkeypatch.setattr(
        vertical_slice,
        "is_market_open",
        lambda market, *, now=None: market == "us",
    )

    async def snapshot(db):  # type: ignore[no-untyped-def]
        return default_snapshot()

    monkeypatch.setattr(runtime_config_service, "get_ai_runtime_snapshot", snapshot)
    monkeypatch.setattr(
        ai_factory,
        "build_model_router",
        lambda *, snapshot=None: object(),
    )
    captured_markets: list[frozenset[str]] = []

    class CapturingSlice:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured_markets.append(kwargs["allowed_markets"])

        async def run_owner(self, owner_user_id: int) -> dict[str, object]:
            return {
                "ownerUserId": owner_user_id,
                "candidateCount": 0,
                "recommendationIds": [],
                "skipped": "screener_candidates_unavailable",
            }

    monkeypatch.setattr(
        vertical_slice,
        "AIRecommendationVerticalSlice",
        CapturingSlice,
    )

    result = await vertical_slice.run_ai_recommendation_cycle_once(
        now=datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
    )

    assert result["openMarkets"] == ["US"]
    assert captured_markets == [frozenset({"US"})]


def test_regular_market_gate_uses_exchange_holidays_and_dst() -> None:
    from app.extensions.kasset.automation import vertical_slice

    christmas = datetime(2026, 12, 25, 15, 0, tzinfo=UTC)
    winter_us_session = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    summer_us_session = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)

    assert vertical_slice._open_regular_markets(now=christmas) == frozenset()
    assert vertical_slice._open_regular_markets(now=winter_us_session) == frozenset(
        {"US"}
    )
    assert vertical_slice._open_regular_markets(now=summer_us_session) == frozenset(
        {"US"}
    )


def test_regular_market_gate_fails_closed_per_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.extensions.kasset.automation import vertical_slice

    calls: list[str] = []

    def unstable_market(market: str, *, now: datetime | None = None) -> bool:
        calls.append(market)
        if market == "kr":
            raise LookupError("XKRX unavailable")
        return True

    monkeypatch.setattr(vertical_slice, "is_market_open", unstable_market)

    assert vertical_slice._open_regular_markets(
        now=datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
    ) == frozenset({"US"})
    assert calls == ["kr", "us"]


@pytest.mark.asyncio
async def test_automation_cycle_logs_its_disabled_short_circuit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TaskIQ는 반환 dict를 로그로 남기지 않는다. 그래서 cycle이 직접 남긴다."""

    from app.extensions.kasset.automation import vertical_slice

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", False)

    with caplog.at_level("INFO", logger=vertical_slice.__name__):
        result = await vertical_slice.run_ai_recommendation_cycle_once()

    assert result == {"enabled": False, "owners": [], "candidateCount": 0}
    assert "KASSET_MARKET_EVENTS_ENABLED=false" in caplog.text


@pytest.mark.asyncio
async def test_automation_cycle_requires_its_starting_terra_lane(
    configured_ai, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """summary/luna가 살아 있어도 시작 lane인 terra가 없으면 cycle은 사용 불가다.

    예전에는 review lane 중 하나만 살아 있으면 ``aiAvailable=true``였지만,
    실제 첫 분석 ``candidate_review``는 terra에서 시작하므로 후보를 처리하지 못했다.
    """

    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    _cycle_session(monkeypatch, [])

    disabled_reviews = _snapshot(
        {
            AiLane.SUMMARY_LUNA: (AiRouteId.DIRECT_LUNA,),
            AiLane.REVIEW_LUNA: (AiRouteId.DIRECT_LUNA,),
        }
    )

    async def snapshot(db):  # type: ignore[no-untyped-def]
        return disabled_reviews

    monkeypatch.setattr(runtime_config_service, "get_ai_runtime_snapshot", snapshot)

    built: list[object] = []

    def never_build(*, snapshot=None):  # type: ignore[no-untyped-def]
        built.append(snapshot)
        raise AssertionError("router must not be built without a usable review route")

    monkeypatch.setattr(ai_factory, "build_model_router", never_build)

    with caplog.at_level("INFO", logger=vertical_slice.__name__):
        result = await vertical_slice.run_ai_recommendation_cycle_once()

    assert built == []
    assert result["aiAvailable"] is False
    assert result["aiUnavailableReason"] == "review_routes_unavailable"
    assert result["aiPolicySource"] == "persisted"
    assert "ai_available=False" in caplog.text
    assert "ai_usable_lanes=['review_luna', 'summary_luna']" in caplog.text


@pytest.mark.asyncio
async def test_automation_cycle_logs_the_owner_failure_stack_not_just_a_class_name(
    configured_ai, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """운영 회귀: owner 예외가 ``errorClass``만 남고 원인이 사라졌다.

    ``current_strategy_artifact()``의 ``ValueError``가 정확히 이 경로로 삼켜져
    09:10 cycle이 왜 죽었는지 로그로 알 수 없었다.
    """

    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    audit = _cycle_session(monkeypatch, [4])

    async def snapshot(db):  # type: ignore[no-untyped-def]
        return default_snapshot()

    monkeypatch.setattr(runtime_config_service, "get_ai_runtime_snapshot", snapshot)
    monkeypatch.setattr(
        ai_factory, "build_model_router", lambda *, snapshot=None: object()
    )

    class _Exploding:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise ValueError("current source commit is unavailable; embed VCS_REF")

    monkeypatch.setattr(vertical_slice, "AIRecommendationVerticalSlice", _Exploding)

    with caplog.at_level("ERROR", logger=vertical_slice.__name__):
        result = await vertical_slice.run_ai_recommendation_cycle_once()

    owner = result["owners"][0]  # type: ignore[index]
    assert owner["skipped"] == "owner_cycle_failed"
    assert owner["errorClass"] == "ValueError"
    assert result["recommendationCount"] == 0
    audit.assert_awaited_once()
    assert "owner_user_id=4" in caplog.text
    # 스택과 원문 메시지가 로그에 남아야 운영에서 원인을 짚을 수 있다.
    assert "current source commit is unavailable" in caplog.text
    assert "Traceback" in caplog.text


@pytest.mark.asyncio
async def test_automation_cycle_audit_failure_is_fail_open(
    configured_ai, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.automation import vertical_slice
    from app.services import ai_runtime_config as runtime_config_service

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)
    audit = _cycle_session(monkeypatch, [4])
    audit.side_effect = RuntimeError("audit database unavailable")

    async def snapshot(db):  # type: ignore[no-untyped-def]
        return default_snapshot()

    monkeypatch.setattr(runtime_config_service, "get_ai_runtime_snapshot", snapshot)
    monkeypatch.setattr(
        ai_factory, "build_model_router", lambda *, snapshot=None: object()
    )

    class _SuccessfulCycle:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def run_owner(self, owner_user_id: int) -> dict[str, object]:
            return {
                "ownerUserId": owner_user_id,
                "candidateCount": 100,
                "recommendationIds": [],
                "skipped": "no_ai_confirmed_signal",
            }

    monkeypatch.setattr(
        vertical_slice, "AIRecommendationVerticalSlice", _SuccessfulCycle
    )

    with caplog.at_level("ERROR", logger=vertical_slice.__name__):
        result = await vertical_slice.run_ai_recommendation_cycle_once()

    owner = result["owners"][0]  # type: ignore[index]
    assert owner["candidateCount"] == 100
    assert owner["skipped"] == "no_ai_confirmed_signal"
    assert result["candidateCount"] == 100
    assert result["recommendationCount"] == 0
    audit.assert_awaited_once()
    assert "cycle audit write failed: owner_user_id=4" in caplog.text
