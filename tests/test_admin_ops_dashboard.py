"""GET /admin/ops — gate, degradation, and the zero-vs-unmeasured distinction.

The gate under test is the existing one: ``AuthMiddleware`` (session cookie for
HTML GETs) in front of ``require_admin`` (session cookie + admin role). Nothing
new is introduced here, so these tests pin that the new page inherits it.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import api
from app.models.trading import User, UserRole
from app.services.ops_dashboard import (
    UNMEASURED_TEXT,
    OpsDashboard,
    OpsMetric,
    OpsPanel,
    OpsRow,
    PanelStatus,
    build_ops_dashboard,
)

OPS_PATH = "/admin/ops"


def _user(uid=7, role=UserRole.admin):
    user = User(username="ops-admin", email="ops@x.co", role=role, is_active=True)
    user.id = uid
    return user


@pytest.fixture
def ops_client(auth_mock_session, reset_auth_mock_db, mock_auth_middleware_db):
    """Client whose DB is mocked at both the middleware and the router seam."""
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
    """Patch both seams that resolve a web session into a user."""
    resolver = AsyncMock(return_value=user)
    return (
        patch("app.middleware.auth.get_current_user_from_session", new=resolver),
        patch("app.auth.admin_router.get_current_user_from_session", new=resolver),
    )


def _stub_dashboard(panels):
    dashboard = OpsDashboard(
        generated_at=datetime(2026, 8, 30, 3, 0, tzinfo=UTC), panels=tuple(panels)
    )
    return patch(
        "app.auth.admin_router.build_ops_dashboard",
        new=AsyncMock(return_value=dashboard),
    )


_OK_PANEL = OpsPanel(
    status=PanelStatus.OK,
    summary="정상",
    metrics=(OpsMetric("서버 버전", "1.2.3"),),
    key="system",
    title="운영 상태",
)


# ---------------------------------------------------------------- gate


def test_unauthenticated_request_is_blocked(ops_client):
    middleware, router = _as_session_user(None)
    with middleware, router, _stub_dashboard([_OK_PANEL]) as builder:
        response = ops_client.get(OPS_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == f"/web-auth/login?next={OPS_PATH}"
    builder.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.trader])
def test_logged_in_non_admin_is_blocked(ops_client, role):
    middleware, router = _as_session_user(_user(role=role))
    with middleware, router, _stub_dashboard([_OK_PANEL]) as builder:
        response = ops_client.get(OPS_PATH)

    assert response.status_code == 403
    assert response.json()["detail"] == "관리자 권한이 필요합니다."
    builder.assert_not_awaited()


def test_android_jwt_bearer_is_blocked(ops_client):
    """A KAsset Android access token never produces a web session."""
    from app.auth.security import create_access_token

    token = create_access_token(
        data={
            "sub": "kasset-mobile",
            "client": "kasset-android",
            "uid": "1",
            "deviceId": "device-1",
            "sessionId": "session-1",
        }
    )
    # No session cookie: the real resolver short-circuits on the missing cookie.
    with _stub_dashboard([_OK_PANEL]) as builder:
        response = ops_client.get(
            OPS_PATH, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/web-auth/login")
    builder.assert_not_awaited()


def test_admin_session_renders_panels(ops_client):
    panels = [
        _OK_PANEL,
        OpsPanel(
            status=PanelStatus.IDLE,
            summary="최근 24시간 추천 0건",
            metrics=(OpsMetric("추천 생성", "0"),),
            key="funnel",
            title="자동매매 funnel",
        ),
    ]
    middleware, router = _as_session_user(_user())
    with middleware, router, _stub_dashboard(panels) as builder:
        response = ops_client.get(OPS_PATH)

    assert response.status_code == 200
    body = response.text
    assert 'id="panel-system"' in body
    assert 'id="panel-funnel"' in body
    assert "운영 대시보드" in body
    builder.assert_awaited_once()


# ------------------------------------------- zero vs unmeasured on screen


def test_zero_rows_and_query_failure_render_differently(ops_client):
    panels = [
        OpsPanel(
            status=PanelStatus.IDLE,
            summary="최근 24시간 추천 0건 (조회 성공, 실행 이력 없음)",
            metrics=(OpsMetric("추천 생성", "0"),),
            columns=("구분", "건수"),
            key="funnel",
            title="자동매매 funnel",
        ),
        OpsPanel(
            status=PanelStatus.ERROR,
            summary="지표를 가져오지 못했습니다.",
            metrics=(OpsMetric("대사한 broker", None),),
            error="ProgrammingError: relation does not exist",
            key="reconcile",
            title="체결 대사",
        ),
    ]
    middleware, router = _as_session_user(_user())
    with middleware, router, _stub_dashboard(panels):
        response = ops_client.get(OPS_PATH)

    assert response.status_code == 200
    body = response.text

    idle_panel = body.split('id="panel-funnel"')[1].split("</section>")[0]
    error_panel = body.split('id="panel-reconcile"')[1].split("</section>")[0]

    # 0건: real zero, marked measured, and explicitly labelled 대기.
    assert 'data-status="idle"' in idle_panel
    assert 'data-measured="true">0<' in idle_panel
    assert UNMEASURED_TEXT not in idle_panel
    assert "조회 성공 · 해당 기간 0건" in idle_panel

    # 조회실패: no zero anywhere, marked unmeasured, error text shown.
    assert 'data-status="error"' in error_panel
    assert f'data-measured="false">{UNMEASURED_TEXT}<' in error_panel
    assert 'data-measured="true">0<' not in error_panel
    assert "relation does not exist" in error_panel


def test_failed_panel_does_not_take_down_the_page(ops_client):
    panels = [
        _OK_PANEL,
        OpsPanel(
            status=PanelStatus.ERROR,
            summary="지표를 가져오지 못했습니다.",
            error="OperationalError: connection reset",
            key="news",
            title="뉴스 파이프라인",
        ),
        OpsPanel(
            status=PanelStatus.IDLE,
            summary="승격 레지스트리 0건",
            metrics=(OpsMetric("PAPER_APPROVED", "0"),),
            key="strategy",
            title="전략 승격",
        ),
    ]
    middleware, router = _as_session_user(_user())
    with middleware, router, _stub_dashboard(panels):
        response = ops_client.get(OPS_PATH)

    assert response.status_code == 200
    body = response.text
    assert 'id="panel-system"' in body
    assert 'id="panel-strategy"' in body
    assert "1.2.3" in body
    assert 'id="ops-degraded"' in body
    assert "뉴스 파이프라인" in body


# ------------------------------------------------------ orchestrator


@pytest.mark.asyncio
async def test_builder_isolates_a_failing_panel_and_rolls_back():
    """One builder raising must not stop the others, and must clear the tx."""

    async def boom(_ctx):
        raise RuntimeError("panel exploded")

    async def fine(_ctx):
        return OpsPanel(status=PanelStatus.OK, summary="정상")

    db = AsyncMock()
    with patch(
        "app.services.ops_dashboard.PANEL_BUILDERS",
        (("a", "패널 A", boom), ("b", "패널 B", fine)),
    ):
        dashboard = await build_ops_dashboard(db, admin_user_id=1)

    failed, healthy = dashboard.panels
    assert failed.key == "a"
    assert failed.status is PanelStatus.ERROR
    assert failed.error == "RuntimeError: panel exploded"
    assert failed.metrics == ()
    assert healthy.key == "b"
    assert healthy.status is PanelStatus.OK
    assert dashboard.failed_panel_titles == ("패널 A",)
    # A failed statement poisons the shared transaction; the next panel only
    # works because the orchestrator rolled it back.
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_builder_stamps_registry_key_and_title():
    async def fine(_ctx):
        return OpsPanel(
            status=PanelStatus.IDLE,
            summary="0건",
            rows=(OpsRow(("x", None)),),
        )

    with patch("app.services.ops_dashboard.PANEL_BUILDERS", (("zzz", "패널 Z", fine),)):
        dashboard = await build_ops_dashboard(AsyncMock(), admin_user_id=1)

    (panel,) = dashboard.panels
    assert (panel.key, panel.title) == ("zzz", "패널 Z")
    assert panel.rows[0].cells == ("x", None)
