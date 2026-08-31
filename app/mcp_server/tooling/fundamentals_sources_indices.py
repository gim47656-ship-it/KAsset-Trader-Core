"""Market index provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import datetime
import math
from collections.abc import Collection, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

from app.mcp_server.tooling.market_session import DATA_STATE_MARKET_CLOSED
from app.monitoring import yfinance_tracing_session
from app.services.brokers.upbit.client import (
    fetch_minute_candles as upbit_fetch_minute_candles,
)
from app.services.brokers.upbit.client import (
    fetch_multiple_tickers as upbit_fetch_multiple_tickers,
)
from app.services.brokers.upbit.client import (
    fetch_ohlcv as upbit_fetch_ohlcv,
)
from app.services.external.btc_dominance import fetch_btc_dominance
from app.services.market_events.session_calendar import previous_trading_session

_INDEX_META: dict[str, dict[str, str]] = {
    "KOSPI": {"name": "코스피", "source": "naver", "naver_code": "KOSPI"},
    "KOSDAQ": {"name": "코스닥", "source": "naver", "naver_code": "KOSDAQ"},
    "SPX": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "SP500": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "NASDAQ": {"name": "NASDAQ Composite", "source": "yfinance", "yf_ticker": "^IXIC"},
    "DJI": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "DOW": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "VIX": {"name": "CBOE 변동성지수(VIX)", "source": "yfinance", "yf_ticker": "^VIX"},
    "RUT": {"name": "러셀2000", "source": "yfinance", "yf_ticker": "^RUT"},
    "SOX": {
        "name": "필라델피아 반도체지수",
        "source": "yfinance",
        "yf_ticker": "^SOX",
    },
    # ^TNX는 가격이 아니라 미국 10년물 금리(%)를 그대로 싣는다. 통화 환산·가격
    # 취급을 하면 값의 의미가 깨지므로 소비자가 % 단위로 읽어야 한다.
    "US10Y": {"name": "미국 10년물 금리", "source": "yfinance", "yf_ticker": "^TNX"},
    "WTI": {"name": "WTI 유가", "source": "yfinance", "yf_ticker": "CL=F"},
    "BRENT": {"name": "브렌트유", "source": "yfinance", "yf_ticker": "BZ=F"},
    "GOLD": {"name": "금", "source": "yfinance", "yf_ticker": "GC=F"},
    # 달러인덱스는 Yahoo가 현물(DX-Y.NYB)을 자주 비우므로 선물(DX=F)을 예비
    # 티커로 둔다. 후보 순서는 fx_dashboard_service의 YAHOO_GLOBAL_DOLLAR와 같다.
    "DXY": {
        "name": "달러인덱스",
        "source": "yfinance",
        "yf_ticker": "DX-Y.NYB",
        "yf_fallback_ticker": "DX=F",
    },
    # 암호화폐는 24시간 시장이라 거래소 세션 cutoff가 없다. 현재가는 Upbit 공개
    # 티커, 이력은 Upbit 캔들을 그대로 쓴다(원화 표기).
    "BTC": {"name": "비트코인", "source": "upbit", "upbit_market": "KRW-BTC"},
    "ETH": {"name": "이더리움", "source": "upbit", "upbit_market": "KRW-ETH"},
    "CRYPTO": {
        "name": "암호화폐 총 시가총액",
        "source": "coingecko",
        "cg_metric": "total_market_cap",
    },
    "BTC.D": {
        "name": "BTC 도미넌스",
        "source": "coingecko",
        "cg_metric": "btc_dominance",
    },
}

# 무인자 기본 배치는 여전히 주식 지수만 담는다(금리·원자재·암호화폐는 제외).
# 여기 없는 심볼은 /market/overview에서 unavailable로 떨어진다.
_DEFAULT_INDICES = ["KOSPI", "KOSDAQ", "SPX", "NASDAQ", "DJI", "RUT", "SOX"]

NAVER_INDEX_BASIC_URL = "https://m.stock.naver.com/api/index/{code}/basic"
NAVER_INDEX_PRICE_URL = "https://m.stock.naver.com/api/index/{code}/price"

_KST = ZoneInfo("Asia/Seoul")
_US_EASTERN = ZoneInfo("America/New_York")

# 지수/지표 상세의 "1일" 차트용 봉 주기. 소비자(market_overview)가 같은 토큰을
# 쓰도록 여기서 한 번만 정의한다.
INDEX_INTRADAY_PERIOD = "10m"
_INTRADAY_YF_INTERVAL = "10m"
# Yahoo 분봉 요청 창. 주말·공휴일을 건너 최근 거래일 하루를 확보하려면 며칠은
# 필요하다(10분봉의 Yahoo 보관 한도는 60일이라 7일은 안전 범위다).
_INTRADAY_LOOKBACK_DAYS = 7
_UPBIT_INTRADAY_UNIT_MINUTES = 10
_UPBIT_MAX_CANDLE_COUNT = 200


def _parse_naver_num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


async def _fetch_index_kr_current(naver_code: str, name: str) -> dict[str, Any]:
    basic_url = NAVER_INDEX_BASIC_URL.format(code=naver_code)
    price_url = NAVER_INDEX_PRICE_URL.format(code=naver_code)

    async with httpx.AsyncClient(timeout=10) as cli:
        basic_resp, price_resp = await asyncio.gather(
            cli.get(basic_url, headers={"User-Agent": "Mozilla/5.0"}),
            cli.get(
                price_url,
                params={"pageSize": 1, "page": 1},
                headers={"User-Agent": "Mozilla/5.0"},
            ),
        )
        basic_resp.raise_for_status()
        price_resp.raise_for_status()

        basic = basic_resp.json()
        price_list = price_resp.json()

    latest = price_list[0] if price_list else {}

    return {
        "symbol": naver_code,
        "name": name,
        "current": _parse_naver_num(basic.get("closePrice")),
        "change": _parse_naver_num(basic.get("compareToPreviousClosePrice")),
        "change_pct": _parse_naver_num(basic.get("fluctuationsRatio")),
        "open": _parse_naver_num(latest.get("openPrice")),
        "high": _parse_naver_num(latest.get("highPrice")),
        "low": _parse_naver_num(latest.get("lowPrice")),
        "volume": _parse_naver_int(latest.get("accumulatedTradingVolume")),
        # ROB-731: the Naver basic payload timestamps the quote it derives the
        # signed change_pct from (minute-granular, tz-aware ISO). Surface it so
        # callers can detect intraday lag — near flat, a stale quote inverts the
        # sign of change_pct vs live (KOSDAQ +0.18 vs −0.46 at 09:10 KST).
        "quote_asof": basic.get("localTradedAt"),
        "source": "naver",
    }


def _naver_completed_index_row(
    price_list: object,
    *,
    naver_code: str,
    name: str,
    completed_as_of: datetime.datetime,
) -> dict[str, Any]:
    """완료된 정규장 날짜와 일치하는 네이버 일봉 두 개로 등락을 계산한다."""
    if completed_as_of.tzinfo is None:
        raise ValueError("completed_as_of must be timezone-aware")
    completed_date = completed_as_of.astimezone(_KST).date()
    dated_rows: list[tuple[datetime.date, dict[str, Any]]] = []
    if isinstance(price_list, list):
        for item in price_list:
            if not isinstance(item, dict):
                continue
            raw_date = item.get("localTradedAt")
            if not isinstance(raw_date, str):
                continue
            try:
                trading_date = datetime.date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            dated_rows.append((trading_date, item))

    dated_rows.sort(key=lambda pair: pair[0])
    latest_pair = next(
        (pair for pair in reversed(dated_rows) if pair[0] == completed_date),
        None,
    )
    if latest_pair is None:
        return {
            "symbol": naver_code,
            "name": name,
            "current": None,
            "previous_close": None,
            "change": None,
            "change_pct": None,
            "source": "naver",
            "unavailable": True,
        }

    latest_date, latest = latest_pair
    previous = next(
        (
            item
            for trading_date, item in reversed(dated_rows)
            if trading_date < latest_date
        ),
        None,
    )
    current = _parse_naver_num(latest.get("closePrice"))
    previous_close = (
        _parse_naver_num(previous.get("closePrice")) if previous is not None else None
    )
    change: float | None = None
    change_pct: float | None = None
    if current is not None and previous_close is not None and previous_close != 0:
        change = round(current - previous_close, 2)
        change_pct = round((current - previous_close) / previous_close * 100, 2)

    return {
        "symbol": naver_code,
        "name": name,
        "current": current,
        "previous_close": previous_close,
        "change": change,
        "change_pct": change_pct,
        "open": _parse_naver_num(latest.get("openPrice")),
        "high": _parse_naver_num(latest.get("highPrice")),
        "low": _parse_naver_num(latest.get("lowPrice")),
        "volume": _parse_naver_int(latest.get("accumulatedTradingVolume")),
        "quote_asof": completed_as_of.isoformat(),
        "source": "naver",
        "data_state": DATA_STATE_MARKET_CLOSED,
        **({"unavailable": True} if current is None else {}),
    }


async def _fetch_index_kr_completed(
    naver_code: str,
    name: str,
    *,
    completed_as_of: datetime.datetime,
) -> dict[str, Any]:
    """완료된 KRX 정규장 일봉과 그 직전 일봉만 조회한다."""
    price_url = NAVER_INDEX_PRICE_URL.format(code=naver_code)
    async with httpx.AsyncClient(timeout=10) as cli:
        response = await cli.get(
            price_url,
            params={"pageSize": 3, "page": 1, "timeframe": "day"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        price_list = response.json()
    return _naver_completed_index_row(
        price_list,
        naver_code=naver_code,
        name=name,
        completed_as_of=completed_as_of,
    )


async def _fetch_index_kr_history(
    naver_code: str,
    count: int,
    period: str,
    *,
    completed_as_of: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    url = NAVER_INDEX_PRICE_URL.format(code=naver_code)
    period_map = {"day": "day", "week": "week", "month": "month"}
    timeframe = period_map.get(period, "day")
    completed_date: datetime.date | None = None
    if completed_as_of is not None:
        if completed_as_of.tzinfo is None:
            raise ValueError("completed_as_of must be timezone-aware")
        completed_date = completed_as_of.astimezone(_KST).date()

    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(
            url,
            params={
                "pageSize": count + 1 if completed_date is not None else count,
                "page": 1,
                "timeframe": timeframe,
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()

    history: list[dict[str, Any]] = []
    has_completed_date = False
    for item in data:
        raw_date = item.get("localTradedAt", "")
        try:
            trading_date = datetime.date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        if completed_date is not None:
            if trading_date > completed_date:
                continue
            has_completed_date = has_completed_date or trading_date == completed_date
        history.append(
            {
                "date": raw_date,
                "close": _parse_naver_num(item.get("closePrice")),
                "open": _parse_naver_num(item.get("openPrice")),
                "high": _parse_naver_num(item.get("highPrice")),
                "low": _parse_naver_num(item.get("lowPrice")),
                "volume": _parse_naver_int(item.get("accumulatedTradingVolume")),
            }
        )

    if completed_date is not None and period == "day" and not has_completed_date:
        return []
    return history[:count]


def _safe_fast_info_attr(info: Any, name: str) -> Any:
    """Read a yfinance ``fast_info`` attribute without propagating its internals.

    ROB-365 hotfix: ``getattr(info, name, default)`` only shields against
    ``AttributeError``. yfinance's ``FastInfo`` computes values lazily on access
    and can raise ``TypeError: 'NoneType' object is not subscriptable`` (and
    similar) when the underlying price frame is missing — that propagates out of
    ``getattr`` and crashes the caller. Treat any such failure as "unavailable".
    """
    if info is None:
        return None
    try:
        return getattr(info, name, None)
    except Exception:  # noqa: BLE001 — FastInfo internals can raise non-AttributeError
        return None


async def _index_current_from_history(yf_ticker: str) -> dict[str, Any] | None:
    """Latest-history-row fallback for a US index current quote (ROB-365 hotfix).

    Returns ``{current, previous_close, open, high, low, volume}`` from the most
    recent daily history row (``previous_close`` from the prior row when present),
    or ``None`` when history is empty/unavailable. Never raises.
    """
    try:
        rows = await _fetch_index_us_history(yf_ticker, 2, "day")
    except Exception:  # noqa: BLE001 — fallback must never raise
        return None
    if not rows:
        return None
    latest = rows[-1]
    current = latest.get("close")
    if current is None:
        return None
    return {
        "current": current,
        "previous_close": rows[-2].get("close") if len(rows) >= 2 else None,
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "volume": latest.get("volume"),
    }


async def _fetch_index_us_current(
    yf_ticker: str, name: str, symbol: str
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def load_fast_info() -> dict[str, Any]:
        # FastInfo resolves attributes lazily. Read them while the traced session
        # is still open; returning the FastInfo object closes its transport first.
        with yfinance_tracing_session() as session:
            info = yf.Ticker(yf_ticker, session=session).fast_info
            return {
                "current": _safe_fast_info_attr(info, "last_price"),
                "previous_close": _safe_fast_info_attr(
                    info,
                    "regular_market_previous_close",
                ),
                "open": _safe_fast_info_attr(info, "open"),
                "high": _safe_fast_info_attr(info, "day_high"),
                "low": _safe_fast_info_attr(info, "day_low"),
                "volume": _safe_fast_info_attr(info, "last_volume"),
            }

    try:
        fast_info = await loop.run_in_executor(None, load_fast_info)
    except Exception:  # noqa: BLE001 — degrade to history fallback below
        fast_info = {}

    current = fast_info.get("current")
    previous_close = fast_info.get("previous_close")
    open_ = fast_info.get("open")
    high = fast_info.get("high")
    low = fast_info.get("low")
    volume = fast_info.get("volume")
    source = "yfinance"

    # ROB-365 hotfix: when fast_info yields no current price (failed internally or
    # returned None), fall back to the latest daily history row.
    if current is None:
        fallback = await _index_current_from_history(yf_ticker)
        if fallback is not None:
            current = fallback["current"]
            if previous_close is None:
                previous_close = fallback["previous_close"]
            open_ = open_ if open_ is not None else fallback["open"]
            high = high if high is not None else fallback["high"]
            low = low if low is not None else fallback["low"]
            volume = volume if volume is not None else fallback["volume"]
            source = "yfinance_history_fallback"

    change: float | None = None
    change_pct: float | None = None
    if current is not None and previous_close is not None and previous_close != 0:
        change = round(current - previous_close, 2)
        change_pct = round((current - previous_close) / previous_close * 100, 2)

    result: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": change,
        "change_pct": change_pct,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "source": source,
    }
    # Fail-closed: neither fast_info nor history yielded a price -> explicit
    # degraded result instead of raising.
    if current is None:
        result["unavailable"] = True
        result["degraded_reason"] = (
            "current quote unavailable: fast_info failed and no history fallback"
        )
    return result


def _batch_ticker_frame(
    frame: pd.DataFrame,
    yf_ticker: str,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    if not isinstance(frame.columns, pd.MultiIndex):
        selected = frame.copy()
    else:
        ticker_level = next(
            (
                level
                for level in range(frame.columns.nlevels)
                if yf_ticker in frame.columns.get_level_values(level)
            ),
            None,
        )
        if ticker_level is None:
            return pd.DataFrame()
        selected = frame.xs(
            yf_ticker,
            axis=1,
            level=ticker_level,
            drop_level=True,
        ).copy()
    if isinstance(selected.columns, pd.MultiIndex):
        selected.columns = [str(column[0]).lower() for column in selected.columns]
    else:
        selected.columns = [str(column).lower() for column in selected.columns]
    return selected


def _batch_float(value: Any) -> float | None:
    if value is None or not pd.notna(value):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _batch_volume(value: Any) -> int | None:
    parsed = _batch_float(value)
    return int(parsed) if parsed is not None else None


def _batch_session_date(value: object) -> datetime.date | None:
    """yfinance 일봉 인덱스의 거래일 라벨을 날짜로 정규화한다.

    일봉 인덱스는 체결 절대시각이 아니라 거래소의 세션 날짜 라벨이다. yfinance가
    그 라벨에 timezone을 붙여도 다른 timezone으로 변환하면 전날로 밀리므로,
    timezone 정보와 무관하게 표시된 calendar date를 그대로 쓴다.
    """
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _normalized_yfinance_history_frame(frame: object) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [str(column[0]).lower() for column in normalized.columns]
    else:
        normalized.columns = [str(column).lower() for column in normalized.columns]
    return normalized


def _metadata_datetime_utc(value: object) -> datetime.datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamp = pd.Timestamp(value, unit="s", tz="UTC")
        else:
            timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        return None
    return timestamp.tz_convert("UTC").to_pydatetime()


def _recover_completed_us_session_rows_from_metadata(
    frame: object,
    metadata: object,
    *,
    completed_as_of: datetime.datetime,
) -> pd.DataFrame:
    """검증 가능한 Yahoo metadata로 null인 완료봉 Close 하나만 복구한다."""
    normalized = _normalized_yfinance_history_frame(frame)
    if (
        completed_as_of.tzinfo is None
        or normalized.empty
        or "close" not in normalized.columns
        or not isinstance(metadata, Mapping)
    ):
        return pd.DataFrame()

    completed_date = completed_as_of.astimezone(_US_EASTERN).date()
    previous_date = previous_trading_session("us", completed_date)
    dated_positions = [
        (position, _batch_session_date(value))
        for position, value in enumerate(normalized.index)
    ]
    completed_positions = [
        position
        for position, session_date in dated_positions
        if session_date == completed_date
    ]
    previous_positions = [
        position
        for position, session_date in dated_positions
        if session_date == previous_date
    ]
    if not completed_positions or not previous_positions:
        return pd.DataFrame()

    completed_position = completed_positions[-1]
    previous_position = previous_positions[-1]
    completed_row = normalized.iloc[completed_position]
    previous_close = _batch_float(normalized.iloc[previous_position].get("close"))
    if (
        _batch_float(completed_row.get("close")) is not None
        or previous_close is None
        or any(
            _batch_float(completed_row.get(field)) is None
            for field in ("open", "high", "low", "volume")
        )
    ):
        return pd.DataFrame()

    current = _batch_float(metadata.get("regularMarketPrice"))
    metadata_previous = _batch_float(metadata.get("previousClose"))
    current_period = metadata.get("currentTradingPeriod")
    regular_period = (
        current_period.get("regular") if isinstance(current_period, Mapping) else None
    )
    regular_end = (
        _metadata_datetime_utc(regular_period.get("end"))
        if isinstance(regular_period, Mapping)
        else None
    )
    low = _batch_float(completed_row.get("low"))
    high = _batch_float(completed_row.get("high"))
    if (
        current is None
        or metadata_previous is None
        or regular_end != completed_as_of.astimezone(datetime.UTC)
        or round(metadata_previous, 2) != round(previous_close, 2)
        or low is None
        or high is None
        or not low <= current <= high
    ):
        return pd.DataFrame()

    selected = normalized.iloc[[previous_position, completed_position]].copy()
    selected.iat[1, selected.columns.get_loc("close")] = current
    return selected


def _fetch_completed_us_metadata_rows(
    yf_ticker: str,
    *,
    completed_as_of: datetime.datetime,
) -> pd.DataFrame:
    with yfinance_tracing_session() as session:
        ticker = yf.Ticker(yf_ticker, session=session)
        frame = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
            keepna=True,
            repair=False,
        )
        metadata = ticker.get_history_metadata(repair=False)
    return _recover_completed_us_session_rows_from_metadata(
        frame,
        metadata,
        completed_as_of=completed_as_of,
    )


def _select_completed_us_session_rows(
    frame: pd.DataFrame,
    *,
    completed_as_of: datetime.datetime,
) -> tuple[pd.DataFrame, datetime.datetime | None]:
    """완료 정규장과 그 직전 XNYS 세션의 확정 일봉만 고른다.

    target ``Close``나 직전 세션 ``Close``가 없으면 빈 결과를 반환한다. CLOSED
    화면에서 더 오래된 상승 봉을 현재 완료봉으로 승격하지 않으며 미래행도 쓰지
    않는다.
    """
    empty = (pd.DataFrame(), None)
    if completed_as_of.tzinfo is None:
        raise ValueError("completed_as_of must be timezone-aware")
    if "close" not in frame.columns:
        return empty

    completed_date = completed_as_of.astimezone(_US_EASTERN).date()
    valid = frame.loc[frame["close"].notna()]
    dated_positions = [
        (position, _batch_session_date(index_value))
        for position, index_value in enumerate(valid.index)
    ]
    completed_positions = [
        position
        for position, session_date in dated_positions
        if session_date == completed_date
    ]
    previous_date = previous_trading_session("us", completed_date)
    previous_positions = [
        position
        for position, session_date in dated_positions
        if session_date == previous_date
    ]
    if not completed_positions or not previous_positions:
        return empty

    return (
        valid.iloc[[previous_positions[-1], completed_positions[-1]]].copy(),
        completed_as_of.astimezone(datetime.UTC),
    )


def _completed_session_bar_as_of(
    frame: pd.DataFrame,
    *,
    completed_as_of: datetime.datetime | None,
) -> datetime.datetime | None:
    """고른 최신 봉이 완료 정규장 봉이면 그 세션 종료 시각을 돌려준다.

    진행 중 세션이라도 공급자가 당일 봉을 아직 만들지 않았으면 최신 봉은 직전
    완료 정규장 봉이다. 그때만 기준 시각을 증명할 수 있다. 당일 형성 중인 봉이면
    증명할 시각이 없으므로 ``None``이고, 없는 근거를 만들지 않는다.
    """
    if completed_as_of is None or frame.empty:
        return None
    if _batch_session_date(frame.index[-1]) != (
        completed_as_of.astimezone(_US_EASTERN).date()
    ):
        return None
    return completed_as_of.astimezone(datetime.UTC)


async def _fetch_indices_us_current_batch(
    symbols: list[str],
    *,
    completed_as_of: datetime.datetime | None = None,
    completed_symbols: Collection[str] | None = None,
    pin_completed_session: bool = True,
) -> list[dict[str, Any]]:
    """Fetch multiple US index rows through one daily download.

    ``completed_symbols``는 일봉이 US 정규장 세션을 따르는 심볼이다(원자재·금리
    선물은 CME 세션이라 여기 들어오지 않는다). 그 심볼을 ``pin_completed_session``
    으로 고정하면 공통 선택기가 target 확정 봉과 직전 세션을 고른다. target
    ``Close``만 비면 Yahoo metadata의 regular end/price/previousClose를 일봉과
    대조해 검증된 종가만 복구한다. 조건이 하나라도 맞지 않으면 unavailable이며
    더 오래된 행을 현재값으로 승격하지 않는다.

    진행 중 세션(``pin_completed_session=False``)은 고정하지 않고 같은 일봉
    프레임의 최신 두 봉을 쓴다. 현재값·등락·등락률이 한 프레임에서 나오므로
    실시간 값에 다른 세션의 등락률이 붙지 않는다. 최신 봉이 완료 정규장 봉이면
    그 세션 종료 시각을 기준 시각으로 싣고, 당일 형성 중인 봉이면 증명할 시각이
    없으므로 비운다.
    """

    normalized_symbols = [symbol.strip().upper() for symbol in symbols]
    definitions: list[tuple[str, str, str]] = []
    for symbol in normalized_symbols:
        meta = _INDEX_META.get(symbol)
        if meta is None or meta.get("source") != "yfinance":
            raise ValueError(f"Unsupported US index symbol '{symbol}'")
        definitions.append((symbol, meta["name"], meta["yf_ticker"]))

    if not definitions:
        return []

    loop = asyncio.get_running_loop()
    yf_tickers = list(
        dict.fromkeys(yf_ticker for _symbol, _name, yf_ticker in definitions)
    )

    def download() -> pd.DataFrame:
        raw_frame = yf.download(
            yf_tickers,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            ignore_tz=True,
            group_by="column",
            session=session,
        )
        return raw_frame if isinstance(raw_frame, pd.DataFrame) else pd.DataFrame()

    with yfinance_tracing_session() as session:
        frame = await loop.run_in_executor(None, download)

    completed_date: datetime.date | None = None
    if completed_as_of is not None:
        if completed_as_of.tzinfo is None:
            raise ValueError("completed_as_of must be timezone-aware")
        completed_date = completed_as_of.astimezone(_US_EASTERN).date()
    completed_symbol_set = (
        {symbol.strip().upper() for symbol in completed_symbols}
        if completed_symbols is not None
        else None
    )
    evidence_by_symbol: dict[str, str] = {}

    selected_by_symbol: dict[
        str,
        tuple[pd.DataFrame, datetime.datetime | None],
    ] = {}
    # 완료봉으로 고정한 심볼. metadata 복구와 예비 티커 규칙이 이 판정에 달려 있다.
    pinned_symbols: set[str] = set()
    for symbol, _name, yf_ticker in definitions:
        follows_us_regular_session = completed_date is not None and (
            completed_symbol_set is None or symbol in completed_symbol_set
        )
        ticker_frame = _batch_ticker_frame(frame, yf_ticker)
        if follows_us_regular_session and pin_completed_session:
            assert completed_as_of is not None
            pinned_symbols.add(symbol)
            selected_by_symbol[symbol] = _select_completed_us_session_rows(
                ticker_frame,
                completed_as_of=completed_as_of,
            )
        elif "close" in ticker_frame.columns:
            live_rows = (
                ticker_frame.loc[ticker_frame["close"].notna()].sort_index().tail(2)
            )
            selected_by_symbol[symbol] = (
                live_rows,
                _completed_session_bar_as_of(
                    live_rows,
                    completed_as_of=completed_as_of,
                )
                if follows_us_regular_session
                else None,
            )
        else:
            selected_by_symbol[symbol] = (pd.DataFrame(), None)
    if completed_as_of is not None and pinned_symbols:
        missing_completed = [
            (symbol, yf_ticker)
            for symbol, _name, yf_ticker in definitions
            if symbol in pinned_symbols and selected_by_symbol[symbol][0].empty
        ]

        async def recover(yf_ticker: str) -> pd.DataFrame:
            try:
                return await asyncio.to_thread(
                    _fetch_completed_us_metadata_rows,
                    yf_ticker,
                    completed_as_of=completed_as_of,
                )
            except Exception:
                return pd.DataFrame()

        recovered_frames = await asyncio.gather(
            *(recover(yf_ticker) for _symbol, yf_ticker in missing_completed)
        )
        for (symbol, _yf_ticker), recovered in zip(
            missing_completed,
            recovered_frames,
            strict=True,
        ):
            selected_by_symbol[symbol] = _select_completed_us_session_rows(
                recovered,
                completed_as_of=completed_as_of,
            )
            if not selected_by_symbol[symbol][0].empty:
                evidence_by_symbol[symbol] = (
                    "regular_market_price_at_completed_session_end"
                )

    # 주 티커가 값을 못 준 심볼 중 예비 티커를 선언한 것만 한 번 더 시도한다.
    # 완료봉으로 고정한 심볼은 metadata 복구 규칙이 따로 있으므로 건드리지 않는다.
    fallback_targets = [
        (symbol, name, _INDEX_META[symbol]["yf_fallback_ticker"])
        for symbol, name, _yf_ticker in definitions
        if selected_by_symbol[symbol][0].empty
        and "yf_fallback_ticker" in _INDEX_META.get(symbol, {})
        and symbol not in pinned_symbols
    ]
    fallback_rows: dict[str, dict[str, Any]] = {}
    if fallback_targets:
        recovered_rows = await asyncio.gather(
            *(
                _fetch_index_us_current(fallback_ticker, name, symbol)
                for symbol, name, fallback_ticker in fallback_targets
            ),
            return_exceptions=True,
        )
        for (symbol, _name, _ticker), recovered_row in zip(
            fallback_targets,
            recovered_rows,
            strict=True,
        ):
            if isinstance(recovered_row, dict) and not recovered_row.get("unavailable"):
                fallback_rows[symbol] = recovered_row

    rows: list[dict[str, Any]] = []
    for symbol, name, _yf_ticker in definitions:
        valid, selected_as_of = selected_by_symbol[symbol]
        if valid.empty:
            fallback_row = fallback_rows.get(symbol)
            if fallback_row is not None:
                rows.append(fallback_row)
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "current": None,
                    "previous_close": None,
                    "change": None,
                    "change_pct": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "source": "yfinance",
                    "unavailable": True,
                }
            )
            continue

        latest = valid.iloc[-1]
        current = _batch_float(latest.get("close"))
        previous_close = (
            _batch_float(valid.iloc[-2].get("close")) if len(valid) >= 2 else None
        )
        change: float | None = None
        change_pct: float | None = None
        if current is not None and previous_close is not None and previous_close != 0:
            change = round(current - previous_close, 2)
            change_pct = round((current - previous_close) / previous_close * 100, 2)

        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "current": current,
                "previous_close": previous_close,
                "change": change,
                "change_pct": change_pct,
                "open": _batch_float(latest.get("open")),
                "high": _batch_float(latest.get("high")),
                "low": _batch_float(latest.get("low")),
                "volume": _batch_volume(latest.get("volume")),
                "source": (
                    "yfinance_history_metadata"
                    if symbol in evidence_by_symbol
                    else "yfinance"
                ),
                # 기준 시각과 완료봉 표기는 고른 봉이 완료 정규장 봉임을
                # 증명했을 때만 붙는다. 진행 중 세션의 형성 중인 봉에는 증명할
                # 시각이 없으므로 둘 다 비운다.
                **(
                    {
                        "quote_asof": selected_as_of.isoformat(),
                        "data_state": DATA_STATE_MARKET_CLOSED,
                    }
                    if selected_as_of is not None
                    else {}
                ),
                **({"unavailable": True} if current is None else {}),
                **(
                    {"close_evidence": evidence_by_symbol[symbol]}
                    if symbol in evidence_by_symbol
                    else {}
                ),
            }
        )
    return rows


async def _fetch_index_us_daily_history(
    yf_ticker: str,
    count: int,
    period: str,
    *,
    completed_as_of: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    period_map = {"day": "1d", "week": "1wk", "month": "1mo"}
    interval = period_map.get(period, "1d")

    completed_date: datetime.date | None = None
    if completed_as_of is not None:
        if completed_as_of.tzinfo is None:
            raise ValueError("completed_as_of must be timezone-aware")
        completed_date = completed_as_of.astimezone(_US_EASTERN).date()
    multiplier = {"day": 2, "week": 10, "month": 40}.get(period, 2)
    end = (
        completed_date + datetime.timedelta(days=1)
        if completed_date is not None
        else datetime.date.today() + datetime.timedelta(days=1)
    )
    start = end - datetime.timedelta(days=count * multiplier)

    def download() -> pd.DataFrame:
        raw_df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=False,
            ignore_tz=True,
            session=session,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()

        return _normalized_yfinance_history_frame(raw_df)

    with yfinance_tracing_session() as session:
        df = await loop.run_in_executor(None, download)
    if df.empty:
        return []

    if completed_date is not None:
        if "close" not in df.columns:
            return []
        valid = df.loc[df["close"].notna()]
        session_dates = [_batch_session_date(value) for value in valid.index]
        if period == "day" and completed_date not in session_dates:
            try:
                recovered = await asyncio.to_thread(
                    _fetch_completed_us_metadata_rows,
                    yf_ticker,
                    completed_as_of=completed_as_of,
                )
            except Exception:
                return []
            if recovered.empty:
                return []
            for recovered_index, recovered_row in recovered.iterrows():
                recovered_date = _batch_session_date(recovered_index)
                matching_positions = [
                    position
                    for position, value in enumerate(df.index)
                    if _batch_session_date(value) == recovered_date
                ]
                if matching_positions:
                    matching_position = matching_positions[-1]
                    for column in ("open", "high", "low", "close", "volume"):
                        if column in df.columns and column in recovered.columns:
                            df.iat[matching_position, df.columns.get_loc(column)] = (
                                recovered_row.get(column)
                            )
                else:
                    df = pd.concat([df, recovered.loc[[recovered_index]]])
            df = df.sort_index()
            valid = df.loc[df["close"].notna()]
            session_dates = [_batch_session_date(value) for value in valid.index]
        positions = [
            position
            for position, session_date in enumerate(session_dates)
            if session_date is not None and session_date <= completed_date
        ]
        if not positions:
            return []
        df = valid.iloc[positions]

    df = df.tail(count).reset_index(names="date")

    history: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        d = row.get("date")
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            date_str = d.strftime("%Y-%m-%d")
        else:
            date_str = str(d)[:10]
        history.append(
            {
                "date": date_str,
                "close": float(row["close"]) if pd.notna(row.get("close")) else None,
                "open": float(row["open"]) if pd.notna(row.get("open")) else None,
                "high": float(row["high"]) if pd.notna(row.get("high")) else None,
                "low": float(row["low"]) if pd.notna(row.get("low")) else None,
                "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
            }
        )
    return history


def _intraday_timestamp_utc(value: object) -> datetime.datetime | None:
    """분봉 인덱스를 UTC 절대시각으로 옮긴다. timezone이 없으면 포기한다.

    일봉 인덱스는 거래일 라벨이라 timezone을 버려도 의미가 남지만, 분봉은 시각
    자체가 데이터다. naive 값을 임의의 timezone으로 가정해 ``Z``를 붙이면 없는
    근거를 만드는 것이므로 그대로 버린다.
    """
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        return None
    return timestamp.tz_convert("UTC").to_pydatetime()


def _intraday_history_row(
    moment: datetime.datetime,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """분봉 한 개를 지수 이력 행으로 옮긴다.

    ``date``에 날짜가 아니라 UTC timestamp를 싣는다. 소비자가 같은 날 여러 봉을
    날짜 하나로 덮어쓰지 않게 하려면 시각이 남아 있어야 한다.
    """
    return {
        "date": moment.isoformat().replace("+00:00", "Z"),
        "close": _batch_float(row.get("close")),
        "open": _batch_float(row.get("open")),
        "high": _batch_float(row.get("high")),
        "low": _batch_float(row.get("low")),
        "volume": _batch_float(row.get("volume")),
    }


async def _fetch_index_us_intraday_history(
    yf_ticker: str,
    count: int,
) -> list[dict[str, Any]]:
    """가장 최근 거래일의 10분봉만 반환한다.

    일봉 경로는 ``ignore_tz=True``로 거래일 라벨만 남기지만 분봉은 절대시각이
    필요하므로 timezone을 유지한 채 받아 UTC로 옮긴다. 주말을 건너 며칠치를
    받아온 뒤 최근 거래일 하루만 남기며, 서로 다른 날의 봉을 이어 붙이지 않는다.
    """
    loop = asyncio.get_running_loop()
    end = datetime.date.today() + datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=_INTRADAY_LOOKBACK_DAYS)

    def download() -> pd.DataFrame:
        raw_df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=_INTRADAY_YF_INTERVAL,
            progress=False,
            auto_adjust=False,
            ignore_tz=False,
            session=session,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()
        return _normalized_yfinance_history_frame(raw_df)

    with yfinance_tracing_session() as session:
        df = await loop.run_in_executor(None, download)
    if df.empty or "close" not in df.columns:
        return []

    valid = df.loc[df["close"].notna()].sort_index()
    if valid.empty:
        return []
    session_dates = [_batch_session_date(value) for value in valid.index]
    latest_session = max(
        (session_date for session_date in session_dates if session_date is not None),
        default=None,
    )
    if latest_session is None:
        return []
    latest = valid.iloc[
        [
            position
            for position, session_date in enumerate(session_dates)
            if session_date == latest_session
        ]
    ].tail(count)

    history: list[dict[str, Any]] = []
    for index_value, row in latest.iterrows():
        moment = _intraday_timestamp_utc(index_value)
        if moment is None:
            continue
        history.append(_intraday_history_row(moment, row))
    return history


async def _fetch_index_us_history(
    yf_ticker: str,
    count: int,
    period: str,
    *,
    completed_as_of: datetime.datetime | None = None,
    fallback_yf_ticker: str | None = None,
) -> list[dict[str, Any]]:
    """지수 이력. ``period``가 분봉 토큰이면 intraday 경로로 갈라진다.

    ``fallback_yf_ticker``는 주 티커가 빈 이력을 줄 때만 한 번 더 시도한다
    (DXY의 현물/선물처럼 Yahoo가 한쪽을 비우는 심볼용). 값이 나오면 fallback을
    타지 않으므로 정상 경로에 추가 왕복이 생기지 않는다.
    """
    if period == INDEX_INTRADAY_PERIOD:
        history = await _fetch_index_us_intraday_history(yf_ticker, count)
        if not history and fallback_yf_ticker:
            history = await _fetch_index_us_intraday_history(fallback_yf_ticker, count)
        return history

    history = await _fetch_index_us_daily_history(
        yf_ticker,
        count,
        period,
        completed_as_of=completed_as_of,
    )
    if not history and fallback_yf_ticker:
        history = await _fetch_index_us_daily_history(
            fallback_yf_ticker,
            count,
            period,
            completed_as_of=completed_as_of,
        )
    return history


def _upbit_kst_timestamp_utc(value: object) -> datetime.datetime | None:
    """Upbit 캔들의 ``candle_date_time_kst``(naive KST)를 UTC로 옮긴다."""
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(_KST)
    return timestamp.tz_convert("UTC").to_pydatetime()


def _upbit_history_rows(frame: object, *, intraday: bool) -> list[dict[str, Any]]:
    """Upbit 캔들 DataFrame을 지수 이력 행 목록으로 옮긴다.

    분봉은 UTC timestamp를, 일봉은 다른 지수와 같은 거래일 라벨(KST 날짜)을
    싣는다. 거래량은 코인 수량이라 소수점이 있으므로 정수로 자르지 않는다.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    key = "datetime" if intraday else "date"
    if key not in frame.columns:
        return []

    rows: list[dict[str, Any]] = []
    for _, row in frame.sort_values(key).iterrows():
        if intraday:
            moment = _upbit_kst_timestamp_utc(row.get(key))
            if moment is None:
                continue
            rows.append(_intraday_history_row(moment, row))
            continue
        session_date = _batch_session_date(row.get(key))
        if session_date is None:
            continue
        rows.append(
            {
                "date": session_date.isoformat(),
                "close": _batch_float(row.get("close")),
                "open": _batch_float(row.get("open")),
                "high": _batch_float(row.get("high")),
                "low": _batch_float(row.get("low")),
                "volume": _batch_float(row.get("volume")),
            }
        )
    return rows


async def _fetch_index_upbit_history(
    upbit_market: str,
    count: int,
    period: str,
) -> list[dict[str, Any]]:
    """Upbit 캔들 이력. 기존 client 함수만 쓰고 새 endpoint를 추가하지 않는다."""
    request_count = min(max(int(count), 1), _UPBIT_MAX_CANDLE_COUNT)
    if period == INDEX_INTRADAY_PERIOD:
        frame = await upbit_fetch_minute_candles(
            upbit_market,
            unit=_UPBIT_INTRADAY_UNIT_MINUTES,
            count=request_count,
        )
        return _upbit_history_rows(frame, intraday=True)
    frame = await upbit_fetch_ohlcv(
        upbit_market,
        days=request_count,
        period=period,
    )
    return _upbit_history_rows(frame, intraday=False)


def _upbit_index_row(ticker: object, symbol: str) -> dict[str, Any] | None:
    """Upbit 공개 티커를 지수 행과 같은 모양으로 정규화한다.

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
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        quote_asof = datetime.datetime.fromtimestamp(
            timestamp / 1000,
            tz=datetime.UTC,
        ).isoformat()

    return {
        "symbol": symbol,
        "current": trade_price,
        "previous_close": previous_close,
        "change": change,
        "change_pct": change_pct,
        "quote_asof": quote_asof,
        "source": "upbit",
    }


async def _fetch_index_upbit_current(
    upbit_market: str,
    name: str,
    symbol: str,
) -> dict[str, Any]:
    """Upbit 공개 티커 한 건으로 현재가 행을 만든다(인증 불필요)."""
    rows = await upbit_fetch_multiple_tickers([upbit_market])
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("market") != upbit_market:
            continue
        row = _upbit_index_row(raw, symbol)
        if row is not None:
            return {**row, "name": name}
    return {
        "symbol": symbol,
        "name": name,
        "source": "upbit",
        "unavailable": True,
    }


async def _fetch_index_crypto_current(
    cg_metric: str, name: str, symbol: str
) -> dict[str, Any]:
    """Crypto market-regime "index" row from CoinGecko /global (cached).

    Row shape matches the KR/US index rows so the snapshot collector and
    MarketStage consume it unchanged. ``total_market_cap`` carries a usable
    24h change_pct (the regime driver); ``btc_dominance`` reports the dominance
    level only (CoinGecko /global has no dominance 24h change) → change_pct is
    None, which the collector intentionally drops and MarketStage skips rather
    than fabricating a flat 0.0%. Raises on an unreachable /global so the
    handler maps it to an error payload (never fabricate values).
    """
    data = await fetch_btc_dominance()
    if not data:
        raise RuntimeError("CoinGecko /global unavailable")

    if cg_metric == "total_market_cap":
        current = data.get("total_market_cap_usd")
        change_pct = data.get("total_market_cap_change_24h")
    elif cg_metric == "btc_dominance":
        current = data.get("btc_dominance")
        change_pct = None
    else:
        raise ValueError(f"unknown cg_metric '{cg_metric}'")

    return {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": None,
        "change_pct": change_pct,
        "source": "coingecko",
    }


__all__ = [
    "INDEX_INTRADAY_PERIOD",
    "_DEFAULT_INDICES",
    "_INDEX_META",
    "_fetch_index_kr_completed",
    "_fetch_index_kr_current",
    "_fetch_index_kr_history",
    "_fetch_index_upbit_current",
    "_fetch_index_upbit_history",
    "_fetch_index_us_current",
    "_fetch_indices_us_current_batch",
    "_fetch_index_us_history",
    "_fetch_index_crypto_current",
    "_upbit_index_row",
]
