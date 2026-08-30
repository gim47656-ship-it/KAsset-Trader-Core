"""AI route catalog, 정책 검증, 그리고 실행 경로에 정책이 실제로 적용되는지.

DB가 필요 없는 순수 계약만 다룬다. singleton 저장/동시성/관리자 API는
``tests/test_admin_ai_routes.py``가 담당한다.
"""

from __future__ import annotations

import importlib.util
import json
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
    DEFAULT_ROUTE_POLICY,
    LANE_ROUTE_IDS,
    AiLane,
    AiRouteId,
    AiRoutePolicyError,
    AiRuntimeSnapshot,
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
