"""Unit tests for PaperTradingService."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.kasset.models import KAssetPaperPositionState
from app.models.paper_trading import (
    PaperAccount,
    PaperDailySnapshot,
    PaperPosition,
    PaperTrade,
)
from app.models.trading import InstrumentType
from app.services.paper_trading_service import (
    FEE_RATES,
    VALUATION_ERROR_QUOTE_UNAVAILABLE,
    PaperTradingService,
    PositionQuote,
    calculate_fee,
)


class TestCalculateFee:
    def test_equity_kr_buy(self):
        # 1,000,000원 매수 → 0.015% = 150원
        fee = calculate_fee("equity_kr", "buy", Decimal("1000000"))
        assert fee == pytest.approx(Decimal("150.0000"))

    def test_equity_kr_sell_includes_tax(self):
        # 1,000,000원 매도 → 수수료 0.015% + 세금 0.18% = 1,950원
        fee = calculate_fee("equity_kr", "sell", Decimal("1000000"))
        assert fee == pytest.approx(Decimal("1950.0000"))

    def test_equity_us_buy_min_fee(self):
        # 작은 금액: 100 USD * 0.07% = $0.07 → min $1
        fee = calculate_fee("equity_us", "buy", Decimal("100"))
        assert fee == pytest.approx(Decimal("1.0000"))

    def test_equity_us_buy_above_min(self):
        # 10,000 USD * 0.07% = $7
        fee = calculate_fee("equity_us", "buy", Decimal("10000"))
        assert fee == pytest.approx(Decimal("7.0000"))

    def test_crypto_buy(self):
        # 1,000,000 KRW * 0.05% = 500 KRW
        fee = calculate_fee("crypto", "buy", Decimal("1000000"))
        assert fee == pytest.approx(Decimal("500.0000"))

    def test_crypto_sell(self):
        fee = calculate_fee("crypto", "sell", Decimal("2000000"))
        assert fee == pytest.approx(Decimal("1000.0000"))

    def test_unsupported_market_raises(self):
        with pytest.raises(ValueError, match="Unsupported instrument_type"):
            calculate_fee("forex", "buy", Decimal("100"))

    def test_fee_rates_structure(self):
        assert FEE_RATES["equity_kr"]["buy"] == pytest.approx(0.00015)
        assert FEE_RATES["equity_kr"]["tax_sell"] == pytest.approx(0.0018)
        assert FEE_RATES["equity_us"]["min_fee_usd"] == pytest.approx(1.0)
        assert FEE_RATES["crypto"]["sell"] == pytest.approx(0.0005)


class TestAccountManagement:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_create_account_defaults(self, service, mock_db):
        account = await service.create_account(
            name="Test",
            initial_capital_krw=Decimal("10000000"),
        )
        assert account.name == "Test"
        assert account.initial_capital == pytest.approx(Decimal("10000000"))
        assert account.initial_capital_usd == pytest.approx(Decimal("0"))
        assert account.cash_krw == pytest.approx(Decimal("10000000"))
        assert account.cash_usd == pytest.approx(Decimal("0"))
        assert account.is_active is True
        mock_db.add.assert_called_once_with(account)
        mock_db.commit.assert_awaited_once()
        mock_db.refresh.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_create_account_with_usd_and_meta(self, service, mock_db):
        account = await service.create_account(
            name="US Bot",
            initial_capital_krw=Decimal("0"),
            initial_capital_usd=Decimal("5000"),
            description="dollar-cost averaging",
            strategy_name="dca-us",
        )
        assert account.initial_capital_usd == pytest.approx(Decimal("5000"))
        assert account.cash_usd == pytest.approx(Decimal("5000"))
        assert account.description == "dollar-cost averaging"
        assert account.strategy_name == "dca-us"

    @pytest.mark.asyncio
    async def test_reset_account_restores_cash_and_deletes_positions(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="Test",
            initial_capital=Decimal("10000000"),
            initial_capital_usd=Decimal("10000"),
            cash_krw=Decimal("3000000"),
            cash_usd=Decimal("100"),
            is_active=True,
        )

        async def fake_get(account_id):
            assert account_id == 1
            return account

        monkeypatch.setattr(service, "get_account", fake_get)

        mock_db.execute = AsyncMock()
        result = await service.reset_account(1)

        assert result.cash_krw == pytest.approx(Decimal("10000000"))
        assert result.cash_usd == pytest.approx(Decimal("10000"))
        assert mock_db.execute.await_count == 3
        lock_statement, close_statement, delete_statement = (
            awaited.args[0] for awaited in mock_db.execute.await_args_list
        )
        assert tuple(lock_statement.selected_columns)[0].name == "id"
        assert close_statement.table.name == KAssetPaperPositionState.__tablename__
        assert delete_statement.table.name == PaperPosition.__tablename__

    @pytest.mark.asyncio
    async def test_reset_account_missing_raises(self, service, mock_db, monkeypatch):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Account 99 not found"):
            await service.reset_account(99)

    @pytest.mark.asyncio
    async def test_delete_account_returns_true_when_found(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="x",
            initial_capital=Decimal("0"),
            cash_krw=Decimal("0"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        mock_db.delete = AsyncMock()

        ok = await service.delete_account(1)

        assert ok is True
        mock_db.delete.assert_awaited_once_with(account)
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_account_returns_false_when_missing(
        self, service, monkeypatch
    ):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        ok = await service.delete_account(42)
        assert ok is False


class TestFetchCurrentPrice:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_fetch_crypto_uses_upbit_batch(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service.fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        price = await service._fetch_current_price("KRW-BTC", "crypto")
        assert price == pytest.approx(Decimal("95000000.0"))

    @pytest.mark.asyncio
    async def test_fetch_crypto_missing_raises(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service.fetch_multiple_current_prices",
            AsyncMock(return_value={}),
        )
        with pytest.raises(ValueError, match="No price for KRW-BTC"):
            await service._fetch_current_price("KRW-BTC", "crypto")


class TestPreviewOrder:
    @pytest.fixture
    def service_with_account(self, mock_db, monkeypatch):
        svc = PaperTradingService(mock_db)
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            svc,
            "_fetch_current_price",
            AsyncMock(return_value=Decimal("70000")),
        )
        return svc

    @pytest.mark.asyncio
    async def test_preview_kr_market_buy_by_amount(self, service_with_account):
        preview = await service_with_account.preview_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="market",
            amount=Decimal("1400000"),
        )
        # 1,400,000 / 70,000 = 20 shares (integer for equity_kr)
        assert preview["success"] is True
        assert preview["dry_run"] is True
        ex = preview["preview"]
        assert ex["instrument_type"] == "equity_kr"
        assert ex["side"] == "buy"
        assert ex["quantity"] == pytest.approx(Decimal("20"))
        assert ex["price"] == pytest.approx(Decimal("70000"))
        assert ex["gross"] == pytest.approx(Decimal("1400000"))
        # fee: 1,400,000 * 0.00015 = 210
        assert ex["fee"] == pytest.approx(Decimal("210.0000"))
        assert ex["total_cost"] == pytest.approx(Decimal("1400210.0000"))
        assert ex["currency"] == "KRW"

    @pytest.mark.asyncio
    async def test_preview_crypto_limit_buy_by_quantity(self, mock_db, monkeypatch):
        svc = PaperTradingService(mock_db)
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        # limit order uses provided price, not live quote
        monkeypatch.setattr(
            svc, "_fetch_current_price", AsyncMock(return_value=Decimal("999"))
        )

        preview = await svc.preview_order(
            account_id=1,
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            quantity=Decimal("0.01"),
            price=Decimal("95000000"),
        )
        ex = preview["preview"]
        assert ex["instrument_type"] == "crypto"
        assert ex["quantity"] == pytest.approx(Decimal("0.01000000"))
        assert ex["price"] == pytest.approx(Decimal("95000000"))
        assert ex["gross"] == pytest.approx(Decimal("950000"))
        # fee: 950,000 * 0.0005 = 475
        assert ex["fee"] == pytest.approx(Decimal("475.0000"))

    @pytest.mark.asyncio
    async def test_preview_rejects_inactive_account(self, mock_db, monkeypatch):
        svc = PaperTradingService(mock_db)
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("0"),
            cash_krw=Decimal("0"),
            cash_usd=Decimal("0"),
            is_active=False,
        )
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))

        with pytest.raises(ValueError, match="Account 1 is inactive"):
            await svc.preview_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="market",
                amount=Decimal("100000"),
            )

    @pytest.mark.asyncio
    async def test_preview_requires_quantity_or_amount(self, service_with_account):
        with pytest.raises(ValueError, match="quantity or amount"):
            await service_with_account.preview_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="market",
            )

    @pytest.mark.asyncio
    async def test_preview_limit_requires_price(self, service_with_account):
        with pytest.raises(ValueError, match="price is required"):
            await service_with_account.preview_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
            )


class TestExecuteOrderBuy:
    @pytest.fixture
    def account(self):
        return PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )

    @pytest.fixture
    def service(self, mock_db, account, monkeypatch):
        svc = PaperTradingService(mock_db)
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            svc, "_fetch_current_price", AsyncMock(return_value=Decimal("70000"))
        )
        return svc

    @pytest.mark.asyncio
    async def test_buy_creates_position_and_debits_cash(
        self, service, account, mock_db, monkeypatch
    ):
        # No existing position
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=None))

        result = await service.execute_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="market",
            amount=Decimal("1400000"),
            reason="test buy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        exec_ = result["execution"]
        assert exec_["quantity"] == pytest.approx(Decimal("20"))
        assert exec_["price"] == pytest.approx(Decimal("70000"))
        # cash after: 10,000,000 - (1,400,000 + 210)
        assert account.cash_krw == pytest.approx(Decimal("8599790.0000"))
        # PaperPosition + PaperTrade were added
        assert mock_db.add.call_count == 2
        added = [c.args[0] for c in mock_db.add.call_args_list]
        assert any(isinstance(x, PaperPosition) for x in added)
        trade = next(x for x in added if isinstance(x, PaperTrade))
        assert trade.side == "buy"
        assert trade.quantity == pytest.approx(Decimal("20"))
        assert trade.total_amount == pytest.approx(Decimal("1400000.0000"))
        assert trade.fee == pytest.approx(Decimal("210.0000"))
        assert trade.reason == "test buy"
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_buy_insufficient_cash_raises_no_commit(
        self, service, account, mock_db, monkeypatch
    ):
        account.cash_krw = Decimal("100000")  # not enough
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=None))

        with pytest.raises(ValueError, match="Insufficient KRW balance"):
            await service.execute_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="market",
                amount=Decimal("1400000"),
            )
        mock_db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_buy_additional_updates_weighted_avg(
        self, service, account, mock_db, monkeypatch
    ):
        existing = PaperPosition(
            id=10,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("10"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("600000"),
        )
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=existing))

        await service.execute_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity=Decimal("10"),
            price=Decimal("70000"),
        )

        # weighted avg: (10*60000 + 10*70000) / 20 = 65000
        assert existing.quantity == pytest.approx(Decimal("20"))
        assert existing.avg_price == pytest.approx(Decimal("65000"))
        assert existing.total_invested == pytest.approx(Decimal("1300000.0000"))
        # Only PaperTrade appended (position already existed)
        added = [c.args[0] for c in mock_db.add.call_args_list]
        assert sum(1 for x in added if isinstance(x, PaperTrade)) == 1
        assert sum(1 for x in added if isinstance(x, PaperPosition)) == 0

    @pytest.mark.asyncio
    async def test_buy_usd_debits_usd_cash(self, mock_db, monkeypatch):
        account = PaperAccount(
            id=1,
            name="US",
            initial_capital=Decimal("10000"),
            cash_krw=Decimal("0"),
            cash_usd=Decimal("10000"),
            is_active=True,
        )
        svc = PaperTradingService(mock_db)
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            svc, "_fetch_current_price", AsyncMock(return_value=Decimal("100"))
        )
        monkeypatch.setattr(svc, "_get_position", AsyncMock(return_value=None))

        await svc.execute_order(
            account_id=1,
            symbol="AAPL",
            side="buy",
            order_type="market",
            quantity=Decimal("10"),
        )
        # gross 1000, fee = max(1000*0.0007, 1) = 1.0
        assert account.cash_usd == pytest.approx(Decimal("8999.0000"))
        assert account.cash_krw == pytest.approx(Decimal("0"))


class TestExecuteOrderSell:
    @pytest.fixture
    def account(self):
        return PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("1000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )

    @pytest.fixture
    def service(self, mock_db, account, monkeypatch):
        svc = PaperTradingService(mock_db)
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            svc, "_fetch_current_price", AsyncMock(return_value=Decimal("80000"))
        )
        return svc

    @pytest.mark.asyncio
    async def test_sell_partial_realized_pnl_credits_cash(
        self, service, account, mock_db, monkeypatch
    ):
        position = PaperPosition(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("20"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("1200000"),
        )
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=position))

        result = await service.execute_order(
            account_id=1,
            symbol="005930",
            side="sell",
            order_type="market",
            quantity=Decimal("10"),
        )

        # proceeds: 10 * 80,000 = 800,000
        # fee: 800,000 * (0.00015 + 0.0018) = 1,560
        # net proceeds: 798,440
        # realized pnl: (80,000 - 60,000) * 10 - 1,560 = 198,440
        assert result["success"] is True
        assert account.cash_krw == pytest.approx(Decimal("1798440.0000"))
        assert position.quantity == pytest.approx(Decimal("10"))
        # avg_price unchanged on sell; total_invested drops proportionally
        assert position.avg_price == pytest.approx(Decimal("60000"))
        assert position.total_invested == pytest.approx(Decimal("600000.0000"))

        added = [c.args[0] for c in mock_db.add.call_args_list]
        trade = next(x for x in added if isinstance(x, PaperTrade))
        assert trade.realized_pnl == pytest.approx(Decimal("198440.0000"))

    @pytest.mark.asyncio
    async def test_sell_full_quantity_deletes_position(
        self, service, account, mock_db, monkeypatch
    ):
        position = PaperPosition(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("10"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("600000"),
        )
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=position))
        mock_db.delete = AsyncMock()
        managed_state = SimpleNamespace(
            paper_position_id=position.id,
            closed_at=None,
        )
        mock_db.scalar.return_value = managed_state

        await service.execute_order(
            account_id=1,
            symbol="005930",
            side="sell",
            order_type="market",
            quantity=Decimal("10"),
        )

        mock_db.delete.assert_awaited_once_with(position)
        assert managed_state.paper_position_id is None
        assert managed_state.closed_at is not None

    @pytest.mark.asyncio
    async def test_sell_without_position_raises(self, service, monkeypatch, mock_db):
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="No position to sell"):
            await service.execute_order(
                account_id=1,
                symbol="005930",
                side="sell",
                order_type="market",
                quantity=Decimal("1"),
            )
        mock_db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sell_more_than_held_raises(self, service, monkeypatch, mock_db):
        position = PaperPosition(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("5"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("300000"),
        )
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=position))
        with pytest.raises(ValueError, match="Insufficient quantity"):
            await service.execute_order(
                account_id=1,
                symbol="005930",
                side="sell",
                order_type="market",
                quantity=Decimal("10"),
            )
        mock_db.commit.assert_not_awaited()


class TestQueries:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    def _make_execute_mock(self, rows):
        """Helper: wire mock_db.execute to return a result whose scalars() yields rows."""
        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars
        return AsyncMock(return_value=result)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("instrument_type", "market"),
        [("equity_kr", "KRX"), ("equity_us", "US")],
    )
    async def test_fetch_current_price_uses_unified_market_quote(
        self, service, mock_db, monkeypatch, instrument_type, market
    ):
        quote_for_market = AsyncMock(
            return_value=SimpleNamespace(
                price=Decimal("70123.45"),
                source="PAPER_TOSS",
                as_of="2026-08-31T05:00:00Z",
                session="REGULAR",
            )
        )
        monkeypatch.setattr(
            "app.extensions.kasset.api.krx_quotes.quote_for_market",
            quote_for_market,
        )

        price = await service._fetch_current_price("005930", instrument_type)

        assert price == Decimal("70123.45")
        quote_for_market.assert_awaited_once_with(
            mock_db,
            market=market,
            symbol="005930",
        )

    @pytest.mark.asyncio
    async def test_fetch_quote_passes_provider_provenance_through(
        self, service, monkeypatch
    ):
        monkeypatch.setattr(
            "app.extensions.kasset.api.krx_quotes.quote_for_market",
            AsyncMock(
                return_value=SimpleNamespace(
                    price=Decimal("70123.45"),
                    source="PAPER_CANDLES",
                    as_of="2026-08-31T05:00:00Z",
                    session="CLOSED",
                )
            ),
        )

        quote = await service._fetch_quote("005930", "equity_kr")

        assert quote.source == "PAPER_CANDLES"
        assert quote.as_of == datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
        assert quote.session == "CLOSED"

    @pytest.mark.asyncio
    async def test_get_positions_enriches_with_current_price(
        self, service, mock_db, monkeypatch
    ):
        position = PaperPosition(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("10"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("600000"),
        )
        mock_db.execute = self._make_execute_mock([position])
        monkeypatch.setattr(
            service,
            "_fetch_quote",
            AsyncMock(
                return_value=PositionQuote(
                    price=Decimal("70000"),
                    source="PAPER_TOSS",
                    as_of=datetime.now(UTC),
                    session="REGULAR",
                )
            ),
        )

        positions = await service.get_positions(account_id=1)
        assert len(positions) == 1
        p = positions[0]
        assert p["symbol"] == "005930"
        assert p["currency"] == "KRW"
        assert p["quantity"] == pytest.approx(Decimal("10"))
        assert p["avg_price"] == pytest.approx(Decimal("60000"))
        assert p["current_price"] == pytest.approx(Decimal("70000"))
        assert p["evaluation_amount"] == pytest.approx(Decimal("700000.0000"))
        assert p["unrealized_pnl"] == pytest.approx(Decimal("100000.0000"))
        # (70000 - 60000) / 60000 * 100 = 16.6666...
        assert p["pnl_pct"] == pytest.approx(Decimal("16.67"))
        assert p["quote_source"] == "PAPER_TOSS"
        assert p["quote_session"] == "REGULAR"
        assert p["quote_is_stale"] is False
        assert p["valuation_error"] is None

    @pytest.mark.asyncio
    async def test_get_positions_reports_stable_code_on_quote_failure(
        self, service, mock_db, monkeypatch
    ):
        position = PaperPosition(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            quantity=Decimal("10"),
            avg_price=Decimal("60000"),
            total_invested=Decimal("600000"),
        )
        mock_db.execute = self._make_execute_mock([position])
        monkeypatch.setattr(
            service,
            "_fetch_quote",
            AsyncMock(side_effect=RuntimeError("net down: token=abc123")),
        )
        positions = await service.get_positions(account_id=1)
        assert positions[0]["current_price"] is None
        assert positions[0]["evaluation_amount"] is None
        assert positions[0]["unrealized_pnl"] is None
        assert positions[0]["pnl_pct"] is None
        assert positions[0]["valuation_error"] == VALUATION_ERROR_QUOTE_UNAVAILABLE
        # The provider's own text never reaches a caller.
        assert "net down" not in repr(positions[0])
        assert "price_error" not in positions[0]

    @pytest.mark.asyncio
    async def test_get_cash_balance(self, service, monkeypatch):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("8000000"),
            cash_usd=Decimal("1234.5"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        balance = await service.get_cash_balance(account_id=1)
        assert balance == pytest.approx(
            {"krw": Decimal("8000000"), "usd": Decimal("1234.5")}
        )

    @pytest.mark.asyncio
    async def test_get_cash_balance_missing_raises(self, service, monkeypatch):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Account 99 not found"):
            await service.get_cash_balance(account_id=99)

    @pytest.mark.asyncio
    async def test_get_trade_history_filters(self, service, mock_db):
        trade = PaperTrade(
            id=1,
            account_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="market",
            quantity=Decimal("10"),
            price=Decimal("70000"),
            total_amount=Decimal("700000"),
            fee=Decimal("105"),
            currency="KRW",
            reason="test",
        )
        mock_db.execute = self._make_execute_mock([trade])
        history = await service.get_trade_history(
            account_id=1, symbol="005930", side="buy", limit=10
        )
        assert len(history) == 1
        assert history[0]["symbol"] == "005930"
        assert history[0]["side"] == "buy"
        assert history[0]["quantity"] == pytest.approx(Decimal("10"))


class TestPortfolioSummary:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_summary_keeps_currencies_apart(self, service, monkeypatch):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            initial_capital_usd=Decimal("1000"),
            cash_krw=Decimal("1000000"),
            cash_usd=Decimal("200"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("600000"),
                        "evaluation_amount": Decimal("700000"),
                        "unrealized_pnl": Decimal("100000"),
                    },
                    {
                        "symbol": "AAPL",
                        "instrument_type": "equity_us",
                        "currency": "USD",
                        "total_invested": Decimal("300"),
                        "evaluation_amount": Decimal("280"),
                        "unrealized_pnl": Decimal("-20"),
                    },
                ]
            ),
        )

        summary = await service.get_portfolio_summary(account_id=1)

        # No portfolio-wide money total exists to be a KRW+USD sum.
        assert "total_invested" not in summary
        assert "total_evaluated" not in summary
        assert "total_pnl" not in summary
        assert "total_pnl_pct" not in summary

        krw = summary["currencies"]["KRW"]
        assert krw["total_invested"] == pytest.approx(Decimal("600000"))
        assert krw["total_evaluated"] == pytest.approx(Decimal("700000"))
        assert krw["total_pnl"] == pytest.approx(Decimal("100000"))
        # 100000 / 600000 * 100 = 16.6666...
        assert krw["total_pnl_pct"] == pytest.approx(Decimal("16.67"))
        assert krw["valuation_complete"] is True

        usd = summary["currencies"]["USD"]
        assert usd["total_invested"] == pytest.approx(Decimal("300"))
        assert usd["total_evaluated"] == pytest.approx(Decimal("280"))
        assert usd["total_pnl"] == pytest.approx(Decimal("-20"))
        # -20 / 300 * 100 = -6.6666...
        assert usd["total_pnl_pct"] == pytest.approx(Decimal("-6.67"))

        assert summary["cash_krw"] == pytest.approx(Decimal("1000000"))
        assert summary["cash_usd"] == pytest.approx(Decimal("200"))
        assert summary["positions_count"] == 2
        assert summary["unsupported_currencies"] == {}

    @pytest.mark.asyncio
    async def test_summary_withholds_totals_for_partially_valued_currency(
        self, service, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("1000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("600000"),
                        "evaluation_amount": Decimal("700000"),
                        "unrealized_pnl": Decimal("100000"),
                    },
                    {
                        "symbol": "000660",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("300000"),
                        "evaluation_amount": None,
                        "unrealized_pnl": None,
                    },
                ]
            ),
        )

        krw = (await service.get_portfolio_summary(account_id=1))["currencies"]["KRW"]

        assert krw["positions_count"] == 2
        assert krw["positions_valued"] == 1
        assert krw["valuation_complete"] is False
        # A total covering only half the holdings would read as the whole.
        assert krw["total_evaluated"] is None
        assert krw["total_pnl"] is None
        assert krw["total_pnl_pct"] is None

    @pytest.mark.asyncio
    async def test_summary_handles_empty_positions(self, service, monkeypatch):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("1000000"),
            cash_krw=Decimal("1000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(service, "get_positions", AsyncMock(return_value=[]))

        summary = await service.get_portfolio_summary(account_id=1)
        assert summary["positions_count"] == 0
        for currency in ("KRW", "USD"):
            metrics = summary["currencies"][currency]
            assert metrics["total_invested"] == pytest.approx(Decimal("0"))
            assert metrics["total_evaluated"] == pytest.approx(Decimal("0"))
            assert metrics["total_pnl"] == pytest.approx(Decimal("0"))
            assert metrics["total_pnl_pct"] is None
            assert metrics["valuation_complete"] is True

    @pytest.mark.asyncio
    async def test_summary_discloses_unreported_currency_instead_of_folding_it(
        self, service, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("1000000"),
            cash_krw=Decimal("1000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "USDT-BTC",
                        "instrument_type": "crypto",
                        "currency": "USDT",
                        "total_invested": Decimal("5000"),
                        "evaluation_amount": Decimal("5500"),
                        "unrealized_pnl": Decimal("500"),
                    }
                ]
            ),
        )

        summary = await service.get_portfolio_summary(account_id=1)

        # USDT is neither added to KRW nor silently dropped.
        assert summary["currencies"]["KRW"]["positions_count"] == 0
        assert summary["currencies"]["USD"]["positions_count"] == 0
        assert summary["unsupported_currencies"] == {"USDT": {"positions": 1}}


class TestDailySnapshots:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_record_snapshot_records_equity_per_currency(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            initial_capital_usd=Decimal("1000"),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("400"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("5000000"),
                        "evaluation_amount": Decimal("6000000"),
                        "unrealized_pnl": Decimal("1000000"),
                    },
                    {
                        "symbol": "AAPL",
                        "instrument_type": "equity_us",
                        "currency": "USD",
                        "total_invested": Decimal("500"),
                        "evaluation_amount": Decimal("600"),
                        "unrealized_pnl": Decimal("100"),
                    },
                ]
            ),
        )

        prior = PaperDailySnapshot(
            id=10,
            account_id=1,
            snapshot_date=date(2026, 4, 12),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("400"),
            equity_krw=Decimal("10000000"),
            equity_usd=Decimal("800"),
            valuation_complete_krw=True,
            valuation_complete_usd=True,
        )

        scalars_none = MagicMock()
        scalars_none.scalar_one_or_none = MagicMock(return_value=None)
        scalars_prior = MagicMock()
        scalars_prior.scalar_one_or_none = MagicMock(return_value=prior)
        mock_db.execute = AsyncMock(side_effect=[scalars_none, scalars_prior])

        snapshot = await service.record_daily_snapshot(account_id=1)

        assert snapshot.cash_krw == pytest.approx(Decimal("5000000"))
        assert snapshot.equity_krw == pytest.approx(Decimal("11000000"))
        assert snapshot.equity_usd == pytest.approx(Decimal("1000"))
        assert snapshot.valuation_complete_krw is True
        assert snapshot.valuation_complete_usd is True
        # 11,000,000 / 10,000,000 and 1,000 / 800 — each within its own currency.
        assert snapshot.daily_return_krw_pct == pytest.approx(Decimal("10.0000"))
        assert snapshot.daily_return_usd_pct == pytest.approx(Decimal("25.0000"))
        # The mixed columns are left alone, not rewritten with a KRW+USD sum.
        assert snapshot.total_equity is None
        assert snapshot.positions_value is None
        assert snapshot.daily_return_pct is None
        mock_db.add.assert_called_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_record_snapshot_first_ever_has_null_returns(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(service, "get_positions", AsyncMock(return_value=[]))

        scalars_none1 = MagicMock()
        scalars_none1.scalar_one_or_none = MagicMock(return_value=None)
        scalars_none2 = MagicMock()
        scalars_none2.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(side_effect=[scalars_none1, scalars_none2])

        snapshot = await service.record_daily_snapshot(account_id=1)
        assert snapshot.daily_return_krw_pct is None
        assert snapshot.daily_return_usd_pct is None
        assert snapshot.equity_krw == pytest.approx(Decimal("10000000"))
        assert snapshot.equity_usd == pytest.approx(Decimal("0"))

    @pytest.mark.asyncio
    async def test_record_snapshot_marks_partially_valued_currency_incomplete(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("5000000"),
                        "evaluation_amount": None,
                        "unrealized_pnl": None,
                    }
                ]
            ),
        )

        prior = PaperDailySnapshot(
            id=10,
            account_id=1,
            snapshot_date=date(2026, 4, 12),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("0"),
            equity_krw=Decimal("10000000"),
            equity_usd=Decimal("0"),
            valuation_complete_krw=True,
            valuation_complete_usd=True,
        )
        scalars_none = MagicMock()
        scalars_none.scalar_one_or_none = MagicMock(return_value=None)
        scalars_prior = MagicMock()
        scalars_prior.scalar_one_or_none = MagicMock(return_value=prior)
        mock_db.execute = AsyncMock(side_effect=[scalars_none, scalars_prior])

        snapshot = await service.record_daily_snapshot(account_id=1)

        assert snapshot.valuation_complete_krw is False
        # An unpriced holding would make this a cash-only "50% drop".
        assert snapshot.daily_return_krw_pct is None

    @pytest.mark.asyncio
    async def test_calculate_daily_returns_reports_each_currency(
        self, service, mock_db
    ):
        snaps = [
            PaperDailySnapshot(
                id=1,
                account_id=1,
                snapshot_date=date(2026, 4, 10),
                cash_krw=Decimal("0"),
                cash_usd=Decimal("0"),
                equity_krw=Decimal("10000000"),
                equity_usd=Decimal("1000"),
                valuation_complete_krw=True,
                valuation_complete_usd=True,
            ),
            # Pre-P0 row: only the mixed columns exist, so no currency is safe.
            PaperDailySnapshot(
                id=2,
                account_id=1,
                snapshot_date=date(2026, 4, 11),
                cash_krw=Decimal("0"),
                cash_usd=Decimal("0"),
                positions_value=Decimal("0"),
                total_equity=Decimal("10100000"),
                daily_return_pct=Decimal("1.0000"),
            ),
        ]
        scalars = MagicMock()
        scalars.all.return_value = snaps
        result = MagicMock()
        result.scalars.return_value = scalars
        mock_db.execute = AsyncMock(return_value=result)

        rows = await service.calculate_daily_returns(
            account_id=1,
            start_date=date(2026, 4, 10),
            end_date=date(2026, 4, 11),
        )
        assert rows == [
            {
                "date": "2026-04-10",
                "currencies": {
                    "KRW": {
                        "total_equity": Decimal("10000000"),
                        "daily_return_pct": None,
                        "valuation_complete": True,
                    },
                    "USD": {
                        "total_equity": Decimal("1000"),
                        "daily_return_pct": None,
                        "valuation_complete": True,
                    },
                },
            },
            {
                "date": "2026-04-11",
                "currencies": {
                    "KRW": {
                        "total_equity": None,
                        "daily_return_pct": None,
                        "valuation_complete": False,
                    },
                    "USD": {
                        "total_equity": None,
                        "daily_return_pct": None,
                        "valuation_complete": False,
                    },
                },
            },
        ]


class TestRoundTrips:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    def _trade(self, **kw):
        from datetime import datetime

        defaults = {
            "id": 0,
            "account_id": 1,
            "symbol": "005930",
            "instrument_type": InstrumentType.equity_kr,
            "side": "buy",
            "order_type": "market",
            "quantity": Decimal("10"),
            "price": Decimal("60000"),
            "total_amount": Decimal("600000"),
            "fee": Decimal("0"),
            "currency": "KRW",
            "reason": None,
            "realized_pnl": None,
            "executed_at": datetime(2026, 4, 1, tzinfo=UTC),
        }
        defaults.update(kw)
        return PaperTrade(**defaults)

    def test_closed_round_trip_computes_holding_days_and_pnl(self, service):
        from datetime import datetime

        trades = [
            self._trade(
                id=1,
                side="buy",
                quantity=Decimal("10"),
                price=Decimal("60000"),
                fee=Decimal("90"),
                executed_at=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
                reason="entry",
            ),
            self._trade(
                id=2,
                side="sell",
                quantity=Decimal("10"),
                price=Decimal("70000"),
                fee=Decimal("1365"),
                realized_pnl=Decimal("98635"),
                executed_at=datetime(2026, 4, 6, 9, 0, tzinfo=UTC),
                reason="exit",
            ),
        ]
        trips = service._build_round_trips(trades)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["symbol"] == "005930"
        assert trip["holding_days"] == 5
        assert trip["pnl"] == pytest.approx(98635.0)
        assert round(trip["return_pct"], 2) == pytest.approx(16.44)
        assert trip["entry_reason"] == "entry"
        assert trip["exit_reason"] == "exit"

    def test_unclosed_position_excluded(self, service):
        from datetime import datetime

        trades = [
            self._trade(
                id=1,
                side="buy",
                quantity=Decimal("10"),
                executed_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            self._trade(
                id=2,
                side="sell",
                quantity=Decimal("4"),
                realized_pnl=Decimal("40000"),
                executed_at=datetime(2026, 4, 3, tzinfo=UTC),
            ),
        ]
        assert service._build_round_trips(trades) == []

    def test_multiple_symbols_grouped_independently(self, service):
        from datetime import datetime

        trades = [
            self._trade(
                id=1,
                symbol="A",
                side="buy",
                executed_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            self._trade(
                id=2,
                symbol="B",
                side="buy",
                executed_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            self._trade(
                id=3,
                symbol="A",
                side="sell",
                realized_pnl=Decimal("100"),
                executed_at=datetime(2026, 4, 2, tzinfo=UTC),
            ),
            self._trade(
                id=4,
                symbol="B",
                side="sell",
                realized_pnl=Decimal("-50"),
                executed_at=datetime(2026, 4, 3, tzinfo=UTC),
            ),
        ]
        trips = service._build_round_trips(trades)
        assert {t["symbol"] for t in trips} == {"A", "B"}


class TestRiskMetrics:
    def test_max_drawdown_pct_basic(self):
        equities = [
            Decimal("100"),
            Decimal("120"),
            Decimal("90"),
            Decimal("110"),
            Decimal("80"),
        ]
        dd = PaperTradingService._calc_max_drawdown_pct(equities)
        assert round(dd, 2) == pytest.approx(33.33)

    def test_max_drawdown_empty_returns_none(self):
        assert PaperTradingService._calc_max_drawdown_pct([]) is None

    def test_max_drawdown_monotonic_returns_zero(self):
        equities = [Decimal("100"), Decimal("110"), Decimal("120")]
        assert PaperTradingService._calc_max_drawdown_pct(equities) == pytest.approx(
            0.0
        )

    def test_sharpe_ratio_with_uniform_returns(self):
        rets = [Decimal("1.0"), Decimal("1.0"), Decimal("1.0")]
        assert PaperTradingService._calc_sharpe_ratio(rets) is None

    def test_sharpe_ratio_mixed(self):
        rets = [Decimal("1.0"), Decimal("-0.5"), Decimal("2.0"), Decimal("0.5")]
        sharpe = PaperTradingService._calc_sharpe_ratio(rets)
        assert sharpe is not None and sharpe > 0

    def test_sharpe_requires_at_least_two(self):
        assert PaperTradingService._calc_sharpe_ratio([Decimal("1.0")]) is None


class TestCalculatePerformance:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @staticmethod
    def _trade(**kw):
        defaults = {
            "account_id": 1,
            "instrument_type": InstrumentType.equity_kr,
            "order_type": "market",
            "fee": Decimal("0"),
            "currency": "KRW",
            "reason": None,
            "realized_pnl": None,
        }
        defaults.update(kw)
        return PaperTrade(**defaults)

    @staticmethod
    def _p0_snapshot(**kw):
        defaults = {
            "account_id": 1,
            "cash_krw": Decimal("0"),
            "cash_usd": Decimal("0"),
            "valuation_complete_krw": True,
            "valuation_complete_usd": True,
        }
        defaults.update(kw)
        return PaperDailySnapshot(**defaults)

    @pytest.mark.asyncio
    async def test_full_performance_all_period(self, service, mock_db, monkeypatch):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            initial_capital_usd=Decimal("1000"),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("400"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "A",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("5500000"),
                        "evaluation_amount": Decimal("6000000"),
                        "unrealized_pnl": Decimal("500000"),
                    },
                    {
                        "symbol": "AAPL",
                        "instrument_type": "equity_us",
                        "currency": "USD",
                        "total_invested": Decimal("700"),
                        "evaluation_amount": Decimal("600"),
                        "unrealized_pnl": Decimal("-100"),
                    },
                ]
            ),
        )

        trades = [
            self._trade(
                id=1,
                symbol="A",
                side="buy",
                quantity=Decimal("10"),
                price=Decimal("60000"),
                total_amount=Decimal("600000"),
                fee=Decimal("90"),
                executed_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            self._trade(
                id=2,
                symbol="A",
                side="sell",
                quantity=Decimal("10"),
                price=Decimal("70000"),
                total_amount=Decimal("700000"),
                fee=Decimal("1365"),
                realized_pnl=Decimal("98635"),
                executed_at=datetime(2026, 4, 6, tzinfo=UTC),
            ),
            # A USD round trip in the same period must not touch KRW metrics.
            self._trade(
                id=3,
                symbol="AAPL",
                instrument_type=InstrumentType.equity_us,
                currency="USD",
                side="buy",
                quantity=Decimal("2"),
                price=Decimal("100"),
                total_amount=Decimal("200"),
                executed_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            self._trade(
                id=4,
                symbol="AAPL",
                instrument_type=InstrumentType.equity_us,
                currency="USD",
                side="sell",
                quantity=Decimal("2"),
                price=Decimal("90"),
                total_amount=Decimal("180"),
                realized_pnl=Decimal("-20"),
                executed_at=datetime(2026, 4, 3, tzinfo=UTC),
            ),
        ]
        scalars_trades = MagicMock()
        scalars_trades.all.return_value = trades
        trade_result = MagicMock()
        trade_result.scalars.return_value = scalars_trades

        snaps = [
            self._p0_snapshot(
                id=1,
                snapshot_date=date(2026, 4, 1),
                equity_krw=Decimal("10000000"),
                equity_usd=Decimal("1000"),
            ),
            self._p0_snapshot(
                id=2,
                snapshot_date=date(2026, 4, 2),
                equity_krw=Decimal("10100000"),
                equity_usd=Decimal("1010"),
                daily_return_krw_pct=Decimal("1.0"),
                daily_return_usd_pct=Decimal("1.0"),
            ),
            self._p0_snapshot(
                id=3,
                snapshot_date=date(2026, 4, 3),
                equity_krw=Decimal("9950000"),
                equity_usd=Decimal("995"),
                daily_return_krw_pct=Decimal("-1.49"),
                daily_return_usd_pct=Decimal("-1.49"),
            ),
        ]
        snap_scalars = MagicMock()
        snap_scalars.all.return_value = snaps
        snap_result = MagicMock()
        snap_result.scalars.return_value = snap_scalars

        mock_db.execute = AsyncMock(side_effect=[trade_result, snap_result])

        perf = await service.calculate_performance(account_id=1)

        # No combined figure exists at the top level.
        assert set(perf) == {"currencies", "unsupported_currencies"}
        assert set(perf["currencies"]) == {"KRW", "USD"}

        krw = perf["currencies"]["KRW"]
        # 5,000,000 cash + 6,000,000 positions vs 10,000,000 opening capital.
        assert krw["total_equity"] == pytest.approx(11000000.0)
        assert krw["total_return_pct"] == pytest.approx(10.0)
        assert krw["realized_pnl"] == pytest.approx(98635.0)
        assert krw["unrealized_pnl"] == pytest.approx(500000.0)
        assert krw["total_trades"] == 1
        assert krw["win_rate"] == pytest.approx(100.0)
        assert krw["avg_holding_days"] == pytest.approx(5.0)
        assert round(krw["max_drawdown_pct"], 2) == pytest.approx(1.49)
        assert krw["sharpe_ratio"] is not None
        assert krw["best_trade"]["symbol"] == "A"
        assert krw["worst_trade"]["symbol"] == "A"
        assert krw["snapshots_used"] == 3

        usd = perf["currencies"]["USD"]
        # 400 cash + 600 positions vs 1,000 opening capital.
        assert usd["total_equity"] == pytest.approx(1000.0)
        assert usd["total_return_pct"] == pytest.approx(0.0)
        assert usd["realized_pnl"] == pytest.approx(-20.0)
        assert usd["unrealized_pnl"] == pytest.approx(-100.0)
        assert usd["total_trades"] == 1
        assert usd["win_rate"] == pytest.approx(0.0)
        assert usd["best_trade"]["symbol"] == "AAPL"
        assert perf["unsupported_currencies"] == {}

    @pytest.mark.asyncio
    async def test_performance_skips_pre_p0_mixed_snapshots(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(service, "get_positions", AsyncMock(return_value=[]))

        legacy = [
            PaperDailySnapshot(
                id=1,
                account_id=1,
                snapshot_date=date(2026, 4, 1),
                cash_krw=Decimal("0"),
                cash_usd=Decimal("0"),
                positions_value=Decimal("0"),
                total_equity=Decimal("10000000"),
                daily_return_pct=Decimal("1.0"),
            ),
            PaperDailySnapshot(
                id=2,
                account_id=1,
                snapshot_date=date(2026, 4, 2),
                cash_krw=Decimal("0"),
                cash_usd=Decimal("0"),
                positions_value=Decimal("0"),
                total_equity=Decimal("5000000"),
                daily_return_pct=Decimal("-50.0"),
            ),
        ]
        empty_trades = MagicMock()
        empty_trades.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        snap_result = MagicMock()
        snap_result.scalars.return_value = MagicMock(all=MagicMock(return_value=legacy))
        mock_db.execute = AsyncMock(side_effect=[empty_trades, snap_result])

        krw = (await service.calculate_performance(account_id=1))["currencies"]["KRW"]

        # The legacy rows carry a 50% mixed-currency drawdown; none of it may
        # leak into a per-currency metric.
        assert krw["snapshots_used"] == 0
        assert krw["max_drawdown_pct"] is None
        assert krw["sharpe_ratio"] is None

    @pytest.mark.asyncio
    async def test_performance_withholds_metrics_for_unvalued_currency(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("5000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(
            service,
            "get_positions",
            AsyncMock(
                return_value=[
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "currency": "KRW",
                        "total_invested": Decimal("5000000"),
                        "evaluation_amount": None,
                        "unrealized_pnl": None,
                    }
                ]
            ),
        )
        empty = MagicMock()
        empty.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        mock_db.execute = AsyncMock(return_value=empty)

        krw = (await service.calculate_performance(account_id=1))["currencies"]["KRW"]

        assert krw["valuation_complete"] is False
        assert krw["positions_valued"] == 0
        assert krw["positions_value"] is None
        assert krw["total_equity"] is None
        # Cash-only equity would look like a 50% loss.
        assert krw["total_return_pct"] is None
        assert krw["unrealized_pnl"] is None

    @pytest.mark.asyncio
    async def test_performance_no_trades_returns_zero_rates_per_currency(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(service, "get_positions", AsyncMock(return_value=[]))

        empty = MagicMock()
        empty.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        mock_db.execute = AsyncMock(return_value=empty)

        perf = await service.calculate_performance(account_id=1)
        for currency in ("KRW", "USD"):
            metrics = perf["currencies"][currency]
            # An unfunded currency reports 0%, never a division by zero.
            assert metrics["total_return_pct"] == pytest.approx(0.0)
            assert metrics["realized_pnl"] == pytest.approx(0.0)
            assert metrics["unrealized_pnl"] == pytest.approx(0.0)
            assert metrics["total_trades"] == 0
            assert metrics["win_rate"] == pytest.approx(0.0)
            assert metrics["avg_holding_days"] == pytest.approx(0.0)
            assert metrics["max_drawdown_pct"] is None
            assert metrics["sharpe_ratio"] is None
            assert metrics["best_trade"] is None
            assert metrics["worst_trade"] is None

    @pytest.mark.asyncio
    async def test_performance_missing_account_raises(self, service, monkeypatch):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Account 99 not found"):
            await service.calculate_performance(account_id=99)
        assert PaperTradingService._calc_sharpe_ratio([]) is None


class TestListAccountsStrategyFilter:
    """list_accounts strategy_name 필터 테스트."""

    @pytest.mark.asyncio
    async def test_filter_by_strategy_name(self, mock_db) -> None:
        service = PaperTradingService(mock_db)
        momentum_account = PaperAccount(
            name="paper-momentum",
            initial_capital=Decimal("100000000"),
            cash_krw=Decimal("100000000"),
            strategy_name="momentum",
            is_active=True,
        )

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [momentum_account]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        accounts = await service.list_accounts(is_active=True, strategy_name="momentum")
        assert len(accounts) == 1
        assert accounts[0].strategy_name == "momentum"

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, mock_db) -> None:
        service = PaperTradingService(mock_db)
        accounts_data = [
            PaperAccount(
                name="a",
                initial_capital=Decimal("100000000"),
                cash_krw=Decimal("100000000"),
                strategy_name="momentum",
                is_active=True,
            ),
            PaperAccount(
                name="b",
                initial_capital=Decimal("100000000"),
                cash_krw=Decimal("100000000"),
                strategy_name=None,
                is_active=True,
            ),
        ]

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = accounts_data
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        accounts = await service.list_accounts(is_active=True)
        assert len(accounts) == 2


class TestResolvedMarketPrice:
    """`resolved_market_price`: 호출자가 이미 승인한 기준가로 체결한다.

    KAsset PAPER 자동매매는 제출 시점에 토스 실시간 시세로 리스크·교차 판정을
    끝낸다. 체결 단계가 가격을 다시 만들면 (a) 재조회 공급자(KRX는 KIS 전용)가
    죽어 있으면 체결이 실패하고 (b) 살아 있어도 승인한 가격과 다른 가격으로
    체결된다.
    """

    @pytest.fixture
    def account(self):
        return PaperAccount(
            id=1,
            name="A",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            cash_usd=Decimal("0"),
            is_active=True,
        )

    @pytest.fixture
    def refetch(self):
        return AsyncMock(return_value=Decimal("70000"))

    @pytest.fixture
    def service(self, mock_db, account, refetch, monkeypatch):
        svc = PaperTradingService(mock_db)
        monkeypatch.setattr(svc, "get_account", AsyncMock(return_value=account))
        monkeypatch.setattr(svc, "_fetch_current_price", refetch)
        return svc

    @pytest.mark.asyncio
    async def test_market_preview_uses_resolved_price_without_refetch(
        self, service, refetch
    ):
        preview = await service.preview_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
            resolved_market_price=Decimal("71234"),
        )

        assert preview["preview"]["price"] == Decimal("71234")
        assert preview["preview"]["gross"] == pytest.approx(Decimal("142468"))
        refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_market_preview_without_resolved_price_still_refetches(
        self, service, refetch
    ):
        preview = await service.preview_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
        )

        assert preview["preview"]["price"] == Decimal("70000")
        refetch.assert_awaited_once_with("005930", "equity_kr")

    @pytest.mark.asyncio
    async def test_market_execute_fills_and_records_resolved_price(
        self, service, refetch, mock_db, monkeypatch
    ):
        monkeypatch.setattr(service, "_get_position", AsyncMock(return_value=None))

        result = await service.execute_order(
            account_id=1,
            symbol="005930",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
            resolved_market_price=Decimal("71234"),
        )

        assert result["execution"]["price"] == Decimal("71234")
        added = [c.args[0] for c in mock_db.add.call_args_list]
        trade = next(x for x in added if isinstance(x, PaperTrade))
        assert trade.price == Decimal("71234")
        assert trade.total_amount == pytest.approx(Decimal("142468.0000"))
        refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_limit_order_rejects_resolved_price(self, service, refetch):
        with pytest.raises(
            ValueError, match="resolved_market_price is only valid for market orders"
        ):
            await service.preview_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=Decimal("2"),
                price=Decimal("60000"),
                resolved_market_price=Decimal("71234"),
            )
        refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_limit_order_rejects_resolved_price_via_execute_order(
        self, service, mock_db
    ):
        with pytest.raises(
            ValueError, match="resolved_market_price is only valid for market orders"
        ):
            await service.execute_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="limit",
                quantity=Decimal("2"),
                price=Decimal("60000"),
                resolved_market_price=Decimal("71234"),
            )
        mock_db.commit.assert_not_awaited()

    @pytest.mark.parametrize(
        ("bad_price", "message"),
        [
            (Decimal("0"), "must be positive"),
            (Decimal("-1"), "must be positive"),
            (0.0, "must be positive"),
            (float("nan"), "must be finite"),
            (float("inf"), "must be finite"),
            (Decimal("-Infinity"), "must be finite"),
            ("not-a-price", "not a valid decimal"),
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_resolved_price_fails_closed(
        self, service, refetch, bad_price, message
    ):
        with pytest.raises(ValueError, match=message):
            await service.preview_order(
                account_id=1,
                symbol="005930",
                side="buy",
                order_type="market",
                quantity=Decimal("2"),
                resolved_market_price=bad_price,
            )
        # 나쁜 기준가는 조용히 재조회로 메워지지 않는다.
        refetch.assert_not_awaited()
