"""GET/PUT /admin/ops/ai-routes — 게이트, CSRF, 낙관적 잠금, 저장 계약.

게이트는 기존 것과 같다. ``AuthMiddleware``(브라우저 세션) 앞단과
``require_admin``(세션 쿠키 + admin 역할), 그리고 이제 ``/admin/*``에도 적용되는
CSRF 미들웨어다. Android JWT는 세션 쿠키를 만들지 않으므로 여기까지 오지 못한다.

singleton 저장/동시성 테스트는 실제 PostgreSQL(``db_session``)을 쓴다.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import settings
from app.extensions.kasset.ai.runtime_config import (
    DEFAULT_ROUTE_POLICY,
    AiLane,
    AiRouteId,
    AiRoutePolicyError,
    serialize_route_policy,
)
from app.main import api
from app.models import AiRuntimeConfig
from app.models.base import Base
from app.models.trading import User, UserRole
from app.services.ai_runtime_config import (
    AiRoutePolicyRevisionConflict,
    apply_ai_routes_update,
    build_ai_routes_view,
    get_ai_runtime_snapshot,
)

ROUTES_PATH = "/admin/ops/ai-routes"

_SECRETS = ("direct-secret-key", "openrouter-secret-key", "mcp-secret-token")


def _user(uid=11, role=UserRole.admin):
    user = User(username="ai-admin", email="ai@x.co", role=role, is_active=True)
    user.id = uid
    return user


@pytest.fixture
def configured_ai(monkeypatch: pytest.MonkeyPatch) -> None:
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


# ---------------------------------------------------------------- HTTP surface


@pytest.fixture
def routes_client(auth_mock_session, reset_auth_mock_db, mock_auth_middleware_db):
    """미들웨어와 라우터 두 접점 모두 DB가 mock인 클라이언트."""

    assert reset_auth_mock_db is auth_mock_session
    from app.core.db import get_db

    async def override_get_db():
        yield auth_mock_session

    api.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(api, follow_redirects=False)
    finally:
        api.dependency_overrides.clear()


def _as_session_user(user):
    resolver = AsyncMock(return_value=user)
    return (
        patch("app.middleware.auth.get_current_user_from_session", new=resolver),
        patch("app.auth.admin_router.get_current_user_from_session", new=resolver),
    )


_VIEW = {
    "revision": 3,
    "updatedAt": "2026-08-30T00:00:00+00:00",
    "updatedByUserId": 11,
    "source": "persisted",
    "lanes": [
        {
            "lane": "review_terra",
            "label": "2차 검토 (표준 판단)",
            "telemetryCovered": True,
            "routes": [
                {
                    "routeId": "direct_terra",
                    "label": "직접 API · 표준 모델",
                    "provider": "direct-api",
                    "model": "model-terra",
                    "configured": True,
                    "available": True,
                    "active": True,
                    "fallbackOrder": 1,
                    "latestSuccessAt": None,
                    "telemetryCovered": True,
                    "unavailableReason": None,
                    "unavailableReasonLabel": None,
                }
            ],
        }
    ],
}


def _valid_lanes() -> dict[str, list[str]]:
    return serialize_route_policy(DEFAULT_ROUTE_POLICY)


def _csrf(client) -> str:
    client.get("/health")
    return client.cookies["csrftoken"]


def test_get_requires_a_browser_session(routes_client):
    middleware_patch, router_patch = _as_session_user(None)
    with middleware_patch, router_patch:
        response = routes_client.get(ROUTES_PATH)

    assert response.status_code == 303
    assert "/web-auth/login" in response.headers["location"]


@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.trader])
def test_get_rejects_non_admin_roles(routes_client, role):
    middleware_patch, router_patch = _as_session_user(_user(role=role))
    with middleware_patch, router_patch:
        response = routes_client.get(ROUTES_PATH)

    assert response.status_code == 403


def test_get_returns_the_service_view_for_admins(routes_client):
    middleware_patch, router_patch = _as_session_user(_user())
    with (
        middleware_patch,
        router_patch,
        patch(
            "app.auth.admin_router.build_ai_routes_view",
            new=AsyncMock(return_value=_VIEW),
        ),
    ):
        response = routes_client.get(ROUTES_PATH)

    assert response.status_code == 200
    assert response.json() == _VIEW


def test_put_without_csrf_is_rejected_before_the_handler(routes_client):
    middleware_patch, router_patch = _as_session_user(_user())
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
        )

    assert response.status_code == 403
    applier.assert_not_awaited()


def test_put_with_forged_csrf_is_rejected(routes_client):
    middleware_patch, router_patch = _as_session_user(_user())
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": "forged-token"},
        )

    assert response.status_code == 403
    applier.assert_not_awaited()


def test_put_with_valid_csrf_but_no_session_is_unauthorized(routes_client):
    middleware_patch, router_patch = _as_session_user(None)
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 401
    applier.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.trader])
def test_put_rejects_non_admin_roles_even_with_csrf(routes_client, role):
    middleware_patch, router_patch = _as_session_user(_user(role=role))
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 403
    applier.assert_not_awaited()


def test_put_with_admin_and_csrf_passes_the_admin_id(routes_client):
    middleware_patch, router_patch = _as_session_user(_user(uid=42))
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 200
    assert response.json() == _VIEW
    kwargs = applier.await_args.kwargs
    assert kwargs["expected_revision"] == 3
    assert kwargs["admin_user_id"] == 42
    assert kwargs["lanes"] == _valid_lanes()


@pytest.mark.parametrize(
    "body",
    [
        # provider/model/URL/key/명령을 넣을 필드는 API에 존재하지 않는다.
        {"expectedRevision": 3, "lanes": {}, "model": "gpt-4o"},
        {"expectedRevision": 3, "lanes": {}, "baseUrl": "https://evil.invalid/v1"},
        {"expectedRevision": 3, "lanes": {}, "apiKey": "sk-live-000"},
        {"expectedRevision": 3, "lanes": {}, "subscriptionCmd": "codex exec"},
        # 계약에 없는 형태
        {"lanes": _valid_lanes()},
        {"expectedRevision": -1, "lanes": _valid_lanes()},
        {"expectedRevision": 3},
    ],
)
def test_put_rejects_payloads_outside_the_contract(routes_client, body):
    middleware_patch, router_patch = _as_session_user(_user())
    applier = AsyncMock(return_value=_VIEW)
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json=body,
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 422
    applier.assert_not_awaited()


def test_put_maps_policy_errors_to_422(routes_client):
    middleware_patch, router_patch = _as_session_user(_user())
    applier = AsyncMock(
        side_effect=AiRoutePolicyError("route_unavailable", "사용할 수 없습니다.")
    )
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "route_unavailable"


def test_put_maps_stale_revision_to_409_with_revision_only(routes_client):
    middleware_patch, router_patch = _as_session_user(_user())
    applier = AsyncMock(side_effect=AiRoutePolicyRevisionConflict(9))
    with (
        middleware_patch,
        router_patch,
        patch("app.auth.admin_router.apply_ai_routes_update", new=applier),
    ):
        token = _csrf(routes_client)
        response = routes_client.put(
            ROUTES_PATH,
            json={"expectedRevision": 3, "lanes": _valid_lanes()},
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["currentRevision"] == 9
    assert set(detail) == {"code", "currentRevision", "message"}
    assert "lanes" not in response.text


# ------------------------------------------------------------- persistence


@pytest_asyncio.fixture
async def clean_policy(db_session):
    """singleton 행을 테스트 전후로 비워 다른 테스트에 새지 않게 한다."""

    await db_session.execute(delete(AiRuntimeConfig))
    await db_session.commit()
    yield db_session
    await db_session.execute(delete(AiRuntimeConfig))
    await db_session.commit()


async def _seed(session, *, revision: int, policy: dict[str, list[str]]) -> None:
    session.add(AiRuntimeConfig(id=1, revision=revision, route_policy=policy))
    await session.commit()


@pytest.mark.asyncio
async def test_missing_singleton_uses_env_equivalent_defaults_without_writing(
    clean_policy,
):
    snapshot = await get_ai_runtime_snapshot(clean_policy)

    assert snapshot.source == "default"
    assert snapshot.revision == 0
    assert snapshot.routes(AiLane.REVIEW_TERRA) == (
        AiRouteId.MCP_TOOL,
        AiRouteId.DIRECT_TERRA,
        AiRouteId.OPENROUTER_PRO,
    )
    # 읽기만으로 행을 만들지 않는다.
    assert await clean_policy.scalar(select(AiRuntimeConfig.id)) is None


@pytest.mark.asyncio
async def test_persisted_policy_is_applied_in_stored_order(clean_policy):
    await _seed(
        clean_policy,
        revision=4,
        policy={
            **_valid_lanes(),
            "review_terra": ["openrouter_pro", "direct_terra"],
        },
    )

    snapshot = await get_ai_runtime_snapshot(clean_policy)

    assert snapshot.source == "persisted"
    assert snapshot.revision == 4
    assert snapshot.routes(AiLane.REVIEW_TERRA) == (
        AiRouteId.OPENROUTER_PRO,
        AiRouteId.DIRECT_TERRA,
    )


@pytest.mark.asyncio
async def test_corrupt_policy_fails_closed_without_env_fallback(clean_policy):
    await _seed(
        clean_policy,
        revision=6,
        policy={**_valid_lanes(), "review_terra": ["gpt-4o-mini"]},
    )

    snapshot = await get_ai_runtime_snapshot(clean_policy)

    assert snapshot.source == "invalid"
    assert snapshot.revision == 6
    for lane in AiLane:
        assert snapshot.routes(lane) == ()


@pytest.mark.asyncio
async def test_db_read_failure_fails_closed_without_env_fallback():
    session = AsyncMock()
    session.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("connection lost")
    )

    snapshot = await get_ai_runtime_snapshot(session)

    assert snapshot.source == "unavailable"
    for lane in AiLane:
        assert snapshot.routes(lane) == ()
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_update_from_missing_row_creates_revision_one(
    clean_policy, configured_ai
):
    result = await apply_ai_routes_update(
        clean_policy,
        expected_revision=0,
        lanes={**_valid_lanes(), "compat_skill": []},
        admin_user_id=None,
    )

    assert result["revision"] == 1
    row = (await clean_policy.execute(select(AiRuntimeConfig))).scalar_one()
    assert row.route_policy["compat_skill"] == []
    assert row.route_policy["review_terra"] == [
        "mcp_tool",
        "direct_terra",
        "openrouter_pro",
    ]


@pytest.mark.asyncio
async def test_update_stores_only_route_ids(clean_policy, configured_ai):
    await apply_ai_routes_update(
        clean_policy,
        expected_revision=0,
        lanes=_valid_lanes(),
        admin_user_id=None,
    )

    stored = await clean_policy.scalar(select(AiRuntimeConfig.route_policy))
    blob = json.dumps(stored, ensure_ascii=False)
    for secret in _SECRETS:
        assert secret not in blob
    assert "invalid" not in blob
    assert "model-" not in blob
    assert "codex" not in blob


@pytest.mark.asyncio
async def test_update_rejects_unavailable_route(
    clean_policy, configured_ai, monkeypatch
):
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")

    with pytest.raises(AiRoutePolicyError) as exc:
        await apply_ai_routes_update(
            clean_policy,
            expected_revision=0,
            lanes=_valid_lanes(),
            admin_user_id=None,
        )

    assert exc.value.code == "route_unavailable"
    assert await clean_policy.scalar(select(AiRuntimeConfig.id)) is None


@pytest.mark.asyncio
async def test_stale_revision_conflicts_and_writes_nothing(clean_policy, configured_ai):
    await _seed(clean_policy, revision=5, policy=_valid_lanes())

    with pytest.raises(AiRoutePolicyRevisionConflict) as exc:
        await apply_ai_routes_update(
            clean_policy,
            expected_revision=4,
            lanes={**_valid_lanes(), "summary_luna": []},
            admin_user_id=None,
        )

    assert exc.value.current_revision == 5
    row = (await clean_policy.execute(select(AiRuntimeConfig))).scalar_one()
    assert row.revision == 5
    assert row.route_policy["summary_luna"] == ["direct_luna", "openrouter_flash"]


@pytest.mark.asyncio
async def test_concurrent_updates_let_exactly_one_win(clean_policy, configured_ai):
    await _seed(clean_policy, revision=1, policy=_valid_lanes())
    from app.core.db import AsyncSessionLocal

    async def attempt(disabled_lane: str):
        async with AsyncSessionLocal() as session:
            return await apply_ai_routes_update(
                session,
                expected_revision=1,
                lanes={**_valid_lanes(), disabled_lane: []},
                admin_user_id=None,
            )

    results = await asyncio.gather(
        attempt("compat_skill"),
        attempt("summary_luna"),
        return_exceptions=True,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, AiRoutePolicyRevisionConflict)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0]["revision"] == 2
    assert conflicts[0].current_revision == 2


@pytest.mark.asyncio
async def test_view_is_secret_free_and_lists_every_lane_option(
    clean_policy, configured_ai
):
    await _seed(
        clean_policy,
        revision=2,
        policy={**_valid_lanes(), "review_terra": ["direct_terra"]},
    )

    view = await build_ai_routes_view(clean_policy)

    blob = json.dumps(view, ensure_ascii=False)
    for secret in _SECRETS:
        assert secret not in blob
    assert "direct.invalid" not in blob
    assert "router.invalid" not in blob
    assert "mcp.invalid" not in blob
    assert "codex" not in blob

    assert view["revision"] == 2
    assert view["source"] == "persisted"
    assert {lane["lane"] for lane in view["lanes"]} == {lane.value for lane in AiLane}

    terra = next(lane for lane in view["lanes"] if lane["lane"] == "review_terra")
    # 활성 route가 먼저, 나머지 허용 route도 비활성 상태로 함께 노출된다.
    assert [route["routeId"] for route in terra["routes"]] == [
        "direct_terra",
        "mcp_tool",
        "openrouter_pro",
    ]
    assert terra["routes"][0]["active"] is True
    assert terra["routes"][0]["fallbackOrder"] == 1
    assert terra["routes"][1]["active"] is False
    assert terra["routes"][1]["fallbackOrder"] is None
    assert all(route["label"] for route in terra["routes"])
    assert terra["operatorControllable"] is True

    compat = next(lane for lane in view["lanes"] if lane["lane"] == "compat_skill")
    # 원장을 우회하는 lane은 latest success를 "측정 불가"로 보고한다.
    assert compat["telemetryCovered"] is False
    assert all(route["latestSuccessAt"] is None for route in compat["routes"])
    assert compat["operatorControllable"] is False


@pytest.mark.asyncio
async def test_view_reports_unavailable_options_with_a_reason(
    clean_policy, configured_ai, monkeypatch
):
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")

    view = await build_ai_routes_view(clean_policy)

    terra = next(lane for lane in view["lanes"] if lane["lane"] == "review_terra")
    mcp = next(route for route in terra["routes"] if route["routeId"] == "mcp_tool")
    assert mcp["available"] is False
    assert mcp["unavailableReason"] == "missing_mcp_url"
    assert mcp["unavailableReasonLabel"]


# ------------------------------------------------- schema / model registration


@pytest.mark.asyncio
async def test_table_constraints_match_the_migration(db_session):
    rows = await db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'kasset_ai_runtime_config'::regclass"
        )
    )
    names = {row[0] for row in rows}

    assert "pk_kasset_ai_runtime_config" in names
    assert "ck_kasset_ai_runtime_config_singleton" in names
    assert "ck_kasset_ai_runtime_config_revision_nonnegative" in names
    assert "fk_kasset_ai_runtime_config_updated_by_user_id_users" in names


@pytest.mark.asyncio
async def test_singleton_check_rejects_a_second_row(clean_policy):
    clean_policy.add(AiRuntimeConfig(id=2, revision=0, route_policy={}))

    with pytest.raises(IntegrityError):
        await clean_policy.commit()
    await clean_policy.rollback()


@pytest.mark.asyncio
async def test_negative_revision_is_rejected(clean_policy):
    clean_policy.add(AiRuntimeConfig(id=1, revision=-1, route_policy={}))

    with pytest.raises(IntegrityError):
        await clean_policy.commit()
    await clean_policy.rollback()


def test_model_is_registered_for_create_all_bootstraps():
    from app import models

    assert "AiRuntimeConfig" in models.__all__
    assert models.AiRuntimeConfig is AiRuntimeConfig
    assert "kasset_ai_runtime_config" in Base.metadata.tables
