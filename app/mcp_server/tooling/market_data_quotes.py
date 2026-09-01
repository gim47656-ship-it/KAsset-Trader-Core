"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot,
ADX, Stochastic RSI, OBV, Fibonacci).
"""

from __future__ import annotations

import datetime
import logging
from statistics import median
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pandas as pd

import app.services.brokers.upbit.client as upbit_service
import app.services.market_data as market_data_service
from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.extensions.kasset.api.toss_market_data import toss_market_data
from app.mcp_server.tooling.market_data_indicators import (
    IndicatorType,
    _compute_crypto_realtime_rsi_from_frame,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_STALE,
    US_SESSION_CLOSED,
    kr_market_data_state,
    us_market_session,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    error_payload_from_exception as _error_payload_from_exception,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    normalize_rows as _normalize_rows,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services.kr_symbol_universe_service import (
    get_kr_nxt_tradability,
    search_kr_symbols,
)
from app.services.market_data.constants import (
    CRYPTO_MINUTE_OHLCV_PERIODS,
    CRYPTO_MINUTE_PUBLIC_ROW_KEYS,
    CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS,
    KR_INTRADAY_OHLCV_PERIODS,
    ORDERBOOK_ASOF_MAX_AGE_S148_N5,
    US_INTRADAY_OHLCV_PERIODS,
    validate_ohlcv_period,
)
from app.services.market_data.toss_ohlcv import (
    fetch_daily_toss_frame,
    fetch_kr_intraday_toss_frame,
    fetch_resampled_daily_toss_frame,
    fetch_us_intraday_toss_frame,
)
from app.services.symbol_analysis.freshness import compute_is_stale
from app.services.upbit_symbol_universe_service import search_upbit_symbols
from app.services.us_symbol_universe_service import (
    get_us_exchange_by_symbol,
    search_us_symbols,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


logger = logging.getLogger(__name__)


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


_OHLCV_INDICATOR_ROW_KEYS = (
    "rsi_14",
    "ema_20",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "vwap",
)

_NON_INTRADAY_PERIODS = {"day", "week", "month"}


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def _build_rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _build_indicator_rows(df: pd.DataFrame, period: str) -> list[dict[str, Any]]:
    close = _numeric_series(df, "close")
    high = _numeric_series(df, "high")
    low = _numeric_series(df, "low")
    volume = _numeric_series(df, "volume")

    ema_20 = close.ewm(span=20, adjust=False).mean()
    rsi_14 = _build_rsi_series(close).round(2)
    bb_mid = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std()
    bb_upper = bb_mid + (bb_std * 2.0)
    bb_lower = bb_mid - (bb_std * 2.0)

    indicator_frame = pd.DataFrame(
        {
            "rsi_14": rsi_14,
            "ema_20": ema_20,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
        },
        index=df.index,
    )

    if period in _NON_INTRADAY_PERIODS:
        indicator_frame["vwap"] = None
    else:
        typical_price = (high + low + close) / 3.0
        cumulative_volume = volume.cumsum()
        weighted_total = (typical_price * volume).cumsum()
        indicator_frame["vwap"] = weighted_total / cumulative_volume.where(
            cumulative_volume != 0
        )

    return _normalize_rows(indicator_frame.loc[:, list(_OHLCV_INDICATOR_ROW_KEYS)])


def _normalize_ohlcv_rows(
    df: pd.DataFrame,
    *,
    period: str,
    include_indicators: bool,
) -> list[dict[str, Any]]:
    frame = df
    if include_indicators and not df.empty:
        frame = _enrich_ohlcv_with_indicators(df, period)
    return _normalize_rows(frame)


def _normalize_kr_intraday_payload_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "datetime" not in df.columns:
        return df

    frame = df.copy()
    frame["datetime"] = frame["datetime"].map(
        lambda value: (
            value
            if pd.isna(value)
            else pd.Timestamp(value).tz_convert("Asia/Seoul").tz_localize(None)
            if pd.Timestamp(value).tzinfo is not None
            else pd.Timestamp(value)
        )
    )
    return frame


def _build_ohlcv_payload(
    *,
    symbol: str,
    instrument_type: str,
    source: str,
    period: str,
    count: int,
    df: pd.DataFrame,
    include_indicators: bool,
    message: str | None = None,
) -> dict[str, Any]:
    normalized_df = df
    if instrument_type == "equity_kr" and period in KR_INTRADAY_OHLCV_PERIODS:
        normalized_df = _normalize_kr_intraday_payload_frame(df)

    payload: dict[str, Any] = {
        "symbol": symbol,
        "instrument_type": instrument_type,
        "source": source,
        "period": period,
        "count": count,
        "rows": _normalize_ohlcv_rows(
            normalized_df,
            period=period,
            include_indicators=include_indicators,
        ),
    }
    if include_indicators:
        payload["indicators_included"] = True
    if message is not None:
        payload["message"] = message
    return payload


def _classify_orderbook_pressure(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio > 2.0:
        return "strong_buy"
    if ratio > 1.3:
        return "buy"
    if ratio >= 0.7:
        return "neutral"
    if ratio >= 0.5:
        return "sell"
    return "strong_sell"


def _build_orderbook_pressure_desc(
    *,
    pressure: str | None,
    total_ask_qty: float,
    total_bid_qty: float,
) -> str | None:
    if pressure is None:
        return None
    if pressure == "neutral":
        return "매수/매도 잔량이 균형권 - 중립"

    if pressure in {"strong_buy", "buy"}:
        if total_ask_qty <= 0:
            return None
        multiplier = total_bid_qty / total_ask_qty
        suffix = "강한 매수 압력" if pressure == "strong_buy" else "매수 압력"
        return f"매수잔량이 매도잔량의 {multiplier:.1f}배 - {suffix}"

    if total_bid_qty <= 0:
        return None
    multiplier = total_ask_qty / total_bid_qty
    suffix = "강한 매도 압력" if pressure == "strong_sell" else "매도 압력"
    return f"매도잔량이 매수잔량의 {multiplier:.1f}배 - {suffix}"


def _calculate_orderbook_spread(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[float | None, float | None]:
    if not snapshot.asks or not snapshot.bids:
        return None, None

    best_ask = snapshot.asks[0].price
    best_bid = snapshot.bids[0].price
    if best_bid <= 0:
        return None, None

    spread = best_ask - best_bid

    spread_pct = round((spread / best_bid) * 100, 3)
    return spread, spread_pct


def _validate_crypto_orderbook_symbol_input(symbol: str | int) -> str:
    value = str(symbol).strip().upper()
    if not value:
        raise ValueError("symbol is required")
    if not value.startswith("KRW-"):
        raise ValueError("crypto orderbook only supports KRW-* symbols")
    return value


_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _current_kst_datetime(now: datetime.datetime | None = None) -> datetime.datetime:
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=_KST)
    return current.astimezone(_KST)


def _build_orderbook_walls_for_side(
    levels: list[market_data_service.OrderbookLevel],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    values: list[int] = []

    for level in levels:
        value_krw = int(round(level.price * level.quantity))
        if value_krw <= 0:
            continue
        values.append(value_krw)
        candidates.append(
            {
                "price": level.price,
                "size": level.quantity,
                "value_krw": value_krw,
            }
        )

    if not values:
        return []

    baseline = median(values)
    if baseline <= 0:
        return []

    walls = [entry for entry in candidates if entry["value_krw"] >= baseline * 2]
    walls.sort(key=lambda entry: entry["value_krw"], reverse=True)
    return walls[:3]


def _build_orderbook_walls(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if snapshot.instrument_type != "crypto":
        return [], []
    return (
        _build_orderbook_walls_for_side(snapshot.bids),
        _build_orderbook_walls_for_side(snapshot.asks),
    )


def _build_orderbook_payload(
    snapshot: market_data_service.OrderbookSnapshot,
) -> dict[str, Any]:
    """시장별 호가 응답과 검증 가능한 freshness 필드를 만든다.

    Upbit 호가는 provider 시각이 있어 N=5분 freshness 경계를 적용한다.
    NH PLUG mock store의 ``asOf``는 transport 수신 시각이므로 shared
    snapshot에 전달하지 않으며, KR 응답에는 ``as_of``와
    ``price_as_of_source``를 싣지 않는다.
    """
    pressure = _classify_orderbook_pressure(snapshot.bid_ask_ratio)
    spread, spread_pct = _calculate_orderbook_spread(snapshot)
    bid_walls, ask_walls = _build_orderbook_walls(snapshot)
    payload: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "instrument_type": snapshot.instrument_type,
        "source": snapshot.source,
        "asks": [
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.asks
        ],
        "bids": [
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.bids
        ],
        "total_ask_qty": snapshot.total_ask_qty,
        "total_bid_qty": snapshot.total_bid_qty,
        "bid_ask_ratio": snapshot.bid_ask_ratio,
        "pressure": pressure,
        "pressure_desc": _build_orderbook_pressure_desc(
            pressure=pressure,
            total_ask_qty=snapshot.total_ask_qty,
            total_bid_qty=snapshot.total_bid_qty,
        ),
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
    }
    if snapshot.as_of is not None:
        if snapshot.price_as_of_source is not None:
            payload["price_as_of_source"] = snapshot.price_as_of_source
        payload["as_of"] = snapshot.as_of.isoformat()
    if snapshot.instrument_type == "crypto":
        _annotate_orderbook_price_freshness(
            payload,
            snapshot.as_of,
            require_trading_date=False,
        )
    if snapshot.venue is not None:
        payload["venue"] = snapshot.venue
    if snapshot.venue_label is not None:
        payload["venue_label"] = snapshot.venue_label
    if snapshot.is_empty_book is not None:
        payload["is_empty_book"] = snapshot.is_empty_book
    if snapshot.requires_final_recheck is not None:
        payload["requires_final_recheck"] = snapshot.requires_final_recheck
    if snapshot.empty_reason is not None:
        payload["empty_reason"] = snapshot.empty_reason
    return payload


# ---------------------------------------------------------------------------
# Symbol Search
# ---------------------------------------------------------------------------


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """Search symbols across KRX, US, and Upbit master datasets."""
    results: list[dict[str, Any]] = []

    if instrument_type is None or instrument_type == "equity_kr":
        kr_results = await search_kr_symbols(query, limit)
        results.extend(kr_results)
        if len(results) >= limit:
            return results

    if instrument_type is None or instrument_type == "equity_us":
        remaining = limit - len(results)
        if remaining > 0:
            us_results = await search_us_symbols(query, remaining)
            results.extend(us_results)
            if len(results) >= limit:
                return results

    if instrument_type is None or instrument_type == "crypto":
        remaining = limit - len(results)
        if remaining > 0:
            crypto_results = await search_upbit_symbols(query, remaining)
            results.extend(crypto_results)
            if len(results) >= limit:
                return results

    return results


# ---------------------------------------------------------------------------
# Quote Fetching
# ---------------------------------------------------------------------------


def _parse_price_as_of(value: Any) -> datetime.datetime | None:
    """Parse a provider timestamp without turning integer indexes into epoch data.

    ROB-1121: Reject timezone-naive datetime objects at the freshness boundary.
    Provider timestamps must be timezone-aware (or constructed as KST-aware).
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp) or timestamp.value <= 0:
        return None
    dt = timestamp.to_pydatetime()
    if dt.tzinfo is None:
        return None
    return dt.astimezone(_KST)


def _price_as_of_from_frame(
    df: pd.DataFrame,
    *,
    timezone: ZoneInfo = _KST,
) -> datetime.datetime | None:
    """실제 캔들 시각을 읽는다. RangeIndex 값은 시각으로 취급하지 않는다."""
    if df.empty:
        return None
    val = None
    for column in ("datetime", "date"):
        row_value = df.iloc[-1].get(column)
        if row_value is not None and not pd.isna(row_value):
            val = row_value
            break
    if val is None and isinstance(df.index, pd.DatetimeIndex):
        val = df.index[-1]
    if val is None:
        return None
    try:
        timestamp = pd.Timestamp(val)
        if pd.isna(timestamp) or timestamp.value <= 0:
            return None
        parsed = timestamp.to_pydatetime()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.astimezone(timezone)
    except (TypeError, ValueError, OverflowError):
        return None


def _annotate_kr_price_freshness(
    quote: dict[str, Any],
    as_of: Any,
    *,
    trading_date: datetime.date | None = None,
) -> None:
    """KR 시세를 현재가로 안전하게 사용할 수 있는지 표시한다."""
    parsed = _parse_price_as_of(as_of)
    quote["price_as_of"] = parsed.isoformat() if parsed is not None else None
    if parsed is None:
        quote.update(
            {
                "is_stale_price": True,
                "price_freshness": "unavailable",
                "price_usable": False,
                "price_unavailable_reason": "missing_price_asof",
            }
        )
        return

    stale = compute_is_stale(
        "price",
        parsed,
        trading_date=trading_date or now_kst().date(),
    )
    quote["is_stale_price"] = stale
    quote["price_freshness"] = "stale" if stale else "fresh"
    quote["price_usable"] = not stale
    if stale:
        quote["price_unavailable_reason"] = "stale_price_asof"
    else:
        quote.pop("price_unavailable_reason", None)


def _annotate_orderbook_price_freshness(
    quote: dict[str, Any],
    as_of: Any,
    *,
    trading_date: datetime.date | None = None,
    require_trading_date: bool,
    now: datetime.datetime | None = None,
) -> None:
    """호가 전용 N=5분 신선도 경계를 적용한다.

    Upbit에는 거래일 경계가 없지만 제한된 wall-clock 과거/미래 판정은
    적용한다. 다른 ``compute_is_stale`` 호출 계약과 섞이지 않도록 별도
    helper로 유지한다.
    """
    parsed = _parse_price_as_of(as_of)
    quote["price_as_of"] = parsed.isoformat() if parsed is not None else None
    if parsed is None:
        quote.update(
            {
                "is_stale_price": True,
                "price_freshness": "unavailable",
                "price_usable": False,
                "price_unavailable_reason": "missing_price_asof",
            }
        )
        return

    current = _current_kst_datetime(now)
    date_stale = require_trading_date and compute_is_stale(
        "price", parsed, trading_date=trading_date or current.date()
    )
    time_stale = parsed > current or current - parsed > ORDERBOOK_ASOF_MAX_AGE_S148_N5
    stale = date_stale or time_stale
    quote["is_stale_price"] = stale
    quote["price_freshness"] = "stale" if stale else "fresh"
    quote["price_usable"] = not stale
    if stale:
        quote["price_unavailable_reason"] = "stale_price_asof"
    else:
        quote.pop("price_unavailable_reason", None)


async def _fetch_quote_crypto(symbol: str) -> dict[str, Any]:
    """Fetch crypto quote from Upbit."""
    prices = await upbit_service.fetch_multiple_current_prices([symbol])
    price = prices.get(symbol)
    if price is None:
        raise ValueError(f"Symbol '{symbol}' not found")
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "price": price,
        "source": "upbit",
    }


async def _fetch_toss_equity_quote(
    symbol: str,
    *,
    instrument_type: str,
    exchange: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if instrument_type == "equity_us" and exchange is None:
        exchange = await get_us_exchange_by_symbol(to_db_symbol(normalized_symbol))

    points = await toss_market_data.prices([normalized_symbol])
    point = points.get(normalized_symbol)
    daily_error: Exception | None = None
    try:
        frame = await fetch_daily_toss_frame(
            symbol=normalized_symbol,
            count=2,
        )
    except Exception as exc:
        daily_error = exc
        frame = pd.DataFrame()

    last = frame.iloc[-1].to_dict() if not frame.empty else {}
    daily_close = _to_float_or_none(last.get("close"))
    price = float(point.price) if point is not None else daily_close
    if price is None or price <= 0:
        if daily_error is not None:
            raise RuntimeError(
                f"Toss quote unavailable for '{normalized_symbol}': {daily_error}"
            ) from daily_error
        raise ValueError(f"Symbol '{normalized_symbol}' not found")

    previous_close: float | None = None
    if len(frame) >= 2:
        previous_close = _to_float_or_none(frame.iloc[-2].get("close"))
    price_as_of: datetime.datetime | None = None
    if point is not None:
        price_as_of = point.as_of
    elif not frame.empty:
        price_as_of = _price_as_of_from_frame(
            frame,
            timezone=_ET if instrument_type == "equity_us" else _KST,
        )

    quote: dict[str, Any] = {
        "symbol": normalized_symbol,
        "instrument_type": instrument_type,
        "price": price,
        "previous_close": previous_close,
        "open": _to_float_or_none(last.get("open")),
        "high": _to_float_or_none(last.get("high")),
        "low": _to_float_or_none(last.get("low")),
        "volume": _to_int_or_none(last.get("volume")),
        "value": _to_float_or_none(last.get("value")),
        "source": "toss",
        "price_source": "toss_price" if point is not None else "toss_daily_close",
        "price_as_of": price_as_of.isoformat() if price_as_of is not None else None,
    }
    if exchange is not None:
        quote["venue"] = exchange
        quote["delayed"] = True
    return quote


async def _fetch_quote_equity_kr(symbol: str) -> dict[str, Any]:
    quote = await _fetch_toss_equity_quote(
        symbol,
        instrument_type="equity_kr",
    )
    _annotate_kr_price_freshness(quote, quote.get("price_as_of"))
    return quote


async def _fetch_kr_live_quote(symbol: str) -> dict[str, Any] | None:
    """analyze 전용 Toss 현재가. 공급자 시각이 없으면 사용할 수 없다."""
    try:
        points = await toss_market_data.prices([symbol])
    except Exception:
        return None
    point = points.get(symbol)
    if point is None or point.price <= 0:
        return None
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": float(point.price),
        "source": "toss",
        "price_source": "toss_price",
        "price_as_of": point.as_of.isoformat(),
        "fetched_at": now_kst().isoformat(),
    }


_US_MARKET_CLOSED_REASON = "us_market_closed"
_US_DAILY_CLOSE_FALLBACK_REASON = "toss_daily_close_fallback"
_US_MISSING_PRICE_ASOF_REASON = "missing_price_asof"
_US_STALE_PRICE_ASOF_REASON = "stale_price_asof"


def _tag_us_quote_session(
    quote: dict[str, Any],
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.datetime.now(datetime.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.UTC)
    session = us_market_session(current)
    quote["session"] = session
    price_as_of = _parse_price_as_of(quote.get("price_as_of"))
    if session == US_SESSION_CLOSED:
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_MARKET_CLOSED_REASON
    elif price_as_of is None:
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_MISSING_PRICE_ASOF_REASON
    elif quote.get("price_source") != "toss_price":
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_DAILY_CLOSE_FALLBACK_REASON
    elif price_as_of.astimezone(_ET).date() != current.astimezone(_ET).date():
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_STALE_PRICE_ASOF_REASON
    else:
        quote["data_state"] = DATA_STATE_FRESH
        quote.pop("data_state_reason", None)
    quote.setdefault("price_source", "toss_price")
    return quote


async def _fetch_quote_equity_us(
    symbol: str,
    *,
    include_extended_hours: bool = False,
) -> dict[str, Any]:
    _ = include_extended_hours
    exchange = await get_us_exchange_by_symbol(to_db_symbol(symbol))
    quote = await _fetch_toss_equity_quote(
        symbol,
        instrument_type="equity_us",
        exchange=exchange,
    )
    return _tag_us_quote_session(quote)


async def fetch_us_live_last_price(symbol: str) -> float | None:
    """Active universe에 포함된 symbol의 fresh Toss 현재가를 반환한다.

    읽기 전용 Toss 현재가가 없거나 stale이면 호출자는 OHLCV 종가를 그대로
    유지할 수 있도록 ``None``을 받는다.
    """
    try:
        quote = await _fetch_quote_equity_us(symbol)
    except Exception:
        # 이 overlay에서는 provider 및 universe 조회 실패를 선택적 결측으로 처리한다.
        return None
    if (
        quote.get("price_source") != "toss_price"
        or quote.get("data_state") != DATA_STATE_FRESH
    ):
        return None

    price = quote.get("price")
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# OHLCV Fetching
# ---------------------------------------------------------------------------


_INTRADAY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})


def _format_crypto_minute_timestamp(date_value: Any, time_value: Any) -> str | None:
    if pd.isna(date_value) or pd.isna(time_value):
        return None
    return f"{date_value}T{time_value}"


def _validate_crypto_minute_source_columns(df: pd.DataFrame) -> None:
    missing = [
        column
        for column in CRYPTO_MINUTE_REQUIRED_SOURCE_COLUMNS
        if column not in df.columns
    ]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(
            f"Crypto minute OHLCV response missing columns: {missing_text}"
        )


def _calculate_rsi_14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean()
    avg_loss = loss.rolling(window=14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask(avg_loss == 0, 100.0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return rsi.round(2)


def _calculate_ema_20(close: pd.Series) -> pd.Series:
    ema = close.ewm(span=20, adjust=False).mean()
    return ema.where(close.expanding().count() >= 20)


def _calculate_bollinger_bands(
    close: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(window=20, min_periods=20).mean()
    std = close.rolling(window=20, min_periods=20).std()
    upper = middle + (std * 2)
    lower = middle - (std * 2)
    return upper, middle, lower


def _calculate_vwap(df: pd.DataFrame, period: str) -> pd.Series:
    if period not in _INTRADAY_OHLCV_PERIODS:
        return pd.Series([None] * len(df), index=df.index, dtype=object)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_volume = df["volume"].cumsum()
    vwap = (typical_price * df["volume"]).cumsum() / cumulative_volume.replace(0, pd.NA)
    return vwap


def _enrich_ohlcv_with_indicators(df: pd.DataFrame, period: str) -> pd.DataFrame:
    frame = df.copy()
    close = frame["close"].astype(float)
    frame["rsi_14"] = _calculate_rsi_14(close)
    frame["ema_20"] = _calculate_ema_20(close)
    bb_upper, bb_mid, bb_lower = _calculate_bollinger_bands(close)
    frame["bb_upper"] = bb_upper
    frame["bb_mid"] = bb_mid
    frame["bb_lower"] = bb_lower

    if period in _INTRADAY_OHLCV_PERIODS:
        frame["high"] = frame["high"].astype(float)
        frame["low"] = frame["low"].astype(float)
        frame["volume"] = frame["volume"].astype(float)
        frame["vwap"] = _calculate_vwap(frame, period)
    else:
        frame["vwap"] = None

    return frame


def _normalize_crypto_minute_ohlcv_rows(
    df: pd.DataFrame, *, include_indicators: bool
) -> list[dict[str, Any]]:
    frame = df.copy()
    frame["timestamp"] = [
        _format_crypto_minute_timestamp(date_value, time_value)
        for date_value, time_value in zip(frame["date"], frame["time"], strict=False)
    ]
    frame["trade_amount"] = frame["value"]
    row_keys: list[str] = list(CRYPTO_MINUTE_PUBLIC_ROW_KEYS)
    if include_indicators:
        row_keys.extend(_OHLCV_INDICATOR_ROW_KEYS)
    return _normalize_rows(frame.loc[:, row_keys])


async def _fetch_ohlcv_crypto(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool,
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=end_date
    )

    if df.empty:
        return {
            "symbol": symbol,
            "instrument_type": "crypto",
            "source": "upbit",
            "period": period,
            "count": 0,
            "rows": [],
            "indicators_included": include_indicators,
            "message": f"No candle data available for {symbol}",
        }

    if period in CRYPTO_MINUTE_OHLCV_PERIODS:
        _validate_crypto_minute_source_columns(df)

    if include_indicators:
        df = _enrich_ohlcv_with_indicators(df, period)

    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "source": "upbit",
        "period": period,
        "count": capped_count,
        "indicators_included": include_indicators,
        "rows": (
            _normalize_crypto_minute_ohlcv_rows(
                df, include_indicators=include_indicators
            )
            if period in CRYPTO_MINUTE_OHLCV_PERIODS
            else _normalize_rows(df)
        ),
    }


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
) -> dict[str, Any]:
    capped_count = min(count, 200)
    if period in KR_INTRADAY_OHLCV_PERIODS:
        df = await fetch_kr_intraday_toss_frame(
            symbol=symbol,
            period=period,
            count=capped_count,
            end_date=end_date,
        )
    elif period == "day":
        df = await fetch_daily_toss_frame(
            symbol=symbol,
            count=capped_count,
            end_date=end_date,
        )
    else:
        df = await fetch_resampled_daily_toss_frame(
            symbol=symbol,
            period=period,
            count=capped_count,
            end_date=end_date,
        )
    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_kr",
        source="toss",
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
    )


async def _fetch_ohlcv_equity_us(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
    *,
    include_indicators: bool = False,
    end_date_is_date_only: bool = False,
) -> dict[str, Any]:
    _ = end_date_is_date_only
    await get_us_exchange_by_symbol(to_db_symbol(symbol))
    capped_count = min(count, 200)
    if period in US_INTRADAY_OHLCV_PERIODS:
        df = await fetch_us_intraday_toss_frame(
            symbol=symbol,
            period=period,
            count=capped_count,
            end_date=end_date,
        )
    elif period == "day":
        df = await fetch_daily_toss_frame(
            symbol=symbol,
            count=capped_count,
            end_date=end_date,
        )
    else:
        df = await fetch_resampled_daily_toss_frame(
            symbol=symbol,
            period=period,
            count=capped_count,
            end_date=end_date,
        )
    return _build_ohlcv_payload(
        symbol=symbol,
        instrument_type="equity_us",
        source="toss",
        period=period,
        count=capped_count,
        df=df,
        include_indicators=include_indicators,
    )


# Tool Registration
# ---------------------------------------------------------------------------

MARKET_DATA_TOOL_NAMES: set[str] = {
    "search_symbol",
    "get_quote",
    "get_orderbook",
    "get_ohlcv",
    "get_indicators",
}


async def _get_indicators_impl(
    symbol: str, indicators: list[str], market: str | None = None
) -> dict[str, Any]:
    """Calculate requested indicators for a symbol.

    Supported indicators:
    - adx: returns adx, plus_di, minus_di
    - stoch_rsi: returns k, d
    - obv: returns obv, signal, divergence
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    normalized_symbol = _normalize_symbol_input(symbol, market)
    market_missing = market is None or not str(market).strip()
    if market_missing and normalized_symbol.isalpha():
        raise ValueError(
            "market is required for plain alphabetic symbols. Use market='us' "
            "for US equities, or provide KRW-/USDT- prefixed symbol for crypto."
        )

    if not indicators:
        raise ValueError("indicators list is required and cannot be empty")

    valid_indicators = {
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "pivot",
        "adx",
        "stoch_rsi",
        "obv",
    }
    normalized_indicators: list[IndicatorType] = []
    for ind in indicators:
        ind_lower = ind.lower().strip()
        if ind_lower not in valid_indicators:
            raise ValueError(
                f"Invalid indicator '{ind}'. Valid options: {', '.join(sorted(valid_indicators))}"
            )
        normalized_indicators.append(cast(IndicatorType, ind_lower))

    market_type, symbol = _resolve_market_type(normalized_symbol, market)

    source_map = {"crypto": "upbit", "equity_kr": "toss", "equity_us": "toss"}
    source = source_map[market_type]

    try:
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

        if df.empty:
            raise ValueError(f"No data available for symbol '{symbol}'")

        close_fallback_price = (
            float(df["close"].iloc[-1]) if "close" in df.columns else None
        )
        current_price = close_fallback_price
        current_price_source = "ohlcv_close"
        if market_type == "crypto":
            try:
                prices = await upbit_service.fetch_multiple_current_prices([symbol])
                ticker_price = prices.get(symbol)
                if ticker_price is not None:
                    current_price = float(ticker_price)
            except Exception:
                current_price = close_fallback_price
        elif market_type == "equity_us":
            live = await fetch_us_live_last_price(symbol)
            if live is not None:
                current_price = live
                current_price_source = "toss_live"

        indicator_results = _compute_indicators(df, normalized_indicators)

        if market_type == "crypto" and "rsi" in normalized_indicators:
            realtime_rsi = _compute_crypto_realtime_rsi_from_frame(df, current_price)
            if realtime_rsi is not None:
                indicator_results.setdefault("rsi", {})["14"] = realtime_rsi

        result = {
            "symbol": symbol,
            "price": current_price,
            "instrument_type": market_type,
            "source": source,
            "indicators": indicator_results,
        }
        if market_type == "equity_us":
            result["current_price_source"] = current_price_source
            result["current_price_stale"] = current_price_source != "toss_live"
        return result

    except Exception as exc:
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


async def _search_symbol_impl(
    query: str,
    limit: int = 20,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Implementation for search_symbol tool."""
    query = (query or "").strip()
    if not query:
        return []

    instrument_type = _normalize_market(market)
    if market is not None and str(market).strip() and instrument_type is None:
        return [
            _error_payload(
                source="master",
                message=f"Unsupported market: {market}",
                query=query,
            )
        ]

    try:
        capped_limit = min(max(limit, 1), 100)
        return await _search_master_data(query, capped_limit, instrument_type)
    except Exception as exc:
        return [_error_payload(source="master", message=str(exc), query=query)]


async def _get_quote_impl(
    symbol: str | int,
    market: str | None = None,
    include_extended_hours: bool = False,
) -> dict[str, Any]:
    """``get_quote`` 도구 구현.

    ``include_extended_hours``는 호환성을 위해 받지만 모든 미국 세션의
    equity quote provider는 Toss로 고정한다.
    """
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    market_type, symbol = _resolve_market_type(symbol, market)

    source_map = {"crypto": "upbit", "equity_kr": "toss", "equity_us": "toss"}
    source = source_map[market_type]

    try:
        if market_type == "equity_us":
            return await _fetch_quote_equity_us(
                symbol,
                include_extended_hours=include_extended_hours,
            )
        if market_type == "crypto":
            return await _fetch_quote_crypto(symbol)
        session_state = kr_market_data_state()
        quote = await _fetch_quote_equity_kr(symbol)
        tradability_map = await get_kr_nxt_tradability([symbol])
        tradability = tradability_map.get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())

        if quote.get("price_usable") is False:
            quote["data_state"] = DATA_STATE_STALE
            quote["data_state_reason"] = quote.get("price_unavailable_reason")
        else:
            quote["data_state"] = session_state
        return quote
    except Exception as exc:
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


async def _get_orderbook_impl(
    symbol: str | int,
    market: str = "kr",
    venue: str | None = None,
) -> dict[str, Any]:
    """Implementation for get_orderbook tool."""
    requested_market = str(market or "kr").strip() or "kr"
    market_type = _normalize_market(requested_market)
    if market_type is None:
        raise ValueError(f"Unsupported market: {market}")

    source = "nhplug"
    instrument_type = "equity_kr"

    if market_type == "equity_kr":
        symbol = _normalize_symbol_input(symbol, "kr")
        if not symbol:
            raise ValueError("symbol is required")
        _, symbol = _resolve_market_type(symbol, "kr")
    elif market_type == "crypto":
        if venue is not None and str(venue).strip():
            raise ValueError("venue is only supported for KR equity orderbook")
        symbol = _validate_crypto_orderbook_symbol_input(symbol)
        source = "upbit"
        instrument_type = "crypto"
    else:
        raise ValueError("get_orderbook only supports KR equity and KRW crypto markets")

    try:
        snapshot = await market_data_service.get_orderbook(
            symbol,
            "crypto" if market_type == "crypto" else "kr",
            venue=venue if market_type == "equity_kr" else None,
        )
        return _build_orderbook_payload(snapshot)
    except Exception as exc:
        payload = _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=instrument_type,
        )
        payload["success"] = False
        return payload


async def _get_ohlcv_impl(
    symbol: str,
    count: int = 100,
    period: str = "day",
    end_date: str | None = None,
    market: str | None = None,
    include_indicators: bool = False,
) -> dict[str, Any]:
    """Implementation for get_ohlcv tool."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    count = int(count)
    if count <= 0:
        raise ValueError("count must be > 0")

    period = (period or "day").strip().lower()

    market_type, symbol = _resolve_market_type(symbol, market)
    period = validate_ohlcv_period(period, market_type)

    parsed_end_date: datetime.datetime | None = None
    end_date_is_date_only = False
    if end_date:
        try:
            is_date_only = len(end_date) == 10  # "YYYY-MM-DD"
            if (
                market_type == "equity_us"
                and period in US_INTRADAY_OHLCV_PERIODS
                and is_date_only
            ):
                end_date_is_date_only = True
                parsed_end_date = datetime.datetime.combine(
                    datetime.date.fromisoformat(end_date),
                    datetime.time(20, 0),  # 20:00 ET = post-market close
                )
            else:
                parsed_end_date = datetime.datetime.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError(
                "end_date must be ISO format (e.g., '2024-01-15')"
            ) from exc

    source_map = {"crypto": "upbit", "equity_kr": "toss", "equity_us": "toss"}
    source = source_map[market_type]
    try:
        if market_type == "crypto":
            return await _fetch_ohlcv_crypto(
                symbol,
                count,
                period,
                parsed_end_date,
                include_indicators=include_indicators,
            )
        if market_type == "equity_kr":
            return await _fetch_ohlcv_equity_kr(
                symbol,
                count,
                period,
                parsed_end_date,
                include_indicators=include_indicators,
            )
        return await _fetch_ohlcv_equity_us(
            symbol,
            count,
            period,
            parsed_end_date,
            include_indicators=include_indicators,
            end_date_is_date_only=end_date_is_date_only,
        )
    except Exception as exc:
        if str(exc).startswith("Crypto minute OHLCV response missing columns:"):
            raise
        return _error_payload_from_exception(
            source=source,
            exc=exc,
            symbol=symbol,
            instrument_type=market_type,
        )


def _register_market_data_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol",
        description=(
            "Search symbols by query (symbol or name). Use market to filter: "
            "kr/kospi/kosdaq (Korean stocks), us/nasdaq/nyse (US stocks), "
            "crypto/upbit (cryptocurrencies)."
        ),
    )
    async def search_symbol(
        query: str, limit: int = 20, market: str | None = None
    ) -> list[dict[str, Any]]:
        return await _search_symbol_impl(query, limit, market)

    @mcp.tool(
        name="get_quote",
        description=(
            "KR/US equity는 Toss, crypto는 Upbit에서 최신 시세를 읽습니다. "
            "analyze_stock_batch quick=True is DB-only and never fetches a live "
            "price; always call get_quote for that. include_extended_hours는 "
            "호환성 인자일 뿐 US provider를 바꾸지 않습니다. 캔들 이력은 "
            "get_ohlcv를 사용하세요."
        ),
    )
    async def get_quote(
        symbol: str | int,
        market: str | None = None,
        include_extended_hours: bool = False,
    ) -> dict[str, Any]:
        return await _get_quote_impl(symbol, market, include_extended_hours)

    @mcp.tool(
        name="get_orderbook",
        description=(
            "KR equity는 NH PLUG mock feed, KRW crypto는 Upbit에서 호가를 "
            "읽습니다. NH PLUG는 KRX만 지원하며 NXT/unified venue 요청은 "
            "provider_unsupported를 반환합니다."
        ),
    )
    async def get_orderbook(
        symbol: str | int, market: str = "kr", venue: str | None = None
    ) -> dict[str, Any]:
        return await _get_orderbook_impl(symbol, market, venue)

    @mcp.tool(
        name="get_ohlcv",
        description=(
            "종목 OHLCV를 읽습니다. KR/US equity와 crypto는 day/week/month 및 "
            "1m/5m/15m/30m을 지원하고, crypto는 4h, 전 시장은 1h와 날짜 기반 "
            "pagination을 지원합니다."
        ),
    )
    async def get_ohlcv(
        symbol: str,
        count: int = 100,
        period: str = "day",
        end_date: str | None = None,
        market: str | None = None,
        include_indicators: bool = False,
    ) -> dict[str, Any]:
        return await _get_ohlcv_impl(
            symbol=symbol,
            count=count,
            period=period,
            end_date=end_date,
            market=market,
            include_indicators=include_indicators,
        )

    @mcp.tool(
        name="get_indicators",
        description=(
            "Calculate technical indicators for a symbol. Available indicators: "
            "sma (Simple Moving Average), ema (Exponential Moving Average), "
            "rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), "
            "atr (Average True Range), pivot (Pivot Points), "
            "adx (Average Directional Index - returns adx, plus_di, minus_di), "
            "stoch_rsi (Stochastic RSI - returns k, d), "
            "obv (On-Balance Volume - returns obv, signal, divergence)."
        ),
    )
    async def get_indicators(
        symbol: str, indicators: list[str], market: str | None = None
    ) -> dict[str, Any]:
        return await _get_indicators_impl(symbol, indicators, market)


# ---------------------------------------------------------------------------
# Public/Shared Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_fetch_quote_crypto",
    "_fetch_quote_equity_kr",
    "_fetch_kr_live_quote",
    "_fetch_quote_equity_us",
    "_fetch_ohlcv_crypto",
    "_fetch_ohlcv_equity_kr",
    "_fetch_ohlcv_equity_us",
    "_build_orderbook_payload",
    "_get_indicators_impl",
    "_search_symbol_impl",
    "_get_quote_impl",
    "_get_orderbook_impl",
    "_get_ohlcv_impl",
    "MARKET_DATA_TOOL_NAMES",
    "_register_market_data_tools_impl",
]
