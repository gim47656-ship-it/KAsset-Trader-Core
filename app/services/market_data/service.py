from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.core.async_rate_limiter import RateLimitExceededError
from app.core.timezone import KST
from app.extensions.kasset.api.orderbook_store import get_orderbook_store
from app.extensions.kasset.api.toss_market_data import toss_market_data
from app.services.brokers.upbit.client import fetch_multiple_current_prices
from app.services.brokers.upbit.client import fetch_ohlcv as fetch_upbit_ohlcv
from app.services.daily_candles.read_service import (
    cache_first_kr,
    cache_first_us,
    write_back_kr,
    write_back_us,
)
from app.services.domain_errors import (
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.market_data.constants import (
    KR_BENCHMARK_INDEX_SYMBOLS,
    KR_INTRADAY_OHLCV_PERIODS,
    US_INTRADAY_OHLCV_PERIODS,
    validate_ohlcv_period,
)
from app.services.market_data.contracts import (
    Candle,
    OrderbookLevel,
    OrderbookSnapshot,
    Quote,
)
from app.services.market_data.toss_ohlcv import (
    fetch_daily_toss_frame,
    fetch_kr_index_daily_toss_frame,
    fetch_kr_index_intraday_toss_frame,
    fetch_kr_intraday_toss_frame,
    fetch_resampled_daily_toss_frame,
    fetch_us_intraday_toss_frame,
)
from app.services.upbit_orderbook import fetch_orderbook
from app.services.upbit_symbol_universe_service import UpbitSymbolUniverseLookupError
from app.services.us_symbol_universe_service import (
    USSymbolUniverseLookupError,
    get_us_exchange_by_symbol,
)

logger = logging.getLogger(__name__)


class ProviderUnsupportedError(UpstreamUnavailableError):
    """현재 provider가 지원하지 않는 시장 데이터 계약."""


_MARKET_TIMEZONES = {
    "equity_kr": ZoneInfo("Asia/Seoul"),
    "equity_us": ZoneInfo("America/New_York"),
}


def _normalize_market(market: str) -> str:
    normalized = str(market or "").strip().lower()
    aliases = {
        "kr": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "us": "equity_us",
        "nasdaq": "equity_us",
        "nyse": "equity_us",
        "crypto": "crypto",
        "upbit": "crypto",
    }
    resolved = aliases.get(normalized, normalized)
    if resolved not in {"equity_kr", "equity_us", "crypto"}:
        raise ValidationError(f"Unsupported market: {market}")
    return resolved


def _normalize_symbol(symbol: str, market: str) -> str:
    value = str(symbol or "").strip()
    if not value:
        raise ValidationError("symbol is required")
    if market == "crypto":
        upper = value.upper()
        if upper.startswith(("KRW-", "USDT-")):
            return upper
        return f"KRW-{upper}"
    if market == "equity_kr" and value.isdigit() and len(value) <= 6:
        return value.zfill(6)
    return value.upper()


def _normalize_period(period: str, market: str) -> str:
    return validate_ohlcv_period(period, market, error_type=ValidationError)


def _to_contract_timestamp(value: object, market: str) -> dt.datetime:
    timestamp = pd.Timestamp(value)
    timezone = _MARKET_TIMEZONES.get(market)
    if timezone is not None and timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(timezone).tz_localize(None)
    return timestamp.to_pydatetime()


def _to_candle_rows(
    frame: pd.DataFrame,
    *,
    symbol: str,
    market: str,
    source: str,
    period: str,
) -> list[Candle]:
    if frame.empty:
        return []

    rows: list[Candle] = []
    for _, row in frame.iterrows():
        timestamp_raw = row.get("datetime")
        if timestamp_raw is None:
            date_raw = row.get("date")
            if date_raw is None:
                raise ValidationError("candle row must include datetime or date")
            timestamp_raw = pd.Timestamp(date_raw)
        timestamp = _to_contract_timestamp(timestamp_raw, market)
        value_raw = row.get("value")
        rows.append(
            Candle(
                symbol=symbol,
                market=market,
                source=source,
                period=period,
                timestamp=timestamp,
                open=float(row.get("open") or 0.0),
                high=float(row.get("high") or 0.0),
                low=float(row.get("low") or 0.0),
                close=float(row.get("close") or 0.0),
                volume=float(row.get("volume") or 0.0),
                value=(float(value_raw) if value_raw is not None else None),
            )
        )
    return rows


def _map_error(exc: Exception) -> Exception:
    if isinstance(
        exc,
        (
            ValidationError,
            SymbolNotFoundError,
            RateLimitError,
            UpstreamUnavailableError,
            UpbitSymbolUniverseLookupError,
            USSymbolUniverseLookupError,
        ),
    ):
        return exc
    if isinstance(exc, RateLimitExceededError):
        return RateLimitError(str(exc))
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
        418,
        429,
    }:
        return RateLimitError(str(exc))
    if isinstance(exc, (httpx.HTTPStatusError, httpx.RequestError)):
        return UpstreamUnavailableError(str(exc))
    text = str(exc)
    if "not found" in text.lower() or "no data" in text.lower():
        return SymbolNotFoundError(text)
    return UpstreamUnavailableError(text)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _validate_crypto_orderbook_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValidationError("symbol is required")
    if not value.startswith("KRW-"):
        raise ValueError("crypto orderbook only supports KRW-* symbols")
    return value


def _parse_upbit_orderbook_levels(
    orderbook_units: list[dict[str, Any]],
    *,
    side: str,
) -> list[OrderbookLevel]:
    price_key = f"{side}_price"
    size_key = f"{side}_size"
    levels: list[OrderbookLevel] = []
    for unit in orderbook_units:
        if not isinstance(unit, dict):
            continue
        price = _to_float(unit.get(price_key))
        if price <= 0:
            continue
        levels.append(
            OrderbookLevel(price=price, quantity=_to_float(unit.get(size_key)))
        )
    return levels


def _parse_nh_orderbook_levels(
    levels: object,
) -> list[OrderbookLevel]:
    if not isinstance(levels, list):
        return []
    parsed: list[OrderbookLevel] = []
    for raw in levels:
        if not isinstance(raw, dict):
            continue
        price = _to_float(raw.get("price"))
        quantity = _to_float(raw.get("volume"))
        if price <= 0 or quantity < 0:
            continue
        parsed.append(OrderbookLevel(price=price, quantity=quantity))
    return parsed


def _parse_upbit_orderbook_as_of(
    raw_timestamp: Any,
) -> dt.datetime | None:
    """Upbit provider 시각을 해석하며, 시각이 없으면 그대로 unavailable로 둔다."""
    try:
        timestamp = float(raw_timestamp)
        if timestamp <= 0:
            raise ValueError
        # Upbit documents milliseconds; accepting seconds keeps old fixtures
        # and equivalent broker payloads honest without using annotation time.
        epoch_seconds = timestamp / 1000.0 if timestamp >= 10_000_000_000 else timestamp
        return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC).astimezone(KST)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


async def get_kr_volume_rank() -> list[dict[str, Any]]:
    raise ProviderUnsupportedError(
        "provider_unsupported: KR volume ranking is unavailable from Toss/NH PLUG"
    )


async def get_quote(symbol: str, market: str) -> Quote:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)

    try:
        if resolved_market == "crypto":
            prices = await fetch_multiple_current_prices([resolved_symbol])
            price = prices.get(resolved_symbol)
            if price is None:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
            return Quote(
                symbol=resolved_symbol,
                market=resolved_market,
                price=float(price),
                source="upbit",
            )

        if resolved_market == "equity_us":
            await get_us_exchange_by_symbol(resolved_symbol)

        daily_error: Exception | None = None
        try:
            frame = await fetch_daily_toss_frame(
                symbol=resolved_symbol,
                count=2,
            )
        except Exception as exc:
            daily_error = exc
            frame = pd.DataFrame()
        points = await toss_market_data.prices([resolved_symbol])
        point = points.get(resolved_symbol)
        last = frame.iloc[-1] if not frame.empty else None
        fallback_close = _to_float(last.get("close")) if last is not None else 0.0
        price = float(point.price) if point is not None else fallback_close
        if price <= 0:
            if daily_error is not None:
                raise daily_error
            raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")

        previous_close: float | None = None
        if len(frame) >= 2:
            raw_previous = frame.iloc[-2].get("close")
            if raw_previous not in (None, "") and not pd.isna(raw_previous):
                previous_close = float(raw_previous)

        return Quote(
            symbol=resolved_symbol,
            market=resolved_market,
            price=price,
            source="toss",
            previous_close=previous_close,
            open=(
                float(last["open"])
                if last is not None and last.get("open") is not None
                else None
            ),
            high=(
                float(last["high"])
                if last is not None and last.get("high") is not None
                else None
            ),
            low=(
                float(last["low"])
                if last is not None and last.get("low") is not None
                else None
            ),
            volume=(
                int(float(last["volume"]))
                if last is not None and last.get("volume") is not None
                else None
            ),
            value=(
                float(last["value"])
                if last is not None and last.get("value") is not None
                else None
            ),
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_orderbook(
    symbol: str, market: str = "kr", venue: str | None = None
) -> OrderbookSnapshot:
    resolved_market = _normalize_market(market)
    if resolved_market == "crypto":
        if venue is not None and venue.strip():
            raise ValueError("venue is only supported for KR equity orderbook")
        resolved_symbol = _validate_crypto_orderbook_symbol(symbol)
        try:
            raw = await fetch_orderbook(resolved_symbol)
            if not raw:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")

            total_ask_qty = _to_float(raw.get("total_ask_size"))
            total_bid_qty = _to_float(raw.get("total_bid_size"))
            as_of = _parse_upbit_orderbook_as_of(raw.get("timestamp"))
            return OrderbookSnapshot(
                symbol=resolved_symbol,
                instrument_type="crypto",
                source="upbit",
                asks=_parse_upbit_orderbook_levels(
                    raw.get("orderbook_units", []),
                    side="ask",
                ),
                bids=_parse_upbit_orderbook_levels(
                    raw.get("orderbook_units", []),
                    side="bid",
                ),
                total_ask_qty=total_ask_qty,
                total_bid_qty=total_bid_qty,
                bid_ask_ratio=(
                    round(total_bid_qty / total_ask_qty, 2)
                    if total_ask_qty > 0
                    else None
                ),
                as_of=as_of,
                price_as_of_source="broker" if as_of is not None else None,
            )
        except Exception as exc:
            raise _map_error(exc) from exc

    if resolved_market != "equity_kr":
        raise ValueError("get_orderbook only supports KR equity and KRW crypto markets")

    requested_venue = str(venue or "krx").strip().lower()
    if requested_venue in {
        "nxt",
        "ntx",
        "nx",
        "afterhours",
        "extended",
        "unified",
        "combined",
        "integrated",
        "all",
        "un",
        "통합",
        "통합시장",
    }:
        raise ProviderUnsupportedError(
            "provider_unsupported: NH PLUG orderbook supports KRX only"
        )
    if requested_venue not in {"krx", "regular", "j"}:
        raise ValueError(f"unsupported KR orderbook venue: {venue!r}")

    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    try:
        raw = await get_orderbook_store().get_snapshot(
            market="KRX",
            symbol=resolved_symbol,
        )
        asks = _parse_nh_orderbook_levels(raw.get("asks"))
        bids = _parse_nh_orderbook_levels(raw.get("bids"))
        total_ask_qty = _to_float(raw.get("totalAskVolume"))
        total_bid_qty = _to_float(raw.get("totalBidVolume"))
        # NH store의 ``asOf``는 수신 시각이므로 provider 시각 증거로 전달하지 않는다.
        is_empty_book = not bool(raw.get("ready")) or not asks or not bids
        return OrderbookSnapshot(
            symbol=resolved_symbol,
            instrument_type="equity_kr",
            source="nhplug",
            asks=asks,
            bids=bids,
            total_ask_qty=total_ask_qty,
            total_bid_qty=total_bid_qty,
            bid_ask_ratio=(
                round(total_bid_qty / total_ask_qty, 2) if total_ask_qty > 0 else None
            ),
            venue="krx",
            venue_label="KRX",
            is_empty_book=is_empty_book,
            requires_final_recheck=is_empty_book,
            empty_reason="empty_nh_orderbook" if is_empty_book else None,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_short_interest(symbol: str, days: int = 20) -> dict[str, object]:
    _normalize_symbol(symbol, "equity_kr")
    _ = days
    raise ProviderUnsupportedError(
        "provider_unsupported: short-interest data is unavailable from Toss/NH PLUG"
    )


async def get_ohlcv(
    symbol: str,
    market: str,
    period: str,
    count: int,
    end: dt.datetime | None = None,
) -> list[Candle]:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    resolved_period = _normalize_period(period, resolved_market)
    if count <= 0:
        raise ValidationError("count must be > 0")

    try:
        if resolved_market == "crypto":
            frame = await fetch_upbit_ohlcv(
                market=resolved_symbol,
                days=min(count, 200),
                period=resolved_period,
                end_date=end,
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="upbit",
                period=resolved_period,
            )

        capped_count = min(count, 200)
        if resolved_market == "equity_us":
            exchange = await get_us_exchange_by_symbol(resolved_symbol)
            if resolved_period in US_INTRADAY_OHLCV_PERIODS:
                frame = await fetch_us_intraday_toss_frame(
                    symbol=resolved_symbol,
                    period=resolved_period,
                    count=capped_count,
                    end_date=end,
                )
            elif resolved_period == "day":
                frame = await cache_first_us(resolved_symbol, capped_count, end)
                if frame is not None and not frame.empty:
                    return _to_candle_rows(
                        frame,
                        symbol=resolved_symbol,
                        market=resolved_market,
                        source="db",
                        period=resolved_period,
                    )
                frame = await fetch_daily_toss_frame(
                    symbol=resolved_symbol,
                    count=capped_count,
                    end_date=end,
                )
                await write_back_us(
                    frame,
                    symbol=resolved_symbol,
                    partition=exchange,
                    source="toss",
                )
            else:
                frame = await fetch_resampled_daily_toss_frame(
                    symbol=resolved_symbol,
                    period=resolved_period,
                    count=capped_count,
                    end_date=end,
                )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="toss",
                period=resolved_period,
            )

        if resolved_symbol in KR_BENCHMARK_INDEX_SYMBOLS:
            if resolved_period in KR_INTRADAY_OHLCV_PERIODS:
                frame = await fetch_kr_index_intraday_toss_frame(
                    symbol=resolved_symbol,
                    period=resolved_period,
                    count=capped_count,
                    end_date=end,
                )
            else:
                frame = await fetch_kr_index_daily_toss_frame(
                    symbol=resolved_symbol,
                    period=resolved_period,
                    count=capped_count,
                    end_date=end,
                )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="toss",
                period=resolved_period,
            )

        if resolved_period in KR_INTRADAY_OHLCV_PERIODS:
            frame = await fetch_kr_intraday_toss_frame(
                symbol=resolved_symbol,
                period=resolved_period,
                count=capped_count,
                end_date=end,
            )
        elif resolved_period == "day":
            frame = await cache_first_kr(resolved_symbol, capped_count, end)
            if frame is not None and not frame.empty:
                return _to_candle_rows(
                    frame,
                    symbol=resolved_symbol,
                    market=resolved_market,
                    source="db",
                    period=resolved_period,
                )
            frame = await fetch_daily_toss_frame(
                symbol=resolved_symbol,
                count=capped_count,
                end_date=end,
            )
            await write_back_kr(frame, symbol=resolved_symbol, source="toss")
        else:
            frame = await fetch_resampled_daily_toss_frame(
                symbol=resolved_symbol,
                period=resolved_period,
                count=capped_count,
                end_date=end,
            )
        return _to_candle_rows(
            frame,
            symbol=resolved_symbol,
            market=resolved_market,
            source="toss",
            period=resolved_period,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


__all__ = [
    "get_quote",
    "get_orderbook",
    "get_short_interest",
    "get_ohlcv",
    "get_kr_volume_rank",
    "ProviderUnsupportedError",
    "Quote",
    "Candle",
    "OrderbookLevel",
    "OrderbookSnapshot",
]
