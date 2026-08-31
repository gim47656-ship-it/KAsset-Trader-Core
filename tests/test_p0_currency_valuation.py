"""P0 evidence integrity — currency partitioning and valuation provenance.

Covers the pieces the service/API tests exercise only indirectly: the migration
that introduces the per-currency snapshot columns, the pure currency/provenance
helpers, and the Android wire contract for a position's quote evidence.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.extensions.kasset.api.paper import PaperAccountAdapter
from app.extensions.kasset.api.paper_schemas import Position
from app.services.paper_trading_service import (
    PAPER_QUOTE_STALE_AFTER,
    REPORTED_CURRENCIES,
    CurrencyValuation,
    PositionQuote,
    parse_quote_as_of,
    position_currency,
    snapshot_equity,
    snapshot_is_currency_safe,
    unsupported_currency_evidence,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260831_p0_currency.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "p0_currency_migration", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    """Records the DDL a migration issues so it can be asserted without a DB."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str, bool]] = []
        self.dropped: list[str] = []
        self.altered: list[tuple[str, bool]] = []
        self.statements: list[str] = []

    def add_column(self, table, column, schema=None):
        self.added.append((f"{schema}.{table}", column.name, column.nullable))

    def drop_column(self, table, column_name, schema=None):
        self.dropped.append(column_name)

    def alter_column(self, table, column_name, schema=None, **kwargs):
        self.altered.append((column_name, kwargs["nullable"]))

    def execute(self, statement):
        self.statements.append(str(statement))


class TestCurrencyMigration:
    def test_revision_chain_matches_the_p0_contract(self):
        module = _load_migration()
        assert module.revision == "20260831_p0_currency"
        assert module.down_revision == "20260831_p0_trace"

    def test_upgrade_adds_nullable_per_currency_columns(self, monkeypatch):
        module = _load_migration()
        recorder = _OpRecorder()
        monkeypatch.setattr(module, "op", recorder)

        module.upgrade()

        assert [name for _table, name, _nullable in recorder.added] == [
            "equity_krw",
            "equity_usd",
            "daily_return_krw_pct",
            "daily_return_usd_pct",
            "valuation_complete_krw",
            "valuation_complete_usd",
        ]
        # Additive only: existing rows have no per-currency history to backfill,
        # so every new column must accept NULL.
        assert all(nullable for _table, _name, nullable in recorder.added)
        assert {table for table, _name, _nullable in recorder.added} == {
            "paper.paper_daily_snapshots"
        }
        # The mixed columns are retired by becoming optional — never dropped.
        assert recorder.altered == [
            ("positions_value", True),
            ("total_equity", True),
        ]
        assert recorder.dropped == []

    def test_downgrade_rebuilds_legacy_columns_before_restoring_not_null(
        self, monkeypatch
    ):
        module = _load_migration()
        recorder = _OpRecorder()
        monkeypatch.setattr(module, "op", recorder)

        module.downgrade()

        assert len(recorder.statements) == 2
        reconstruct = " ".join(recorder.statements[0].split())
        # Legacy meaning restored exactly: raw KRW+USD equity, and positions
        # value as equity minus the untouched cash columns.
        assert "total_equity = COALESCE(total_equity, equity_krw + equity_usd)" in (
            reconstruct
        )
        assert "(equity_krw - cash_krw) + (equity_usd - cash_usd)" in reconstruct
        # A row with no equity evidence at all still has to satisfy NOT NULL.
        fail_safe = " ".join(recorder.statements[1].split())
        assert "COALESCE(total_equity, 0)" in fail_safe
        assert "COALESCE(positions_value, 0)" in fail_safe

        assert recorder.altered == [
            ("positions_value", False),
            ("total_equity", False),
        ]
        assert recorder.dropped == [
            "valuation_complete_usd",
            "valuation_complete_krw",
            "daily_return_usd_pct",
            "daily_return_krw_pct",
            "equity_usd",
            "equity_krw",
        ]

    def test_downgrade_writes_data_before_tightening_nullability(self, monkeypatch):
        """Ordering is the whole point: tighten first and the migration fails."""
        module = _load_migration()
        order: list[str] = []

        class _Ordered(_OpRecorder):
            def execute(self, statement):
                order.append("execute")
                super().execute(statement)

            def alter_column(self, table, column_name, schema=None, **kwargs):
                order.append("alter")
                super().alter_column(table, column_name, schema=schema, **kwargs)

            def drop_column(self, table, column_name, schema=None):
                order.append("drop")
                super().drop_column(table, column_name, schema=schema)

        monkeypatch.setattr(module, "op", _Ordered())
        module.downgrade()

        assert order.index("execute") < order.index("alter")
        # The per-currency columns must survive until the rebuild has read them.
        assert order.index("alter") < order.index("drop")


class TestPositionCurrency:
    @pytest.mark.parametrize(
        ("instrument_type", "expected"),
        [
            ("equity_kr", "KRW"),
            ("equity_us", "USD"),
            ("crypto", "KRW"),
        ],
    )
    def test_cash_ledger_currency(self, instrument_type, expected):
        assert position_currency(instrument_type) == expected

    def test_reported_currencies_match_the_account_cash_ledgers(self):
        assert REPORTED_CURRENCIES == ("KRW", "USD")


class TestQuoteProvenance:
    def test_parses_provider_timestamp(self):
        assert parse_quote_as_of("2026-08-31T05:00:00Z") == datetime(
            2026, 8, 31, 5, 0, tzinfo=UTC
        )

    def test_naive_timestamp_is_read_as_utc(self):
        assert parse_quote_as_of("2026-08-31T05:00:00") == datetime(
            2026, 8, 31, 5, 0, tzinfo=UTC
        )

    @pytest.mark.parametrize("value", [None, "", "not-a-time", 12345])
    def test_missing_or_unparseable_timestamp_stays_missing(self, value):
        # Substituting "now" here would make an undated quote look fresh.
        assert parse_quote_as_of(value) is None

    def test_stale_rule_uses_only_provider_time_and_observation_time(self):
        now = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
        fresh = PositionQuote(
            price=Decimal("100"),
            source="PAPER_TOSS",
            as_of=now - PAPER_QUOTE_STALE_AFTER + timedelta(seconds=1),
            session="REGULAR",
        )
        stale = PositionQuote(
            price=Decimal("100"),
            source="PAPER_CANDLES",
            as_of=now - PAPER_QUOTE_STALE_AFTER - timedelta(seconds=1),
            session="CLOSED",
        )
        assert fresh.is_stale(now=now) is False
        assert stale.is_stale(now=now) is True
        # Same quote, same moment, same verdict — for every reader.
        assert stale.is_stale(now=now) is stale.is_stale(now=now)

    def test_unknown_timestamp_reports_unknown_not_fresh(self):
        quote = PositionQuote(
            price=Decimal("100"),
            source="UPBIT_TICKER",
            as_of=None,
            session=None,
        )
        assert quote.is_stale(now=datetime.now(UTC)) is None


class TestSnapshotSafety:
    @staticmethod
    def _snapshot(**kw):
        defaults = {
            "equity_krw": None,
            "equity_usd": None,
            "valuation_complete_krw": None,
            "valuation_complete_usd": None,
        }
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_pre_p0_row_is_never_currency_safe(self):
        legacy = self._snapshot()
        for currency in REPORTED_CURRENCIES:
            assert snapshot_is_currency_safe(legacy, currency) is False
            assert snapshot_equity(legacy, currency) is None

    def test_complete_row_is_safe_for_that_currency_only(self):
        row = self._snapshot(
            equity_krw=Decimal("100"),
            equity_usd=Decimal("10"),
            valuation_complete_krw=True,
            valuation_complete_usd=False,
        )
        assert snapshot_is_currency_safe(row, "KRW") is True
        assert snapshot_is_currency_safe(row, "USD") is False
        assert snapshot_equity(row, "KRW") == Decimal("100")

    def test_flagged_complete_without_equity_is_not_safe(self):
        row = self._snapshot(valuation_complete_krw=True)
        assert snapshot_is_currency_safe(row, "KRW") is False

    def test_unknown_currency_is_not_safe(self):
        row = self._snapshot(equity_krw=Decimal("100"), valuation_complete_krw=True)
        assert snapshot_is_currency_safe(row, "USDT") is False
        assert snapshot_equity(row, "USDT") is None


class TestCurrencyValuationEvidence:
    def test_valuation_complete_requires_every_position_priced(self):
        assert CurrencyValuation().valuation_complete is True
        assert (
            CurrencyValuation(positions_count=2, positions_valued=2).valuation_complete
            is True
        )
        assert (
            CurrencyValuation(positions_count=2, positions_valued=1).valuation_complete
            is False
        )

    def test_unreported_currency_is_disclosed_not_folded(self):
        evidence = unsupported_currency_evidence(
            valuations={
                "KRW": CurrencyValuation(positions_count=3, positions_valued=3),
                "USDT": CurrencyValuation(positions_count=2, positions_valued=2),
            },
            trade_counts={"KRW": 5, "USD": 1, "USDT": 4},
        )
        assert evidence == {"USDT": {"positions": 2, "trades": 4}}

    def test_no_unreported_currency_yields_empty_evidence(self):
        assert (
            unsupported_currency_evidence(
                valuations={"KRW": CurrencyValuation(positions_count=1)},
                trade_counts={"KRW": 1, "USD": 0},
            )
            == {}
        )


class TestPositionWireProvenance:
    @staticmethod
    def _position(**overrides) -> Position:
        fields = {
            "broker": "PAPER",
            "account_id": "PAPER-1",
            "market": "KRX",
            "symbol": "005930",
            "currency": "KRW",
            "quantity": "10",
            "average_price": "60000",
            "updated_at": "2026-08-31T06:00:00Z",
        }
        fields.update(overrides)
        return Position(**fields)

    def test_provenance_fields_use_the_android_camel_case_contract(self):
        wire = self._position(
            current_price="70000",
            market_value="700000",
            quote_source="PAPER_TOSS",
            quote_as_of="2026-08-31T05:59:00Z",
            quote_session="REGULAR",
            quote_is_stale=False,
        ).model_dump(by_alias=True)

        assert wire["quoteSource"] == "PAPER_TOSS"
        assert wire["quoteAsOf"] == "2026-08-31T05:59:00Z"
        assert wire["quoteSession"] == "REGULAR"
        assert wire["quoteIsStale"] is False
        assert wire["valuationError"] is None

    def test_provenance_is_optional_so_existing_clients_keep_working(self):
        wire = self._position().model_dump(by_alias=True)
        for key in (
            "quoteSource",
            "quoteAsOf",
            "quoteSession",
            "quoteIsStale",
            "valuationError",
        ):
            assert wire[key] is None

    def test_unavailable_valuation_carries_a_code_and_no_numbers(self):
        wire = self._position(valuation_error="QUOTE_UNAVAILABLE").model_dump(
            by_alias=True
        )
        assert wire["valuationError"] == "QUOTE_UNAVAILABLE"
        assert wire["currentPrice"] is None
        assert wire["marketValue"] is None
        assert wire["unrealizedPnl"] is None
        assert wire["unrealizedPnlRate"] is None

    def test_adapter_passes_service_provenance_through(self):
        as_of = datetime(2026, 8, 31, 5, 59, 0, tzinfo=UTC)
        provenance = PaperAccountAdapter._quote_provenance(
            {
                "quote_source": "PAPER_CANDLES",
                "quote_as_of": as_of,
                "quote_session": "CLOSED",
                "quote_is_stale": True,
                "valuation_error": None,
            }
        )
        assert provenance == {
            "quote_source": "PAPER_CANDLES",
            "quote_as_of": "2026-08-31T05:59:00Z",
            "quote_session": "CLOSED",
            "quote_is_stale": True,
            "valuation_error": None,
        }

    def test_adapter_leaves_as_of_empty_when_the_provider_gave_none(self):
        provenance = PaperAccountAdapter._quote_provenance(
            {
                "quote_source": "UPBIT_TICKER",
                "quote_as_of": None,
                "quote_session": None,
                "quote_is_stale": None,
                "valuation_error": None,
            }
        )
        # The server clock must never stand in for a provider timestamp.
        assert provenance["quote_as_of"] is None
        assert provenance["quote_is_stale"] is None
        assert provenance["quote_source"] == "UPBIT_TICKER"
