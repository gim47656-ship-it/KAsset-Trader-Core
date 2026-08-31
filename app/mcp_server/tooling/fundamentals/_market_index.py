"""Handler for get_market_index tool."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.fundamentals_sources_indices import (
    _DEFAULT_INDICES,
    _INDEX_META,
    INDEX_INTRADAY_PERIOD,
    _fetch_index_crypto_current,
    _fetch_index_kr_completed,
    _fetch_index_kr_current,
    _fetch_index_kr_history,
    _fetch_index_upbit_current,
    _fetch_index_upbit_history,
    _fetch_index_us_current,
    _fetch_index_us_history,
    _fetch_indices_us_current_batch,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_STALE,
    kr_market_data_state,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload

_KR_INDEX_LAGGING_REASON = "kr_index_fresh_clock_payload_lagging"

# ROB-731: during an OPEN KRX session the Naver basic payload can lag real time.
# Near flat, a stale quote inverts the sign of change_pct vs live (KOSDAQ +0.18
# vs −0.46 at 09:10 KST 2026-07-06). Naver stamps the quote it derives the
# change from at minute granularity, so allow one minute of natural granularity
# plus a small margin before calling the quote stale. Tunable pending live
# measurement of the real intraday lag distribution.
_KR_INDEX_QUOTE_LAG_STALE_SECONDS = 120
_KR_INDEX_QUOTE_LAG_REASON = "kr_index_quote_lagging"
_MAX_INDEX_HISTORY_COUNT = 126
# 분봉은 하루치가 일봉 126개보다 많다(10분봉 24시간 = 144개). Upbit 분봉 API
# 상한이 200이므로 그 이상은 어차피 못 받는다.
_MAX_INDEX_INTRADAY_COUNT = 200


def _parse_quote_asof(value: Any) -> datetime | None:
    """Parse a Naver ``localTradedAt`` quote timestamp into a tz-aware datetime.

    Naver returns a full ISO timestamp with a ``+09:00`` offset during the
    session (e.g. ``2026-07-06T11:19:00+09:00``). Date-only strings (the daily
    price rows) and unparseable values yield ``None`` — a missing timestamp
    cannot be used to assess lag.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A bare date carries no intraday time → not usable for lag detection.
    if len(value) <= 10:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _kr_index_quote_lag_seconds(quote_asof: Any) -> int | None:
    """Seconds the Naver quote lags ``now_kst`` (None if unknown/in the future)."""
    parsed = _parse_quote_asof(quote_asof)
    if parsed is None:
        return None
    lag = (now_kst() - parsed).total_seconds()
    if lag < 0:
        return None
    return int(lag)


def _is_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and value == 0


def _has_distinct_prices(left: Any, right: Any) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return left != right


def _is_fresh_clock_lagging_kr_index(index: dict[str, Any]) -> bool:
    return (
        _is_zero(index.get("change"))
        and _is_zero(index.get("change_pct"))
        and _has_distinct_prices(index.get("open"), index.get("current"))
    )


def _tag_kr_index_data_state(index: Any) -> Any:
    """ROB-464: tag KR (naver) index dicts with the KRX session data_state.

    Pre-market / closed sessions otherwise return change_pct=0 (frozen at the
    prior close), which reads as a real flat session.
    """
    if isinstance(index, dict) and "error" not in index:
        data_state = kr_market_data_state()
        if data_state == DATA_STATE_FRESH:
            if _is_fresh_clock_lagging_kr_index(index):
                # ROB-464: all-zero change frozen at the prior close.
                data_state = DATA_STATE_STALE
                index["data_state_reason"] = _KR_INDEX_LAGGING_REASON
                index["as_of"] = now_kst().isoformat()
            else:
                # ROB-731: minute-granular quote lag. The signed change_pct is
                # only as fresh as the quote it was derived from; when that lags
                # real time the near-flat sign can be inverted vs live.
                lag = _kr_index_quote_lag_seconds(index.get("quote_asof"))
                if lag is not None and lag > _KR_INDEX_QUOTE_LAG_STALE_SECONDS:
                    data_state = DATA_STATE_STALE
                    index["data_state_reason"] = _KR_INDEX_QUOTE_LAG_REASON
                    index["quote_lag_seconds"] = lag
        index["data_state"] = data_state
    return index


async def handle_get_market_index(
    symbol: str | None = None,
    period: str = "day",
    count: int = 20,
    *,
    completed_as_of_by_market: Mapping[str, datetime] | None = None,
    live_markets: Collection[str] | None = None,
) -> dict[str, Any]:
    """지수 현재 요약과 범위 이력을 조회한다.

    기본 호출은 기존 현재가 의미를 유지한다. 완료 시각 사전을 넘기면 요약과
    이력이 동일한 완료 정규장 cutoff를 사용한다. target 봉이나 직전 세션 봉을
    증명하지 못하면 현재값과 이력을 unavailable로 두며, 진행 중·미래·더 오래된
    봉으로 대체하지 않는다.

    ``live_markets``에 든 시장은 진행 중 세션이므로 완료봉으로 고정하지 않고
    공급자의 현재 스냅샷을 쓴다. 현재값과 등락·등락률·기준 시각이 같은 스냅샷에서
    나오게 하려는 것이다(실시간 값에 직전 정규장 등락률을 붙이지 않는다). 이력도
    같은 판정을 따라 진행 중 세션 봉을 잘라내지 않는다.

    ``period``가 ``INDEX_INTRADAY_PERIOD``면 이력이 분봉이 되고 ``date``에 날짜
    라벨이 아니라 UTC timestamp가 실린다. 분봉은 완료 세션 cutoff와 결합하지
    않는다(진행 중 세션을 보는 것이 목적이다).
    """
    live = frozenset(live_markets or ())
    period = (period or "day").strip().lower()
    if period not in ("day", "week", "month", INDEX_INTRADAY_PERIOD):
        raise ValueError(
            f"period must be 'day', 'week', 'month', or '{INDEX_INTRADAY_PERIOD}'"
        )

    capped_count = min(
        max(count, 1),
        _MAX_INDEX_INTRADAY_COUNT
        if period == INDEX_INTRADAY_PERIOD
        else _MAX_INDEX_HISTORY_COUNT,
    )

    if symbol:
        sym = symbol.strip().upper()
        meta = _INDEX_META.get(sym)
        if meta is None:
            raise ValueError(
                f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
            )
        if period == INDEX_INTRADAY_PERIOD and meta["source"] not in (
            "yfinance",
            "upbit",
        ):
            # 네이버 지수 API에는 분봉 endpoint가 없고 CoinGecko /global은 이력이
            # 없다. 없는 소스를 일봉으로 대체하면 "1일"이 아닌 차트가 나온다.
            raise ValueError(
                f"index symbol '{sym}' has no intraday source for period "
                f"'{INDEX_INTRADAY_PERIOD}'"
            )

        def unavailable_current() -> dict[str, Any]:
            return {
                "symbol": sym,
                "name": meta["name"],
                "source": meta["source"],
                "unavailable": True,
            }

        try:
            if meta["source"] == "naver":

                async def load_kr_current() -> dict[str, Any]:
                    if completed_as_of_by_market is None or "KRX" in live:
                        return _tag_kr_index_data_state(
                            await _fetch_index_kr_current(
                                meta["naver_code"], meta["name"]
                            )
                        )
                    completed_as_of = completed_as_of_by_market.get("KRX")
                    if completed_as_of is None:
                        return unavailable_current()
                    return await _fetch_index_kr_completed(
                        meta["naver_code"],
                        meta["name"],
                        completed_as_of=completed_as_of,
                    )

                async def load_kr_history() -> list[dict[str, Any]]:
                    if completed_as_of_by_market is None or "KRX" in live:
                        return await _fetch_index_kr_history(
                            meta["naver_code"], capped_count, period
                        )
                    completed_as_of = completed_as_of_by_market.get("KRX")
                    if completed_as_of is None:
                        return []
                    return await _fetch_index_kr_history(
                        meta["naver_code"],
                        capped_count,
                        period,
                        completed_as_of=completed_as_of,
                    )

                current_data, history = await asyncio.gather(
                    load_kr_current(),
                    load_kr_history(),
                )
                return {"indices": [current_data], "history": history}
            if meta["source"] == "coingecko":
                current_data = await _fetch_index_crypto_current(
                    meta["cg_metric"], meta["name"], sym
                )
                return {"indices": [current_data], "history": []}
            if meta["source"] == "upbit":
                # 암호화폐는 24시간 시장이라 완료 정규장 cutoff가 없다. 넘어온
                # completed_as_of_by_market은 KRX/US 전용이므로 무시한다.
                current_data, history = await asyncio.gather(
                    _fetch_index_upbit_current(
                        meta["upbit_market"],
                        meta["name"],
                        sym,
                    ),
                    _fetch_index_upbit_history(
                        meta["upbit_market"],
                        capped_count,
                        period,
                    ),
                )
                return {"indices": [current_data], "history": history}

            async def load_us_current() -> dict[str, Any]:
                if completed_as_of_by_market is None or period == INDEX_INTRADAY_PERIOD:
                    # 분봉 차트는 진행 중 세션을 보는 것이 목적이므로 요약도
                    # 완료봉이 아니라 실시간 현재가를 쓴다.
                    return await _fetch_index_us_current(
                        meta["yf_ticker"], meta["name"], sym
                    )
                completed_as_of = completed_as_of_by_market.get("US")
                us_live = "US" in live
                if completed_as_of is None:
                    if not us_live:
                        return unavailable_current()
                    # 진행 중 세션인데 캘린더가 완료 창을 주지 못한 경우다. 고정할
                    # 근거가 없으므로 공급자 현재가를 그대로 쓴다.
                    return await _fetch_index_us_current(
                        meta["yf_ticker"], meta["name"], sym
                    )
                # 진행 중 세션에서도 홈 격자와 같은 배치 선택기를 쓴다. 상세와
                # 홈이 같은 프레임에서 값·등락·기준 시각을 뽑게 하려는 것이다.
                rows = await _fetch_indices_us_current_batch(
                    [sym],
                    completed_as_of=completed_as_of,
                    completed_symbols=(sym,),
                    pin_completed_session=not us_live,
                )
                return rows[0] if rows else unavailable_current()

            async def load_us_history() -> list[dict[str, Any]]:
                fallback_yf_ticker = meta.get("yf_fallback_ticker")
                if (
                    completed_as_of_by_market is None
                    or period == INDEX_INTRADAY_PERIOD
                    or "US" in live
                ):
                    return await _fetch_index_us_history(
                        meta["yf_ticker"],
                        capped_count,
                        period,
                        fallback_yf_ticker=fallback_yf_ticker,
                    )
                completed_as_of = completed_as_of_by_market.get("US")
                if completed_as_of is None:
                    return []
                return await _fetch_index_us_history(
                    meta["yf_ticker"],
                    capped_count,
                    period,
                    completed_as_of=completed_as_of,
                    fallback_yf_ticker=fallback_yf_ticker,
                )

            current_data, history = await asyncio.gather(
                load_us_current(),
                load_us_history(),
            )
            return {"indices": [current_data], "history": history}
        except Exception as exc:
            return _error_payload(source=meta["source"], message=str(exc), symbol=sym)

    if completed_as_of_by_market is None:
        return await handle_get_market_index_current_batch(
            _DEFAULT_INDICES,
            live_markets=live,
        )
    return await handle_get_market_index_current_batch(
        _DEFAULT_INDICES,
        completed_as_of_by_market=completed_as_of_by_market,
        live_markets=live,
    )


async def handle_get_market_index_current_batch(
    symbols: Sequence[str],
    *,
    completed_as_of_by_market: Mapping[str, datetime] | None = None,
    live_markets: Collection[str] | None = None,
) -> dict[str, Any]:
    """다중 심볼 지수 조회. yfinance 심볼은 단일 배치 다운로드로 묶는다.

    기본 호출은 기존 현재가 의미를 유지한다. ``completed_as_of_by_market``를
    넘기는 home/detail 호출은 같은 provider 선택기를 쓰며, target 완료봉과 그
    직전 세션을 모두 증명하거나 검증된 metadata 종가를 복구한 경우만 허용한다.
    캘린더 cutoff가 없거나 근거가 맞지 않으면 값을 만들지 않는다.

    ``live_markets``에 든 시장은 진행 중 세션이므로 완료봉으로 고정하지 않는다.
    그 시장의 행은 값·등락·등락률·기준 시각이 모두 공급자의 한 스냅샷에서 나오며,
    같은 스냅샷의 직전 종가를 증명하지 못하면 등락을 비운다.
    """
    live = frozenset(live_markets or ())
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    unsupported = sorted(
        {
            symbol
            for symbol in normalized
            if _INDEX_META.get(symbol, {}).get("source") not in ("naver", "yfinance")
        }
    )
    if unsupported:
        raise ValueError(
            "Unsupported batch index symbols: "
            f"{', '.join(unsupported)}. Batch supports naver/yfinance sources only."
        )

    kr_symbols = [
        symbol for symbol in normalized if _INDEX_META[symbol]["source"] == "naver"
    ]
    us_symbols = [
        symbol for symbol in normalized if _INDEX_META[symbol]["source"] == "yfinance"
    ]

    async def load_kr(symbol: str) -> dict[str, Any]:
        meta = _INDEX_META[symbol]
        if completed_as_of_by_market is None or "KRX" in live:
            return _tag_kr_index_data_state(
                await _fetch_index_kr_current(meta["naver_code"], meta["name"])
            )
        completed_as_of = completed_as_of_by_market.get("KRX")
        if completed_as_of is None:
            return {
                "symbol": symbol,
                "source": "naver",
                "unavailable": True,
            }
        return await _fetch_index_kr_completed(
            meta["naver_code"],
            meta["name"],
            completed_as_of=completed_as_of,
        )

    async def load_us() -> list[dict[str, Any]]:
        if completed_as_of_by_market is None:
            return await _fetch_indices_us_current_batch(us_symbols)
        completed_symbols = tuple(
            symbol for symbol in us_symbols if symbol in _DEFAULT_INDICES
        )
        completed_as_of = completed_as_of_by_market.get("US")
        us_live = "US" in live
        if completed_as_of is None:
            rows = await _fetch_indices_us_current_batch(us_symbols)
            if us_live:
                # 진행 중 세션인데 캘린더가 완료 창을 주지 못한 경우다. 고정할
                # 근거가 없으므로 공급자 현재 스냅샷을 그대로 쓴다.
                return rows
            return [
                (
                    {
                        "symbol": symbol,
                        "source": "yfinance",
                        "unavailable": True,
                    }
                    if symbol in completed_symbols
                    else row
                )
                for symbol, row in zip(us_symbols, rows, strict=True)
            ]
        return await _fetch_indices_us_current_batch(
            us_symbols,
            completed_as_of=completed_as_of,
            completed_symbols=completed_symbols,
            pin_completed_session=not us_live,
        )

    results = await asyncio.gather(
        *(load_kr(symbol) for symbol in kr_symbols),
        load_us(),
        return_exceptions=True,
    )

    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, result in zip(kr_symbols, results[:-1], strict=True):
        if isinstance(result, BaseException):
            rows_by_symbol[symbol] = {"symbol": symbol, "error": str(result)}
        elif isinstance(result, dict):
            rows_by_symbol[symbol] = result
        else:
            rows_by_symbol[symbol] = {"symbol": symbol, "error": str(result)}

    us_result = results[-1]
    if isinstance(us_result, BaseException):
        for symbol in us_symbols:
            rows_by_symbol[symbol] = {"symbol": symbol, "error": str(us_result)}
    elif isinstance(us_result, list):
        for row in us_result:
            if not isinstance(row, dict):
                continue
            row_symbol = row.get("symbol")
            if isinstance(row_symbol, str) and row_symbol in us_symbols:
                rows_by_symbol[row_symbol] = row
        for symbol in us_symbols:
            rows_by_symbol.setdefault(
                symbol,
                {"symbol": symbol, "error": "batch result unavailable"},
            )
    else:
        for symbol in us_symbols:
            rows_by_symbol[symbol] = {"symbol": symbol, "error": str(us_result)}

    return {"indices": [rows_by_symbol[symbol] for symbol in normalized]}


async def handle_get_market_index_current_only(symbol: str) -> dict[str, Any]:
    """ROB-689: current-quote-only index fetch (drops the unused history page).

    market-parity's get_index_quote reads only the current row, but the shared
    handle_get_market_index also fetches a full history page per call. This sibling
    returns the same current-row shape ({"indices": [row]}) WITHOUT the history
    fetch. _fetch_index_kr_current (basic + 1-row price page) is kept intact so the
    'open' field is present and _tag_kr_index_data_state can still apply the ROB-464
    freshness override. The shared handle_get_market_index is intentionally NOT
    modified (its other callers consume the history).
    """
    sym = (symbol or "").strip().upper()
    meta = _INDEX_META.get(sym)
    if meta is None:
        raise ValueError(
            f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
        )
    try:
        if meta["source"] == "naver":
            current_data = await _fetch_index_kr_current(
                meta["naver_code"], meta["name"]
            )
            return {"indices": [_tag_kr_index_data_state(current_data)]}
        if meta["source"] == "coingecko":
            current_data = await _fetch_index_crypto_current(
                meta["cg_metric"], meta["name"], sym
            )
            return {"indices": [current_data]}
        if meta["source"] == "upbit":
            current_data = await _fetch_index_upbit_current(
                meta["upbit_market"], meta["name"], sym
            )
            return {"indices": [current_data]}
        current_data = await _fetch_index_us_current(
            meta["yf_ticker"], meta["name"], sym
        )
        return {"indices": [current_data]}
    except Exception as exc:
        return _error_payload(source=meta["source"], message=str(exc), symbol=sym)
