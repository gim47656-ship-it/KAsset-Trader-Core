"""KAsset 토큰이 도달할 수 있는 경로 계약.

Android 파사드에 설치된 라우트는 전부 kasset 토큰으로 호출 가능해야 한다.
라우트를 추가하고 `paths.py` 허용목록에 넣지 않으면 프로덕션의
`AuthMiddleware` -> `get_current_user` 경로가 401 을 반환한다.

실측 결함(2026-08-28): `/api/v1/market/orderbook` 라우트와 NH 호가 WS 수집이
모두 정상인데도 앱의 1초 호가 폴링이 전부 401 이었다. 허용목록 누락이 유일한
원인이었고, 기존 `test_orderbook.py` 는 미들웨어 없는 bare FastAPI +
`dependency_overrides` 로 라우트를 직접 호출하므로 이 결함을 구조적으로 잡을 수
없었다. 그래서 라우트 목록 자체를 계약으로 고정한다.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.api.paths import (
    is_android_compat_path,
    is_kasset_token_allowed_path,
)


def _installed_api_paths() -> list[str]:
    app = FastAPI()
    install_android_compat_api(app)
    return sorted(
        {
            path
            for route in app.routes
            if (path := getattr(route, "path", "")).startswith("/api/v1")
        }
    )


def test_installed_routes_are_discovered() -> None:
    """계약 테스트가 빈 목록을 통과해 스스로 무력화되지 않게 방어한다."""

    paths = _installed_api_paths()
    assert len(paths) > 20
    assert "/api/v1/market/orderbook" in paths


def test_every_installed_android_route_is_reachable_with_kasset_token() -> None:
    unreachable = [
        path
        for path in _installed_api_paths()
        if not is_kasset_token_allowed_path(path)
    ]
    assert unreachable == []


def test_orderbook_path_is_allowed() -> None:
    assert is_kasset_token_allowed_path("/api/v1/market/orderbook")
    assert is_android_compat_path("/api/v1/market/orderbook")


def test_allowlist_stays_exact_and_rejects_generic_core_surface() -> None:
    """허용목록이 접두사 매칭으로 넓어지지 않았는지 확인한다."""

    assert not is_kasset_token_allowed_path("/api/v1/market")
    assert not is_kasset_token_allowed_path("/api/v1/market/orderbook/extra")
    assert not is_kasset_token_allowed_path("/api/v1/analysis")
    assert not is_kasset_token_allowed_path("/api/v1/users")
