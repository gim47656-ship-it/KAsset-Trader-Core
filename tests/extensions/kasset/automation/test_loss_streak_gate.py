"""활성 손실 연속 관문은 BUY만 차단하고 계산 불가는 통과시킨다."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extensions.kasset.automation import loss_streak_gate as gate_module
from app.extensions.kasset.automation.loss_streak_gate import (
    LOSS_STREAK_GATE_SCHEMA_VERSION,
    LossStreakGate,
)
from app.extensions.kasset.automation.shadow_loss_streak import ShadowPaperTradeFact

_NOW = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)
_OWNER = 17
_ACCOUNT_KEY = "paper:31"


class _Db:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def _fact(
    id_: int,
    *,
    minutes_ago: int,
    symbol: str = "005930",
    reason: str = "STOP",
    realized_pnl: str = "-1000",
) -> ShadowPaperTradeFact:
    return ShadowPaperTradeFact(
        id=id_,
        transaction_id=f"order-{id_}",
        trade_id=f"trade-{id_}",
        owner_user_id=_OWNER,
        account_key=_ACCOUNT_KEY,
        market="KRX",
        symbol=symbol,
        side="SELL",
        lifecycle_status="CLOSED",
        realized_pnl=Decimal(realized_pnl),
        exit_reason=reason,
        executed_at=_NOW - timedelta(minutes=minutes_ago),
    )


def _patch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    facts: tuple[ShadowPaperTradeFact, ...],
) -> tuple[AsyncMock, AsyncMock]:
    loader = AsyncMock(return_value=(_ACCOUNT_KEY, facts))
    persister = AsyncMock(return_value=())
    monkeypatch.setattr(gate_module, "_load_paper_trade_facts", loader)
    monkeypatch.setattr(gate_module, "persist_shadow_loss_locks", persister)
    monkeypatch.setattr(
        gate_module,
        "current_regular_session",
        lambda _market, _now: SimpleNamespace(
            opens_at=_NOW - timedelta(hours=1),
            closes_at=_NOW + timedelta(hours=5),
        ),
    )
    return loader, persister


async def _evaluate(*, symbol: str = "005930", side: str = "BUY"):
    return await LossStreakGate().evaluate(
        _Db(),  # type: ignore[arg-type]
        _OWNER,
        market="KRX",
        symbol=symbol,
        side=side,
        now=_NOW,
    )


@pytest.mark.asyncio
async def test_three_recent_stop_losses_block_buy_but_sell_always_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader, persister = _patch_dependencies(
        monkeypatch,
        (
            _fact(1, minutes_ago=30),
            _fact(2, minutes_ago=20),
            _fact(3, minutes_ago=10),
        ),
    )

    buy = await _evaluate()
    sell = await _evaluate(side="SELL")

    assert buy.code == "LOSS_STREAK"
    assert buy.passed is False
    assert buy.reason == "global_lock"
    assert buy.evidence["schemaVersion"] == LOSS_STREAK_GATE_SCHEMA_VERSION
    assert buy.evidence["streakGlobal"] == 3
    assert buy.evidence["globalLock"] == {
        "scope": "GLOBAL",
        "symbol": None,
        "streakCount": 3,
        "lossLimit": 3,
        "newestLossAt": (_NOW - timedelta(minutes=10)).isoformat(),
        "expiresAt": (_NOW + timedelta(minutes=50)).isoformat(),
        "reason": "LOSS_STREAK_LIMIT_REACHED",
    }
    assert sell.passed is True
    assert sell.reason == "sell_bypass"
    assert loader.await_count == 1
    persister.assert_awaited_once()


@pytest.mark.asyncio
async def test_general_exit_resets_stop_loss_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(
        monkeypatch,
        (
            _fact(1, minutes_ago=30),
            _fact(2, minutes_ago=20),
            _fact(3, minutes_ago=10, reason="TIME_STOP"),
        ),
    )

    result = await _evaluate()

    assert result.passed is True
    assert result.reason is None
    assert result.evidence["streakGlobal"] == 0
    assert result.evidence["streakSymbol"] == 0


@pytest.mark.asyncio
async def test_global_lock_expiry_allows_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(
        monkeypatch,
        (
            _fact(1, minutes_ago=80, symbol="000001"),
            _fact(2, minutes_ago=70, symbol="000002"),
            _fact(3, minutes_ago=61, symbol="000003"),
        ),
    )

    result = await _evaluate(symbol="005930")

    assert result.passed is True
    assert result.reason is None
    assert result.evidence["streakGlobal"] == 3
    assert result.evidence["globalLock"] is None


@pytest.mark.asyncio
async def test_two_symbol_losses_block_only_that_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(
        monkeypatch,
        (
            _fact(1, minutes_ago=20),
            _fact(2, minutes_ago=10),
        ),
    )

    samsung = await _evaluate(symbol="005930")
    hynix = await _evaluate(symbol="000660")

    assert samsung.passed is False
    assert samsung.reason == "symbol_lock"
    assert samsung.evidence["streakGlobal"] == 2
    assert samsung.evidence["streakSymbol"] == 2
    assert samsung.evidence["symbolLock"] == {
        "scope": "SYMBOL",
        "symbol": "005930",
        "streakCount": 2,
        "lossLimit": 2,
        "newestLossAt": (_NOW - timedelta(minutes=10)).isoformat(),
        "expiresAt": (_NOW + timedelta(hours=5)).isoformat(),
        "reason": "LOSS_STREAK_LIMIT_REACHED",
    }
    assert hynix.passed is True
    assert hynix.reason is None
    assert hynix.evidence["streakGlobal"] == 2
    assert hynix.evidence["streakSymbol"] == 0


@pytest.mark.asyncio
async def test_lock_persistence_failure_keeps_confirmed_buy_lock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _loader, persister = _patch_dependencies(
        monkeypatch,
        (
            _fact(1, minutes_ago=30),
            _fact(2, minutes_ago=20),
            _fact(3, minutes_ago=10),
        ),
    )
    persister.side_effect = RuntimeError("lock store offline")

    result = await _evaluate()

    assert result.passed is False
    assert result.reason == "global_lock"
    assert result.evidence["streakGlobal"] == 3
    assert result.evidence["persistFailed"] == "RuntimeError:lock store offline"
    assert "LOSS_STREAK persistence failed" in caplog.text


@pytest.mark.asyncio
async def test_database_exception_passes_with_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader, _persister = _patch_dependencies(monkeypatch, ())
    loader.side_effect = RuntimeError("database offline")

    result = await _evaluate()

    assert result.passed is True
    assert result.reason == "unavailable"
    assert result.detail == "unavailable=RuntimeError:database offline"
    assert result.evidence == {
        "schemaVersion": LOSS_STREAK_GATE_SCHEMA_VERSION,
        "globalLock": None,
        "symbolLock": None,
        "streakGlobal": 0,
        "streakSymbol": 0,
        "unavailable": "RuntimeError:database offline",
        "persistFailed": None,
    }
    assert "LOSS_STREAK gate unavailable" in caplog.text
