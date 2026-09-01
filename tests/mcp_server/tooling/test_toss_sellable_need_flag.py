from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp_server.tooling import analysis_tool_handlers, portfolio_holdings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_build_batch_position_index_requests_no_sellable(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return [], [], kwargs.get("market"), None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    index, err = await analysis_tool_handlers._build_batch_position_index("kr")

    assert err is None
    assert index == {}
    # ROB-685: the batch index never reads sellable_quantity.
    assert captured["need_sellable"] is False
    assert captured["include_current_price"] is False


async def test_collect_portfolio_positions_defaults_to_no_sellable_and_allows_explicit_opt_in(
    monkeypatch,
):
    calls: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True, **_):
        calls.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: SimpleNamespace(usable=False),
    )
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)

    # Isolate the sibling active collectors — with market=None this path would
    # otherwise call the real Upbit API and manual-holdings DB. Only the Toss
    # forwarding contract belongs in this unit test.
    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    # General holdings reads omit sellable by default.
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    # Explicit opt-out remains no-op, and the lower-level opt-in is preserved
    # for the broker/order-adjacent caller only.
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, need_sellable=False
    )
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, need_sellable=True
    )

    assert calls == [False, False, True]


async def test_collect_toss_api_positions_defaults_need_sellable_false(monkeypatch):
    seen: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True, **_):
        seen.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)

    await portfolio_holdings._collect_toss_api_positions(None)
    await portfolio_holdings._collect_toss_api_positions(None, need_sellable=False)
    await portfolio_holdings._collect_toss_api_positions(None, need_sellable=True)

    assert seen == [False, False, True]


async def test_collect_toss_api_positions_skips_sellable_cache_and_cash(monkeypatch):
    seen: dict[str, Any] = {}
    sentinel_cache = object()

    async def fake_fetch(*, need_sellable=True, need_cash=True, sellable_cache=None):
        seen["need_sellable"] = need_sellable
        seen["need_cash"] = need_cash
        seen["sellable_cache"] = sellable_cache

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)
    monkeypatch.setattr(
        portfolio_holdings, "get_shared_sellable_cache", lambda: sentinel_cache
    )

    # General reads do not consult the sellable cache at all.
    await portfolio_holdings._collect_toss_api_positions(None)
    assert seen["need_sellable"] is False
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is None

    # The legacy flag cannot re-enable sellable reads on a general path.
    await portfolio_holdings._collect_toss_api_positions(None, fresh_sellable=True)
    assert seen["need_sellable"] is False
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is None


async def test_collect_portfolio_positions_forwards_fresh_sellable(monkeypatch):
    seen: list[bool] = []

    async def fake_collect_toss(
        market_filter, *, need_sellable=True, fresh_sellable=False
    ):
        seen.append(fresh_sellable)
        return [], [], False

    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings,
        "get_shared_portfolio_snapshot_cache",
        lambda: SimpleNamespace(usable=False),
    )
    monkeypatch.setattr(
        portfolio_holdings, "_collect_toss_api_positions", fake_collect_toss
    )
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, fresh_sellable=True
    )

    assert seen == [False, True]


async def test_get_holdings_impl_forwards_fresh_sellable(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        seen.update(kwargs)
        return [], [], None, None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    await portfolio_holdings._get_holdings_impl()
    assert seen["fresh_sellable"] is False

    await portfolio_holdings._get_holdings_impl(fresh_sellable=True)
    assert seen["fresh_sellable"] is True
