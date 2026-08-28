from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.extensions.kasset.api import market_overview as mod
from app.extensions.kasset.api.installation import install_android_compat_api


def test_deployed_startup_warms_market_sources_before_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh process pays a one-time provider client warmup on its first market
    # read. If startup does not absorb it, the first app request does.
    warmed = asyncio.Event()

    async def warm() -> None:
        warmed.set()

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(mod, "warm_market_sources", warm)

    app = FastAPI()
    install_android_compat_api(app)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert warmed.is_set()


def test_test_environment_startup_never_touches_market_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def warm() -> None:
        calls.append(1)

    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(mod, "warm_market_sources", warm)

    app = FastAPI()
    install_android_compat_api(app)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert calls == []


@pytest.mark.asyncio
async def test_warmup_never_propagates_source_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode() -> None:
        raise RuntimeError("provider down")

    monkeypatch.setattr(mod, "get_market_overview", explode)

    await mod.warm_market_sources()
