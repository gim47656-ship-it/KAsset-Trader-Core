"""ROB-273 — snapshot-backed collector tests.

Each test verifies that the collector emits a well-formed
:class:`SnapshotCollectResult` and never reaches into broker /
order / watch / scheduler write paths.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _request(
    market: str = "kr",
    account_scope: str = "toss_live",
    symbols: list[str] | None = None,
    *,
    user_id: int | None = 42,
) -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=symbols,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Portfolio collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_portfolio_collector_returns_holdings(monkeypatch: pytest.MonkeyPatch):
    """manual-primary 경로가 운영 Toss scope 밖에서 그대로 유지되는지 확인한다.

    KR/US의 ``toss_live``는 live portfolio collector를 사용하고, 그 외
    조합은 manual holdings를 ``primary_source="manual"``로 노출한다.
    """
    from app.models.manual_holdings import MarketType

    session = MagicMock()

    class _Row:
        ticker = "AAPL"
        market_type = MarketType.US
        quantity = 10
        avg_price = 150.0
        display_name = "Apple"
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[_Row()])
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    # us + alpaca_paper is a non-canonical (collector-only) combo that still
    # falls back to manual_primary — exactly the contract this test asserts.
    results = await collector.collect(
        _request(market="us", account_scope="alpaca_paper")
    )
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].source_kind == "auto_trader_mcp"
    assert results[0].payload_json["count"] == 1
    assert results[0].payload_json["holdings"][0]["ticker"] == "AAPL"
    # ROB-278 — payload v2 surfaces primary_source even on the v1 path.
    assert results[0].payload_json["primary_source"] == "manual"


@pytest.mark.asyncio
async def test_portfolio_collector_empty_holdings_returns_partial():
    """No matching holdings → result still emitted, status='partial'."""
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    result = MagicMock(scalars=MagicMock(return_value=scalars))
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    results = await collector.collect(
        _request(market="us", account_scope="alpaca_paper")
    )
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "partial"
    assert results[0].payload_json["count"] == 0


# ---------------------------------------------------------------------------
# Portfolio — KR/US Toss 일반 snapshot primary, 수동 보유는 owner-scoped reference.
# ---------------------------------------------------------------------------
def _equity_request(
    market: str = "kr",
    *,
    account_scope: str = "toss_live",
    user_id: int | None = 42,
) -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _empty_manual_session() -> MagicMock:
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _manual_kr_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualRow:
        def __init__(
            self,
            ticker: str = "005930",
            quantity: float = 5.0,
            avg_price: float = 70_000,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.KR
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _toss_position(
    *,
    symbol: str = "005930",
    market: str = "kr",
) -> Any:
    from decimal import Decimal

    from app.services.toss_portfolio_service import TossPortfolioPosition

    is_kr = market == "kr"
    return TossPortfolioPosition(
        account="toss",
        account_name="Toss",
        broker="toss",
        source="toss_api",
        instrument_type="equity_kr" if is_kr else "equity_us",
        market=market,
        symbol=symbol,
        name="삼성전자" if is_kr else "Apple",
        quantity=Decimal("10"),
        avg_buy_price=Decimal("70000" if is_kr else "150"),
        current_price=Decimal("75000" if is_kr else "180"),
        evaluation_amount=Decimal("750000" if is_kr else "1800"),
        profit_loss=Decimal("50000" if is_kr else "300"),
        profit_rate=Decimal("0.0714"),
        sellable_quantity=Decimal("8"),
    )


def _toss_snapshot(*positions: Any, errors: list[dict[str, Any]] | None = None) -> Any:
    from decimal import Decimal

    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    return TossPortfolioSnapshot(
        positions=list(positions),
        cash_krw=Decimal("1200000"),
        cash_usd=Decimal("500"),
        errors=errors or [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["kis_live", "kis_mock"])
async def test_portfolio_rejects_non_operational_kis_scopes(scope: str):
    fetcher = AsyncMock(return_value=_toss_snapshot())
    collector = PortfolioSnapshotCollector(
        _empty_manual_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request(account_scope=scope, user_id=42))

    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason"] == "provider kis is not operational"
    fetcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_portfolio_toss_live_missing_user_id_is_fail_closed():
    fetcher = AsyncMock(return_value=_toss_snapshot())
    collector = PortfolioSnapshotCollector(
        _empty_manual_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request(user_id=None))

    assert results[0].freshness_status == "unavailable"
    assert results[0].errors_json["reason_code"] == "user_id_missing"
    assert results[0].payload_json["primary_source"] == "none"
    fetcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_portfolio_toss_live_uses_general_snapshot_without_sellable_evidence():
    fetcher = AsyncMock(
        return_value=_toss_snapshot(
            _toss_position(),
            _toss_position(symbol="AAPL", market="us"),
        )
    )
    collector = PortfolioSnapshotCollector(
        _manual_kr_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request())

    payload = results[0].payload_json
    assert payload["primary_source"] == "toss"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "005930"
    assert payload["holdings"][0]["source"] == "toss_api"
    assert payload["holdings"][0]["sellable_quantity"] is None
    assert payload["holdings"][0]["pending_sell_quantity"] is None
    assert payload["sellable_summary"] is None
    assert payload["cash"] is None
    assert payload["buying_power"]["krw"] is not None
    assert payload["reference_holdings"][0]["source"] == "manual"
    assert payload["provenance"]["toss_fetch_status"] == "ok"
    assert payload["nav_scope"] == "toss_primary_general_snapshot"
    fetcher.assert_awaited_once_with(need_sellable=False, need_cash=True)


@pytest.mark.asyncio
async def test_portfolio_toss_failure_does_not_promote_manual_or_leak_detail():
    fetcher = AsyncMock(side_effect=RuntimeError("secret-token"))
    collector = PortfolioSnapshotCollector(
        _manual_kr_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request())

    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["reference_holdings"][0]["source"] == "manual"
    assert payload["provenance"]["toss_fetch_status"] == "failed"
    assert "secret-token" not in str(results[0].errors_json)


# ---------------------------------------------------------------------------
# Portfolio v2 — crypto+upbit_live는 ``UpbitHomeReader``를 사용한다.
#
# live holdings와 KRW cash/orderable을 primary로 제공하고, manual CRYPTO
# rows는 reference-only로 유지한다. 따라서 실제 계정이 있는데도
# NAV/buying power가 0으로 보이는 회귀를 막는다.
# ---------------------------------------------------------------------------
def _crypto_upbit_request(user_id: int | None = 1) -> CollectorRequest:
    return CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _manual_crypto_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualCryptoRow:
        def __init__(
            self,
            ticker: str = "KRW-BTC",
            quantity: float = 0.01,
            avg_price: float = 40_000_000.0,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.CRYPTO
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualCryptoRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _upbit_reader_with_holdings(
    *,
    warning: Any | None = None,
    cash_krw: float | None = 365_342.0,
    hidden_dust: int = 0,
    hidden_inactive: int = 0,
) -> MagicMock:
    """UpbitHomeReader stub: one live BTC holding + KRW cash/orderable.

    Upbit reports the same KRW figure for cash balance and buying power
    (orderable), and never carries a USD leg — mirrors the real reader.
    ``cash_krw=None`` models a KRW-less (all-coin) account; ``hidden_*`` model
    the reader's dust/inactive filtering.
    """
    from app.schemas.invest_home import (
        Account,
        CashAmounts,
        Holding,
        InvestHomeHiddenCounts,
    )
    from app.services.invest_home_service import _SourceFetchResult

    holding_btc = Holding(
        holdingId="upbit:BTC",
        accountId="upbit_account",
        source="upbit",
        accountKind="live",
        symbol="BTC",
        market="CRYPTO",
        assetType="crypto",
        assetCategory="crypto",
        displayName="BTC",
        quantity=0.235,
        averageCost=90_000_000.0,
        costBasis=21_150_000.0,
        currency="KRW",
        valueNative=25_384_658.0,
        valueKrw=25_384_658.0,
        pnlKrw=4_234_658.0,
        pnlRate=0.20,
        priceState="live",
    )
    account = Account(
        accountId="upbit_account",
        displayName="Upbit",
        source="upbit",
        accountKind="live",
        includedInHome=True,
        valueKrw=25_384_658.0,
        costBasisKrw=21_150_000.0,
        pnlKrw=4_234_658.0,
        pnlRate=0.20,
        cashBalances=CashAmounts(krw=cash_krw, usd=None),
        buyingPower=CashAmounts(krw=cash_krw, usd=None),
    )
    hidden_counts = InvestHomeHiddenCounts()
    hidden_counts.upbitDust = hidden_dust
    hidden_counts.upbitInactive = hidden_inactive
    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[account],
            holdings=[holding_btc],
            warning=warning,
            hidden_counts=hidden_counts,
        )
    )
    return reader


def _upbit_reader_failed() -> MagicMock:
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import _SourceFetchResult

    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[],
            holdings=[],
            warning=InvestHomeWarning(source="upbit", message="upbit auth refused"),
        )
    )
    return reader


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_success_populates_upbit_primary():
    """ROB-369 E9 — Upbit live success: primary_source=upbit, live KRW NAV/cash,
    manual CRYPTO rows surface as reference only (never promoted)."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings()
    collector = PortfolioSnapshotCollector(
        session,
        upbit_reader=upbit_reader,
    )
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    assert payload["primary_source"] == "upbit"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "BTC"
    assert payload["holdings"][0]["source"] == "upbit"
    assert payload["holdings"][0]["value_krw"] == 25_384_658.0
    # KRW cash + orderable surfaced — the E9 fix (not None/0).
    assert payload["cash"]["krw"] == 365_342.0
    assert payload["buying_power"]["krw"] == 365_342.0
    # Crypto has no pending-sell concept on the reader.
    assert payload["sellable_summary"] is None
    # Manual CRYPTO row visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["ticker"] == "KRW-BTC"
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance.
    assert payload["provenance"]["upbit_fetch_status"] == "ok"
    assert payload["provenance"]["account_scope"] == "upbit_live"
    upbit_reader.fetch.assert_awaited_once_with(user_id=1)


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_failure_does_not_promote_manual():
    """ROB-369 E9 — Upbit fetch failure: primary_source=none, manual stays reference."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_failed()
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["count"] == 0
    assert payload["cash"] is None
    assert payload["buying_power"] is None
    # Manual remains visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    assert payload["provenance"]["upbit_fetch_status"] == "failed"
    assert "upbit" in str(payload["provenance"]["warnings"]).lower()


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_exception_is_fail_closed():
    """ROB-369 E9 — UpbitHomeReader raising is treated like 'failed', not a crash."""
    session = _manual_crypto_session()
    upbit_reader = MagicMock()
    upbit_reader.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert "boom" in payload["provenance"]["errors"][0]


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_price_warning_is_partial():
    """ROB-369 E9 — a price warning with live holdings → partial, still upbit primary."""
    from app.schemas.invest_home import InvestHomeWarning

    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(
        warning=InvestHomeWarning(
            source="upbit",
            message="일부 코인은 현재가가 없어 평가금액에서 제외했습니다.",
        )
    )
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    assert results[0].freshness_status == "partial"
    assert payload["primary_source"] == "upbit"
    assert payload["provenance"]["upbit_fetch_status"] == "partial"
    assert payload["count"] == 1


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_krw_less_account_emits_explicit_zero():
    """ROB-369 E9 — a KRW-less (all-coin) Upbit account reports krw=None on the
    reader. The collector must coerce to an explicit 0.0 (the honest value for
    Upbit, unlike KIS-overseas None=unsupported) so the portfolio citation
    ``$.buying_power.krw`` resolves to a real number, not null."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(cash_krw=None)
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    # Successful fetch — a 0-KRW coin-only account is a complete, valid state.
    assert results[0].freshness_status == "fresh"
    assert payload["primary_source"] == "upbit"
    # Explicit 0.0, never None — the citation must point at a real value.
    assert payload["cash"]["krw"] == 0.0
    assert payload["buying_power"]["krw"] == 0.0
    assert payload["cash"]["krw"] is not None
    assert payload["buying_power"]["krw"] is not None


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_surfaces_hidden_dust_count():
    """ROB-369 E9 — the reader hides dust (<5000 KRW) and inactive coins, so the
    snapshot NAV undercounts the raw Upbit eval. Surface the hidden counts in
    coverage so the divergence is auditable rather than silent."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(hidden_dust=3, hidden_inactive=2)
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    coverage = results[0].coverage_json
    assert coverage["hidden_dust_count"] == 3
    assert coverage["hidden_inactive_count"] == 2


# ---------------------------------------------------------------------------
# Portfolio — US Toss snapshot은 US 포지션만 primary로 사용한다.
# ---------------------------------------------------------------------------
def _manual_us_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualUSRow:
        def __init__(
            self,
            ticker: str = "AAPL",
            quantity: float = 5.0,
            avg_price: float = 140.0,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.US
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualUSRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


@pytest.mark.asyncio
async def test_portfolio_us_toss_snapshot_filters_market_without_manual_sum():
    fetcher = AsyncMock(
        return_value=_toss_snapshot(
            _toss_position(),
            _toss_position(symbol="AAPL", market="us"),
        )
    )
    collector = PortfolioSnapshotCollector(
        _manual_us_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request("us"))

    payload = results[0].payload_json
    assert payload["primary_source"] == "toss"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "AAPL"
    assert payload["holdings"][0]["quantity"] != (
        payload["reference_holdings"][0]["quantity"]
        + payload["holdings"][0]["quantity"]
    )
    assert payload["holdings"][0]["sellable_quantity"] is None
    assert payload["sellable_summary"] is None
    assert payload["cash"] is None
    assert payload["buying_power"]["usd"] is not None


# ---------------------------------------------------------------------------
# Journal collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_collector_returns_active_and_recent():
    session = MagicMock()

    class _ActiveJournal:
        id = 1
        symbol = "005930"
        instrument_type = MagicMock(value="kr_stock")
        side = "buy"
        status = "active"
        entry_price = 70_000
        quantity = 10
        thesis = "thesis"
        strategy = "swing"
        target_price = 80_000
        stop_loss = 65_000
        hold_until = None
        exit_price = None
        exit_reason = None
        pnl_pct = None
        account_type = "live"
        account = "toss"
        created_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    active_scalars = MagicMock(all=MagicMock(return_value=[_ActiveJournal()]))
    active_result = MagicMock(scalars=MagicMock(return_value=active_scalars))
    recent_scalars = MagicMock(all=MagicMock(return_value=[]))
    recent_result = MagicMock(scalars=MagicMock(return_value=recent_scalars))
    session.execute = AsyncMock(side_effect=[active_result, recent_result])

    collector = JournalSnapshotCollector(session)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].snapshot_kind == "journal"
    assert results[0].payload_json["active_count"] == 1
    assert results[0].payload_json["retrospective_count"] == 0


# ---------------------------------------------------------------------------
# Watch-context collector — MUST NOT call activation paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_watch_context_collector_uses_only_read_methods():
    """The collector reads via list_active_alerts and never touches activation."""

    session = MagicMock()
    repo = MagicMock()

    class _Alert:
        alert_uuid = "11111111-1111-1111-1111-111111111111"
        source_report_uuid = "22222222-1111-1111-1111-111111111111"
        source_item_uuid = "33333333-1111-1111-1111-111111111111"
        market = "kr"
        symbol = "005930"
        metric = "price"
        operator = "above"
        threshold = 80_000
        threshold_key = "80000"
        intent = "buy_review"
        action_mode = "notify_only"
        rationale = "rationale"
        valid_until = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
        status = "active"
        activated_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)

    repo.list_active_alerts = AsyncMock(return_value=[_Alert()])
    # Force the test to fail if the collector tries to activate/insert/transition.
    repo.insert_alert = MagicMock(
        side_effect=AssertionError("collector must not insert_alert")
    )
    repo.update_alert_status = MagicMock(
        side_effect=AssertionError("collector must not update_alert_status")
    )

    collector = WatchContextSnapshotCollector(session, repository=repo)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "watch_context"
    assert results[0].payload_json["active_count"] == 1
    repo.list_active_alerts.assert_awaited_once()
    # Mutation methods must not have been called.
    assert not repo.insert_alert.called
    assert not repo.update_alert_status.called


# ---------------------------------------------------------------------------
# Market collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_collector_returns_events():
    from app.schemas.market_events import MarketEventsRangeResponse

    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(
        return_value=MarketEventsRangeResponse(
            from_date=dt.date(2026, 5, 19),
            to_date=dt.date(2026, 5, 20),
            count=0,
            events=[],
        )
    )
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "market"
    assert results[0].payload_json["event_count"] == 0


@pytest.mark.asyncio
async def test_market_collector_query_failure_returns_unavailable():
    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(side_effect=RuntimeError("boom"))
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "boom" in results[0].errors_json["reason"]


def _empty_events_query() -> MagicMock:
    from app.schemas.market_events import MarketEventsRangeResponse

    query = MagicMock()
    query.list_for_range = AsyncMock(
        return_value=MarketEventsRangeResponse(
            from_date=dt.date(2026, 5, 19),
            to_date=dt.date(2026, 5, 20),
            count=0,
            events=[],
        )
    )
    return query


@pytest.mark.asyncio
async def test_market_collector_us_populates_indices_dict():
    # ROB-366 B5: market-conditioned US index set, list-rows adapted into the
    # dict-of-dicts MarketStage reads, with change_pct → change_percent.
    captured: dict = {}

    async def fake_index_fn(symbols):
        captured["symbols"] = list(symbols)
        return [
            {"symbol": "SPX", "name": "S&P 500", "current": 5000.0, "change_pct": 1.1},
            {
                "symbol": "NASDAQ",
                "name": "NASDAQ",
                "current": 16000.0,
                "change_pct": 0.8,
            },
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    payload = results[0].payload_json
    assert payload["indices"]["SPX"]["change_percent"] == 1.1
    assert payload["indices"]["NASDAQ"]["change_percent"] == 0.8
    assert "events" in payload  # events payload still emitted
    # market-conditioned symbol set requested from the source
    assert "SPX" in captured["symbols"] and "NASDAQ" in captured["symbols"]


@pytest.mark.asyncio
async def test_market_collector_kr_populates_kospi():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.5},
            {
                "symbol": "KOSDAQ",
                "name": "코스닥",
                "current": 850.0,
                "change_pct": -0.2,
            },
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))
    payload = results[0].payload_json
    assert payload["indices"]["KOSPI"]["change_percent"] == 0.5
    assert "events" in payload


@pytest.mark.asyncio
async def test_market_collector_preserves_index_freshness_metadata():
    async def fake_index_fn(symbols):
        return [
            {
                "symbol": "KOSPI",
                "name": "KOSPI",
                "current": 2700.0,
                "change_pct": -0.46,
                "quote_asof": "2026-07-06T09:05:00+09:00",
                "data_state": "stale",
                "data_state_reason": "kr_index_quote_lagging",
                "quote_lag_seconds": 300,
            }
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))

    kospi = results[0].payload_json["indices"]["KOSPI"]
    assert kospi["change_percent"] == -0.46
    assert kospi["quote_asof"] == "2026-07-06T09:05:00+09:00"
    assert kospi["data_state"] == "stale"
    assert kospi["data_state_reason"] == "kr_index_quote_lagging"
    assert kospi["quote_lag_seconds"] == 300


@pytest.mark.asyncio
async def test_market_collector_omits_index_with_none_change_pct():
    # yfinance previous_close missing → change_pct None must be omitted, never 0.0.
    async def fake_index_fn(symbols):
        return [{"symbol": "SPX", "name": "S&P 500", "change_pct": None}]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    payload = results[0].payload_json
    assert payload.get("indices", {}) == {}


@pytest.mark.asyncio
async def test_market_collector_index_fetch_failure_is_soft():
    async def fake_index_fn(symbols):
        raise RuntimeError("yfinance down")

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    # Index failure is soft: events payload still emitted, snapshot still fresh.
    assert results[0].freshness_status == "fresh"
    assert "events" in results[0].payload_json
    assert results[0].payload_json.get("indices", {}) == {}


@pytest.mark.asyncio
async def test_market_collector_crypto_emits_no_indices_when_fn_returns_empty():
    # ROB-377: crypto now fetches the CRYPTO symbol, but if the source returns
    # empty (e.g. CoinGecko down), indices is still {} (fail-open).
    called = {"hit": False}

    async def fake_index_fn(symbols):
        called["hit"] = True
        return []

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="crypto"))
    assert results[0].payload_json.get("indices", {}) == {}
    assert called["hit"] is True  # crypto now triggers index fetch (ROB-377)


@pytest.mark.asyncio
async def test_market_collector_crypto_populates_indices_dict():
    # ROB-377 PR1: crypto market dimension gets a CRYPTO (total mcap) index so
    # MarketStage no longer fails closed for crypto.
    captured: dict = {}

    async def fake_index_fn(symbols):
        captured["symbols"] = list(symbols)
        return [
            {
                "symbol": "CRYPTO",
                "name": "암호화폐 총 시가총액",
                "current": 2.31e12,
                "change_pct": 1.85,
            }
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="crypto"))
    payload = results[0].payload_json
    assert payload["indices"]["CRYPTO"]["change_percent"] == 1.85
    assert "CRYPTO" in captured["symbols"]
    assert results[0].coverage_json["index_count"] == 1


@pytest.mark.asyncio
async def test_market_collector_no_index_fn_emits_no_indices():
    # Back-compat: without an injected source the payload is events-only.
    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query()
    )
    results = await collector.collect(_request(market="us"))
    assert "indices" not in results[0].payload_json


@pytest.mark.asyncio
async def test_market_collector_crypto_attaches_altseason():
    # ROB-381 PR3: crypto market dimension gains an Upbit altseason snapshot.
    called = {"hit": False}

    async def fake_altseason_fn():
        called["hit"] = True
        return {"ubai_ubmi_ratio": 0.455, "breadth": {"alts_beating_btc_pct": 0.42}}

    collector = MarketEventsSnapshotCollector(
        MagicMock(),
        query_service=_empty_events_query(),
        altseason_fn=fake_altseason_fn,
    )
    results = await collector.collect(_request(market="crypto"))
    payload = results[0].payload_json
    assert called["hit"] is True
    assert payload["altseason"]["ubai_ubmi_ratio"] == 0.455
    assert results[0].coverage_json["has_altseason"] is True


@pytest.mark.asyncio
async def test_market_collector_altseason_only_for_crypto():
    # Non-crypto markets never call the altseason source.
    called = {"hit": False}

    async def fake_altseason_fn():
        called["hit"] = True
        return {"ubai_ubmi_ratio": 0.5}

    collector = MarketEventsSnapshotCollector(
        MagicMock(),
        query_service=_empty_events_query(),
        altseason_fn=fake_altseason_fn,
    )
    results = await collector.collect(_request(market="us"))
    assert called["hit"] is False
    assert "altseason" not in results[0].payload_json
    assert results[0].coverage_json["has_altseason"] is False


@pytest.mark.asyncio
async def test_market_collector_altseason_failure_is_soft():
    # Altseason is best-effort: a fetch error leaves the rest of the snapshot.
    index_rows = [
        {
            "symbol": "CRYPTO",
            "name": "Crypto Total Market",
            "current": 3_100_000_000_000.0,
            "change_pct": 1.85,
        }
    ]

    async def fake_index_quote_fn(symbols):
        assert symbols == ["CRYPTO"]
        return index_rows

    async def fake_altseason_fn():
        raise RuntimeError("provider off")

    collector = MarketEventsSnapshotCollector(
        MagicMock(),
        query_service=_empty_events_query(),
        index_quote_fn=fake_index_quote_fn,
        altseason_fn=fake_altseason_fn,
    )
    results = await collector.collect(_request(market="crypto"))
    assert results[0].freshness_status == "fresh"
    assert results[0].errors_json["altseason"] == "RuntimeError: provider off"
    assert "events" in results[0].payload_json
    assert results[0].payload_json["indices"] == {
        "CRYPTO": {
            "change_percent": 1.85,
            "name": "Crypto Total Market",
            "current": 3_100_000_000_000.0,
        }
    }
    assert "altseason" not in results[0].payload_json


@pytest.mark.asyncio
async def test_market_collector_altseason_none_is_omitted():
    # Source returning None (both planes down) → no altseason key, not fabricated.
    async def fake_altseason_fn():
        return None

    collector = MarketEventsSnapshotCollector(
        MagicMock(),
        query_service=_empty_events_query(),
        altseason_fn=fake_altseason_fn,
    )
    results = await collector.collect(_request(market="crypto"))
    assert "altseason" not in results[0].payload_json
    assert results[0].coverage_json["has_altseason"] is False


@pytest.mark.asyncio
async def test_build_altseason_fn_returns_payload(monkeypatch):
    # ROB-381 PR3: registry adapter passes through the upbit altseason service.
    from app.services.action_report.snapshot_backed.collectors import registry

    async def fake_fetch():
        return {"ubai_ubmi_ratio": 0.47}

    monkeypatch.setattr(
        "app.services.external.upbit_index.fetch_upbit_altseason", fake_fetch
    )
    fn = registry._build_altseason_fn()
    assert await fn() == {"ubai_ubmi_ratio": 0.47}


@pytest.mark.asyncio
async def test_production_registry_preserves_altseason_error_diagnostic(monkeypatch):
    async def boom():
        raise RuntimeError("upbit down")

    monkeypatch.setattr("app.services.external.upbit_index.fetch_upbit_altseason", boom)
    collectors = production_collector_registry(MagicMock())
    collector = collectors.get("market")
    assert isinstance(collector, MarketEventsSnapshotCollector)
    collector._query = _empty_events_query()

    results = await collector.collect(_request(market="crypto"))

    assert results[0].freshness_status == "fresh"
    assert results[0].errors_json["altseason"] == "RuntimeError: upbit down"
    assert "altseason" not in results[0].payload_json
    assert results[0].payload_json["events"] == []


# ---------------------------------------------------------------------------
# News collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_news_collector_returns_citations():
    from app.schemas.research_reports import (
        ResearchReportCitation,
        ResearchReportCitationListResponse,
    )

    session = MagicMock()
    query = MagicMock()
    citation = ResearchReportCitation(
        report_uuid="44444444-1111-1111-1111-111111111111",
        title="t",
        source="kr_news",
        symbol_candidates=[],
        published_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        summary_text="s",
    )
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(count=1, citations=[citation])
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "news"
    assert results[0].source_kind == "news_ingestor"
    assert results[0].payload_json["count"] == 1


@pytest.mark.asyncio
async def test_news_collector_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.find_relevant = AsyncMock(side_effect=RuntimeError("transient"))
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"


# --- ROB-366 B8: market-scoped news articles via injected fetch fn ----------
@pytest.mark.asyncio
async def test_news_collector_articles_per_symbol_from_seam():
    from app.services.symbol_news_service import (
        SymbolNewsArticle,
        SymbolNewsFetchResult,
    )

    captured: list[tuple[str, str, int]] = []

    async def fake_fetch(symbol: str, market: str, limit: int):
        captured.append((symbol, market, limit))
        art = SymbolNewsArticle(
            provider="finnhub",
            market=market,
            symbol=symbol,
            external_article_id=f"id-{symbol}",
            title=f"{symbol} up",
            source_name="Reuters",
            canonical_url=f"https://x/{symbol}",
            summary="s",
            published_at=None,
            fetched_at=dt.datetime(2026, 5, 5, tzinfo=dt.UTC),
            provider_metadata={"sentiment": "positive"},
        )
        return SymbolNewsFetchResult(symbol, market, "finnhub", "ok", limit, 1, [art])

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=["AAPL", "MSFT"]))

    payload = results[0].payload_json
    assert payload["count"] == 2
    assert {a["symbol"] for a in payload["articles"]} == {"AAPL", "MSFT"}
    assert payload["articles"][0]["sentiment"] == "positive"
    assert payload["articles"][0]["external_article_id"] == "id-AAPL"
    assert [r["symbol"] for r in payload["fetch_records"]] == ["AAPL", "MSFT"]
    assert {c[0] for c in captured} == {"AAPL", "MSFT"}
    assert results[0].freshness_status == "fresh"


@pytest.mark.asyncio
async def test_news_collector_per_symbol_failure_is_fail_open():
    from app.services.symbol_news_service import SymbolNewsFetchResult

    async def fake_fetch(symbol: str, market: str, limit: int):
        return SymbolNewsFetchResult(
            symbol, market, "finnhub", "error", limit, 0, [], "RuntimeError"
        )

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=["AAPL"]))

    payload = results[0].payload_json
    assert payload["count"] == 0
    assert payload["fetch_records"][0]["status"] == "error"
    # fail-open: never raises, degrades to partial
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_news_collector_no_symbols_is_partial():

    async def fake_fetch(symbol: str, market: str, limit: int):  # pragma: no cover
        raise AssertionError("should not fetch without symbols")

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_fetch)
    results = await collector.collect(_request(market="us", symbols=[]))

    assert results[0].payload_json["count"] == 0
    assert results[0].freshness_status == "partial"


def _make_citation(*, report_uuid: str, symbols: list[str]):
    from app.schemas.research_reports import (
        ResearchReportCitation,
        ResearchReportSymbolCandidate,
    )

    return ResearchReportCitation(
        report_uuid=report_uuid,
        title=f"t-{report_uuid[:4]}",
        source="kr_news",
        symbol_candidates=[
            ResearchReportSymbolCandidate(symbol=s, market="kr") for s in symbols
        ],
        published_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        summary_text="s",
    )


@pytest.mark.asyncio
async def test_news_collector_filters_to_focus_symbols_when_supplied():
    """ROB-278 Phase 2 — request.symbols → return only citations that touch
    one of the focus symbols; record the symbol-match mapping."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["005930"],
        ),
        _make_citation(
            report_uuid="22222222-2222-2222-2222-222222222222",
            symbols=["999999"],  # not in focus
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    # Only the 005930 citation reached the output.
    assert payload["count"] == 1
    symbols_in_first = {
        cand["symbol"] for cand in payload["citations"][0]["symbol_candidates"]
    }
    assert symbols_in_first == {"005930"}
    # Per-symbol match map preserved.
    assert payload["symbol_matches"]["005930"] == 1
    assert payload["symbol_matches"]["000660"] == 0
    assert payload.get("no_data_reason") is None


@pytest.mark.asyncio
async def test_news_collector_no_focus_matches_surfaces_no_data_reason():
    """ROB-278 Phase 2 — focus symbols supplied, but no citation touches them →
    payload carries an explicit no_data_reason and a partial freshness."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["XYZ"],
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["count"] == 0
    assert payload["citations"] == []
    assert payload["no_data_reason"]
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_news_collector_no_focus_symbols_returns_general_feed():
    """ROB-278 Phase 2 — when no focus symbols, return general citations
    (legacy behaviour) but still emit the symbol_matches/no_data_reason fields."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["005930"],
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    # Legacy path: all citations included.
    assert payload["count"] == 1
    assert payload["symbol_matches"] == {}
    assert payload.get("no_data_reason") is None


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collector_cls",
    [
        NaverRemoteDebugStubCollector,
        TossRemoteDebugStubCollector,
        BrowserProbeStubCollector,
    ],
)
async def test_remote_debug_stubs_return_unavailable(collector_cls: type) -> None:
    collector = collector_cls()
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].snapshot_kind == collector.snapshot_kind


# ---------------------------------------------------------------------------
# Symbol collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_symbol_collector_returns_unavailable_when_no_symbols():
    session = MagicMock()
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(_request())  # symbols=None
    assert results[0].snapshot_kind == "symbol"
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_symbol_collector_returns_results_for_each_symbol():
    from app.services.investment_snapshots.collectors import CollectorRequest

    class _Row:
        def __init__(self, symbol: str, name: str) -> None:
            self.symbol = symbol
            self.name = name
            self.instrument_type = "equity_kr"
            self.exchange = "KRX"
            self.sector = "Tech"
            self.market_cap = 1_000_000.0
            self.is_active = True

    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[_Row("005930", "삼성전자")]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )

    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    # Two entries: one for resolved 005930, one partial for missing 000660.
    assert len(results) == 2
    assert any(r.symbol == "005930" for r in results)
    assert any(r.freshness_status == "partial" for r in results)


@pytest.mark.asyncio
async def test_symbol_collector_query_failure_is_fail_open():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("transient"))
    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    assert results[0].freshness_status == "unavailable"


def _us_universe_row(
    symbol: str,
    *,
    name_kr: str = "",
    name_en: str = "",
    exchange: str = "NASD",
    is_active: bool = True,
):
    class _Row:
        def __init__(self) -> None:
            self.symbol = symbol
            self.name_kr = name_kr
            self.name_en = name_en
            self.exchange = exchange
            self.is_active = is_active

    return _Row()


def _two_stage_session(stock_rows: list[Any], universe_rows: list[Any]) -> MagicMock:
    """Session whose 1st execute() returns stock_info rows, 2nd returns
    us_symbol_universe rows, and 3rd returns whether the universe is non-empty."""
    session = MagicMock()

    def _result(rows: list[Any]) -> MagicMock:
        scalars = MagicMock(all=MagicMock(return_value=rows))
        return MagicMock(scalars=MagicMock(return_value=scalars))

    any_rows_mock = MagicMock()
    any_rows_mock.scalar_one_or_none.return_value = 1 if universe_rows else None

    session.execute = AsyncMock(
        side_effect=[
            _result(stock_rows),
            _result(universe_rows),
            any_rows_mock,
        ]
    )
    return session


@pytest.mark.asyncio
async def test_symbol_collector_us_falls_back_to_universe_for_unheld():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # stock_info has the held name; the candidate is only in us_symbol_universe.
    session = _two_stage_session(
        stock_rows=[_stock_info_row("AAPL", "애플")],
        universe_rows=[_us_universe_row("HCA", name_en="HCA Healthcare")],
    )
    req = CollectorRequest(
        market="us",
        account_scope="toss_live",
        symbols=["AAPL", "HCA"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    resolved = {r.symbol for r in results if r.symbol}
    assert resolved == {"AAPL", "HCA"}
    hca = next(r for r in results if r.symbol == "HCA")
    assert hca.payload_json["instrument_type"] == "equity_us"
    assert hca.payload_json["name"] == "HCA Healthcare"
    assert hca.payload_json["exchange"] == "NASD"
    # No partial/missing row when everything resolved.
    assert all(r.freshness_status != "partial" for r in results)


@pytest.mark.asyncio
async def test_symbol_collector_us_prefers_stock_info_meta_no_dup():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # AAPL is in BOTH stock_info and the universe; stock_info must win and the
    # universe row must NOT produce a duplicate.
    session = _two_stage_session(
        stock_rows=[_stock_info_row("AAPL", "애플")],
        universe_rows=[_us_universe_row("AAPL", name_en="Apple Inc")],
    )
    req = CollectorRequest(
        market="us",
        account_scope="toss_live",
        symbols=["AAPL"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    aapl_rows = [r for r in results if r.symbol == "AAPL"]
    assert len(aapl_rows) == 1
    # stock_info meta preserved (sector/market_cap come only from stock_info).
    assert aapl_rows[0].payload_json["sector"] == "Tech"
    assert aapl_rows[0].payload_json["market_cap"] == 1_000_000.0
    assert aapl_rows[0].payload_json["name"] == "애플"


@pytest.mark.asyncio
async def test_symbol_collector_us_unresolved_reason_codes():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # NOPE: absent everywhere → not_registered.
    # DEAD: present in universe but inactive → inactive.
    session = _two_stage_session(
        stock_rows=[],
        universe_rows=[_us_universe_row("DEAD", name_en="Dead Co", is_active=False)],
    )
    req = CollectorRequest(
        market="us",
        account_scope="toss_live",
        symbols=["NOPE", "DEAD"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    unresolved = {
        u["symbol"]: u["reason_code"] for u in partial.payload_json["unresolved"]
    }
    assert unresolved == {"NOPE": "not_registered", "DEAD": "inactive"}
    # back-compat bulk list still present.
    assert set(partial.payload_json["missing_symbols"]) == {"NOPE", "DEAD"}


@pytest.mark.asyncio
async def test_symbol_collector_us_universe_empty_reason():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _two_stage_session(stock_rows=[], universe_rows=[])
    req = CollectorRequest(
        market="us",
        account_scope="toss_live",
        symbols=["NVDA"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    unresolved = {
        u["symbol"]: u["reason_code"] for u in partial.payload_json["unresolved"]
    }
    assert unresolved == {"NVDA": "universe_empty"}


@pytest.mark.asyncio
async def test_symbol_collector_kr_missing_has_no_unresolved_field():
    from app.services.investment_snapshots.collectors import CollectorRequest

    # KR resolves metadata, then reads the Toss/snapshot quote fallback.
    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])
    req = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)

    partial = next(r for r in results if r.freshness_status == "partial")
    assert partial.payload_json["missing_symbols"] == ["000660"]
    assert "unresolved" not in partial.payload_json
    resolved = next(r for r in results if r.symbol == "005930")
    assert resolved.payload_json["quote"]["venue"] == "toss"
    assert resolved.payload_json["quote"]["session"] == "snapshot"
    assert session.execute.await_count == 2


# ---------------------------------------------------------------------------
# Symbol collector equity quote cutover.
# ---------------------------------------------------------------------------
def _stock_info_row(symbol: str = "005930", name: str = "삼성전자"):
    class _Row:
        def __init__(self) -> None:
            self.symbol = symbol
            self.name = name
            self.instrument_type = "equity_kr"
            self.exchange = "KRX"
            self.sector = "Tech"
            self.market_cap = 1_000_000.0
            self.is_active = True

    return _Row()


def _stock_info_session(rows: list[Any]) -> MagicMock:
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["kis_live", "kis_mock"])
async def test_symbol_collector_marks_kis_quote_provider_unsupported(scope: str):
    session = _stock_info_session([_stock_info_row()])
    collector = SymbolSnapshotCollector(session)
    request = CollectorRequest(
        market="kr",
        account_scope=scope,  # type: ignore[arg-type]
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )

    results = await collector.collect(request)

    quote = results[0].payload_json["quote"]
    assert quote["status"] == "unavailable"
    assert quote["unavailable_reason"] == (
        "provider_unsupported: KIS is non-operational"
    )


# ---------------------------------------------------------------------------
# Symbol collector — crypto enrichment (ROB-369 Slice 2c).
#
# Crypto symbols are NOT in stock_info; they live in upbit_symbol_universe.
# For (market=crypto, account_scope=upbit_live) the collector resolves metadata
# from that universe and enriches each symbol with read-only Upbit orderbook
# liquidity (best bid/ask, spread, depth). Public market-data → no user_id
# required (unlike KIS). Per-symbol fail-open + the shared enrichment cap.
# ---------------------------------------------------------------------------
def _upbit_universe_row(market: str = "KRW-BTC", korean_name: str = "비트코인"):
    class _Row:
        def __init__(self) -> None:
            self.market = market
            self.korean_name = korean_name
            self.english_name = "Bitcoin"
            self.base_currency = market.split("-")[-1]
            self.quote_currency = "KRW"
            self.is_active = True

    return _Row()


def _fake_upbit_quote_client_ok() -> MagicMock:
    """Fake Upbit orderbook adapter returning a full top-of-book (no last_price)."""

    async def fetch(symbol: str, venue: str = "upbit") -> dict[str, Any]:
        return {
            "last_price": None,
            "best_bid": 94_900_000.0,
            "best_ask": 95_100_000.0,
            "bid_depth": 0.5,
            "ask_depth": 0.3,
            "venue": "upbit",
            "session": "24h",
            "nxt_eligible": False,
            "as_of": "1716200000000",
        }

    client = MagicMock()
    client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    return client


@pytest.mark.asyncio
async def test_symbol_collector_crypto_resolves_metadata_from_upbit_universe():
    """ROB-369 2c — crypto symbols resolve from upbit_symbol_universe (not
    stock_info); metadata is populated even when no quote adapter is wired."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-ETH", "이더리움")])
    collector = SymbolSnapshotCollector(session)  # no upbit quote client wired
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-ETH"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=1,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-ETH"
    assert payload["name"] == "이더리움"
    assert payload["instrument_type"] == "crypto"
    assert payload["exchange"] == "upbit"
    assert payload["is_active"] is True
    # Enrichment is wanted (crypto+upbit_live) but no client → honest unavailable.
    assert payload["quote"]["status"] == "unavailable"
    assert "no quote client" in payload["quote"]["unavailable_reason"]


@pytest.mark.asyncio
async def test_symbol_collector_crypto_enriches_with_upbit_orderbook_no_user_id():
    """ROB-369 2c — crypto+upbit_live enriches via the Upbit orderbook adapter;
    public market-data requires NO user_id (unlike KIS)."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-BTC", "비트코인")])
    quote_client = _fake_upbit_quote_client_ok()
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-BTC"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,  # no user_id — Upbit market-data is public
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-BTC"
    q = payload["quote"]
    assert q["status"] == "ok"
    assert q["best_bid"] == 94_900_000.0
    assert q["best_ask"] == 95_100_000.0
    assert q["spread"] == 200_000.0
    assert q["spread_bps"] == pytest.approx(21.05, rel=0.05)
    assert q["venue"] == "upbit"
    assert q["last_price"] is None  # orderbook carries no last trade — honest
    quote_client.fetch_quote_orderbook.assert_awaited_once_with(
        "KRW-BTC", venue="upbit"
    )


@pytest.mark.asyncio
async def test_symbol_collector_crypto_skips_quote_when_not_upbit_live():
    """ROB-369 2c — crypto without upbit_live scope must not call the quote client."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-BTC", "비트코인")])
    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(
        side_effect=AssertionError("upbit quote client must not be called")
    )
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope=None,
        symbols=["KRW-BTC"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=1,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-BTC"
    assert "quote" not in payload or payload.get("quote") is None
    quote_client.fetch_quote_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_collector_crypto_orderbook_failure_is_fail_open():
    """ROB-369 2c — an Upbit orderbook error marks that symbol unavailable
    without crashing others (per-symbol fail-open)."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session(
        [
            _upbit_universe_row("KRW-BTC", "비트코인"),
            _upbit_universe_row("KRW-XRP", "리플"),
        ]
    )

    async def fetch(symbol: str, venue: str = "upbit"):
        if symbol == "KRW-BTC":
            raise RuntimeError("upbit timeout")
        return {
            "last_price": None,
            "best_bid": 800.0,
            "best_ask": 801.0,
            "bid_depth": 100.0,
            "ask_depth": 120.0,
            "venue": "upbit",
            "session": "24h",
            "nxt_eligible": False,
            "as_of": None,
        }

    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-BTC", "KRW-XRP"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    by_symbol = {r.symbol: r for r in results if r.symbol}
    assert by_symbol["KRW-BTC"].payload_json["quote"]["status"] == "unavailable"
    assert (
        "upbit timeout"
        in by_symbol["KRW-BTC"].payload_json["quote"]["unavailable_reason"]
    )
    assert by_symbol["KRW-XRP"].payload_json["quote"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Candidate-universe collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_candidate_universe_kr_emits_candidate_evidence():
    from types import SimpleNamespace

    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=12,
            stale_count=3,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    repo.list_top_candidates = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="005930",
                source="toss",
                change_rate=3.0,
                latest_close=78500,
                daily_volume=14_000_000,
                consecutive_up_days=3,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="toss_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "useful"
    assert payload["fresh_count"] == 12
    assert payload["stale_count"] == 3
    assert payload["freshness_status"] == "fresh"
    assert payload["candidates"][0]["symbol"] == "005930"
    assert payload["candidates"][0]["score"] == 6.5
    assert payload["source_coverage"] == {"external_reference": 1}
    assert payload["missing_data"] is None


@pytest.mark.asyncio
async def test_candidate_universe_kr_stale_only_sets_missing_data():
    from types import SimpleNamespace

    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=0,
            stale_count=42,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    repo.list_top_candidates = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="000660",
                source="toss",
                change_rate=1.5,
                latest_close=120000,
                daily_volume=5_000_000,
                consecutive_up_days=1,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="toss_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "stale_only"
    assert payload["freshness_status"] == "stale"
    assert payload["candidates"], "stale partition still yields candidate rows"
    assert payload["missing_data"]["confidence_impact"] == "cap 40"
    assert "stale" in payload["missing_data"]["what"].lower()
    # Optional kind degrades the bundle to partial, never fails it.
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_candidate_universe_kr_empty_sets_missing_data():
    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=0,
            stale_count=0,
            last_computed_at=None,
        )
    )
    repo.list_top_candidates = AsyncMock(return_value=[])
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="toss_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "empty"
    assert payload["candidates"] == []
    assert payload["source_coverage"] == {}
    assert payload["missing_data"]["confidence_impact"] == "cap 20"
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_candidate_universe_crypto_emits_candidate_evidence():
    from types import SimpleNamespace

    from app.services.invest_crypto_screener_snapshots.repository import (
        CryptoCoverageCounts,
    )

    crypto_repo = MagicMock()
    crypto_repo.coverage = AsyncMock(
        return_value=CryptoCoverageCounts(
            latest_partition_date=dt.date(2026, 5, 19),
            latest_partition_count=7,
            stale_count=0,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    crypto_repo.list_latest = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="KRW-BTC",
                name="비트코인",
                source="tvscreener_upbit",
                change_rate=8.0,
                latest_close=95_000_000,
                rsi=60,
                adx=30,
                trade_amount_24h=500_000_000,
                volume_24h=10,
                market_cap=None,
                market_warning=False,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(
        MagicMock(), crypto_repository=crypto_repo
    )
    results = await collector.collect(
        _request(market="crypto", account_scope="upbit_live")
    )
    payload = results[0].payload_json
    assert payload["usefulness"] == "useful"
    assert payload["actionable_count"] == 7
    assert payload["candidates"][0]["symbol"] == "KRW-BTC"
    assert payload["candidates"][0]["score"] == 9.0
    assert payload["source_coverage"] == {"tvscreener_upbit": 1}


@pytest.mark.asyncio
async def test_candidate_universe_failure_is_fail_open():
    session = MagicMock()
    repo = MagicMock()
    repo.coverage = AsyncMock(side_effect=RuntimeError("boom"))
    collector = CandidateUniverseSnapshotCollector(session, equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="toss_live"))
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Invest-page collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invest_page_returns_recent_published_reports():
    session = MagicMock()
    query = MagicMock()

    class _Report:
        report_uuid = "55555555-1111-1111-1111-111111111111"
        report_type = "snapshot_backed_advisory_v1"
        status = "published"
        title = "t"
        published_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)
        snapshot_bundle_uuid = "66666666-1111-1111-1111-111111111111"
        snapshot_freshness_summary = {"overall": "fresh"}

    query.list_reports = AsyncMock(return_value=[_Report()])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].payload_json["count"] == 1
    assert (
        results[0].payload_json["recent_published_reports"][0][
            "snapshot_freshness_overall"
        ]
        == "fresh"
    )


@pytest.mark.asyncio
async def test_invest_page_returns_partial_when_no_recent_reports():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(return_value=[])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_invest_page_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(side_effect=RuntimeError("transient"))
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_production_registry_covers_all_policy_kinds():
    from app.services.investment_snapshots.policy import INTRADAY_ACTION_REPORT_V1

    registry = production_collector_registry(session=MagicMock())
    registered = registry.list_kinds()
    policy_kinds = {k.snapshot_kind for k in INTRADAY_ACTION_REPORT_V1.kinds}

    missing = policy_kinds - registered
    assert missing == set(), f"policy kinds missing collectors: {missing}"


def test_production_registry_registers_pending_orders():
    """ROB-274 — pending_orders collector is wired into the production registry."""
    registry = production_collector_registry(session=MagicMock())
    assert "pending_orders" in registry.list_kinds()


def test_production_registry_wires_only_active_symbol_quote_clients():
    """Equity quote는 collector 내부 Toss→snapshot, registry는 Upbit만 주입한다."""
    registry = production_collector_registry(session=MagicMock())
    symbol_collector = registry.get("symbol")
    assert symbol_collector is not None
    assert symbol_collector._upbit_quote_client is not None
    assert not hasattr(symbol_collector, "_kis_quote_client")


@pytest.mark.asyncio
async def test_upbit_quote_adapter_maps_orderbook_top_of_book(monkeypatch):
    """ROB-369 2c — the Upbit adapter maps the orderbook top-of-book into the
    shared quote contract (last_price None — orderbook has no last trade)."""
    from app.services.action_report.snapshot_backed.collectors import (
        registry as registry_mod,
    )

    async def fake_fetch_orderbook(market: str):
        assert market == "KRW-BTC"
        return {
            "market": "KRW-BTC",
            "timestamp": 1716200000000,
            "total_ask_size": 1.0,
            "total_bid_size": 2.0,
            "orderbook_units": [
                {
                    "ask_price": 95_100_000.0,
                    "bid_price": 94_900_000.0,
                    "ask_size": 0.3,
                    "bid_size": 0.5,
                },
                {
                    "ask_price": 95_200_000.0,
                    "bid_price": 94_800_000.0,
                    "ask_size": 1.0,
                    "bid_size": 1.0,
                },
            ],
        }

    monkeypatch.setattr(
        "app.services.upbit_orderbook.fetch_orderbook", fake_fetch_orderbook
    )
    adapter = registry_mod._UpbitQuoteOrderbookAdapter()
    quote = await adapter.fetch_quote_orderbook("KRW-BTC")
    assert quote["best_bid"] == 94_900_000.0
    assert quote["best_ask"] == 95_100_000.0
    assert quote["bid_depth"] == 0.5
    assert quote["ask_depth"] == 0.3
    assert quote["last_price"] is None
    assert quote["venue"] == "upbit"
    assert quote["as_of"] == "1716200000000"


@pytest.mark.asyncio
async def test_upbit_quote_adapter_empty_orderbook_yields_none_top(monkeypatch):
    """ROB-369 2c — empty/missing orderbook → None top-of-book, which the
    collector's empty-book branch then marks unavailable."""
    from app.services.action_report.snapshot_backed.collectors import (
        registry as registry_mod,
    )

    async def fake_fetch_orderbook(market: str):
        return {}

    monkeypatch.setattr(
        "app.services.upbit_orderbook.fetch_orderbook", fake_fetch_orderbook
    )
    adapter = registry_mod._UpbitQuoteOrderbookAdapter()
    quote = await adapter.fetch_quote_orderbook("KRW-FOO")
    assert quote["best_bid"] is None
    assert quote["best_ask"] is None
    assert quote["venue"] == "upbit"


# ---------------------------------------------------------------------------
# Static-import guard — none of the collector modules pull in known
# mutation paths. If a future contributor wires the trade execution
# service, the broker SDK, or WatchActivationService into a collector
# module's import graph, this assertion fires.
# ---------------------------------------------------------------------------
def test_collector_modules_do_not_import_broker_or_activation_paths():
    import importlib
    import sys

    forbidden_substrings: tuple[str, ...] = (
        "kis_trading_service",
        "investment_reports.watch_activation",
        "alpaca_paper_ledger_service",
        "upbit.client",  # upbit broker client
        # ROB-278 — also forbid explicit broker order-mutation verbs even
        # when shipped under different paths (defence in depth).
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
    )
    target_modules = [
        "app.services.action_report.snapshot_backed.collectors.portfolio",
        "app.services.action_report.snapshot_backed.collectors.journal",
        "app.services.action_report.snapshot_backed.collectors.watch_context",
        "app.services.action_report.snapshot_backed.collectors.market",
        "app.services.action_report.snapshot_backed.collectors.news",
        "app.services.action_report.snapshot_backed.collectors.symbol",
        "app.services.action_report.snapshot_backed.collectors.candidate_universe",
        "app.services.action_report.snapshot_backed.collectors.invest_page",
        "app.services.action_report.snapshot_backed.collectors.optional_stubs",
        "app.services.action_report.snapshot_backed.collectors.pending_orders",
        "app.services.action_report.snapshot_backed.collectors.registry",
        "app.services.action_report.snapshot_backed.generator",
        "app.services.action_report.snapshot_backed.symbol_derivation",
        "app.services.action_report.snapshot_backed.auto_emit",
    ]

    for name in target_modules:
        importlib.import_module(name)
        module = sys.modules[name]
        source = open(module.__file__, encoding="utf-8").read()  # type: ignore[arg-type]
        for forbidden in forbidden_substrings:
            assert forbidden not in source, (
                f"{name} unexpectedly references {forbidden!r} — "
                "collectors must remain read-only"
            )


def test_collector_request_carries_market_session_default_none():
    from app.services.investment_snapshots.collectors import CollectorRequest

    req = CollectorRequest(market="kr", account_scope="toss_live", policy_snapshot={})
    assert req.market_session is None
    req2 = CollectorRequest(
        market="kr",
        account_scope="toss_live",
        policy_snapshot={},
        market_session="nxt",
    )
    assert req2.market_session == "nxt"


def test_ensure_bundle_request_carries_market_session_default_none():
    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest

    req = EnsureBundleRequest(
        market="kr",
        account_scope="toss_live",
        purpose="testing",
        policy_version="v1",
    )
    assert req.market_session is None


@pytest.mark.asyncio
async def test_snapshot_bundle_threads_market_session_into_collector_request():
    import datetime as dt
    import types

    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
    from app.services.action_report.common.snapshot_bundle import (
        SnapshotBundleEnsureService,
    )
    from app.services.investment_snapshots.collectors import (
        SnapshotCollectResult,
    )

    captured: dict = {}

    class _CapturingCollector:
        snapshot_kind = "market"

        async def collect(self, request: CollectorRequest):
            captured["market_session"] = request.market_session
            return [
                SnapshotCollectResult(
                    snapshot_kind="market",
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"ok": True},
                    origin="auto_trader_db",
                    as_of=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
                    freshness_status="fresh",
                    coverage={},
                )
            ]

    service = SnapshotBundleEnsureService.__new__(SnapshotBundleEnsureService)
    service._collectors = {"market": _CapturingCollector()}

    kind_policy = types.SimpleNamespace(
        snapshot_kind="market",
        collector_timeout=dt.timedelta(seconds=5),
    )

    results, warnings, attempted = await service._collect_for_kind(
        kind_policy=kind_policy,
        request=EnsureBundleRequest(
            market="kr",
            account_scope="toss_live",
            market_session="nxt",
            purpose="testing",
            policy_version="v1",
        ),
        policy_snapshot={},
    )
    assert attempted is True
    assert captured["market_session"] == "nxt"


@pytest.mark.asyncio
async def test_market_collector_kr_nxt_marks_index_frozen():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.0},
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    req = _request(market="kr")
    req = req.model_copy(update={"market_session": "nxt"})
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["index_session"] == "regular_closed"
    assert "frozen" in payload["index_session_note"]


@pytest.mark.asyncio
async def test_market_collector_kr_regular_session_has_no_frozen_note():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.5},
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))
    payload = results[0].payload_json
    assert "index_session" not in payload


# ---------------------------------------------------------------------------
# ROB-392 Slice A — NAV scope label + KR code-as-name fallback.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kr_toss_live_payload_carries_general_snapshot_nav_scope_label():
    fetcher = AsyncMock(return_value=_toss_snapshot(_toss_position()))
    collector = PortfolioSnapshotCollector(
        _manual_kr_session(),
        toss_snapshot_fetcher=fetcher,
    )

    results = await collector.collect(_equity_request())

    payload = results[0].payload_json
    assert payload["primary_source"] == "toss"
    assert payload["nav_scope"] == "toss_primary_general_snapshot"
    assert "매도가능 수량 근거로 사용하지 않음" in payload["nav_scope_label"]
    assert payload["count"] == len(payload["holdings"])


def test_apply_kr_name_fallback_fills_code_as_name_rows():
    from app.services.action_report.snapshot_backed.collectors.portfolio import (
        _apply_kr_name_fallback,
    )

    rows = [
        {"ticker": "035420", "display_name": None},  # missing
        {"ticker": "035720", "display_name": "035720"},  # code-as-name
        {"ticker": "005930", "display_name": "삼성전자"},  # already good
    ]
    _apply_kr_name_fallback(rows, {"035420": "NAVER", "035720": "카카오"})
    assert rows[0]["display_name"] == "NAVER"
    assert rows[1]["display_name"] == "카카오"
    assert rows[2]["display_name"] == "삼성전자"  # untouched


def test_apply_kr_name_fallback_keeps_code_when_unresolved():
    from app.services.action_report.snapshot_backed.collectors.portfolio import (
        _apply_kr_name_fallback,
    )

    rows = [{"ticker": "999999", "display_name": None}]
    _apply_kr_name_fallback(rows, {})  # lookup returned nothing
    assert rows[0]["display_name"] is None  # no fabricated name
