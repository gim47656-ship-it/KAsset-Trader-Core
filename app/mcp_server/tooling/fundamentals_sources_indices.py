"""Market index provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import datetime
import math
from collections.abc import Collection
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

from app.mcp_server.tooling.market_session import DATA_STATE_MARKET_CLOSED
from app.monitoring import yfinance_tracing_session
from app.services.external.btc_dominance import fetch_btc_dominance

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
    naver_code: str, count: int, period: str
) -> list[dict[str, Any]]:
    url = NAVER_INDEX_PRICE_URL.format(code=naver_code)
    period_map = {"day": "day", "week": "week", "month": "month"}
    timeframe = period_map.get(period, "day")

    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(
            url,
            params={"pageSize": count, "page": 1, "timeframe": timeframe},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()

    history: list[dict[str, Any]] = []
    for item in data:
        history.append(
            {
                "date": item.get("localTradedAt", ""),
                "close": _parse_naver_num(item.get("closePrice")),
                "open": _parse_naver_num(item.get("openPrice")),
                "high": _parse_naver_num(item.get("highPrice")),
                "low": _parse_naver_num(item.get("lowPrice")),
                "volume": _parse_naver_int(item.get("accumulatedTradingVolume")),
            }
        )
    return history


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
    """yfinance 일봉 인덱스를 미국 거래일로 정규화한다."""
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        return timestamp.date()
    return timestamp.tz_convert(_US_EASTERN).date()


async def _fetch_indices_us_current_batch(
    symbols: list[str],
    *,
    completed_as_of: datetime.datetime | None = None,
    completed_symbols: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch multiple US index rows through one daily download.

    ``completed_as_of`` 대상 심볼은 그 정규장 날짜의 확정 일봉만 고른다. 장중
    형성 중인 마지막 행을 ``current``로 쓰거나 다른 날짜의 종가와 섞지 않는다.
    ``completed_symbols``를 생략하면 요청 심볼 전체가 대상이다.
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
    yf_tickers = [yf_ticker for _symbol, _name, yf_ticker in definitions]

    def download() -> pd.DataFrame:
        raw_frame = yf.download(
            yf_tickers,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=False,
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

    rows: list[dict[str, Any]] = []
    for symbol, name, yf_ticker in definitions:
        use_completed_session = completed_date is not None and (
            completed_symbol_set is None or symbol in completed_symbol_set
        )
        ticker_frame = _batch_ticker_frame(frame, yf_ticker)
        if "close" not in ticker_frame.columns:
            valid = pd.DataFrame()
        else:
            valid = ticker_frame.loc[ticker_frame["close"].notna()].sort_index()
            if use_completed_session:
                valid = valid.loc[
                    [
                        session_date is not None and session_date <= completed_date
                        for session_date in (
                            _batch_session_date(value) for value in valid.index
                        )
                    ]
                ]
                if (
                    valid.empty
                    or _batch_session_date(valid.index[-1]) != completed_date
                ):
                    valid = pd.DataFrame()
            valid = valid.tail(2)
        if valid.empty:
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
                "source": "yfinance",
                **(
                    {"quote_asof": completed_as_of.isoformat()}
                    if use_completed_session and completed_as_of is not None
                    else {}
                ),
                **(
                    {"data_state": DATA_STATE_MARKET_CLOSED}
                    if use_completed_session
                    else {}
                ),
                **({"unavailable": True} if current is None else {}),
            }
        )
    return rows


async def _fetch_index_us_history(
    yf_ticker: str, count: int, period: str
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    period_map = {"day": "1d", "week": "1wk", "month": "1mo"}
    interval = period_map.get(period, "1d")

    multiplier = {"day": 2, "week": 10, "month": 40}.get(period, 2)
    end = datetime.date.today() + datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=count * multiplier)

    def download() -> pd.DataFrame:
        raw_df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=False,
            session=session,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()

        df = raw_df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return df.reset_index(names="date")

    with yfinance_tracing_session() as session:
        df = await loop.run_in_executor(None, download)
    if df.empty:
        return []

    df = df.tail(count).reset_index(drop=True)

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
    "_DEFAULT_INDICES",
    "_INDEX_META",
    "_fetch_index_kr_completed",
    "_fetch_index_kr_current",
    "_fetch_index_kr_history",
    "_fetch_index_us_current",
    "_fetch_indices_us_current_batch",
    "_fetch_index_us_history",
    "_fetch_index_crypto_current",
]
