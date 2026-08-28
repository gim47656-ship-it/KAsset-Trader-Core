from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import exchange_rate_service as mod


@pytest.fixture(autouse=True)
def clear_exchange_rate_cache() -> None:
    mod._cache.clear()


def test_parse_toss_usd_krw_quote_uses_mid_rate_as_default() -> None:
    quote = mod._parse_toss_usd_krw_quote(
        {
            "baseCurrency": "USD",
            "quoteCurrency": "KRW",
            "rate": "1522.2",
            "midRate": "1522.05",
            "basisPoint": "15.2",
            "rateChangeType": "UP",
            "validFrom": "2026-06-12T09:30:00+09:00",
            "validUntil": "2026-06-12T09:31:00+09:00",
        }
    )

    assert quote.source == "toss"
    assert quote.rate == pytest.approx(1522.2)
    assert quote.mid_rate == pytest.approx(1522.05)
    assert quote.default_rate == pytest.approx(1522.05)
    assert quote.basis_point == pytest.approx(15.2)
    assert quote.rate_change_type == "UP"
    assert quote.valid_from == datetime(2026, 6, 12, 0, 30, tzinfo=UTC)
    assert quote.valid_until == datetime(2026, 6, 12, 0, 31, tzinfo=UTC)


def test_parse_open_er_api_quote_exposes_same_rate_and_mid_rate() -> None:
    quote = mod._parse_open_er_api_usd_krw_quote({"rates": {"KRW": 1498.7}})

    assert quote.source == "open_er_api"
    assert quote.rate == pytest.approx(1498.7)
    assert quote.mid_rate == pytest.approx(1498.7)
    assert quote.default_rate == pytest.approx(1498.7)
    assert quote.valid_from is None
    assert quote.valid_until is None


def test_open_er_api_snapshot_validates_cross_rates_and_source_time() -> None:
    as_of = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)

    snapshot = mod._parse_open_er_api_usd_snapshot(
        {
            "result": "success",
            "base_code": "USD",
            "time_last_update_unix": int(as_of.timestamp()),
            "rates": {
                "KRW": "1500.00",
                "JPY": "150",
                "EUR": "0.75",
            },
        }
    )

    assert snapshot.usd_krw == Decimal("1500.00")
    assert snapshot.jpy_krw == Decimal("10.00")
    assert snapshot.eur_krw == Decimal("2000")
    assert snapshot.as_of == as_of


def test_parse_open_er_api_usd_snapshot_keeps_missing_source_time_null() -> None:
    snapshot = mod._parse_open_er_api_usd_snapshot(
        {
            "result": "success",
            "base_code": "USD",
            "rates": {
                "KRW": "1500",
                "JPY": "150",
                "EUR": "0.75",
            },
        }
    )

    assert snapshot.as_of is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "result": "error",
                "base_code": "USD",
                "rates": {"KRW": "1500", "JPY": "150", "EUR": "0.75"},
            },
            "result is not success",
        ),
        (
            {
                "result": "success",
                "base_code": "EUR",
                "rates": {"KRW": "1500", "JPY": "150", "EUR": "0.75"},
            },
            "base_code is not USD",
        ),
        (
            {
                "result": "success",
                "base_code": "USD",
                "rates": {"KRW": "1500", "JPY": "0", "EUR": "0.75"},
            },
            "rates.JPY must be a positive decimal",
        ),
        (
            {
                "result": "success",
                "base_code": "USD",
                "rates": {"KRW": "NaN", "JPY": "150", "EUR": "0.75"},
            },
            "rates.KRW must be a positive decimal",
        ),
    ],
)
def test_parse_open_er_api_usd_snapshot_rejects_invalid_base_or_cross_rates(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        mod._parse_open_er_api_usd_snapshot(payload)


@pytest.mark.asyncio
async def test_open_er_api_snapshot_cache_is_single_flight(monkeypatch) -> None:
    calls = 0
    expected = mod.OpenErApiUsdSnapshot(
        usd_krw=Decimal("1500"),
        jpy_per_usd=Decimal("150"),
        eur_per_usd=Decimal("0.75"),
    )

    async def fetch() -> mod.OpenErApiUsdSnapshot:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return expected

    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_snapshot", fetch)

    results = await asyncio.gather(
        *(mod.get_open_er_api_usd_snapshot() for _ in range(5))
    )

    assert calls == 1
    assert all(result is expected for result in results)


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_toss_when_enabled(monkeypatch) -> None:
    toss_quote = mod.UsdKrwExchangeRateQuote(
        rate=1522.2,
        mid_rate=1522.05,
        source="toss",
        valid_until=datetime(2026, 6, 12, 0, 31, tzinfo=UTC),
    )
    fallback_called = False

    async def fake_toss() -> mod.UsdKrwExchangeRateQuote:
        return toss_quote

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        nonlocal fallback_called
        fallback_called = True
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fake_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote is toss_quote
    assert fallback_called is False


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_fallback_when_toss_disabled(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise AssertionError("Toss should not be called when disabled")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", False)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_falls_back_when_toss_fails(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise RuntimeError("Toss is unavailable")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)


@pytest.mark.asyncio
async def test_cache_uses_toss_valid_until(monkeypatch) -> None:
    calls = 0
    now_utc = datetime(2026, 6, 12, 0, 30, 0, tzinfo=UTC)
    monotonic_now = 1000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1522.2 + calls,
            mid_rate=1522.05 + calls,
            source="toss",
            valid_until=datetime(2026, 6, 12, 0, 31, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(mod, "_now_utc", lambda: now_utc)
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    second = await mod.get_usd_krw_rate_details()

    assert first is second
    assert calls == 1

    monotonic_now = 1059.9
    third = await mod.get_usd_krw_rate_details()

    assert third is first
    assert calls == 1

    monotonic_now = 1060.1
    fourth = await mod.get_usd_krw_rate_details()

    assert fourth is not first
    assert fourth.mid_rate == pytest.approx(1524.05)
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_uses_fixed_ttl_for_open_er_api(monkeypatch) -> None:
    calls = 0
    monotonic_now = 2000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7 + calls,
            mid_rate=1498.7 + calls,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    monotonic_now = 2299.9
    second = await mod.get_usd_krw_rate_details()

    assert second is first
    assert calls == 1

    monotonic_now = 2300.1
    third = await mod.get_usd_krw_rate_details()

    assert third is not first
    assert calls == 2


@pytest.mark.asyncio
async def test_scalar_helpers_return_mid_rate_default(monkeypatch) -> None:
    async def fake_details() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1522.2,
            mid_rate=1522.05,
            source="toss",
            valid_until=datetime(2026, 6, 12, 0, 31, tzinfo=UTC),
        )

    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_details)

    rate = await mod.get_usd_krw_rate()
    quote = await mod.get_usd_krw_quote()
    details = await mod.get_usd_krw_rate_details()

    assert rate == pytest.approx(1522.05)
    assert quote == pytest.approx(1522.05)
    assert details.rate == pytest.approx(1522.2)
    assert details.mid_rate == pytest.approx(1522.05)
