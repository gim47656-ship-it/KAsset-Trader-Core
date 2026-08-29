"""Cached Android market overview composed from existing read-only sources."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from typing import Any, Literal

from app.core.config import settings
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import (
    MarketIndexCandle,
    MarketIndexDetailResponse,
    MarketIndexRange,
    MarketIndexSummary,
    MarketIndicatorGroup,
    MarketIndicatorItem,
    MarketIndicatorKey,
    MarketIndicatorUnit,
    MarketOverviewError,
    MarketOverviewErrorCode,
    MarketOverviewItem,
    MarketOverviewItemStatus,
    MarketOverviewResponse,
    MarketOverviewSession,
    MarketSessionState,
)
from app.extensions.kasset.api.toss_market_data import (
    TossIndicatorPoint,
    toss_market_data,
)
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index,
    handle_get_market_index_current_batch,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_MARKET_CLOSED,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    DATA_STATE_STALE,
)
from app.services.brokers.toss.market_calendar import (
    get_latest_completed_regular_window_from_toss,
)
from app.services.brokers.upbit.client import fetch_multiple_tickers
from app.services.exchange_rate_service import (
    OpenErApiUsdSnapshot,
    UsdKrwExchangeRateQuote,
    get_open_er_api_usd_snapshot,
    get_usd_krw_rate_details,
)

# 홈 화면 지연 예산: 앱이 15초 주기로 폴링하므로 서버 캐시도 15초다. 60초였을
# 때는 앱 폴링 주기와 겹쳐 최악 약 2분까지 오래된 값이 보였다.
OVERVIEW_CACHE_TTL_SECONDS = 15.0
SOURCE_TIMEOUT_SECONDS = 6.0
INDEX_DECIMAL_PLACES = 2
FX_DECIMAL_PLACES = 2
CHANGE_RATE_DECIMAL_PLACES = 2
INDICATOR_DECIMAL_PLACES: dict[MarketIndicatorUnit, int] = {
    "POINT": 2,
    "PERCENT": 2,
    "USD": 2,
    "KRW": 0,
}

_IndexMarket = Literal["KRX", "US"]
_IndexCurrency = Literal["KRW", "USD"]
_INDEX_DEFINITIONS: tuple[tuple[str, str, _IndexMarket, _IndexCurrency], ...] = (
    ("KOSPI", "KOSPI", "KRX", "KRW"),
    ("KOSDAQ", "KOSDAQ", "KRX", "KRW"),
    ("SPX", "S&P 500", "US", "USD"),
    ("NASDAQ", "NASDAQ", "US", "USD"),
    ("DJI", "다우지수", "US", "USD"),
    ("RUT", "러셀2000", "US", "USD"),
    ("SOX", "필라델피아 반도체", "US", "USD"),
)
_INDEX_DEFINITIONS_BY_SYMBOL: dict[
    str,
    tuple[str, _IndexMarket, _IndexCurrency],
] = {
    symbol: (name, market, currency)
    for symbol, name, market, currency in _INDEX_DEFINITIONS
}
_INDEX_RANGE_CONFIG: dict[MarketIndexRange, tuple[str, int]] = {
    "1W": ("day", 5),
    "1M": ("day", 20),
    "3M": ("day", 60),
    "6M": ("week", 26),
}
_FX_DEFINITIONS = (
    ("USDKRW", "USD/KRW"),
    ("JPYKRW", "JPY/KRW"),
    ("EURKRW", "EUR/KRW"),
)
# 비주식 지표. market 필드를 두지 않으므로 세션 딕셔너리를 심볼로 인덱싱하는
# 경로가 생기지 않는다. provider는 이 행을 채우는 소스이며, 상태 판정과 그룹
# 실패 코드 배정이 provider별로 갈린다.
_IndicatorProvider = Literal["us_batch", "upbit", "toss"]
_INDICATOR_DEFINITIONS: tuple[
    tuple[
        MarketIndicatorKey,
        str,
        MarketIndicatorGroup,
        MarketIndicatorUnit,
        _IndicatorProvider,
    ],
    ...,
] = (
    ("VIX", "변동성지수", "VOLATILITY", "POINT", "us_batch"),
    # ^TNX는 가격이 아니라 % 수익률이다. 통화 환산·가격 취급을 하지 않는다.
    ("US10Y", "미국 10년물", "RATE", "PERCENT", "us_batch"),
    # 한국 국채는 토스 시장지표가 % 수익률로 준다. 토스는 국내 국채만 주므로
    # 미국 10년물은 계속 yfinance(^TNX)에서 온다.
    ("KR_BOND_2Y", "국고채 2년", "RATE", "PERCENT", "toss"),
    ("KR_BOND_3Y", "국고채 3년", "RATE", "PERCENT", "toss"),
    ("KR_BOND_5Y", "국고채 5년", "RATE", "PERCENT", "toss"),
    ("KR_BOND_10Y", "국고채 10년", "RATE", "PERCENT", "toss"),
    ("KR_BOND_20Y", "국고채 20년", "RATE", "PERCENT", "toss"),
    ("KR_BOND_30Y", "국고채 30년", "RATE", "PERCENT", "toss"),
    ("WTI", "WTI", "COMMODITY", "USD", "us_batch"),
    ("BRENT", "브렌트유", "COMMODITY", "USD", "us_batch"),
    ("GOLD", "금", "COMMODITY", "USD", "us_batch"),
    ("BTC", "비트코인", "CRYPTO", "KRW", "upbit"),
)
# yfinance 지표는 지수와 같은 배치(yf.download 1회)에 합류한다. 왕복이 늘지 않는다.
_OVERVIEW_BATCH_SYMBOLS: tuple[str, ...] = tuple(
    symbol for symbol, _name, _market, _currency in _INDEX_DEFINITIONS
) + tuple(
    key
    for key, _name, _group, _unit, provider in _INDICATOR_DEFINITIONS
    if provider == "us_batch"
)
# 토스 지표는 한 번의 배치 호출(최대 200심볼)로 함께 조회한다.
_TOSS_INDICATOR_SYMBOLS: tuple[str, ...] = tuple(
    key
    for key, _name, _group, _unit, provider in _INDICATOR_DEFINITIONS
    if provider == "toss"
)
_UPBIT_BTC_MARKET = "KRW-BTC"
_SessionStates = dict[str, MarketSessionState | None]

_cache: dict[str, object] = {}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_index_detail_cache: dict[
    tuple[str, MarketIndexRange],
    tuple[float, MarketIndexDetailResponse],
] = {}
_index_detail_locks: dict[tuple[str, MarketIndexRange], asyncio.Lock] = {}
_index_detail_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _get_cached(now: float) -> MarketOverviewResponse | None:
    expires_at = _cache.get("expires_at")
    response = _cache.get("response")
    if (
        isinstance(expires_at, int | float)
        and expires_at > now
        and isinstance(response, MarketOverviewResponse)
    ):
        return response
    return None


def _set_cached(response: MarketOverviewResponse, now: float) -> None:
    _cache["response"] = response
    _cache["expires_at"] = now + OVERVIEW_CACHE_TTL_SECONDS


def _get_index_detail_lock(
    key: tuple[str, MarketIndexRange],
) -> asyncio.Lock:
    global _index_detail_lock_loop
    loop = asyncio.get_running_loop()
    if _index_detail_lock_loop is not loop:
        _index_detail_locks.clear()
        _index_detail_lock_loop = loop
    lock = _index_detail_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _index_detail_locks[key] = lock
    return lock


def _get_cached_index_detail(
    key: tuple[str, MarketIndexRange],
    now: float,
) -> MarketIndexDetailResponse | None:
    cached = _index_detail_cache.get(key)
    if cached is None:
        return None
    expires_at, response = cached
    return response if expires_at > now else None


def _set_cached_index_detail(
    key: tuple[str, MarketIndexRange],
    response: MarketIndexDetailResponse,
    now: float,
) -> None:
    _index_detail_cache[key] = (now + OVERVIEW_CACHE_TTL_SECONDS, response)


def _decimal_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return format(parsed, "f")


def _quantized_decimal_text(
    value: object,
    *,
    decimal_places: int,
) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float):
            # 지원 Python에서 str(float)와 repr(float)는 같은 최단 왕복 문자열을
            # 만든다. 표시용 변환이 아니라 이진 float를 재구성하는 리터럴을
            # Decimal에 넘긴다는 의도를 명확히 하려고 repr을 사용한다.
            parsed = Decimal(repr(value))
        else:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        if not parsed.is_finite():
            return None
        quantum = Decimal(1).scaleb(-decimal_places)
        rounded = parsed.quantize(quantum, rounding=ROUND_HALF_UP)
    except (DecimalException, ValueError):
        return None

    # normalize()는 큰 정수를 1E+8처럼 바꿀 수 있어 와이어 정규식을 깨뜨린다.
    # 고정소수점으로 만든 뒤 문자열에서만 0을 걷어 지수 표기를 막는다.
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_as_of(row: dict[str, Any]) -> str | None:
    """Normalize a provider quote timestamp to the wire's UTC ``Z`` form.

    Naver stamps the KR index quote with a ``+09:00`` offset. The Android client
    parses wire timestamps with a strict UTC parser, so an offset string would be
    dropped and the screen would lose its "기준 시각". Convert here instead of
    widening the client parser.
    """
    value = row.get("quote_asof")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _datetime_text(parsed)


def _latest_as_of(items: list[MarketOverviewItem]) -> str | None:
    parsed_values: list[tuple[datetime, str]] = []
    for item in items:
        if item.as_of is None:
            continue
        try:
            parsed = datetime.fromisoformat(item.as_of.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        parsed_values.append((parsed.astimezone(UTC), item.as_of))
    if not parsed_values:
        return None
    return max(parsed_values, key=lambda pair: pair[0])[1]


async def _session_snapshot(
    *, moment: datetime | None = None
) -> tuple[list[MarketOverviewSession], _SessionStates]:
    kr_state, us_state = await asyncio.gather(
        krx_quotes.resolve_market_session_state("KRX", moment=moment),
        krx_quotes.resolve_market_session_state("US", moment=moment),
    )
    states: _SessionStates = {"KRX": kr_state, "US": us_state}
    return (
        [
            MarketOverviewSession(market="KRX", state=kr_state),
            MarketOverviewSession(market="US", state=us_state),
        ],
        states,
    )


def _unavailable_item(
    *,
    symbol: str,
    name: str,
    market: Literal["KRX", "US", "FX"],
    currency: Literal["KRW", "USD"],
    session_state: MarketSessionState | None,
) -> MarketOverviewItem:
    return MarketOverviewItem(
        symbol=symbol,
        name=name,
        market=market,
        currency=currency,
        price=None,
        change_amount=None,
        change_rate=None,
        as_of=None,
        status="unavailable",
        session_state=session_state,
    )


def _index_status(
    row: dict[str, Any], *, session_state: MarketSessionState | None
) -> MarketOverviewItemStatus:
    data_state = row.get("data_state")
    if data_state == DATA_STATE_FRESH:
        return "available"
    if data_state in {
        DATA_STATE_STALE,
        DATA_STATE_PREMARKET_UNAVAILABLE,
        DATA_STATE_MARKET_CLOSED,
    }:
        return "stale"
    if row.get("source") == "yfinance_history_fallback":
        return "stale"
    return "available" if session_state == "REGULAR" else "stale"


def _index_rows_by_symbol(result: object) -> dict[str, dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    if not isinstance(result, dict):
        return rows_by_symbol
    rows = result.get("indices")
    if not isinstance(rows, list):
        return rows_by_symbol
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol not in rows_by_symbol:
            rows_by_symbol[symbol] = row
    return rows_by_symbol


def _usable_index_row(
    rows_by_symbol: dict[str, dict[str, Any]],
    symbol: str,
    *,
    decimal_places: int,
) -> tuple[dict[str, Any], str] | None:
    row = rows_by_symbol.get(symbol)
    price = (
        _quantized_decimal_text(row.get("current"), decimal_places=decimal_places)
        if row is not None
        else None
    )
    if (
        row is None
        or row.get("error") is not None
        or row.get("unavailable") is True
        or price is None
    ):
        return None
    return row, price


def _history_date_bucket(value: object) -> str | None:
    if isinstance(value, datetime):
        trading_date = value.date()
    elif isinstance(value, date):
        trading_date = value
    elif isinstance(value, str):
        try:
            trading_date = date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    else:
        return None
    return f"{trading_date.isoformat()}T00:00:00Z"


def _index_candles(result: object) -> list[MarketIndexCandle]:
    if not isinstance(result, dict):
        return []
    history = result.get("history")
    if not isinstance(history, list):
        return []

    by_time: dict[str, MarketIndexCandle] = {}
    for row in history:
        if not isinstance(row, dict):
            continue
        bucket = _history_date_bucket(row.get("date"))
        open_ = _decimal_text(row.get("open"))
        high = _decimal_text(row.get("high"))
        low = _decimal_text(row.get("low"))
        close = _decimal_text(row.get("close"))
        if (
            bucket is None
            or open_ is None
            or high is None
            or low is None
            or close is None
        ):
            continue
        by_time[bucket] = MarketIndexCandle(
            time=bucket,
            open=open_,
            high=high,
            low=low,
            close=close,
            # The KR index price source publishes no volume for an index bar.
            volume=_decimal_text(row.get("volume")),
        )
    return [by_time[bucket] for bucket in sorted(by_time)]


def _index_summary(
    result: object,
    *,
    symbol: str,
    range_: MarketIndexRange,
    sessions: _SessionStates,
) -> MarketIndexSummary:
    name, market, currency = _INDEX_DEFINITIONS_BY_SYMBOL[symbol]
    session_state = sessions[market]
    usable = _usable_index_row(
        _index_rows_by_symbol(result),
        symbol,
        decimal_places=INDEX_DECIMAL_PLACES,
    )
    if usable is None:
        return MarketIndexSummary(
            symbol=symbol,
            name=name,
            market=market,
            currency=currency,
            price=None,
            change_amount=None,
            change_rate=None,
            as_of=None,
            status="unavailable",
            session_state=session_state,
            range=range_,
        )

    row, price = usable
    return MarketIndexSummary(
        symbol=symbol,
        name=name,
        market=market,
        currency=currency,
        price=price,
        change_amount=_quantized_decimal_text(
            row.get("change"),
            decimal_places=INDEX_DECIMAL_PLACES,
        ),
        change_rate=_quantized_decimal_text(
            row.get("change_pct"),
            decimal_places=CHANGE_RATE_DECIMAL_PLACES,
        ),
        as_of=_source_as_of(row),
        status=_index_status(row, session_state=session_state),
        session_state=session_state,
        range=range_,
    )


def _index_items(
    result: object,
    *,
    sessions: _SessionStates,
    group_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketOverviewItem], list[MarketOverviewError]]:
    rows_by_symbol = _index_rows_by_symbol(result)

    items: list[MarketOverviewItem] = []
    errors: list[MarketOverviewError] = []
    for symbol, name, market, currency in _INDEX_DEFINITIONS:
        session_state = sessions[market]
        usable = _usable_index_row(
            rows_by_symbol,
            symbol,
            decimal_places=INDEX_DECIMAL_PLACES,
        )
        if usable is None:
            items.append(
                _unavailable_item(
                    symbol=symbol,
                    name=name,
                    market=market,
                    currency=currency,
                    session_state=session_state,
                )
            )
            errors.append(
                MarketOverviewError(
                    scope="indices",
                    symbol=symbol,
                    code=group_error_code or "UNAVAILABLE",
                )
            )
            continue

        row, price = usable

        items.append(
            MarketOverviewItem(
                symbol=symbol,
                name=name,
                market=market,
                currency=currency,
                price=price,
                change_amount=_quantized_decimal_text(
                    row.get("change"),
                    decimal_places=INDEX_DECIMAL_PLACES,
                ),
                # The source already expresses change_pct in percentage points.
                change_rate=_quantized_decimal_text(
                    row.get("change_pct"),
                    decimal_places=CHANGE_RATE_DECIMAL_PLACES,
                ),
                as_of=_source_as_of(row),
                status=_index_status(row, session_state=session_state),
                session_state=session_state,
            )
        )
    return items, errors


def _upbit_btc_row(ticker: object) -> dict[str, Any] | None:
    """Upbit KRW-BTC 티커를 지수 행과 같은 모양으로 정규화한다.

    ``signed_change_rate``는 비율(0.01 = 1%)이므로 지수 행의 관례(퍼센트포인트)에
    맞춰 100을 곱한다. 전일종가가 없으면 등락을 계산하지 않고 그대로 비운다.
    """
    if not isinstance(ticker, dict):
        return None
    trade_price = ticker.get("trade_price")
    if trade_price is None:
        return None
    previous_close = ticker.get("prev_closing_price")
    change = ticker.get("signed_change_price") if previous_close is not None else None
    raw_rate = ticker.get("signed_change_rate") if previous_close is not None else None
    try:
        change_pct = (
            Decimal(str(raw_rate)) * 100
            if raw_rate is not None and not isinstance(raw_rate, bool)
            else None
        )
    except (InvalidOperation, ValueError):
        change_pct = None

    timestamp = ticker.get("trade_timestamp") or ticker.get("timestamp")
    quote_asof: str | None = None
    if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
        quote_asof = datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat()

    return {
        "symbol": "BTC",
        "current": trade_price,
        "previous_close": previous_close,
        "change": change,
        "change_pct": change_pct,
        "quote_asof": quote_asof,
        "source": "upbit",
    }


def _toss_indicator_rows(points: object) -> dict[str, dict[str, Any]]:
    """토스 지표 포인트를 지수 행과 같은 모양으로 정규화한다.

    지표 현재가 응답에는 전일종가·등락 필드가 없다. 당일 값을 전일종가로
    재사용하면 등락이 0으로 위조되므로 그대로 비워 둔다(일봉에서 직전 거래일
    종가를 뽑는 경로는 별도 슬라이스다).
    """
    if not isinstance(points, dict):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for symbol, point in points.items():
        last_price = getattr(point, "last_price", None)
        if not isinstance(symbol, str) or last_price is None:
            continue
        as_of = getattr(point, "as_of", None)
        rows[symbol] = {
            "symbol": symbol,
            "current": last_price,
            "previous_close": None,
            "change": None,
            "change_pct": None,
            "quote_asof": _datetime_text(as_of) if as_of is not None else None,
            "source": "toss",
        }
    return rows


def _indicator_status(
    row: dict[str, Any],
    *,
    provider: _IndicatorProvider,
    sessions: _SessionStates,
) -> MarketOverviewItemStatus:
    """지표 한 줄의 신선도. 지표는 세션 필드를 노출하지 않으므로 여기서만 쓴다."""
    if provider == "toss":
        # 토스 지표 현재가는 장 마감 상태에서 timestamp가 null로 온다. 기준
        # 시각을 증명할 수 없으면 available이라고 말하지 않는다.
        return "available" if row.get("quote_asof") else "stale"
    # Upbit(암호화폐)는 24시간 시장이라 세션 개념이 없다. US 배치 지표만 미국
    # 세션 상태로 판정한다(고정 키 조회이므로 sessions KeyError 경로가 없다).
    session_state: MarketSessionState | None = (
        sessions["US"] if provider == "us_batch" else "REGULAR"
    )
    return _index_status(row, session_state=session_state)


def _indicator_items(
    index_result: object,
    btc_ticker: object,
    toss_points: object,
    *,
    sessions: _SessionStates,
    us_batch_error_code: MarketOverviewErrorCode | None = None,
    upbit_error_code: MarketOverviewErrorCode | None = None,
    toss_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketIndicatorItem], list[MarketOverviewError]]:
    """비주식 지표 행을 조립한다. 한 공급자가 죽어도 그 항목만 unavailable이다."""

    rows_by_symbol = _index_rows_by_symbol(index_result)
    btc_row = _upbit_btc_row(btc_ticker)
    if btc_row is not None:
        rows_by_symbol["BTC"] = btc_row
    rows_by_symbol.update(_toss_indicator_rows(toss_points))

    group_error_codes: dict[_IndicatorProvider, MarketOverviewErrorCode | None] = {
        "us_batch": us_batch_error_code,
        "upbit": upbit_error_code,
        "toss": toss_error_code,
    }
    items: list[MarketIndicatorItem] = []
    errors: list[MarketOverviewError] = []
    for key, name, group, unit, provider in _INDICATOR_DEFINITIONS:
        decimal_places = INDICATOR_DECIMAL_PLACES[unit]
        usable = _usable_index_row(
            rows_by_symbol,
            key,
            decimal_places=decimal_places,
        )
        if usable is None:
            items.append(
                MarketIndicatorItem(
                    key=key,
                    name=name,
                    group=group,
                    value=None,
                    previous_close=None,
                    change_amount=None,
                    change_rate=None,
                    unit=unit,
                    as_of=None,
                    status="unavailable",
                )
            )
            errors.append(
                MarketOverviewError(
                    scope="indicators",
                    symbol=key,
                    code=group_error_codes[provider] or "UNAVAILABLE",
                )
            )
            continue

        row, value = usable
        items.append(
            MarketIndicatorItem(
                key=key,
                name=name,
                group=group,
                value=value,
                previous_close=_quantized_decimal_text(
                    row.get("previous_close"),
                    decimal_places=decimal_places,
                ),
                change_amount=_quantized_decimal_text(
                    row.get("change"),
                    decimal_places=decimal_places,
                ),
                # 공급자가 이미 퍼센트포인트로 준 등락률을 소수 2자리로 제한한다.
                change_rate=_quantized_decimal_text(
                    row.get("change_pct"),
                    decimal_places=CHANGE_RATE_DECIMAL_PLACES,
                ),
                unit=unit,
                as_of=_source_as_of(row),
                status=_indicator_status(row, provider=provider, sessions=sessions),
            )
        )
    return items, errors


def _fx_items(
    snapshot: OpenErApiUsdSnapshot | None,
    toss_usd_quote: UsdKrwExchangeRateQuote | None,
    *,
    group_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketOverviewItem], list[MarketOverviewError]]:
    snapshot_as_of = _datetime_text(snapshot.as_of) if snapshot is not None else None
    toss_as_of = (
        _datetime_text(toss_usd_quote.valid_from)
        if toss_usd_quote is not None
        else None
    )
    try:
        usd_krw = (
            Decimal(str(toss_usd_quote.default_rate))
            if toss_usd_quote is not None
            else snapshot.usd_krw
            if snapshot is not None
            else None
        )
        values = (
            (usd_krw, toss_as_of or snapshot_as_of),
            (snapshot.jpy_krw, snapshot_as_of)
            if snapshot is not None
            else (None, None),
            (snapshot.eur_krw, snapshot_as_of)
            if snapshot is not None
            else (None, None),
        )
    except DecimalException:
        values = ((None, None), (None, None), (None, None))

    items: list[MarketOverviewItem] = []
    errors: list[MarketOverviewError] = []
    for (symbol, name), (raw_price, as_of) in zip(
        _FX_DEFINITIONS,
        values,
        strict=True,
    ):
        price = _quantized_decimal_text(
            raw_price,
            decimal_places=FX_DECIMAL_PLACES,
        )
        if price is None:
            items.append(
                _unavailable_item(
                    symbol=symbol,
                    name=name,
                    market="FX",
                    currency="KRW",
                    session_state=None,
                )
            )
            errors.append(
                MarketOverviewError(
                    scope="fx",
                    symbol=symbol,
                    code=group_error_code or "UNAVAILABLE",
                )
            )
            continue

        items.append(
            MarketOverviewItem(
                symbol=symbol,
                name=name,
                market="FX",
                currency="KRW",
                price=price,
                change_amount=None,
                change_rate=None,
                as_of=as_of,
                status="available",
                session_state=None,
            )
        )
    return items, errors


async def _bounded[T](source: Awaitable[T]) -> T:
    return await asyncio.wait_for(source, timeout=SOURCE_TIMEOUT_SECONDS)


async def _toss_usd_quote() -> UsdKrwExchangeRateQuote | None:
    if not settings.toss_api_enabled:
        return None
    quote = await get_usd_krw_rate_details()
    return quote if quote.source == "toss" else None


async def _upbit_btc_ticker() -> dict[str, Any] | None:
    """Upbit 공개 티커에서 KRW-BTC 한 건만 읽는다(인증 불필요)."""
    rows = await fetch_multiple_tickers([_UPBIT_BTC_MARKET])
    for row in rows:
        if isinstance(row, dict) and row.get("market") == _UPBIT_BTC_MARKET:
            return row
    return None


async def _toss_indicator_points() -> dict[str, TossIndicatorPoint]:
    """토스 시장지표(한국 국채) 현재가. 토스 게이트가 꺼져 있으면 빈 사전이다."""
    return await toss_market_data.market_indicators(_TOSS_INDICATOR_SYMBOLS)


async def _completed_index_snapshot() -> tuple[
    object,
    list[MarketOverviewSession],
    _SessionStates,
    MarketOverviewErrorCode | None,
]:
    """시장 캘린더가 증명한 최신 완료 정규장으로 지수 행을 제한한다."""
    moment = datetime.now(UTC)
    sessions, session_states = await _session_snapshot(moment=moment)
    kr_window, us_window = await asyncio.gather(
        get_latest_completed_regular_window_from_toss("kr", moment),
        get_latest_completed_regular_window_from_toss("us", moment),
    )
    completed_as_of_by_market = {
        market: window.end
        for market, window in (("KRX", kr_window), ("US", us_window))
        if window is not None
    }
    try:
        result = await _bounded(
            handle_get_market_index_current_batch(
                _OVERVIEW_BATCH_SYMBOLS,
                completed_as_of_by_market=completed_as_of_by_market,
            )
        )
    except TimeoutError:
        return None, sessions, session_states, "TIMEOUT"
    except Exception:
        return None, sessions, session_states, "UNAVAILABLE"
    return result, sessions, session_states, None


async def _build_market_overview() -> MarketOverviewResponse:
    # 환율·BTC·시장지표는 지수의 완료 세션 확인과 동시에 조회한다. 지수 가격은
    # 캘린더가 증명한 정규장 종료 시각을 얻은 뒤에만 일봉에서 고른다.
    (
        index_snapshot_result,
        fx_result,
        toss_usd_result,
        btc_result,
        toss_indicator_result,
    ) = await asyncio.gather(
        _completed_index_snapshot(),
        _bounded(get_open_er_api_usd_snapshot()),
        _bounded(_toss_usd_quote()),
        _bounded(_upbit_btc_ticker()),
        _bounded(_toss_indicator_points()),
        return_exceptions=True,
    )

    index_error_code: MarketOverviewErrorCode | None = None
    if isinstance(index_snapshot_result, BaseException):
        index_error_code = (
            "TIMEOUT"
            if isinstance(index_snapshot_result, TimeoutError)
            else "UNAVAILABLE"
        )
        index_payload: object = None
        sessions = [
            MarketOverviewSession(market="KRX", state=None),
            MarketOverviewSession(market="US", state=None),
        ]
        session_states: _SessionStates = {"KRX": None, "US": None}
    else:
        (
            index_payload,
            sessions,
            session_states,
            index_error_code,
        ) = index_snapshot_result

    indices, index_errors = _index_items(
        index_payload,
        sessions=session_states,
        group_error_code=index_error_code,
    )

    btc_error_code: MarketOverviewErrorCode | None = None
    if isinstance(btc_result, BaseException):
        btc_error_code = (
            "TIMEOUT" if isinstance(btc_result, TimeoutError) else "UNAVAILABLE"
        )
        btc_payload: object = None
    else:
        btc_payload = btc_result
    toss_indicator_error_code: MarketOverviewErrorCode | None = None
    if isinstance(toss_indicator_result, BaseException):
        toss_indicator_error_code = (
            "TIMEOUT"
            if isinstance(toss_indicator_result, TimeoutError)
            else "UNAVAILABLE"
        )
        toss_indicator_payload: object = None
    else:
        toss_indicator_payload = toss_indicator_result
    indicators, indicator_errors = _indicator_items(
        index_payload,
        btc_payload,
        toss_indicator_payload,
        sessions=session_states,
        us_batch_error_code=index_error_code,
        upbit_error_code=btc_error_code,
        toss_error_code=toss_indicator_error_code,
    )

    fx_error_code: MarketOverviewErrorCode | None = None
    if isinstance(fx_result, BaseException):
        fx_error_code = (
            "TIMEOUT" if isinstance(fx_result, TimeoutError) else "UNAVAILABLE"
        )
        snapshot = None
    elif isinstance(fx_result, OpenErApiUsdSnapshot):
        snapshot = fx_result
    else:
        snapshot = None
    toss_usd_quote = (
        toss_usd_result
        if isinstance(toss_usd_result, UsdKrwExchangeRateQuote)
        else None
    )
    fx, fx_errors = _fx_items(
        snapshot,
        toss_usd_quote,
        group_error_code=fx_error_code,
    )

    all_items = [*indices, *fx]
    # 지표는 상태 집계에는 넣되 as_of 계산에서는 뺀다. BTC는 초 단위로 갱신되므로
    # 개요의 "기준 시각"이 지수·환율 블록과 무관한 값으로 튀면 표시가 어긋난다.
    statuses = [item.status for item in (*all_items, *indicators)]
    if all(status == "unavailable" for status in statuses):
        status = "unavailable"
    elif any(status != "available" for status in statuses):
        status = "partial"
    else:
        status = "fresh"

    return MarketOverviewResponse(
        as_of=_latest_as_of(all_items),
        status=status,
        indices=indices,
        indicators=indicators,
        fx=fx,
        sessions=sessions,
        errors=[*index_errors, *indicator_errors, *fx_errors],
    )


async def get_market_overview() -> MarketOverviewResponse:
    """Return one single-flight market overview snapshot, cached for the overview TTL."""

    now = time.monotonic()
    cached = _get_cached(now)
    if cached is not None:
        return cached

    async with _get_lock():
        now = time.monotonic()
        cached = _get_cached(now)
        if cached is not None:
            return cached

        response = await _build_market_overview()
        _set_cached(response, time.monotonic())
        return response


async def warm_market_sources() -> None:
    """Pre-build the overview snapshot so the first client request is warm.

    Failures are irrelevant here: the snapshot is rebuilt on demand and every
    source already degrades to an ``unavailable`` item. A warmup must never
    take the API process down.
    """

    try:
        await get_market_overview()
    except Exception:
        return


async def _build_market_index_detail(
    symbol: str,
    range_: MarketIndexRange,
) -> MarketIndexDetailResponse:
    _, session_states = await _session_snapshot()
    period, count = _INDEX_RANGE_CONFIG[range_]
    try:
        result: object = await _bounded(
            handle_get_market_index(
                symbol=symbol,
                period=period,
                count=count,
            )
        )
    except Exception:
        result = None

    return MarketIndexDetailResponse(
        summary=_index_summary(
            result,
            symbol=symbol,
            range_=range_,
            sessions=session_states,
        ),
        candles=_index_candles(result),
    )


async def get_market_index_detail(
    symbol: str,
    range_: MarketIndexRange,
) -> MarketIndexDetailResponse:
    """Return one sanitized, keyed 60-second index detail snapshot."""

    normalized_symbol = symbol.strip().upper()
    if normalized_symbol not in _INDEX_DEFINITIONS_BY_SYMBOL:
        raise MobileApiError(404, "UNKNOWN_INDEX", "지원하지 않는 지수입니다.")

    key = (normalized_symbol, range_)
    now = time.monotonic()
    cached = _get_cached_index_detail(key, now)
    if cached is not None:
        return cached

    async with _get_index_detail_lock(key):
        now = time.monotonic()
        cached = _get_cached_index_detail(key, now)
        if cached is not None:
            return cached

        response = await _build_market_index_detail(normalized_symbol, range_)
        _set_cached_index_detail(key, response, time.monotonic())
        return response
