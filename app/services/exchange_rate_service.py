from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Literal, TypedDict, cast

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_KEY = "usd_krw"
_OPEN_ER_API_CACHE_TTL_SECONDS = 300.0
_MIN_TOSS_CACHE_TTL_SECONDS = 1.0
_OPEN_ER_API_SNAPSHOT_CACHE_KEY = "open_er_api_usd_snapshot"
_cache: dict[str, dict[str, object]] = {}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_open_er_api_snapshot_lock: asyncio.Lock | None = None
_open_er_api_snapshot_lock_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True)
class UsdKrwExchangeRateQuote:
    rate: float
    mid_rate: float
    source: Literal["toss", "open_er_api"]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    basis_point: float | None = None
    rate_change_type: str | None = None

    @property
    def default_rate(self) -> float:
        return self.mid_rate


@dataclass(frozen=True)
class OpenErApiUsdSnapshot:
    """Validated USD-base rates from one open.er-api response."""

    usd_krw: Decimal
    jpy_per_usd: Decimal
    eur_per_usd: Decimal
    as_of: datetime | None = None
    # CNY는 응답에서 빠질 수 있다. 없으면 CNY 교차환율만 비우고 USD/JPY/EUR
    # 경로는 그대로 살린다(전체를 실패로 만들지 않는다).
    cny_per_usd: Decimal | None = None

    @property
    def jpy_krw(self) -> Decimal:
        """KRW value of one JPY (not the customary 100-JPY display unit)."""

        return self.usd_krw / self.jpy_per_usd

    @property
    def eur_krw(self) -> Decimal:
        """KRW value of one EUR."""

        return self.usd_krw / self.eur_per_usd

    @property
    def cny_krw(self) -> Decimal | None:
        """KRW value of one CNY, or ``None`` when the source omitted CNY."""

        if self.cny_per_usd is None:
            return None
        try:
            rate = self.usd_krw / self.cny_per_usd
        except DecimalException:
            return None
        return rate if rate.is_finite() and rate > 0 else None


class _ExchangeRatePayload(TypedDict, total=False):
    result: str
    base_code: str
    rates: dict[str, float]
    time_last_update_unix: int


def _parse_decimal_float(value: object) -> float:
    if isinstance(value, float):
        raise TypeError("Toss decimal values must be strings, not float")
    if value is None:
        raise TypeError("Decimal value is required")
    return float(Decimal(str(value)))


def _parse_optional_decimal_float(value: object) -> float | None:
    if value is None:
        return None
    return _parse_decimal_float(value)


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_toss_usd_krw_quote(raw: dict[str, Any]) -> UsdKrwExchangeRateQuote:
    if raw.get("baseCurrency") != "USD" or raw.get("quoteCurrency") != "KRW":
        raise ValueError("Toss exchange-rate response is not USD/KRW")
    return UsdKrwExchangeRateQuote(
        rate=_parse_decimal_float(raw["rate"]),
        mid_rate=_parse_decimal_float(raw["midRate"]),
        source="toss",
        valid_from=_parse_datetime(raw.get("validFrom")),
        valid_until=_parse_datetime(raw.get("validUntil")),
        basis_point=_parse_optional_decimal_float(raw.get("basisPoint")),
        rate_change_type=str(raw["rateChangeType"])
        if raw.get("rateChangeType") is not None
        else None,
    )


def _strict_positive_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a positive decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a positive decimal")
    return parsed


def _parse_open_er_api_usd_snapshot(data: object) -> OpenErApiUsdSnapshot:
    if not isinstance(data, dict):
        raise TypeError("open.er-api response must be an object")
    if data.get("result") != "success":
        raise ValueError("open.er-api response result is not success")
    if data.get("base_code") != "USD":
        raise ValueError("open.er-api response base_code is not USD")

    rates = data.get("rates")
    if not isinstance(rates, dict):
        raise TypeError("open.er-api response rates must be an object")
    usd_krw = _strict_positive_decimal(rates.get("KRW"), field="rates.KRW")
    jpy_per_usd = _strict_positive_decimal(rates.get("JPY"), field="rates.JPY")
    eur_per_usd = _strict_positive_decimal(rates.get("EUR"), field="rates.EUR")
    try:
        cross_rates = (usd_krw / jpy_per_usd, usd_krw / eur_per_usd)
    except DecimalException as exc:
        raise ValueError("open.er-api cross rates are not computable") from exc
    if any(not rate.is_finite() or rate <= 0 for rate in cross_rates):
        raise ValueError("open.er-api cross rates must be positive finite decimals")

    # CNY는 필수가 아니다. 빠졌거나 값이 이상하면 CNY만 비우고 나머지 통화는
    # 그대로 내려준다(교차환율 하나 때문에 스냅샷 전체를 버리지 않는다).
    raw_cny = rates.get("CNY")
    cny_per_usd: Decimal | None = None
    if raw_cny is not None:
        try:
            cny_per_usd = _strict_positive_decimal(raw_cny, field="rates.CNY")
        except ValueError:
            cny_per_usd = None

    raw_as_of = data.get("time_last_update_unix")
    as_of: datetime | None = None
    if raw_as_of is not None:
        if type(raw_as_of) is not int or raw_as_of < 0:
            raise ValueError("time_last_update_unix must be a non-negative integer")
        try:
            as_of = datetime.fromtimestamp(raw_as_of, tz=UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError(
                "time_last_update_unix is outside the supported range"
            ) from exc

    return OpenErApiUsdSnapshot(
        usd_krw=usd_krw,
        jpy_per_usd=jpy_per_usd,
        eur_per_usd=eur_per_usd,
        as_of=as_of,
        cny_per_usd=cny_per_usd,
    )


def _parse_open_er_api_usd_krw_quote(
    data: _ExchangeRatePayload,
) -> UsdKrwExchangeRateQuote:
    rate = float(data["rates"]["KRW"])
    return UsdKrwExchangeRateQuote(
        rate=rate,
        mid_rate=rate,
        source="open_er_api",
    )


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _get_open_er_api_snapshot_lock() -> asyncio.Lock:
    global _open_er_api_snapshot_lock, _open_er_api_snapshot_lock_loop
    loop = asyncio.get_running_loop()
    if (
        _open_er_api_snapshot_lock is None
        or _open_er_api_snapshot_lock_loop is not loop
    ):
        _open_er_api_snapshot_lock = asyncio.Lock()
        _open_er_api_snapshot_lock_loop = loop
    return _open_er_api_snapshot_lock


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _quote_cache_ttl_seconds(quote: UsdKrwExchangeRateQuote) -> float:
    if quote.source == "toss" and quote.valid_until is not None:
        ttl = (quote.valid_until - _now_utc()).total_seconds()
        return max(ttl, _MIN_TOSS_CACHE_TTL_SECONDS)
    return _OPEN_ER_API_CACHE_TTL_SECONDS


def _get_cached_quote(now: float) -> UsdKrwExchangeRateQuote | None:
    cached = _cache.get(_CACHE_KEY)
    if cached and float(cached["expires_at"]) > now:
        quote = cached.get("quote")
        if isinstance(quote, UsdKrwExchangeRateQuote):
            return quote
        rate = cached.get("rate")
        if rate is not None:
            scalar_rate = float(rate)
            return UsdKrwExchangeRateQuote(
                rate=scalar_rate,
                mid_rate=scalar_rate,
                source="open_er_api",
            )
    return None


def _set_cached_quote(quote: UsdKrwExchangeRateQuote, now: float) -> None:
    _cache[_CACHE_KEY] = {
        "quote": quote,
        "rate": quote.default_rate,
        "expires_at": now + _quote_cache_ttl_seconds(quote),
    }


def _get_cached_open_er_api_snapshot(now: float) -> OpenErApiUsdSnapshot | None:
    cached = _cache.get(_OPEN_ER_API_SNAPSHOT_CACHE_KEY)
    if cached and float(cached["expires_at"]) > now:
        snapshot = cached.get("snapshot")
        if isinstance(snapshot, OpenErApiUsdSnapshot):
            return snapshot
    return None


def _set_cached_open_er_api_snapshot(
    snapshot: OpenErApiUsdSnapshot, now: float
) -> None:
    _cache[_OPEN_ER_API_SNAPSHOT_CACHE_KEY] = {
        "snapshot": snapshot,
        "expires_at": now + _OPEN_ER_API_CACHE_TTL_SECONDS,
    }


async def _fetch_toss_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    from app.services.brokers.toss.client import TossReadClient

    client = TossReadClient.from_settings()
    try:
        raw = await client.exchange_rate(base_currency="USD", quote_currency="KRW")
    finally:
        await client.aclose()
    if not isinstance(raw, dict):
        raise TypeError("Toss exchange-rate response must be an object")
    quote = _parse_toss_usd_krw_quote(raw)
    logger.debug(
        "Fetched USD/KRW exchange rate from Toss: rate=%s mid_rate=%s valid_until=%s",
        quote.rate,
        quote.mid_rate,
        quote.valid_until,
    )
    return quote


async def _fetch_open_er_api_usd_snapshot() -> OpenErApiUsdSnapshot:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_EXCHANGE_RATE_URL)
        _ = response.raise_for_status()
        data = response.json()

    return _parse_open_er_api_usd_snapshot(data)


async def get_open_er_api_usd_snapshot() -> OpenErApiUsdSnapshot:
    """Return a cached, validated USD-base snapshot for cross-rate consumers."""

    now = time.monotonic()
    cached_snapshot = _get_cached_open_er_api_snapshot(now)
    if cached_snapshot is not None:
        return cached_snapshot

    async with _get_open_er_api_snapshot_lock():
        now = time.monotonic()
        cached_snapshot = _get_cached_open_er_api_snapshot(now)
        if cached_snapshot is not None:
            return cached_snapshot

        snapshot = await _fetch_open_er_api_usd_snapshot()
        _set_cached_open_er_api_snapshot(snapshot, now)
        return snapshot


async def _fetch_open_er_api_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_EXCHANGE_RATE_URL)
        _ = response.raise_for_status()
        data = cast(_ExchangeRatePayload, response.json())

    quote = _parse_open_er_api_usd_krw_quote(data)
    logger.debug("Fetched USD/KRW exchange rate from open.er-api.com: %s", quote.rate)
    return quote


async def _fetch_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    if bool(getattr(settings, "toss_api_enabled", False)):
        try:
            return await _fetch_toss_usd_krw_quote()
        except Exception as exc:
            logger.warning(
                "Toss USD/KRW exchange-rate fetch failed; falling back to open.er-api.com: %s",
                exc,
            )
    return await _fetch_open_er_api_usd_krw_quote()


async def get_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    now = time.monotonic()
    cached_quote = _get_cached_quote(now)
    if cached_quote is not None:
        return cached_quote

    async with _get_lock():
        now = time.monotonic()
        cached_quote = _get_cached_quote(now)
        if cached_quote is not None:
            return cached_quote

        quote = await _fetch_usd_krw_rate_details()
        _set_cached_quote(quote, now)
        return quote


async def get_usd_krw_rate() -> float:
    quote = await get_usd_krw_rate_details()
    return quote.default_rate


async def get_usd_krw_quote() -> float:
    """Return the default USD/KRW quote for existing scalar consumers."""
    return await get_usd_krw_rate()
