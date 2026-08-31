"""Cached Android market overview composed from existing read-only sources."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from typing import Any, Literal

from app.core.config import settings
from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.schemas import (
    MarketIndexCandle,
    MarketIndexDetailResponse,
    MarketIndexKind,
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
from app.mcp_server.tooling.fundamentals_sources_indices import (
    _INDEX_META,
    INDEX_INTRADAY_PERIOD,
    _upbit_index_row,
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
INDEX_SOURCE_TIMEOUT_SECONDS = 10.0
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
_DAILY_INDEX_RANGES: tuple[MarketIndexRange, ...] = ("1W", "1M", "3M", "6M")
_INDEX_RANGE_CONFIG: dict[MarketIndexRange, tuple[str, int]] = {
    "1W": ("day", 5),
    "1M": ("day", 20),
    "3M": ("day", 60),
    "6M": ("day", 126),
}
# "1일"은 10분봉이다. 24시간 시장(암호화폐)을 다 담는 길이로 요청하고, 정규장만
# 있는 심볼은 소스가 최근 거래일 하루만 남기므로 자연히 짧아진다.
_INTRADAY_CANDLE_COUNT = 144
_FX_DEFINITIONS = (
    ("USDKRW", "USD/KRW"),
    ("JPYKRW", "JPY/KRW"),
    ("EURKRW", "EUR/KRW"),
    ("CNYKRW", "CNY/KRW"),
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
    # 달러인덱스는 지수 포인트다(달러 가격이 아니므로 USD로 표기하지 않는다).
    ("DXY", "달러인덱스", "FX", "POINT", "us_batch"),
    ("BTC", "비트코인", "CRYPTO", "KRW", "upbit"),
    ("ETH", "이더리움", "CRYPTO", "KRW", "upbit"),
)
# KRX HTTP와 yfinance 배치를 서로 독립적으로 제한한다. 한 공급자가 느리다고 이미
# 끝난 다른 시장 값까지 버리면 홈의 모든 지수가 동시에 unavailable이 된다.
_OVERVIEW_KRX_SYMBOLS: tuple[str, ...] = tuple(
    symbol for symbol, _name, market, _currency in _INDEX_DEFINITIONS if market == "KRX"
)
_OVERVIEW_US_BATCH_SYMBOLS: tuple[str, ...] = tuple(
    symbol for symbol, _name, market, _currency in _INDEX_DEFINITIONS if market == "US"
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
# Upbit 마켓 코드는 _INDEX_META가 유일한 근거다. 여기서 따로 적으면 상세 경로와
# 홈 경로가 서로 다른 마켓을 볼 수 있으므로 그대로 끌어온다(누락은 import 시점에
# KeyError로 드러난다).
_UPBIT_MARKET_BY_SYMBOL: dict[str, str] = {
    key: _INDEX_META[key]["upbit_market"]
    for key, _name, _group, _unit, provider in _INDICATOR_DEFINITIONS
    if provider == "upbit"
}

_DetailProvider = Literal["naver", "yfinance", "upbit"]
# 상세 이력 소스. 토스 시장지표는 현재값 endpoint만 있고 차트 소스가 없으므로
# 상세 화이트리스트에서 빠진다(앱에서도 이 심볼은 누를 수 없다).
_DETAIL_PROVIDER_BY_INDICATOR_PROVIDER: dict[
    _IndicatorProvider,
    _DetailProvider | None,
] = {
    "us_batch": "yfinance",
    "upbit": "upbit",
    "toss": None,
}
# 분봉이 있는 소스만 "1D"를 노출한다. 네이버 지수 API에는 분봉 endpoint가 없어
# KOSPI/KOSDAQ은 "1D"를 제공하지 않는다.
_INTRADAY_DETAIL_PROVIDERS: frozenset[str] = frozenset({"yfinance", "upbit"})


@dataclass(frozen=True, slots=True)
class _IndexDetailDefinition:
    """상세 한 심볼의 단일 정의.

    홈 격자 정의(``_INDEX_DEFINITIONS``/``_INDICATOR_DEFINITIONS``)에서 전부
    파생한다. 상세와 홈이 서로 다른 이름·단위·공급자를 갖는 상태가 만들어지지
    않게 하려는 것이므로 여기에 심볼을 직접 적지 않는다.
    """

    name: str
    kind: MarketIndexKind
    market: Literal["KRX", "US", "GLOBAL"]
    currency: _IndexCurrency | None
    unit: MarketIndicatorUnit
    group: MarketIndicatorGroup | None
    provider: _DetailProvider
    # 지표만 값이 있다. 상태 판정을 홈 격자와 같은 규칙으로 돌리는 데 쓴다.
    indicator_provider: _IndicatorProvider | None
    supported_ranges: tuple[MarketIndexRange, ...]


def _detail_supported_ranges(
    provider: _DetailProvider,
) -> tuple[MarketIndexRange, ...]:
    if provider in _INTRADAY_DETAIL_PROVIDERS:
        return ("1D", *_DAILY_INDEX_RANGES)
    return _DAILY_INDEX_RANGES


def _build_index_detail_definitions() -> dict[str, _IndexDetailDefinition]:
    definitions: dict[str, _IndexDetailDefinition] = {}
    for symbol, name, market, currency in _INDEX_DEFINITIONS:
        provider: _DetailProvider = "naver" if market == "KRX" else "yfinance"
        definitions[symbol] = _IndexDetailDefinition(
            name=name,
            kind="INDEX",
            market=market,
            currency=currency,
            unit="POINT",
            group=None,
            provider=provider,
            indicator_provider=None,
            supported_ranges=_detail_supported_ranges(provider),
        )
    for key, name, group, unit, indicator_provider in _INDICATOR_DEFINITIONS:
        detail_provider = _DETAIL_PROVIDER_BY_INDICATOR_PROVIDER[indicator_provider]
        if detail_provider is None:
            continue
        definitions[key] = _IndexDetailDefinition(
            name=name,
            kind="INDICATOR",
            # 지표는 한 거래소 세션에 속하지 않는다. 원자재·금리 선물은 미국
            # 정규장 밖에도 거래되고 암호화폐는 24시간이다. 통화도 붙이지 않고
            # unit만으로 값의 의미를 전달한다.
            market="GLOBAL",
            currency=None,
            unit=unit,
            group=group,
            provider=detail_provider,
            indicator_provider=indicator_provider,
            supported_ranges=_detail_supported_ranges(detail_provider),
        )
    return definitions


_INDEX_DETAIL_DEFINITIONS = _build_index_detail_definitions()
_SessionStates = dict[str, MarketSessionState | None]
# 진행 중 세션. 이 상태의 시장은 완료 정규장 봉으로 고정하지 않고 공급자의 현재
# 스냅샷을 쓴다. 완료봉 고정은 CLOSED 화면에서 값·등락·기준 시각을 한 세션으로
# 묶어 주지만, 진행 중 세션에 그대로 걸면 세션 표기만 현재로 바뀐 직전 정규장
# 스냅샷이 남는다(운영 관측: sessionState=REGULAR인데 asOf가 직전 거래일).
# CLOSED와 미확인(None)은 기존 고정 규칙을 유지한다.
_LIVE_SESSION_STATES: frozenset[MarketSessionState] = frozenset(
    {"REGULAR", "PRE_MARKET", "AFTER_MARKET", "DAY_MARKET"}
)

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


def _live_markets(sessions: _SessionStates) -> frozenset[str]:
    """진행 중 세션인 시장 키. 지수 소스 선택의 유일한 근거다."""
    return frozenset(
        market for market, state in sessions.items() if state in _LIVE_SESSION_STATES
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
    """일봉 한 행의 거래일 라벨. 시각은 의미가 없으므로 자정으로 정규화한다."""
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


def _history_moment_bucket(value: object) -> str | None:
    """분봉 한 행의 체결 시각. timezone을 증명하지 못하면 버린다.

    naive 값에 임의로 UTC를 붙이면 없는 근거를 만드는 것이고, 날짜로 뭉개면 같은
    날 봉들이 서로를 덮어쓴다. 둘 다 하지 않고 그 행만 버린다.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return _datetime_text(parsed)


def _index_candles(
    result: object, *, intraday: bool = False
) -> list[MarketIndexCandle]:
    """이력 행을 와이어 캔들로 옮긴다.

    일봉은 같은 거래일 행이 여러 번 오면 마지막 행이 이긴다(공급자가 같은 날을
    갱신해 보내는 경우가 있다). 분봉은 시각까지 키로 쓰므로 같은 날 여러 봉이
    그대로 남는다.
    """
    if not isinstance(result, dict):
        return []
    history = result.get("history")
    if not isinstance(history, list):
        return []

    by_time: dict[str, MarketIndexCandle] = {}
    for row in history:
        if not isinstance(row, dict):
            continue
        bucket = (
            _history_moment_bucket(row.get("date"))
            if intraday
            else _history_date_bucket(row.get("date"))
        )
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
    definition: _IndexDetailDefinition,
    range_: MarketIndexRange,
    sessions: _SessionStates,
) -> MarketIndexSummary:
    # 지표는 특정 거래소 세션에 속하지 않으므로 sessionState를 노출하지 않는다.
    # 세션 딕셔너리를 "GLOBAL" 키로 조회하는 경로도 만들지 않는다.
    session_state = sessions[definition.market] if definition.kind == "INDEX" else None
    decimal_places = INDICATOR_DECIMAL_PLACES[definition.unit]
    usable = _usable_index_row(
        _index_rows_by_symbol(result),
        symbol,
        decimal_places=decimal_places,
    )

    price: str | None = None
    change_amount: str | None = None
    change_rate: str | None = None
    as_of: str | None = None
    status: MarketOverviewItemStatus = "unavailable"
    if usable is not None:
        row, price = usable
        change_amount = _quantized_decimal_text(
            row.get("change"),
            decimal_places=decimal_places,
        )
        change_rate = _quantized_decimal_text(
            row.get("change_pct"),
            decimal_places=CHANGE_RATE_DECIMAL_PLACES,
        )
        as_of = _source_as_of(row)
        # 상태 판정 규칙은 홈 격자와 같은 함수를 쓴다. 지표는 provider별로
        # 갈리므로(Upbit 24시간 / 토스 기준시각 유무) 지수 규칙을 강요하지 않는다.
        status = (
            _index_status(row, session_state=session_state)
            if definition.indicator_provider is None
            else _indicator_status(
                row,
                provider=definition.indicator_provider,
                sessions=sessions,
            )
        )

    return MarketIndexSummary(
        symbol=symbol,
        name=definition.name,
        market=definition.market,
        currency=definition.currency,
        price=price,
        change_amount=change_amount,
        change_rate=change_rate,
        as_of=as_of,
        status=status,
        session_state=session_state,
        range=range_,
        kind=definition.kind,
        unit=definition.unit,
        group=definition.group,
        supported_ranges=list(definition.supported_ranges),
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
            raw_row = rows_by_symbol.get(symbol)
            row_error_code = (
                raw_row.get("error_code") if isinstance(raw_row, dict) else None
            )
            errors.append(
                MarketOverviewError(
                    scope="indices",
                    symbol=symbol,
                    code=(
                        row_error_code
                        if row_error_code in {"TIMEOUT", "UNAVAILABLE"}
                        else group_error_code or "UNAVAILABLE"
                    ),
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


def _upbit_indicator_rows(tickers: object) -> dict[str, dict[str, Any]]:
    """Upbit 공개 티커 목록을 지표별 지수 행으로 정규화한다.

    정규화 규칙은 상세 경로와 공용(``_upbit_index_row``)이다. 홈과 상세가 같은
    티커에서 서로 다른 등락률을 만들지 않게 하려는 것이다.
    """
    if not isinstance(tickers, list):
        return {}
    rows_by_market = {
        ticker.get("market"): ticker for ticker in tickers if isinstance(ticker, dict)
    }
    rows: dict[str, dict[str, Any]] = {}
    for symbol, market in _UPBIT_MARKET_BY_SYMBOL.items():
        row = _upbit_index_row(rows_by_market.get(market), symbol)
        if row is not None:
            rows[symbol] = row
    return rows


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
    upbit_tickers: object,
    toss_points: object,
    *,
    sessions: _SessionStates,
    us_batch_error_code: MarketOverviewErrorCode | None = None,
    upbit_error_code: MarketOverviewErrorCode | None = None,
    toss_error_code: MarketOverviewErrorCode | None = None,
) -> tuple[list[MarketIndicatorItem], list[MarketOverviewError]]:
    """비주식 지표 행을 조립한다. 한 공급자가 죽어도 그 항목만 unavailable이다."""

    rows_by_symbol = _index_rows_by_symbol(index_result)
    rows_by_symbol.update(_upbit_indicator_rows(upbit_tickers))
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
            raw_row = rows_by_symbol.get(key)
            row_error_code = (
                raw_row.get("error_code") if isinstance(raw_row, dict) else None
            )
            errors.append(
                MarketOverviewError(
                    scope="indicators",
                    symbol=key,
                    code=(
                        row_error_code
                        if row_error_code in {"TIMEOUT", "UNAVAILABLE"}
                        else group_error_codes[provider] or "UNAVAILABLE"
                    ),
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
            # open.er-api가 CNY를 빼고 응답하면 cny_krw만 None이 된다. 나머지
            # 통화는 그대로 살아 있어야 하므로 여기서 전체를 버리지 않는다.
            (snapshot.cny_krw, snapshot_as_of)
            if snapshot is not None
            else (None, None),
        )
    except DecimalException:
        values = ((None, None), (None, None), (None, None), (None, None))

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


async def _upbit_indicator_tickers() -> list[dict[str, Any]]:
    """Upbit 공개 티커를 지표 심볼 전체에 대해 한 번에 읽는다(인증 불필요)."""
    markets = list(_UPBIT_MARKET_BY_SYMBOL.values())
    rows = await fetch_multiple_tickers(markets)
    return [row for row in rows if isinstance(row, dict)]


async def _toss_indicator_points() -> dict[str, TossIndicatorPoint]:
    """토스 시장지표(한국 국채) 현재가. 토스 게이트가 꺼져 있으면 빈 사전이다."""
    return await toss_market_data.market_indicators(_TOSS_INDICATOR_SYMBOLS)


async def _completed_index_snapshot() -> tuple[
    object,
    list[MarketOverviewSession],
    _SessionStates,
    MarketOverviewErrorCode | None,
]:
    """진행 중 세션은 공급자 현재 스냅샷, 끝난 세션은 완료봉 cutoff를 쓴다."""
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
    live_markets = _live_markets(session_states)

    async def load_group(symbols: tuple[str, ...]) -> object:
        return await asyncio.wait_for(
            handle_get_market_index_current_batch(
                symbols,
                completed_as_of_by_market=completed_as_of_by_market,
                live_markets=live_markets,
            ),
            timeout=INDEX_SOURCE_TIMEOUT_SECONDS,
        )

    group_results = await asyncio.gather(
        load_group(_OVERVIEW_KRX_SYMBOLS),
        load_group(_OVERVIEW_US_BATCH_SYMBOLS),
        return_exceptions=True,
    )
    merged_rows: list[dict[str, Any]] = []
    for symbols, result in zip(
        (_OVERVIEW_KRX_SYMBOLS, _OVERVIEW_US_BATCH_SYMBOLS),
        group_results,
        strict=True,
    ):
        if isinstance(result, BaseException):
            error_code: MarketOverviewErrorCode = (
                "TIMEOUT" if isinstance(result, TimeoutError) else "UNAVAILABLE"
            )
            merged_rows.extend(
                {
                    "symbol": symbol,
                    "unavailable": True,
                    "error_code": error_code,
                }
                for symbol in symbols
            )
            continue
        rows_by_symbol = _index_rows_by_symbol(result)
        merged_rows.extend(
            rows_by_symbol.get(
                symbol,
                {
                    "symbol": symbol,
                    "unavailable": True,
                    "error_code": "UNAVAILABLE",
                },
            )
            for symbol in symbols
        )

    return {"indices": merged_rows}, sessions, session_states, None


async def _build_market_overview() -> MarketOverviewResponse:
    # 환율·암호화폐·시장지표는 지수의 완료 세션 확인과 동시에 조회한다. 지수
    # 가격은 캘린더 cutoff와 공통 선택기가 허용한 target/직전 1개 세션 일봉만 쓴다.
    (
        index_snapshot_result,
        fx_result,
        toss_usd_result,
        upbit_result,
        toss_indicator_result,
    ) = await asyncio.gather(
        _completed_index_snapshot(),
        _bounded(get_open_er_api_usd_snapshot()),
        _bounded(_toss_usd_quote()),
        _bounded(_upbit_indicator_tickers()),
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

    upbit_error_code: MarketOverviewErrorCode | None = None
    if isinstance(upbit_result, BaseException):
        upbit_error_code = (
            "TIMEOUT" if isinstance(upbit_result, TimeoutError) else "UNAVAILABLE"
        )
        upbit_payload: object = None
    else:
        upbit_payload = upbit_result
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
        upbit_payload,
        toss_indicator_payload,
        sessions=session_states,
        us_batch_error_code=index_error_code,
        upbit_error_code=upbit_error_code,
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
    definition = _INDEX_DETAIL_DEFINITIONS[symbol]
    intraday = range_ == "1D"
    moment = datetime.now(UTC)
    _, session_states = await _session_snapshot(moment=moment)
    period, count = (
        (INDEX_INTRADAY_PERIOD, _INTRADAY_CANDLE_COUNT)
        if intraday
        else _INDEX_RANGE_CONFIG[range_]
    )

    # 완료 정규장 cutoff는 주식지수 일봉에만 의미가 있다. 분봉은 진행 중 세션을
    # 보는 것이 목적이고, 지표(원자재·금리 선물·암호화폐)는 KRX/US 정규장 밖에도
    # 거래되므로 cutoff를 걸면 값이 통째로 사라진다. 그런 심볼은 None을 넘겨
    # 공급자의 실시간 경로를 그대로 쓴다.
    completed_as_of_by_market: dict[str, datetime] | None = None
    live_markets: frozenset[str] = frozenset()
    if not intraday and definition.kind == "INDEX":
        # 진행 중 세션이면 완료봉으로 고정하지 않는다. 홈 격자와 같은 판정을
        # 쓰므로 상세와 홈이 서로 다른 세션 스냅샷을 보여 주지 않는다.
        live_markets = _live_markets(session_states) & {definition.market}
        completed_window = await get_latest_completed_regular_window_from_toss(
            "kr" if definition.market == "KRX" else "us",
            moment,
        )
        completed_as_of_by_market = (
            {definition.market: completed_window.end}
            if completed_window is not None
            else {}
        )
    try:
        result: object = await _bounded(
            handle_get_market_index(
                symbol=symbol,
                period=period,
                count=count,
                completed_as_of_by_market=completed_as_of_by_market,
                live_markets=live_markets,
            )
        )
    except Exception:
        result = None

    return MarketIndexDetailResponse(
        summary=_index_summary(
            result,
            symbol=symbol,
            definition=definition,
            range_=range_,
            sessions=session_states,
        ),
        candles=_index_candles(result, intraday=intraday),
    )


async def get_market_index_detail(
    symbol: str,
    range_: MarketIndexRange,
) -> MarketIndexDetailResponse:
    """Return one sanitized, keyed 15-second index detail snapshot."""

    normalized_symbol = symbol.strip().upper()
    definition = _INDEX_DETAIL_DEFINITIONS.get(normalized_symbol)
    if definition is None:
        raise MobileApiError(404, "UNKNOWN_INDEX", "지원하지 않는 지수입니다.")
    if range_ not in definition.supported_ranges:
        # supportedRanges가 클라이언트 range 칩의 유일한 근거이므로, 목록에 없는
        # range를 다른 주기로 바꿔 응답하지 않고 거절한다.
        raise MobileApiError(
            400,
            "UNSUPPORTED_RANGE",
            "이 심볼은 해당 기간 차트를 제공하지 않습니다.",
        )

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
