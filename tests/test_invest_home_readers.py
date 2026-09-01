"""ROB-123 — Invest home reader mapping tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.services import invest_home_readers as readers


def test_kis_home_readers_are_not_runtime_symbols() -> None:
    assert not hasattr(readers, "SafeKISClient")
    assert not hasattr(readers, "KISHomeReader")
    assert not hasattr(readers, "SafeKISMockClient")
    assert not hasattr(readers, "KISMockHomeReader")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_uses_coin_value_not_krw_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "BTC",
                "balance": "0.1",
                "locked": "0",
                "avg_buy_price": "80000000",
            },
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 100_000_000.0}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers, "get_active_upbit_markets", AsyncMock(return_value={"KRW-BTC"})
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    assert account.valueKrw == 10_000_000
    assert account.costBasisKrw == 8_000_000
    assert account.pnlKrw == 2_000_000
    assert account.cashBalances.krw == 90_000
    assert account.buyingPower.krw == 90_000
    assert account.valueKrw != account.cashBalances.krw
    assert result.holdings[0].valueKrw == 10_000_000
    assert result.holdings[0].pnlKrw == 2_000_000
    assert result.holdings[0].assetCategory == "crypto"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_falls_back_per_market_and_skips_zero_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "BTC",
                "balance": "0.1",
                "locked": "0",
                "avg_buy_price": "80000000",
            },
            {
                "currency": "XYM",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "10",
            },
            {
                "currency": "PCI",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "1000",
            },
        ]

    calls: list[list[str]] = []

    async def _prices(markets: list[str]) -> dict[str, float]:
        calls.append(markets)
        if markets == ["KRW-BTC", "KRW-PCI"]:
            return {}
        if markets == ["KRW-BTC"]:
            return {"KRW-BTC": 100_000_000.0}
        return {}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)

    # Mock active/warning markets
    monkeypatch.setattr(
        readers,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC", "KRW-PCI"}),
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert [h.symbol for h in result.holdings] == ["BTC", "PCI"]
    assert calls == [["KRW-BTC", "KRW-PCI"], ["KRW-BTC"], ["KRW-PCI"]]
    assert result.holdings[0].valueKrw == 10_000_000
    assert result.accounts[0].valueKrw == 10_000_000
    assert result.accounts[0].costBasisKrw == 8_000_000
    assert result.accounts[0].pnlKrw == 2_000_000
    assert result.warning is not None
    assert result.hidden_counts.upbitInactive == 1  # XYM is inactive


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_filters_dust(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "BTC", "balance": "1", "locked": "0"},  # 100M
            {"currency": "DOGE", "balance": "1", "locked": "0"},  # 100 (dust)
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 100_000_000.0, "KRW-DOGE": 100.0}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC", "KRW-DOGE"}),
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert len(result.holdings) == 1
    assert result.holdings[0].symbol == "BTC"
    assert len(result.hidden_holdings) == 1
    assert result.hidden_holdings[0].symbol == "DOGE"
    assert result.hidden_counts.upbitDust == 1
    assert result.accounts[0].valueKrw == 100_000_000  # DOGE excluded


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_does_not_show_loss_when_all_prices_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "PCI",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "1000",
            },
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-PCI"]
        return {}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers, "get_active_upbit_markets", AsyncMock(return_value={"KRW-PCI"})
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.accounts[0].valueKrw == 0
    assert result.accounts[0].costBasisKrw is None
    assert result.accounts[0].pnlKrw is None
    assert result.accounts[0].pnlRate is None
    assert result.warning is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_valuates_with_quote_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SimpleNamespace(id=3, broker_type="toss", account_name="Toss 수동")
    holding = SimpleNamespace(
        id=11,
        broker_account_id=3,
        broker_account=broker,
        ticker="005930",
        market_type=MarketType.KR,
        display_name="삼성전자",
        quantity=10,
        avg_price=70_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    # Mock QuoteService
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(return_value={"005930": 72_000.0})
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(db=None, quote_service=quote_service).fetch(
        user_id=1
    )  # type: ignore[arg-type]

    h = result.holdings[0]
    assert h.symbol == "005930"
    assert h.valueKrw == pytest.approx(720_000.0)
    assert h.pnlKrw == pytest.approx(20_000.0)
    assert h.priceState == "live"
    assert h.accountKind == "manual"
    assert h.sourceOfTruth is False
    assert h.isTradeable is False
    assert h.manualOnly is True
    assert h.sellableQuantity == 0
    assert h.pendingSellQuantity == 0
    assert h.referenceQuantity == 10
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_preserves_distinct_broker_accounts_in_home_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_a = SimpleNamespace(id=101, broker_type="toss", account_name="Broker Alpha")
    broker_b = SimpleNamespace(id=102, broker_type="toss", account_name="Broker Beta")
    holdings = [
        SimpleNamespace(
            id=11,
            broker_account_id=101,
            broker_account=broker_a,
            ticker="035930",
            market_type=MarketType.KR,
            display_name="Manual Alpha",
            quantity=1,
            avg_price=50_000,
        ),
        SimpleNamespace(
            id=12,
            broker_account_id=102,
            broker_account=broker_b,
            ticker="000660",
            market_type=MarketType.KR,
            display_name="Manual Beta",
            quantity=1,
            avg_price=60_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(
        return_value={"035930": 55_000.0, "000660": 65_000.0}
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    accounts = {account.accountId: account for account in result.accounts}
    assert set(accounts) == {"101", "102"}
    assert accounts["101"].displayName == "Broker Alpha"
    assert accounts["102"].displayName == "Broker Beta"
    assert accounts["101"].valueKrw == pytest.approx(55_000)
    assert accounts["102"].valueKrw == pytest.approx(65_000)
    assert {holding.accountId for holding in result.holdings} == {"101", "102"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_preserves_all_supported_broker_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_rows = [
        (201, "toss", "Toss Main", "005930"),
        (202, "kis", "KIS Main", "000660"),
        (203, "upbit", "Upbit Main", "KRW-BTC"),
        (204, "samsung", "Samsung Main", "035720"),
    ]
    holdings = [
        SimpleNamespace(
            id=account_id,
            broker_account_id=account_id,
            broker_account=SimpleNamespace(
                id=account_id,
                broker_type=broker_type,
                account_name=account_name,
            ),
            ticker=ticker,
            market_type=MarketType.KR,
            display_name=f"{broker_type} holding",
            quantity=1,
            avg_price=50_000,
        )
        for account_id, broker_type, account_name, ticker in broker_rows
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(
        return_value={ticker: 55_000.0 for *_, ticker in broker_rows}
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    accounts = {account.accountId: account for account in result.accounts}
    assert set(accounts) == {str(account_id) for account_id, *_ in broker_rows}
    assert {
        accounts[str(account_id)].displayName for account_id, *_ in broker_rows
    } == {account_name for _, _, account_name, _ in broker_rows}
    assert {holding.accountId for holding in result.holdings} == set(accounts)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_account_identity_survives_price_availability_toss_and_non_toss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 SHOULD-1: the DB account identity (id/name/source) must not
    depend on whether any holding in it currently has a price. Both a Toss
    and a non-Toss (samsung pension) manual account must keep the same
    identity whether priced or fully unpriced -- previously an all-unpriced
    manual account vanished from result.accounts, forcing MCP to fall back
    to a different hardcoded canonical id/name.
    """

    broker_toss = SimpleNamespace(
        id=501, broker_type="toss", account_name="토스 수동계좌"
    )
    broker_samsung = SimpleNamespace(
        id=502, broker_type="samsung", account_name="삼성 퇴직연금 DC"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=501,
            broker_account=broker_toss,
            ticker="005930",
            market_type=MarketType.KR,
            display_name="삼성전자",
            quantity=1,
            avg_price=70_000,
        ),
        SimpleNamespace(
            id=2,
            broker_account_id=502,
            broker_account=broker_samsung,
            ticker="000660",
            market_type=MarketType.KR,
            display_name="SK하이닉스",
            quantity=1,
            avg_price=100_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    priced_quote_service = MagicMock()
    priced_quote_service.fetch_kr_prices = AsyncMock(
        return_value={"005930": 75_000.0, "000660": 105_000.0}
    )
    priced_quote_service.fetch_us_prices = AsyncMock(return_value={})

    priced_result = await readers.ManualHomeReader(
        db=None,
        quote_service=priced_quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)
    priced_accounts = {a.accountId: a for a in priced_result.accounts}
    assert set(priced_accounts) == {"501", "502"}
    assert priced_accounts["501"].displayName == "토스 수동계좌"
    assert priced_accounts["501"].source == "toss_manual"
    assert priced_accounts["502"].displayName == "삼성 퇴직연금 DC"
    assert priced_accounts["502"].source == "pension_manual"
    assert priced_accounts["501"].valueKrw == pytest.approx(75_000.0)
    assert priced_accounts["502"].valueKrw == pytest.approx(105_000.0)

    # No quote_service at all -> every holding stays unpriced.
    unpriced_result = await readers.ManualHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]
    unpriced_accounts = {a.accountId: a for a in unpriced_result.accounts}
    assert set(unpriced_accounts) == {"501", "502"}
    assert unpriced_accounts["501"].displayName == "토스 수동계좌"
    assert unpriced_accounts["501"].source == "toss_manual"
    assert unpriced_accounts["502"].displayName == "삼성 퇴직연금 DC"
    assert unpriced_accounts["502"].source == "pension_manual"
    assert unpriced_accounts["501"].valueKrw == 0.0
    assert unpriced_accounts["502"].valueKrw == 0.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_does_not_fabricate_value_from_cost_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SimpleNamespace(id=3, broker_type="toss", account_name="Toss 수동")
    holding = SimpleNamespace(
        id=11,
        broker_account_id=3,
        broker_account=broker,
        ticker="005930",
        market_type=MarketType.KR,
        display_name="삼성전자",
        quantity=10,
        avg_price=70_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            assert user_id == 1
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    result = await readers.ManualHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    # ROB-1310 SHOULD-1: the account identity (id/name/source) must survive
    # even when nothing in it is priced yet -- it must not vanish (which used
    # to force a hardcoded fallback identity downstream). The value must
    # still not be fabricated from cost basis: 0.0 (nothing priced), not a
    # guess derived from costBasis.
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.accountId == "3"
    assert account.displayName == "Toss 수동"
    assert account.source == "toss_manual"
    assert account.valueKrw == 0.0
    assert account.costBasisKrw is None
    assert account.pnlKrw is None
    assert result.holdings[0].costBasis == 700_000
    assert result.holdings[0].valueKrw is None
    assert result.holdings[0].assetCategory == "kr_stock"
    assert result.holdings[0].priceState == "missing"
    assert result.warning is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_emits_load_quote_and_fx_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _BrokerAccount:
        broker_type = "toss"

    class _ManualHolding:
        id = 1
        broker_account_id = 10
        broker_account = _BrokerAccount()
        ticker = "005930"
        display_name = "삼성전자"
        market_type = MarketType.KR
        quantity = 2
        avg_price = 70_000

    class _ManualHoldingsService:
        def __init__(self, db: object) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[_ManualHolding]:
            assert user_id == 1
            return [_ManualHolding()]

    class _QuoteService:
        async def fetch_kr_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == ["005930"]
            return {"005930": 72_000.0}

        async def fetch_us_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == []
            return {}

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "ManualHoldingsService", _ManualHoldingsService)

    result = await readers.ManualHomeReader(
        db=object(), quote_service=_QuoteService()
    ).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.warning is None
    assert "invest.home.manual.load_holdings" in started
    assert "invest.home.manual.fetch_kr_prices" in started
    assert "invest.home.manual.fetch_us_prices" in started


@pytest.mark.asyncio
async def test_manual_reader_fetches_kr_and_us_prices_concurrently(monkeypatch):
    """ROB-702: KR and US price fetches must run concurrently, not sequentially.

    Each fake fetch signals it has started, then waits for the other to start.
    Under the concurrent ``asyncio.gather`` both events fire and both proceed;
    a sequential kr-then-us implementation would deadlock (us never starts while
    kr awaits it) and time out — so a passing result proves concurrency.
    """

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __enter__(self) -> _Span:
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext()

    class _BrokerAccount:
        broker_type = "toss"

    class _KRHolding:
        id = 1
        broker_account_id = 10
        broker_account = _BrokerAccount()
        ticker = "005930"
        display_name = "삼성전자"
        market_type = MarketType.KR
        quantity = 2
        avg_price = 70_000

    class _USHolding:
        id = 2
        broker_account_id = 10
        broker_account = _BrokerAccount()
        ticker = "AAPL"
        display_name = "Apple"
        market_type = MarketType.US
        quantity = 1
        avg_price = 100

    class _ManualHoldingsService:
        def __init__(self, db: object) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return [_KRHolding(), _USHolding()]

    kr_started = asyncio.Event()
    us_started = asyncio.Event()

    class _QuoteService:
        async def fetch_kr_prices(self, tickers: list[str]) -> dict[str, float | None]:
            kr_started.set()
            await asyncio.wait_for(us_started.wait(), timeout=1.0)
            return dict.fromkeys(tickers, 72000.0)

        async def fetch_us_prices(self, tickers: list[str]) -> dict[str, float | None]:
            us_started.set()
            await asyncio.wait_for(kr_started.wait(), timeout=1.0)
            return dict.fromkeys(tickers, 190.0)

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "ManualHoldingsService", _ManualHoldingsService)
    monkeypatch.setattr(readers, "get_usd_krw_rate", AsyncMock(return_value=1_350.0))

    result = await readers.ManualHomeReader(
        db=object(), quote_service=_QuoteService()
    ).fetch(user_id=1)  # type: ignore[arg-type]

    # Sequential fetches would deadlock on the cross-waits above; a clean result
    # with both events set proves the two fetches were in flight simultaneously.
    assert result.warning is None
    assert kr_started.is_set() and us_started.is_set()


# ---------------------------------------------------------------------------
# ROB-238: AlpacaPaperHomeReader tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_maps_positions_and_converts_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot, Position

    fake_account = AccountSnapshot(
        id="alpaca-test",
        buying_power=Decimal("1000"),
        cash=Decimal("500"),
        portfolio_value=Decimal("5500"),
        status="ACTIVE",
    )
    fake_positions = [
        Position(
            asset_id="aapl-id",
            symbol="AAPL",
            qty=Decimal("2"),
            avg_entry_price=Decimal("100"),
            current_price=Decimal("110"),
            market_value=Decimal("220"),
            unrealized_pl=Decimal("20"),
            side="long",
        )
    ]

    class _FakeAlpacaSvc:
        async def get_account(self) -> AccountSnapshot:
            return fake_account

        async def list_positions(self) -> list[Position]:
            return fake_positions

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _FakeAlpacaSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.source == "alpaca_paper"
    assert account.accountKind == "paper"
    assert account.includedInHome is False
    assert account.accountId == "alpaca_paper_account"
    assert account.cashBalances.usd == pytest.approx(500.0)
    assert account.buyingPower.usd == pytest.approx(1000.0)
    assert account.valueKrw == pytest.approx(220 * 1_300.0)
    assert account.costBasisKrw == pytest.approx(200 * 1_300.0)
    assert account.pnlKrw == pytest.approx(20 * 1_300.0)
    assert account.pnlRate == pytest.approx(20 / 200)

    h = result.holdings[0]
    assert h.source == "alpaca_paper"
    assert h.accountKind == "paper"
    assert h.symbol == "AAPL"
    assert h.market == "US"
    assert h.currency == "USD"
    assert h.assetCategory == "us_stock"
    assert h.valueNative == pytest.approx(220.0)
    assert h.valueKrw == pytest.approx(220 * 1_300.0)
    assert h.pnlKrw == pytest.approx(20 * 1_300.0)
    assert h.pnlRate == pytest.approx(20 / 200)
    assert h.priceState == "live"
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_keeps_account_pnl_unknown_when_basis_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot, Position

    class _MissingBasisSvc:
        async def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                id="alpaca-missing-basis",
                buying_power=Decimal("100"),
                cash=Decimal("50"),
                portfolio_value=Decimal("100"),
                status="ACTIVE",
            )

        async def list_positions(self) -> list[Position]:
            return [
                Position(
                    asset_id="free-share",
                    symbol="FREE",
                    qty=Decimal("1"),
                    avg_entry_price=Decimal("0"),
                    current_price=Decimal("50"),
                    market_value=Decimal("50"),
                    unrealized_pl=Decimal("50"),
                    side="long",
                ),
                Position(
                    asset_id="missing-price",
                    symbol="MISS",
                    qty=Decimal("1"),
                    avg_entry_price=Decimal("10"),
                    current_price=None,
                    market_value=None,
                    unrealized_pl=None,
                    side="long",
                ),
            ]

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _MissingBasisSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.valueKrw == pytest.approx(50 * 1_300.0)
    assert account.costBasisKrw is None
    assert account.pnlKrw is None
    assert account.pnlRate is None

    free = next(h for h in result.holdings if h.symbol == "FREE")
    assert free.valueKrw == pytest.approx(50 * 1_300.0)
    assert free.costBasis is None
    assert free.priceState == "live"

    missing = next(h for h in result.holdings if h.symbol == "MISS")
    assert missing.valueKrw is None
    assert missing.pnlKrw is None
    assert missing.priceState == "missing"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_not_configured_returns_warning() -> None:
    from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError

    class _Reader(readers.AlpacaPaperHomeReader):
        @staticmethod
        def _make_service() -> Any:
            raise AlpacaPaperConfigurationError("no creds")

    result = await _Reader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "alpaca_paper"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_mutation_methods_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that submit_order and cancel_order are never called during fetch."""
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot

    mutation_called: list[str] = []

    class _SafeFakeAlpacaSvc:
        async def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                id="safe-test",
                buying_power=Decimal("0"),
                cash=Decimal("0"),
                portfolio_value=Decimal("0"),
                status="ACTIVE",
            )

        async def list_positions(self) -> list[Any]:
            return []

        async def submit_order(self, *args: Any, **kwargs: Any) -> Any:
            mutation_called.append("submit_order")
            raise AssertionError("submit_order must not be called from Invest Home")

        async def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
            mutation_called.append("cancel_order")
            raise AssertionError("cancel_order must not be called from Invest Home")

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _SafeFakeAlpacaSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    assert mutation_called == [], (
        "Mutation methods were called during Invest Home fetch"
    )


@pytest.mark.asyncio
async def test_toss_api_home_reader_maps_read_only_holdings_and_cash(monkeypatch):
    from decimal import Decimal

    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(readers, "get_usd_krw_rate", AsyncMock(return_value=1_350.0))
    # ROB-549: mutations disabled (default) -> reference-only.
    from app.core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", False, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert result.warning is None
    assert result.accounts[0].source == "toss_api"
    assert result.accounts[0].accountKind == "live"
    assert result.accounts[0].cashBalances.krw == 123456.0
    assert result.accounts[0].cashBalances.usd == 789.01
    assert result.accounts[0].buyingPower.krw == 123456.0
    assert result.accounts[0].buyingPower.usd == 789.01
    holding = result.holdings[0]
    assert holding.source == "toss_api"
    assert holding.sourceOfTruth is True
    assert holding.isTradeable is False
    assert holding.manualOnly is False
    assert holding.sellableQuantity is None
    assert holding.referenceQuantity == 1.5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_api_home_reader_populates_buying_power_card(monkeypatch):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    async def fake_fetch_toss_snapshot(*, need_sellable=True, sellable_cache=None):
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=Decimal("500000"),
            cash_usd=Decimal("42.5"),
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", False, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)
    account = result.accounts[0]
    # buyingPower is wired from the Toss cashBuyingPower fetch...
    assert account.buyingPower.krw == 500000.0
    assert account.buyingPower.usd == 42.5
    # ...and cashBalances is left exactly as before (additive, no regression).
    assert account.cashBalances.krw == 500000.0
    assert account.cashBalances.usd == 42.5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_api_home_reader_buying_power_fail_open_when_cash_missing(
    monkeypatch,
):
    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    async def fake_fetch_toss_snapshot(*, need_sellable=True, sellable_cache=None):
        # buying_power fetch failed for both currencies -> None + error rows.
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=None,
            cash_usd=None,
            errors=[
                {
                    "source": "toss_api",
                    "stage": "buying_power",
                    "currency": "KRW",
                    "error": "boom",
                },
            ],
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", False, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)
    account = result.accounts[0]
    assert account.buyingPower.krw is None
    assert account.buyingPower.usd is None
    assert account.cashBalances.krw is None
    assert account.cashBalances.usd is None
    # Fail-open: still returns an account, error surfaced as a warning.
    assert result.warning is not None
    assert "boom" in result.warning.message


@pytest.mark.asyncio
async def test_toss_api_home_reader_tradeable_when_mutations_enabled(monkeypatch):
    """A routed Toss holding remains tradeable, but read paths do not expose
    a sellable quantity sourced from the shared snapshot."""
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", True, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    holding = result.holdings[0]
    assert holding.isTradeable is True
    assert holding.sellableQuantity is None
    assert holding.pendingSellQuantity == 0.0


@pytest.mark.asyncio
async def test_toss_api_home_reader_converts_us_holdings_to_krw(monkeypatch):
    from decimal import Decimal

    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    async def fake_fx() -> float:
        return 1300.0

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(readers, "get_usd_krw_rate", fake_fx)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert result.warning is None
    assert result.holdings[0].valueKrw == pytest.approx(645.18 * 1300.0)
    assert result.holdings[0].pnlKrw == pytest.approx(45.18 * 1300.0)
    assert result.accounts[0].valueKrw == pytest.approx(645.18 * 1300.0)
    assert result.accounts[0].costBasisKrw == pytest.approx(600.0 * 1300.0)
    assert result.accounts[0].pnlKrw == pytest.approx(45.18 * 1300.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutations", [False, True])
async def test_toss_api_home_reader_gates_sellable_fetch_on_mutations(
    monkeypatch, mutations
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    captured: dict[str, bool] = {}

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        captured["need_sellable"] = need_sellable
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    # ROB-1310: mutation enablement never turns a general home read into an
    # ORDER_INFO sellable fan-out.
    assert captured["need_sellable"] is False


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("mutations", [False, True])
async def test_toss_api_home_reader_passes_sellable_cache_when_mutations_on(
    monkeypatch, mutations
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    captured: dict[str, object] = {}

    async def fake_fetch_toss_snapshot(*, need_sellable=True, sellable_cache=None):
        captured["need_sellable"] = need_sellable
        captured["sellable_cache"] = sellable_cache
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    # The shared portfolio snapshot is the read model; the per-symbol
    # sellable cache is reserved for order-adjacent invalidation/preflight.
    assert captured["sellable_cache"] is None
    assert captured["need_sellable"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_portfolio_snapshot_emits_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services import toss_portfolio_service as toss_service

    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _Client:
        async def holdings(self) -> SimpleNamespace:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        symbol="005930",
                        name="삼성전자",
                        market_country="KR",
                        quantity=Decimal("2"),
                        average_purchase_price=Decimal("70000"),
                        last_price=Decimal("72000"),
                        market_value={"amount": Decimal("144000")},
                        profit_loss={
                            "amount": Decimal("4000"),
                            "rate": Decimal("0.0285"),
                        },
                    )
                ]
            )

        async def sellable_quantity(self, *, symbol: str) -> SimpleNamespace:
            assert symbol == "005930"
            return SimpleNamespace(sellable_quantity=Decimal("1"))

        async def buying_power(self, *, currency: str) -> SimpleNamespace:
            return SimpleNamespace(currency=currency, cash_buying_power=Decimal("1000"))

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(toss_service.sentry_sdk, "start_span", _start_span)

    snapshot = await toss_service.fetch_toss_portfolio_snapshot(
        client=_Client(), need_sellable=True
    )

    assert snapshot.positions[0].symbol == "005930"
    assert "invest.home.toss_api.holdings" in started
    assert "invest.home.toss_api.sellable_quantity" in started
    assert "invest.home.toss_api.buying_power" in started


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_cash_snapshot_runs_concurrently_with_holdings(monkeypatch):
    """ROB-707: fetch_toss_cash_snapshot overlaps the holdings fetch. The fake's
    holdings() only completes once buying_power() has started, so a serial
    (holdings-then-cash) ordering deadlocks and this test times out."""
    import asyncio
    from decimal import Decimal
    from types import SimpleNamespace

    from app.services import toss_portfolio_service as svc

    bp_started = asyncio.Event()

    class _Client:
        async def holdings(self):
            # Completes ONLY if the cash fetch is already in flight.
            await asyncio.wait_for(bp_started.wait(), timeout=1.0)
            return SimpleNamespace(items=[])

        async def sellable_quantity(self, *, symbol):  # unused: no items
            raise AssertionError("no holdings -> no sellable fanout")

        async def buying_power(self, *, currency):
            bp_started.set()
            return SimpleNamespace(currency=currency, cash_buying_power=Decimal("10"))

        async def aclose(self):
            return None

    snap = await asyncio.wait_for(
        svc.fetch_toss_portfolio_snapshot(need_sellable=False, client=_Client()),
        timeout=2.0,
    )
    assert snap.positions == []
    assert snap.cash_krw == Decimal("10")
    assert snap.cash_usd == Decimal("10")
    assert snap.errors == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_cash_snapshot_drained_when_holdings_chain_raises(monkeypatch):
    """ROB-707: when the holdings/sellable chain raises before the cash task
    is awaited, the finally block must cancel and drain the pending cash task
    so it never touches a closed client and never leaks a pending coroutine."""
    import asyncio
    from types import SimpleNamespace

    from app.services import toss_portfolio_service as svc

    aclose_calls = 0
    bp_started = asyncio.Event()
    bp_cancelled = asyncio.Event()

    class _Client:
        async def holdings(self):
            # Raise AFTER the cash fetch has started so the cash task is
            # genuinely pending when the finally block runs.
            await asyncio.wait_for(bp_started.wait(), timeout=1.0)
            raise RuntimeError("holdings boom")

        async def sellable_quantity(self, *, symbol):
            raise AssertionError("no fanout on raise")

        async def buying_power(self, *, currency):
            bp_started.set()
            # Block forever until cancelled — proves drain works.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                bp_cancelled.set()
                raise
            return SimpleNamespace(currency=currency, cash_buying_power=0)

        async def aclose(self):
            nonlocal aclose_calls
            aclose_calls += 1

    with pytest.raises(RuntimeError, match="holdings boom"):
        await svc.fetch_toss_portfolio_snapshot(need_sellable=False, client=_Client())

    # The pending cash task must have been cancelled and drained (the buying
    # power coroutine observed CancelledError). The shared client (created
    # client=False here) must NOT be closed — caller owns it.
    assert bp_cancelled.is_set(), "pending cash task was not cancelled"
    assert aclose_calls == 0, "client must not be closed when caller owns it"


# ---------------------------------------------------------------------------
# ROB-1310 R8 — manual reader quote-key and warning-source regressions
# ---------------------------------------------------------------------------


def _contract_faithful_kr_quote_fake(
    known: dict[str, float], requested: list[list[str]]
):
    """Fake ``fetch_kr_prices`` with the real resolver's key contract.

    ``PriceFallbackResolver.resolve`` seeds ``dict.fromkeys(symbols, None)``
    and only ever writes back under a *requested* key, so the returned map
    never contains a key the caller did not ask for. A fake that invents an
    unrequested key would hide exactly the defect under test.
    """

    async def _fetch(symbols: list[str]) -> dict[str, float | None]:
        requested.append(list(symbols))
        return {symbol: known.get(symbol) for symbol in symbols}

    return AsyncMock(side_effect=_fetch)


def _emitted_warning_sources(result: Any) -> list[str]:
    """Every warning the reader actually emits, in emission order.

    Written so it works against both the single-warning and the per-source
    shapes: the assertion that fails must be about *attribution*, not about a
    missing attribute name.
    """

    emitted = [result.warning] if result.warning is not None else []
    emitted.extend(getattr(result, "extra_warnings", None) or [])
    return [warning.source for warning in emitted]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_crypto_quote_is_requested_with_the_upbit_market_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R8 / review 3820016131.

    A manual CRYPTO holding stored as the bare base coin (``BTC``) must be
    normalized through the shared ``to_upbit_symbol`` helper *before* the
    quote request, because the legacy quote contract keys crypto as
    ``KRW-BTC`` and the resolver only returns requested keys. Requesting the
    raw ``BTC`` makes the later ``KRW-BTC`` lookup structurally unable to hit,
    so an available quote is reported missing.
    """

    broker_upbit = SimpleNamespace(
        id=601, broker_type="upbit", account_name="업비트 수동계좌"
    )
    broker_toss = SimpleNamespace(
        id=602, broker_type="toss", account_name="토스 수동계좌"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=601,
            broker_account=broker_upbit,
            ticker="BTC",
            market_type=MarketType.CRYPTO,
            display_name="비트코인",
            quantity=2,
            avg_price=50_000_000,
        ),
        SimpleNamespace(
            id=2,
            broker_account_id=602,
            broker_account=broker_toss,
            ticker="005930",
            market_type=MarketType.KR,
            display_name="삼성전자",
            quantity=10,
            avg_price=70_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            assert user_id == 1
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    requested_kr: list[list[str]] = []
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = _contract_faithful_kr_quote_fake(
        {"KRW-BTC": 130_000_000.0, "005930": 75_000.0}, requested_kr
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    assert len(requested_kr) == 1
    requested = requested_kr[0]
    assert "KRW-BTC" in requested, (
        "manual crypto must be normalized to the Upbit market key before the "
        f"quote request; requested={requested!r}"
    )
    assert "BTC" not in requested, (
        f"the raw base coin must not be the requested quote key: {requested!r}"
    )
    # KR equity keys are untouched by the crypto normalization.
    assert "005930" in requested

    by_symbol = {holding.symbol: holding for holding in result.holdings}
    crypto = by_symbol["BTC"]
    assert crypto.market == "CRYPTO"
    assert crypto.assetCategory == "crypto"
    assert crypto.priceState == "live"
    assert crypto.valueNative == pytest.approx(260_000_000.0)
    assert crypto.valueKrw == pytest.approx(260_000_000.0)
    assert crypto.pnlKrw == pytest.approx(160_000_000.0)

    equity = by_symbol["005930"]
    assert equity.priceState == "live"
    assert equity.valueKrw == pytest.approx(750_000.0)

    # Everything priced -> no partial-valuation warning at all.
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_valuation_warning_names_the_affected_non_toss_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R8 / review 3826407065.

    W2 widened manual holdings past Toss. A valuation failure for an
    ``upbit_manual`` holding must not be reported under the ``toss_manual``
    source.
    """

    broker_upbit = SimpleNamespace(
        id=701, broker_type="upbit", account_name="업비트 수동계좌"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=701,
            broker_account=broker_upbit,
            ticker="DOGE",
            market_type=MarketType.CRYPTO,
            display_name="도지코인",
            quantity=100,
            avg_price=200,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    requested_kr: list[list[str]] = []
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = _contract_faithful_kr_quote_fake({}, requested_kr)
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    assert result.holdings[0].priceState == "missing"
    assert _emitted_warning_sources(result) == ["upbit_manual"]
    assert result.warning is not None
    # Sanitized fixed text only -- no raw exception/secret material.
    assert "현재가" in result.warning.message


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_valuation_warnings_do_not_cross_attribute_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R8 / review 3826407065 — mixed Toss / non-Toss batch.

    Two distinct manual sources fail valuation and a third is priced. Each
    failure must be reported under its own source, deterministically ordered,
    and the healthy source must not be warned about at all.
    """

    broker_toss = SimpleNamespace(
        id=801, broker_type="toss", account_name="토스 수동계좌"
    )
    broker_upbit = SimpleNamespace(
        id=802, broker_type="upbit", account_name="업비트 수동계좌"
    )
    broker_pension = SimpleNamespace(
        id=803, broker_type="samsung", account_name="삼성 퇴직연금 DC"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=801,
            broker_account=broker_toss,
            ticker="000660",
            market_type=MarketType.KR,
            display_name="SK하이닉스",
            quantity=1,
            avg_price=100_000,
        ),
        SimpleNamespace(
            id=2,
            broker_account_id=802,
            broker_account=broker_upbit,
            ticker="ETH",
            market_type=MarketType.CRYPTO,
            display_name="이더리움",
            quantity=1,
            avg_price=3_000_000,
        ),
        SimpleNamespace(
            id=3,
            broker_account_id=803,
            broker_account=broker_pension,
            ticker="005930",
            market_type=MarketType.KR,
            display_name="삼성전자",
            quantity=1,
            avg_price=70_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    requested_kr: list[list[str]] = []
    quote_service = MagicMock()
    # Only the pension holding resolves.
    quote_service.fetch_kr_prices = _contract_faithful_kr_quote_fake(
        {"005930": 75_000.0}, requested_kr
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    emitted = _emitted_warning_sources(result)
    assert emitted == ["toss_manual", "upbit_manual"], (
        f"each failing manual source must be reported once, under its own "
        f"source, deterministically ordered; got {emitted!r}"
    )
    # The priced source is healthy and must not be warned about.
    assert "pension_manual" not in emitted
    for source in emitted:
        assert source in {"toss_manual", "upbit_manual"}
    messages = [result.warning.message] if result.warning is not None else []
    messages.extend(
        warning.message for warning in (getattr(result, "extra_warnings", None) or [])
    )
    for message in messages:
        assert "현재가" in message
        assert "Traceback" not in message


# ---------------------------------------------------------------------------
# ROB-1310 R9 (B1 / B4) — manual quote-failure isolation, attribution, and
# non-Toss broker identity. Fake/local only; no broker, credential, or
# network access.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_isolates_kr_quote_exception_from_healthy_us_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B1.

    A raised ``fetch_kr_prices`` exception must not fall through to the
    reader's outer catch-all: the KR holding must survive unpriced under its
    real source, and a healthy concurrent US fetch must still value the US
    holding. Previously any quote-fetch exception discarded every account and
    holding and reported a hardcoded ``toss_manual`` source.
    """

    broker_toss = SimpleNamespace(
        id=901, broker_type="toss", account_name="토스 수동계좌"
    )
    broker_kis = SimpleNamespace(id=902, broker_type="kis", account_name="KIS 수동계좌")
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=901,
            broker_account=broker_toss,
            ticker="005930",
            market_type=MarketType.KR,
            display_name="삼성전자",
            quantity=10,
            avg_price=70_000,
        ),
        SimpleNamespace(
            id=2,
            broker_account_id=902,
            broker_account=broker_kis,
            ticker="AAPL",
            market_type=MarketType.US,
            display_name="Apple",
            quantity=5,
            avg_price=100,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(
        side_effect=RuntimeError("fake-kr-provider-outage-ROB1310")
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={"AAPL": 120.0})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    by_symbol = {holding.symbol: holding for holding in result.holdings}
    assert "005930" in by_symbol, "the KR holding must survive the KR quote failure"
    assert by_symbol["005930"].priceState == "missing"
    assert by_symbol["005930"].valueKrw is None

    assert "AAPL" in by_symbol, "a healthy concurrent US fetch must not be discarded"
    assert by_symbol["AAPL"].priceState == "live"
    assert by_symbol["AAPL"].valueNative == pytest.approx(600.0)

    assert {a.accountId for a in result.accounts} == {"901", "902"}

    emitted = _emitted_warning_sources(result)
    assert emitted == ["toss_manual"], (
        f"only the actually-affected KR/toss source may be warned about; got {emitted!r}"
    )
    messages = [result.warning.message] if result.warning is not None else []
    messages.extend(
        warning.message for warning in (getattr(result, "extra_warnings", None) or [])
    )
    for message in messages:
        assert "fake-kr-provider-outage-ROB1310" not in message
        assert "RuntimeError" not in message
        assert "Traceback" not in message


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_holdings_load_failure_uses_unknown_source_without_leak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ROB-1310 R9 / B1.

    When the manual holdings load itself fails (before any broker/source is
    even known), the reader must not guess ``toss_manual``: there is no
    holding to attribute, so the explicit ``manual_unknown`` source is the
    only truthful choice. The raw exception text/traceback must not leak into
    the returned warning or the log record.
    """

    sentinel = "fake-manual-db-secret-ROB1310"

    class _BrokenManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            raise RuntimeError(f"database password={sentinel}")

    monkeypatch.setattr(readers, "ManualHoldingsService", _BrokenManualService)

    with caplog.at_level("WARNING", logger="app.services.invest_home_readers"):
        result = await readers.ManualHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "manual_unknown"
    assert sentinel not in result.warning.message

    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in repr(record.args)
        assert sentinel not in repr(record.exc_info)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_unknown_broker_type_is_explicit_not_toss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B4.

    ``broker_accounts.broker_type`` is a free-form column; an unrecognized
    value must never be silently attributed to Toss. It must map to an
    explicit, truthful, non-Toss source, and the holding must still be kept
    (never dropped for having an unknown broker label).
    """

    broker_unknown = SimpleNamespace(
        id=1001, broker_type="mystery_broker", account_name="Mystery"
    )
    holding = SimpleNamespace(
        id=1,
        broker_account_id=1001,
        broker_account=broker_unknown,
        ticker="000660",
        market_type=MarketType.KR,
        display_name="SK하이닉스",
        quantity=1,
        avg_price=100_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(return_value={"000660": 105_000.0})
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    assert len(result.holdings) == 1
    h = result.holdings[0]
    assert h.source == "manual_unknown"
    assert h.source != "toss_manual"
    assert h.valueKrw == pytest.approx(105_000.0)
    assert result.accounts[0].source == "manual_unknown"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_isa_broker_type_maps_to_isa_manual_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B4.

    ``isa_manual`` is already a declared ``AccountSourceLiteral`` value with
    full frontend metadata, but no code path produced it yet. ISA is a known,
    supported manual broker type -- it must map explicitly, not fall through
    to the ``manual_unknown``/``toss_manual`` catch-all.
    """

    broker_isa = SimpleNamespace(id=1002, broker_type="isa", account_name="ISA 계좌")
    holding = SimpleNamespace(
        id=1,
        broker_account_id=1002,
        broker_account=broker_isa,
        ticker="005930",
        market_type=MarketType.KR,
        display_name="삼성전자",
        quantity=1,
        avg_price=70_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(return_value={"005930": 75_000.0})
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    assert len(result.holdings) == 1
    assert result.holdings[0].source == "isa_manual"
    assert result.accounts[0].source == "isa_manual"


# ---------------------------------------------------------------------------
# ROB-1310 R9 / B1 — a total quote-fetch failure must not wipe every manual
# holding/account, must not hardcode the affected source to toss_manual, and
# must not leak the raw exception text/trace anywhere observable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_isolates_total_quote_failure_and_leaks_no_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROB-1310 R9 / B1 (R8 verifier blocker 1).

    A total quote-fetch failure (e.g. the whole provider is unreachable) must
    not fall into the outer exception handler and wipe out every manual
    holding/account -- the affected holding must survive as unpriced, its
    warning must name its actual (non-Toss) source, and the raw exception
    text/traceback must not leak into the warning, the response, or the log.
    """

    fake_secret_marker = "FAKE_SECRET_zzq9x_do_not_leak"
    broker_upbit = SimpleNamespace(
        id=901, broker_type="upbit", account_name="업비트 수동계좌"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=901,
            broker_account=broker_upbit,
            ticker="ETH",
            market_type=MarketType.CRYPTO,
            display_name="이더리움",
            quantity=1,
            avg_price=3_000_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(
        side_effect=RuntimeError(f"quote provider exploded: {fake_secret_marker}")
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    with caplog.at_level("WARNING"):
        result = await readers.ManualHomeReader(
            db=None,  # type: ignore[arg-type]
            quote_service=quote_service,  # type: ignore[arg-type]
        ).fetch(user_id=1)

    # The holding/account must survive -- not wiped by the outer handler.
    assert len(result.holdings) == 1
    assert result.holdings[0].symbol == "ETH"
    assert result.holdings[0].priceState == "missing"
    assert len(result.accounts) == 1

    emitted = _emitted_warning_sources(result)
    assert emitted == ["upbit_manual"], (
        "a non-Toss holding's failed valuation must not be hardcoded to "
        f"toss_manual; got {emitted!r}"
    )

    messages = [result.warning.message] if result.warning is not None else []
    messages.extend(
        warning.message for warning in (getattr(result, "extra_warnings", None) or [])
    )
    for message in messages:
        assert fake_secret_marker not in message

    for record in caplog.records:
        assert fake_secret_marker not in record.getMessage()
        if record.exc_text:
            assert fake_secret_marker not in record.exc_text


# ---------------------------------------------------------------------------
# ROB-1310 R9 / B4 — an unknown manual broker_type must never be silently
# attributed to Toss, and a known ISA broker_type must map to the isa_manual
# source the schema already declares.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_maps_isa_broker_type_and_isolates_unknown_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1310 R9 / B4 (R8 verifier blocker 4 / review 3826632061).

    ``isa`` must map to the ``isa_manual`` source the schema already
    declares, and a truly unknown ``broker_type`` string must never be
    silently attributed to Toss -- it must surface a distinct, truthful,
    non-Toss source, and the holding must still be present (not dropped).
    """

    broker_isa = SimpleNamespace(id=1001, broker_type="isa", account_name="ISA 계좌")
    broker_unknown = SimpleNamespace(
        id=1002, broker_type="mystery_broker", account_name="정체불명 계좌"
    )
    holdings = [
        SimpleNamespace(
            id=1,
            broker_account_id=1001,
            broker_account=broker_isa,
            ticker="005930",
            market_type=MarketType.KR,
            display_name="삼성전자",
            quantity=1,
            avg_price=70_000,
        ),
        SimpleNamespace(
            id=2,
            broker_account_id=1002,
            broker_account=broker_unknown,
            ticker="000660",
            market_type=MarketType.KR,
            display_name="SK하이닉스",
            quantity=1,
            avg_price=100_000,
        ),
    ]

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return holdings

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    requested_kr: list[list[str]] = []
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = _contract_faithful_kr_quote_fake(
        {"005930": 75_000.0, "000660": 120_000.0}, requested_kr
    )
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(
        db=None,  # type: ignore[arg-type]
        quote_service=quote_service,  # type: ignore[arg-type]
    ).fetch(user_id=1)

    by_symbol = {holding.symbol: holding for holding in result.holdings}
    assert len(result.holdings) == 2, "an unknown broker_type must not drop the holding"
    assert by_symbol["005930"].source == "isa_manual"
    unknown_source = by_symbol["000660"].source
    assert unknown_source != "toss_manual", (
        "an unknown broker_type must never be silently attributed to Toss"
    )
    assert unknown_source == "manual_unknown"
