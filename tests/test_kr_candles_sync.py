from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services import kr_candles_sync_service as service
from app.services.candles_sync_common import build_symbol_union


def _universe(symbol: str, *, active: bool = True, nxt: bool = False):
    return SimpleNamespace(symbol=symbol, is_active=active, nxt_eligible=nxt)


def test_target_union_combines_toss_positions_and_manual_holdings() -> None:
    symbols = build_symbol_union(
        [SimpleNamespace(symbol="5930"), SimpleNamespace(symbol="035420")],
        [SimpleNamespace(ticker="005930"), SimpleNamespace(ticker="000660")],
        holdings_field="symbol",
        normalize_fn=service._normalize_symbol,
    )

    assert symbols == {"005930", "035420", "000660"}


def test_universe_validation_rejects_inactive_or_unregistered_symbols() -> None:
    with pytest.raises(ValueError, match="not registered"):
        service._validate_universe_rows(
            target_symbols={"005930", "000660"},
            universe_rows=[_universe("005930")],
            table_has_rows=True,
        )
    with pytest.raises(ValueError, match="inactive"):
        service._validate_universe_rows(
            target_symbols={"005930"},
            universe_rows=[_universe("005930", active=False)],
            table_has_rows=True,
        )


def test_toss_rows_use_session_compat_partition_without_provider_provenance() -> None:
    now = datetime(2025, 1, 2, 15, 31, 30, tzinfo=service._KST)
    frame = pd.DataFrame(
        {
            "datetime": [
                "2025-01-02 08:59:00",
                "2025-01-02 09:00:00",
                "2025-01-02 15:31:00",
            ],
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [1, 2, 3],
            "value": [1, 2, 3],
        }
    )

    rows = service._normalize_toss_rows(
        frame=frame,
        symbol="005930",
        nxt_eligible=True,
        allowed_days={now.date()},
        cutoff_kst=datetime(2025, 1, 2, 8, 0, tzinfo=service._KST),
        now_kst=now,
    )

    assert [(row.local_time.time(), row.venue) for row in rows] == [
        (datetime(2025, 1, 2, 8, 59).time(), "NTX"),
        (datetime(2025, 1, 2, 9, 0).time(), "KRX"),
    ]


@pytest.mark.asyncio
async def test_sync_uses_toss_and_manual_union(monkeypatch) -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "fetch_toss_portfolio_snapshot",
        AsyncMock(
            return_value=SimpleNamespace(
                positions=[
                    SimpleNamespace(symbol="005930", instrument_type="equity_kr")
                ],
                errors=[],
            )
        ),
    )
    holdings_service = SimpleNamespace(
        get_holdings_by_user=AsyncMock(return_value=[SimpleNamespace(ticker="000660")])
    )
    monkeypatch.setattr(
        service, "ManualHoldingsService", lambda _session: holdings_service
    )
    monkeypatch.setattr(
        service,
        "_load_universe_context",
        AsyncMock(
            return_value=(
                [_universe("005930"), _universe("000660")],
                True,
            )
        ),
    )
    sync_symbol = AsyncMock(return_value=(3, 1))
    monkeypatch.setattr(service, "_sync_symbol", sync_symbol)

    result = await service.sync_kr_candles(mode="backfill", sessions=1)

    assert result["source"] == "toss"
    assert result["symbols_total"] == 2
    assert result["symbol_venues_total"] == 2
    assert result["rows_upserted"] == 6
    assert {call.kwargs["symbol"] for call in sync_symbol.await_args_list} == {
        "005930",
        "000660",
    }
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_toss_source_is_rejected_before_provider_call() -> None:
    with pytest.raises(ValueError, match="source must be 'toss'"):
        await service.sync_kr_candles(mode="backfill", source="kis")
