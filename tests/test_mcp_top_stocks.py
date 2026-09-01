from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from app.mcp_server.tooling import analysis_screening
from app.mcp_server.tooling.registry import register_all_tools


class _MCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            return func

        return decorator


def _tools() -> dict[str, Callable[..., Any]]:
    mcp = _MCP()
    register_all_tools(cast(Any, mcp))
    return mcp.tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ranking_type",
    [
        "volume",
        "market_cap",
        "gainers",
        "losers",
        "foreigners",
        "foreign_net_buy",
        "foreign_net_sell",
    ],
)
async def test_all_kr_rankings_are_explicitly_provider_unsupported(
    ranking_type: str,
) -> None:
    result = await _tools()["get_top_stocks"](
        market="kr", ranking_type=ranking_type, limit=3
    )

    assert result["success"] is False
    assert result["error"] == "provider_unsupported"
    assert result["detail"] == "KR rankings are unavailable from Toss/NH PLUG"
    assert result["source"] == "unsupported"


@pytest.mark.asyncio
async def test_us_rankings_remain_available(monkeypatch) -> None:
    async def rankings(ranking_type: str, limit: int):
        assert (ranking_type, limit) == ("volume", 3)
        return ([{"rank": 1, "symbol": "AAPL", "name": "Apple"}], "us-source")

    monkeypatch.setattr(analysis_screening, "_get_us_rankings", rankings)

    result = await _tools()["get_top_stocks"](
        market="us", ranking_type="volume", limit=3
    )

    assert result["source"] == "us-source"
    assert result["rankings"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_crypto_rankings_remain_available(monkeypatch) -> None:
    async def rankings(ranking_type: str, limit: int):
        assert (ranking_type, limit) == ("gainers", 2)
        return ([{"rank": 1, "symbol": "KRW-BTC"}], "upbit")

    monkeypatch.setattr(analysis_screening, "_get_crypto_rankings", rankings)

    result = await _tools()["get_top_stocks"](
        market="crypto", ranking_type="gainers", limit=2
    )

    assert result["source"] == "upbit"
    assert result["rankings"][0]["symbol"] == "KRW-BTC"
