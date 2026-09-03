from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services import us_candles_sync_service as service
from app.services.candles_sync_common import build_symbol_union


def _mock_session(*watchlist_symbols: str) -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    watchlist_result = MagicMock()
    watchlist_result.scalars.return_value.all.return_value = list(watchlist_symbols)
    session.execute = AsyncMock(return_value=watchlist_result)
    return session


def test_target_union_combines_toss_positions_and_manual_holdings() -> None:
    symbols = build_symbol_union(
        [SimpleNamespace(symbol="AAPL"), SimpleNamespace(symbol="MSFT")],
        [SimpleNamespace(ticker="AAPL"), SimpleNamespace(ticker="NVDA")],
        holdings_field="symbol",
        normalize_fn=service._normalize_symbol,
    )

    assert symbols == {"AAPL", "MSFT", "NVDA"}


def test_offset_aware_toss_minutes_normalize_to_utc_and_respect_bounds() -> None:
    frame = pd.DataFrame(
        {
            "datetime": [
                "2024-06-28T09:30:00-04:00",
                "2024-06-28T09:31:00-04:00",
                "2024-06-28T09:32:00-04:00",
            ],
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [1, 2, 3],
            "value": [1, 2, 3],
        }
    )

    rows = service._normalize_minute_page(
        frame=frame,
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2024, 6, 28, 13, 31, tzinfo=UTC),
    )

    assert [row.time_utc for row in rows] == [
        datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
        datetime(2024, 6, 28, 13, 31, tzinfo=UTC),
    ]
    assert all(row.local_time.tzinfo == service._NY for row in rows)


@pytest.mark.asyncio
async def test_collect_window_uses_toss_only(monkeypatch) -> None:
    toss = AsyncMock(
        return_value=pd.DataFrame(
            {
                "datetime": ["2024-06-28T09:30:00-04:00"],
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [10.0],
                "value": [15.0],
            }
        )
    )
    monkeypatch.setattr(service, "fetch_us_intraday_toss_frame", toss)

    rows, requests = await service._collect_window_rows(
        symbol="AAPL",
        exchange="NASD",
        lower_bound_utc=datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
        upper_bound_utc=datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
    )

    assert requests == 1
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    toss.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_uses_manual_and_toss_position_union(monkeypatch) -> None:
    session = _mock_session()
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "fetch_toss_portfolio_snapshot",
        AsyncMock(
            return_value=SimpleNamespace(
                positions=[SimpleNamespace(symbol="AAPL", instrument_type="equity_us")]
            )
        ),
    )
    manual = SimpleNamespace(
        get_holdings_by_user=AsyncMock(return_value=[SimpleNamespace(ticker="MSFT")])
    )
    monkeypatch.setattr(service, "ManualHoldingsService", lambda _session: manual)
    monkeypatch.setattr(
        service,
        "_resolve_symbol_pairs",
        AsyncMock(
            return_value=service.ResolvedSymbolPairs(
                symbol_pairs=[("AAPL", "NASD"), ("MSFT", "NASD")],
                skipped_symbols=[],
                lookup_refresh_attempted=False,
            )
        ),
    )
    window = service.SessionWindow(
        session=pd.Timestamp("2024-06-28"),
        open_utc=datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
        close_utc=datetime(2024, 6, 28, 20, 0, tzinfo=UTC),
        last_minute_utc=datetime(2024, 6, 28, 19, 59, tzinfo=UTC),
    )
    monkeypatch.setattr(service, "_select_closed_sessions", lambda *_: [window])
    collect = AsyncMock(return_value=([], 1))
    monkeypatch.setattr(service, "_collect_window_rows", collect)
    monkeypatch.setattr(service, "_upsert_rows", AsyncMock(return_value=0))

    result = await service.sync_us_candles(mode="backfill", sessions=1)

    assert result["symbols_total"] == 2
    assert result["pairs_processed"] == 2
    assert result["holdings_snapshot_ok"] is True
    assert {call.kwargs["symbol"] for call in collect.await_args_list} == {
        "AAPL",
        "MSFT",
    }
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_uses_us_watchlist_when_holdings_are_empty(monkeypatch) -> None:
    session = _mock_session(" nvda ")
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "fetch_toss_portfolio_snapshot",
        AsyncMock(return_value=SimpleNamespace(positions=[])),
    )
    manual = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "ManualHoldingsService", lambda _session: manual)
    resolve = AsyncMock(
        return_value=service.ResolvedSymbolPairs(
            symbol_pairs=[("NVDA", "NASD")],
            skipped_symbols=[],
            lookup_refresh_attempted=False,
        )
    )
    monkeypatch.setattr(service, "_resolve_symbol_pairs", resolve)
    monkeypatch.setattr(service, "_select_closed_sessions", lambda *_: [])

    result = await service.sync_us_candles(mode="backfill", sessions=1)

    assert resolve.await_args.kwargs["target_symbols"] == {"NVDA"}
    assert result["symbols_total"] == 1
    assert "reason" not in result
    assert result["holdings_snapshot_ok"] is True
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_continues_with_watchlist_when_snapshot_fails(
    monkeypatch, caplog
) -> None:
    session = _mock_session("AAPL")
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "fetch_toss_portfolio_snapshot",
        AsyncMock(side_effect=RuntimeError("snapshot unavailable")),
    )
    manual = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "ManualHoldingsService", lambda _session: manual)
    resolve = AsyncMock(
        return_value=service.ResolvedSymbolPairs(
            symbol_pairs=[("AAPL", "NASD")],
            skipped_symbols=[],
            lookup_refresh_attempted=False,
        )
    )
    monkeypatch.setattr(service, "_resolve_symbol_pairs", resolve)
    monkeypatch.setattr(service, "_select_closed_sessions", lambda *_: [])
    caplog.set_level("WARNING", logger=service.__name__)

    result = await service.sync_us_candles(mode="backfill", sessions=1)

    assert resolve.await_args.kwargs["target_symbols"] == {"AAPL"}
    assert result["symbols_total"] == 1
    assert result["holdings_snapshot_ok"] is False
    assert "RuntimeError" in caplog.text
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_returns_no_target_when_all_symbol_sources_are_empty(
    monkeypatch,
) -> None:
    session = _mock_session()
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "fetch_toss_portfolio_snapshot",
        AsyncMock(return_value=SimpleNamespace(positions=[])),
    )
    manual = SimpleNamespace(get_holdings_by_user=AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "ManualHoldingsService", lambda _session: manual)
    resolve = AsyncMock()
    monkeypatch.setattr(service, "_resolve_symbol_pairs", resolve)

    result = await service.sync_us_candles(mode="backfill", sessions=1)

    assert result["reason"] == "no_target_symbols"
    assert result["symbols_total"] == 0
    assert result["holdings_snapshot_ok"] is True
    resolve.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_toss_provider_failure_propagates(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "fetch_us_intraday_toss_frame",
        AsyncMock(side_effect=RuntimeError("toss unavailable")),
    )

    with pytest.raises(RuntimeError, match="toss unavailable"):
        await service._collect_window_rows(
            symbol="AAPL",
            exchange="NASD",
            lower_bound_utc=datetime(2024, 6, 28, 13, 30, tzinfo=UTC),
            upper_bound_utc=datetime(2024, 6, 28, 13, 31, tzinfo=UTC),
        )
