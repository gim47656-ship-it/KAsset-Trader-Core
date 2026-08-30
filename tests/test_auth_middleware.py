from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth.web_router import create_session_token
from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.models.trading import User

# Allow /api/data as a public API path for testing the allowlist behavior
settings.PUBLIC_API_PATHS = ["/api/data"]

# Create a separate app for middleware testing to avoid side effects
app = FastAPI()
app.add_middleware(AuthMiddleware)


@app.get("/test-protected", response_class=HTMLResponse)
async def protected_route(request: Request):
    return "Protected Content"


@app.get("/web-auth/login", response_class=HTMLResponse)
async def login_page():
    return "Login Page"


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return "Admin Page"


@app.get("/admin/ops", response_class=HTMLResponse)
async def admin_ops_page():
    return "Admin Ops"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/auth/login")
async def mobile_login_boundary():
    return {"surface": "mobile"}


@app.get("/api/data")
async def api_data():
    return {"data": "ok"}


@app.get("/nested/api/data")
async def nested_api_data():
    return {"data": "nested-ok"}


@app.get("/secure-page/", response_class=HTMLResponse)
async def secure_page(request: Request):
    return "Secure Page"


@app.get("/manual-holdings/", response_class=HTMLResponse)
async def legacy_page_placeholder():
    return HTMLResponse("Deprecated page", status_code=410)


@app.get("/dashboard/", response_class=HTMLResponse)
async def legacy_dashboard_placeholder():
    return HTMLResponse("Deprecated dashboard", status_code=410)


@app.get("/upbit-trading/api/my-coins")
async def legacy_api_placeholder():
    return JSONResponse(
        status_code=410,
        content={
            "detail": "deprecated",
            "replacement_url": "/invest/",
            "deprecated_at": "2026-02-20T00:00:00+09:00",
        },
    )


@app.get("/dashboard/api/analysis")
async def legacy_dashboard_api_placeholder():
    return JSONResponse(
        status_code=410,
        content={
            "detail": "deprecated",
            "replacement_url": "/invest/",
            "deprecated_at": "2026-02-20T00:00:00+09:00",
        },
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_db_session():
    return AsyncMock()


@pytest.fixture
def mock_session_local(mock_db_session):
    with patch("app.middleware.auth.AsyncSessionLocal") as mock:
        mock.return_value.__aenter__.return_value = mock_db_session
        yield mock


def test_public_path_access(client, mock_session_local, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "KASSET_ADMIN_EDGE_KEY", None)
    response = client.get("/web-auth/login")
    assert response.status_code == 200
    assert response.text == "Login Page"


def _assert_secret_not_exposed(response, secret: str) -> None:
    response_wire = response.content + repr(dict(response.headers)).encode()
    assert secret.encode() not in response_wire


@pytest.mark.parametrize(
    "path",
    ["/admin", "/admin/ops", "/web-auth", "/web-auth/login"],
)
def test_production_admin_edge_denies_when_key_is_not_configured(
    client, monkeypatch, path
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "KASSET_ADMIN_EDGE_KEY", None)

    response = client.get(
        path,
        headers={
            "X-Forwarded-For": "100.64.0.1",
            "X-Real-IP": "100.64.0.1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@pytest.mark.parametrize("supplied_key", [None, "wrong-edge-key"])
def test_production_admin_edge_denies_missing_or_wrong_key_without_leaking_secret(
    client, monkeypatch, supplied_key
):
    expected_key = "expected-edge-key-that-must-stay-secret"
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings,
        "KASSET_ADMIN_EDGE_KEY",
        SecretStr(expected_key),
    )
    headers = {
        "X-Forwarded-For": "100.64.0.1",
        "X-Real-IP": "100.64.0.1",
    }
    if supplied_key is not None:
        headers["X-KAsset-Admin-Edge-Key"] = supplied_key

    response = client.get(
        "/web-auth/login",
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    _assert_secret_not_exposed(response, expected_key)


def test_production_admin_edge_accepts_correct_key_without_leaking_it(
    client, monkeypatch
):
    expected_key = "expected-edge-key-that-must-stay-secret"
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings,
        "KASSET_ADMIN_EDGE_KEY",
        SecretStr(expected_key),
    )

    response = client.get(
        "/web-auth/login",
        headers={"X-KAsset-Admin-Edge-Key": expected_key},
    )

    assert response.status_code == 200
    assert response.text == "Login Page"
    _assert_secret_not_exposed(response, expected_key)


def test_production_admin_edge_allows_authenticated_admin_with_correct_key(
    client, monkeypatch
):
    expected_key = "expected-edge-key-that-must-stay-secret"
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings,
        "KASSET_ADMIN_EDGE_KEY",
        SecretStr(expected_key),
    )
    admin_user = User(id=1, username="admin", is_active=True)
    monkeypatch.setattr(
        AuthMiddleware,
        "_load_user",
        staticmethod(AsyncMock(return_value=admin_user)),
    )

    response = client.get(
        "/admin/ops",
        headers={"X-KAsset-Admin-Edge-Key": expected_key},
    )

    assert response.status_code == 200
    assert response.text == "Admin Ops"
    _assert_secret_not_exposed(response, expected_key)


def test_non_production_admin_edge_remains_usable_without_key(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "KASSET_ADMIN_EDGE_KEY", None)

    response = client.get("/web-auth/login")

    assert response.status_code == 200
    assert response.text == "Login Page"


@pytest.mark.parametrize(
    ("path", "expected_payload"),
    [
        ("/health", {"status": "ok"}),
        ("/api/data", {"data": "ok"}),
        ("/api/v1/auth/login", {"surface": "mobile"}),
    ],
)
def test_production_non_admin_surfaces_do_not_require_edge_key(
    client, mock_session_local, monkeypatch, path, expected_payload
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "KASSET_ADMIN_EDGE_KEY", None)

    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == expected_payload


def test_api_path_access(client, mock_session_local):
    response = client.get("/api/data")
    assert response.status_code == 200
    assert response.json() == {"data": "ok"}


def test_nested_api_path_without_auth_returns_401(client, mock_session_local):
    response = client.get("/nested/api/data", follow_redirects=False)
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required for this endpoint."


def test_create_session_token_returns_string():
    token = create_session_token(1)
    assert isinstance(token, str)


def test_protected_route_no_auth(client, mock_session_local):
    # Should redirect to login
    response = client.get("/test-protected", follow_redirects=False)
    assert response.status_code == 303
    assert "/web-auth/login" in response.headers["location"]


def test_protected_route_with_auth(client, mock_session_local, mock_db_session):
    # Setup mock user
    user = User(id=1, username="testuser", is_active=True)

    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = 0
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    mock_db_session.execute.side_effect = [version_result, user_result]

    # Create session token
    token = create_session_token(1)

    # Set cookie
    client.cookies.set("session", token)

    # Mock Redis and Blacklist to ensure fallback to DB
    with (
        patch("app.auth.web_router.get_session_blacklist") as mock_blacklist,
        patch("app.auth.web_router.redis.from_url") as mock_redis,
    ):
        # Blacklist check returns False (not blacklisted)
        mock_blacklist.return_value.is_blacklisted = AsyncMock(return_value=False)

        # Redis raises exception to trigger "redis_error = True" path
        # which allows fallback to DB in non-production environment
        mock_redis.side_effect = Exception("Redis connection failed")

        response = client.get("/test-protected")
        assert response.status_code == 200
        assert response.text == "Protected Content"


def test_legacy_deprecated_page_path_bypasses_auth_redirect(client, mock_session_local):
    response = client.get("/manual-holdings/", follow_redirects=False)
    assert response.status_code == 410
    assert response.text == "Deprecated page"


def test_dashboard_deprecated_page_path_bypasses_auth_redirect(
    client, mock_session_local
):
    response = client.get("/dashboard/", follow_redirects=False)
    assert response.status_code == 410
    assert response.text == "Deprecated dashboard"


def test_legacy_deprecated_api_path_bypasses_auth_401(client, mock_session_local):
    response = client.get(
        "/upbit-trading/api/my-coins",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert response.status_code == 410
    payload = response.json()
    assert payload["replacement_url"] == "/invest/"


def test_dashboard_deprecated_api_path_bypasses_auth_401(client, mock_session_local):
    response = client.get(
        "/dashboard/api/analysis",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert response.status_code == 410
    payload = response.json()
    assert payload["replacement_url"] == "/invest/"


def test_protected_route_redirects_cleanly_with_sentry_fastapi_enabled(monkeypatch):
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    test_app = FastAPI()
    test_app.add_middleware(AuthMiddleware)

    @test_app.get("/test-protected", response_class=HTMLResponse)
    async def protected_route(request: Request):
        return "Protected Content"

    @test_app.get("/web-auth/login", response_class=HTMLResponse)
    async def login_page():
        return "Login Page"

    monkeypatch.setattr(
        AuthMiddleware,
        "_load_user",
        staticmethod(AsyncMock(return_value=None)),
    )

    sentry_sdk.init(
        dsn=None,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    client = TestClient(test_app)

    response = client.get("/test-protected", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/web-auth/login")


def test_redirect_next_uses_relative_path(client, mock_session_local):
    """AuthMiddleware must generate relative-path next, not absolute URL."""
    response = client.get("/secure-page/", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    # next must be a relative path, not http://testserver/secure-page/
    assert location == "/web-auth/login?next=/secure-page/"


def test_redirect_next_preserves_query_string(client, mock_session_local):
    """Query string in original URL must survive the redirect."""
    response = client.get("/secure-page/?tab=crypto&sort=asc", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == "/web-auth/login?next=/secure-page/?tab=crypto&sort=asc"


def test_redirect_next_no_trailing_question_mark(client, mock_session_local):
    """Path without query string must not have trailing '?'."""
    response = client.get("/test-protected", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == "/web-auth/login?next=/test-protected"
    assert "next=/test-protected?" not in location
