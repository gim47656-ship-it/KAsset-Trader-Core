# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest  # type: ignore[reportMissingImports]
from pydantic import SecretStr


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return False
        _ = ex
        self.store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str):
        _ = ttl
        self.store[key] = value
        return True

    async def delete(self, key: str):
        self.store.pop(key, None)
        return 1


class _AlwaysFailClaimRedis(_FakeRedis):
    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx:
            return False
        return await super().set(key, value, ex=ex, nx=nx)


def _generated_report() -> dict[str, Any]:
    return {
        "decision": "hold",
        "confidence": 60,
        "reasons": ["range"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        "detailed_text": "done",
    }


async def _await_background_reports() -> None:
    from app.services import screener_service

    tasks = tuple(screener_service._BACKGROUND_TASKS)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_list_screening_uses_5m_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "name": "Apple"}],
            "total_count": 1,
            "returned_count": 1,
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", limit=20)
    second = await service.list_screening(market="us", limit=20)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert mock_screen.await_count == 1


@pytest.mark.asyncio
async def test_list_screening_coerces_crypto_volume_sort_to_trade_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "market": "crypto",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(
        market="crypto",
        sort_by="volume",
        sort_order="desc",
        limit=20,
    )

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["market"] == "crypto"
    assert call_kwargs["sort_by"] == "trade_amount"
    assert call_kwargs["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_list_screening_filters_us_by_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "AAPL", "volume": 500},
                {"code": "MSFT", "volume": 1000},
                {"code": "NVDA", "volume": 2500},
                {"code": "TSLA", "volume": 1500},
            ],
            "total_count": 4,
            "returned_count": 4,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(market="us", min_volume=1000, limit=2)

    assert result["cache_hit"] is False
    assert [item["code"] for item in result["results"]] == ["MSFT", "NVDA"]
    assert result["returned_count"] == 2
    assert result["total_count"] == 3
    assert result["filters_applied"]["min_volume"] == 1000

    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_filters_crypto_by_trade_amount_24h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "KRW-BTC", "trade_amount_24h": 900},
                {"code": "KRW-ETH", "trade_amount_24h": 1200},
                {"code": "KRW-XRP"},
                {"code": "KRW-SOL", "trade_amount_24h": 3000},
            ],
            "total_count": 4,
            "returned_count": 4,
            "filters_applied": {"market": "crypto"},
            "market": "crypto",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await service.list_screening(market="crypto", min_volume=1000, limit=2)

    assert result["cache_hit"] is False
    assert [item["code"] for item in result["results"]] == ["KRW-ETH", "KRW-SOL"]
    assert result["returned_count"] == 2
    assert result["total_count"] == 2
    assert result["filters_applied"]["min_volume"] == 1000

    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    assert call_kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_uses_separate_cache_keys_for_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "volume": 1000}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", min_volume=1000, limit=1)
    second = await service.list_screening(market="us", min_volume=2000, limit=1)
    third = await service.list_screening(market="us", min_volume=1000, limit=1)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert third["cache_hit"] is True
    assert mock_screen.await_count == 2


@pytest.mark.asyncio
async def test_list_screening_uses_separate_cache_keys_for_new_fundamentals_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "sector": "Technology"}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )
    second = await cast(Any, service).list_screening(
        market="us",
        sector="Healthcare",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )
    third = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=6,
        min_dividend=2.0,
        limit=1,
    )
    fourth = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=3.0,
        limit=1,
    )
    fifth = await cast(Any, service).list_screening(
        market="us",
        sector="Technology",
        min_analyst_buy=5,
        min_dividend=2.0,
        limit=1,
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert third["cache_hit"] is False
    assert fourth["cache_hit"] is False
    assert fifth["cache_hit"] is True
    assert mock_screen.await_count == 4


@pytest.mark.asyncio
async def test_list_screening_overfetches_when_post_screen_analyst_filtering_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [
                {"code": "AAPL", "analyst_buy": 12},
                {"code": "MSFT", "analyst_buy": 10},
                {"code": "NVDA", "analyst_buy": 8},
            ],
            "total_count": 3,
            "returned_count": 3,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))
    result = await cast(Any, service).list_screening(
        market="us", min_analyst_buy=10, limit=2
    )

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    assert await_args.kwargs["market"] == "us"
    assert await_args.kwargs["min_analyst_buy"] == 10
    assert await_args.kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_list_screening_rejects_negative_min_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(return_value={})
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    with pytest.raises(ValueError, match="min_volume must be >= 0"):
        await service.list_screening(market="us", min_volume=-1, limit=20)

    assert mock_screen.await_count == 0


@pytest.mark.asyncio
async def test_list_screening_min_volume_overfetch_caps_at_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        return_value={
            "results": [{"code": "AAPL", "volume": 1000}],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {"market": "us"},
            "market": "us",
        }
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=fake_redis)
    result = await service.list_screening(market="us", min_volume=1000, limit=80)

    assert result["cache_hit"] is False
    await_args = mock_screen.await_args
    assert await_args is not None
    call_kwargs = await_args.kwargs
    # With limit=80, overfetch should be min(100, max(80*3, 80)) = min(100, 240) = 100
    assert call_kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_refresh_screening_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    mock_screen = AsyncMock(
        side_effect=[
            {
                "results": [{"code": "AAPL", "volume": 1200}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
            {
                "results": [{"code": "MSFT", "volume": 1500}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
            },
        ]
    )
    monkeypatch.setattr("app.services.screener_service.screen_stocks_impl", mock_screen)

    service = ScreenerService(redis_client=cast(Any, fake_redis))

    first = await service.list_screening(market="us", min_volume=1000, limit=5)
    cached = await service.list_screening(market="us", min_volume=1000, limit=5)
    refreshed = await service.refresh_screening(market="us", min_volume=1000, limit=5)
    recached = await service.list_screening(market="us", min_volume=1000, limit=5)

    assert first["results"][0]["code"] == "AAPL"
    assert cached["cache_hit"] is True
    assert refreshed["results"][0]["code"] == "MSFT"
    assert refreshed["cache_hit"] is False
    assert recached["cache_hit"] is True
    assert mock_screen.await_count == 2


def _terra_snapshot(*route_ids: Any):
    """``review_terra``만 지정 순서로 채운 정책 snapshot."""
    from app.extensions.kasset.ai.runtime_config import (
        DEFAULT_ROUTE_POLICY,
        AiLane,
        AiRuntimeSnapshot,
        freeze_route_policy,
    )

    lanes = dict(DEFAULT_ROUTE_POLICY)
    lanes[AiLane.REVIEW_TERRA] = tuple(route_ids)
    return AiRuntimeSnapshot(
        revision=7,
        updated_at=None,
        updated_by_user_id=None,
        source="persisted",
        lanes=freeze_route_policy(lanes),
    )


def _install_report_route_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: Any,
) -> dict[str, list[str]]:
    """전송 계층을 가짜로 바꾸고 어떤 provider가 실제로 불렸는지 기록한다."""
    from app.core.config import settings
    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.ai import structured_router
    from app.mcp_server.tooling import analysis_tool_handlers
    from app.services import screener_service

    used: dict[str, list[str]] = {"built": [], "called": []}

    class _FakeMcpClient:
        def __init__(self, **_kwargs: object) -> None:
            used["built"].append("mcp")

        @property
        def name(self) -> str:
            return "mcp"

        @property
        def tool_name(self) -> str:
            return "run_skill"

        async def request_json(self, **kwargs: object) -> dict[str, Any]:
            used["called"].append("mcp")
            used["payload"] = kwargs["input_payload"]  # type: ignore[assignment]
            return _generated_report()

    class _FakeResponsesClient:
        def __init__(self, *, name: str, **_kwargs: object) -> None:
            self._name = name
            used["built"].append(name)

        @property
        def name(self) -> str:
            return self._name

        async def request_json(self, **kwargs: object) -> dict[str, Any]:
            used["called"].append(self._name)
            used["payload"] = kwargs["input_payload"]  # type: ignore[assignment]
            return _generated_report()

    async def _snapshot() -> Any:
        return snapshot

    async def _no_ledger(attempts: object) -> bool:
        return True

    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "http://mcp.test/mcp")
    monkeypatch.setattr(settings, "KASSET_AI_API_BASE_URL", "https://direct.invalid/v1")
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("direct-key"))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "model-terra")
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_BASE_URL", "https://router.invalid/api/v1"
    )
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr("openrouter-key")
    )
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "model-openrouter-pro"
    )
    monkeypatch.setattr(ai_factory, "McpStructuredJsonClient", _FakeMcpClient)
    monkeypatch.setattr(ai_factory, "OpenAiResponsesClient", _FakeResponsesClient)
    monkeypatch.setattr(structured_router, "record_ai_call_attempts", _no_ledger)
    monkeypatch.setattr(screener_service, "_report_route_snapshot", _snapshot)
    monkeypatch.setattr(
        analysis_tool_handlers,
        "analyze_stock_impl",
        AsyncMock(return_value={"success": True, "current_price": 101}),
    )
    return used


@pytest.mark.asyncio
async def test_generate_screener_report_uses_review_terra_mcp_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.extensions.kasset.ai.runtime_config import AiRouteId
    from app.services.screener_service import generate_screener_report

    used = _install_report_route_doubles(
        monkeypatch,
        snapshot=_terra_snapshot(
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
    )

    report = await generate_screener_report(market="us", symbol="KLAC", name="KLA")

    assert report["decision"] == "hold"
    assert used["called"] == ["mcp"]
    assert used["payload"]["analysis"] == {  # type: ignore[index]
        "success": True,
        "current_price": 101,
    }


@pytest.mark.asyncio
async def test_generate_screener_report_uses_direct_route_when_policy_drops_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영자가 ``mcp_tool``을 끄면 MCP URL이 있어도 direct fallback으로 간다."""
    from app.extensions.kasset.ai.runtime_config import AiRouteId
    from app.services.screener_service import generate_screener_report

    used = _install_report_route_doubles(
        monkeypatch,
        snapshot=_terra_snapshot(AiRouteId.DIRECT_TERRA, AiRouteId.OPENROUTER_PRO),
    )

    report = await generate_screener_report(market="us", symbol="KLAC", name="KLA")

    assert report["decision"] == "hold"
    assert used["called"] == ["direct-api"]
    assert "mcp" not in used["built"]


@pytest.mark.asyncio
async def test_generate_screener_report_falls_back_past_an_unavailable_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP가 사용 불가로 실패하면 정책의 다음 route가 이어받는다."""
    from app.extensions.kasset.ai import factory as ai_factory
    from app.extensions.kasset.ai.base import AiProviderUnavailable
    from app.extensions.kasset.ai.runtime_config import AiRouteId
    from app.services.screener_service import generate_screener_report

    used = _install_report_route_doubles(
        monkeypatch,
        snapshot=_terra_snapshot(
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
    )

    class _DownMcpClient:
        def __init__(self, **_kwargs: object) -> None:
            used["built"].append("mcp")

        @property
        def name(self) -> str:
            return "mcp"

        @property
        def tool_name(self) -> str:
            return "run_skill"

        async def request_json(self, **_kwargs: object) -> dict[str, Any]:
            used["called"].append("mcp")
            raise AiProviderUnavailable("sidecar down")

    monkeypatch.setattr(ai_factory, "McpStructuredJsonClient", _DownMcpClient)

    report = await generate_screener_report(market="us", symbol="KLAC", name="KLA")

    assert report["decision"] == "hold"
    assert used["called"] == ["mcp", "direct-api"]


@pytest.mark.asyncio
async def test_generate_screener_report_fails_closed_on_empty_terra_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.screener_service import generate_screener_report

    _install_report_route_doubles(monkeypatch, snapshot=_terra_snapshot())

    with pytest.raises(RuntimeError, match="review_terra"):
        await generate_screener_report(market="us", symbol="KLAC", name="KLA")


@pytest.mark.asyncio
async def test_request_report_reuses_completed_report() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    generator = AsyncMock(return_value=_generated_report())
    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=generator,
    )

    first = await service.request_report(market="us", symbol="AAPL", name="Apple")
    await _await_background_reports()
    second = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert first["status"] == "queued"
    assert second["job_id"] == first["job_id"]
    assert second["status"] == "completed"
    assert second["is_reused"] is True
    generator.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_report_concurrent_same_symbol_single_generation() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    release = asyncio.Event()

    async def delayed_report(**_kwargs: object) -> dict[str, Any]:
        await release.wait()
        return _generated_report()

    generator = AsyncMock(side_effect=delayed_report)
    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=generator,
    )

    first = await service.request_report(market="us", symbol="AAPL", name="Apple")
    second = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert first["job_id"] == second["job_id"]
    assert {first["is_reused"], second["is_reused"]} == {False, True}
    release.set()
    await _await_background_reports()
    generator.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_report_returns_failed_when_inflight_claim_is_unavailable() -> (
    None
):
    from app.services.screener_service import ScreenerService

    fake_redis = _AlwaysFailClaimRedis()
    service = ScreenerService(redis_client=cast(Any, fake_redis))

    result = await service.request_report(market="us", symbol="AAPL", name="Apple")

    assert result["status"] == "failed"
    assert result["is_reused"] is False
    assert result["error"] == "inflight_job_unavailable"

    status = await service.get_report_status(result["job_id"])
    assert status["status"] == "failed"
    assert status["error"] == "inflight_job_unavailable"


@pytest.mark.asyncio
async def test_get_report_status_marks_running_while_generation_is_inflight() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    release = asyncio.Event()

    async def delayed_report(**_kwargs: object) -> dict[str, Any]:
        await release.wait()
        return _generated_report()

    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=delayed_report,
    )
    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    await asyncio.sleep(0)
    status = await service.get_report_status(queued["job_id"])

    assert queued["status"] == "queued"
    assert status["status"] == "running"
    release.set()
    await _await_background_reports()


@pytest.mark.asyncio
async def test_get_report_status_unknown_job_returns_not_found_failed() -> None:
    from app.services.screener_service import ScreenerService

    service = ScreenerService(redis_client=cast(Any, _FakeRedis()))

    result = await service.get_report_status("missing-job")

    assert result == {
        "job_id": "missing-job",
        "status": "failed",
        "error": "job_not_found",
        "not_found": True,
    }


@pytest.mark.asyncio
async def test_request_report_marks_failed_when_mcp_generation_fails() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    generator = AsyncMock(side_effect=RuntimeError("mcp down"))
    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=generator,
    )

    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    await _await_background_reports()
    status = await service.get_report_status(queued["job_id"])

    assert status["status"] == "failed"
    assert "mcp down" in status["error"]


def test_report_generation_timeout_stays_below_the_inflight_ttl() -> None:
    from app.services.screener_service import ScreenerService

    assert (
        ScreenerService.REPORT_GENERATION_TIMEOUT_SECONDS
        < ScreenerService.REPORT_INFLIGHT_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_report_generation_times_out_and_releases_its_own_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """무제한 생성은 inflight TTL을 넘긴다. 상한에서 끊고 claim을 반납한다."""
    from app.services.screener_service import ScreenerService

    monkeypatch.setattr(
        ScreenerService, "REPORT_GENERATION_TIMEOUT_SECONDS", 0.05, raising=True
    )
    fake_redis = _FakeRedis()

    async def never_returns(**_kwargs: object) -> dict[str, Any]:
        await asyncio.sleep(30)
        raise AssertionError("generator must be cut off by the timeout")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=never_returns,
    )

    queued = await service.request_report(market="us", symbol="AAPL", name="Apple")
    await _await_background_reports()
    status = await service.get_report_status(queued["job_id"])

    assert status["status"] == "failed"
    assert status["error"] == "report_generation_timeout"
    assert "screener:report:inflight:us:AAPL" not in fake_redis.store


@pytest.mark.asyncio
async def test_failed_job_does_not_release_a_newer_jobs_inflight_claim() -> None:
    """늦게 실패한 job이 그 사이 시작한 job의 claim을 지우면 3차 생성이 열린다."""
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    release = asyncio.Event()

    async def blocked_report(**_kwargs: object) -> dict[str, Any]:
        await release.wait()
        raise RuntimeError("mcp down")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=blocked_report,
    )

    stale = await service.request_report(market="us", symbol="AAPL", name="Apple")
    await asyncio.sleep(0)
    # TTL이 만료된 것과 같은 상태: 다음 요청이 같은 key를 새 job_id로 다시 잡는다.
    inflight_key = "screener:report:inflight:us:AAPL"
    await fake_redis.set(inflight_key, "job-newer")

    release.set()
    await _await_background_reports()

    assert fake_redis.store[inflight_key] == "job-newer"
    status = await service.get_report_status(stale["job_id"])
    assert status["status"] == "failed"


@pytest.mark.asyncio
async def test_shutdown_cancels_tracked_report_tasks() -> None:
    from app.services import screener_service
    from app.services.screener_service import (
        ScreenerService,
        shutdown_screener_report_tasks,
    )

    fake_redis = _FakeRedis()
    started = asyncio.Event()

    async def hanging_report(**_kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("shutdown must cancel the generation task")

    service = ScreenerService(
        redis_client=cast(Any, fake_redis),
        report_generator=hanging_report,
    )

    await service.request_report(market="us", symbol="AAPL", name="Apple")
    await asyncio.wait_for(started.wait(), timeout=5)
    tracked = tuple(screener_service._BACKGROUND_TASKS)
    assert len(tracked) == 1

    await shutdown_screener_report_tasks()

    assert tracked[0].cancelled()
    assert not screener_service._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_callback_with_unknown_instrument_type_marks_failed() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    service = ScreenerService(redis_client=cast(Any, fake_redis))

    result = await service.process_callback(
        {
            "request_id": "job-unknown-type",
            "symbol": "AAPL",
            "name": "Apple",
            "instrument_type": "equity_jp",
            "decision": "hold",
            "confidence": 55,
            "reasons": ["r1"],
            "price_analysis": {
                "appropriate_buy_range": {"min": 100, "max": 110},
                "appropriate_sell_range": {"min": 120, "max": 130},
                "buy_hope_range": {"min": 95, "max": 98},
                "sell_target_range": {"min": 150, "max": 160},
            },
            "detailed_text": "report",
        }
    )

    assert result["status"] == "failed"
    assert "instrument_type must be one of" in result["error"]

    status = await service.get_report_status("job-unknown-type")
    assert status["status"] == "failed"
    assert "instrument_type must be one of" in status["error"]


@pytest.mark.asyncio
async def test_callback_payload_mismatch_marks_failed_and_clears_inflight() -> None:
    from app.services.screener_service import ScreenerService

    fake_redis = _FakeRedis()
    service = ScreenerService(redis_client=cast(Any, fake_redis))
    keys = service._report_keys("us", "AAPL", "job-mismatch")
    await service._store_json(
        keys.job_key,
        service.REPORT_CACHE_TTL_SECONDS,
        {
            "job_id": "job-mismatch",
            "market": "us",
            "symbol": "AAPL",
            "result_key": keys.result_key,
            "status_key": keys.status_key,
            "inflight_key": keys.inflight_key,
        },
    )
    await fake_redis.set(keys.status_key, "running")
    await fake_redis.set(keys.inflight_key, "job-mismatch")

    callback_result = await service.process_callback(
        {
            "request_id": "job-mismatch",
            "symbol": "MSFT",
            "name": "Microsoft",
            "instrument_type": "equity_us",
            "decision": "hold",
            "confidence": 55,
            "reasons": ["r1"],
            "price_analysis": {
                "appropriate_buy_range": {"min": 100, "max": 110},
                "appropriate_sell_range": {"min": 120, "max": 130},
                "buy_hope_range": {"min": 95, "max": 98},
                "sell_target_range": {"min": 150, "max": 160},
            },
            "detailed_text": "report",
        }
    )

    assert callback_result["status"] == "failed"
    assert "callback_payload_mismatch" in callback_result["error"]
    assert "screener:report:inflight:us:AAPL" not in fake_redis.store
    assert "screener:report:result:us:AAPL" not in fake_redis.store

    status = await service.get_report_status("job-mismatch")
    assert status["status"] == "failed"
    assert "callback_payload_mismatch" in status["error"]


@pytest.mark.asyncio
async def test_place_order_is_preview_only_and_confirm_fails_closed() -> None:
    from app.services import screener_service as module
    from app.services.screener_service import ScreenerService

    service = ScreenerService(redis_client=cast(Any, _FakeRedis()))

    preview = await service.place_order(
        market="us",
        symbol="aapl",
        side="buy",
        order_type="limit",
        quantity=1,
        price=100,
        confirm=False,
    )
    rejected = await service.place_order(
        market="us",
        symbol="aapl",
        side="buy",
        order_type="limit",
        quantity=1,
        price=100,
        confirm=True,
    )

    assert not hasattr(module, "_place_order_impl")
    assert preview["success"] is True
    assert preview["request"]["preview_only"] is True
    assert preview["request"]["dry_run"] is True
    assert preview["request"]["symbol"] == "AAPL"
    assert rejected["success"] is False
    assert rejected["error_code"] == "live_order_submission_unavailable"
    assert (
        rejected["error"] == "live order submission is not available on this endpoint"
    )


@pytest.mark.asyncio
async def test_list_screening_passes_min_consecutive_up_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any, cast

    from app.services.screener_service import ScreenerService

    captured: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "results": [],
            "stocks": [],
            "filters_applied": {},
            "timestamp": "2026-05-10T05:30:00+00:00",
        }

    monkeypatch.setattr(
        "app.services.screener_service.screen_stocks_impl",
        fake_screen,
    )
    fake_redis = _FakeRedis()
    svc = ScreenerService(redis_client=cast(Any, fake_redis))
    out = await svc.list_screening(market="kr", min_consecutive_up_days=5)
    assert captured.get("min_consecutive_up_days") == 5
    assert out.get("filters_applied", {}).get("min_consecutive_up_days") == 5


@pytest.mark.asyncio
async def test_normalize_screen_request_rejects_out_of_range_streak() -> None:
    from app.mcp_server.tooling.screening.common import normalize_screen_request

    with pytest.raises(ValueError):
        normalize_screen_request(
            market="kr",
            min_consecutive_up_days=0,
            asset_type=None,
            category=None,
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=50,
        )
    with pytest.raises(ValueError):
        normalize_screen_request(
            market="kr",
            min_consecutive_up_days=31,
            asset_type=None,
            category=None,
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=50,
        )
    out = normalize_screen_request(
        market="kr",
        min_consecutive_up_days=5,
        asset_type=None,
        category=None,
        sector=None,
        strategy=None,
        sort_by=None,
        sort_order=None,
        min_market_cap=None,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        min_dividend=None,
        min_analyst_buy=None,
        max_rsi=None,
        limit=50,
    )
    assert out["min_consecutive_up_days"] == 5
