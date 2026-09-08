"""일봉 저장소의 KR 지수 벤치마크 전용 fetcher."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Protocol, cast

import httpx
import pandas as pd

_NAVER_INDEX_PRICE_URL = "https://m.stock.naver.com/api/index/{symbol}/price"
_NAVER_PAGE_SIZE = 60
_NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}
_SUPPORTED_KR_BENCHMARKS = frozenset({"KOSPI", "KOSDAQ"})


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpClient(Protocol):
    async def get(self, url: str, **kwargs: object) -> _Response: ...

    async def aclose(self) -> None: ...


def _finite_number(value: object, *, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(
            f"Naver 벤치마크 행의 {field} 값이 올바르지 않습니다: {value!r}"
        )
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Naver 벤치마크 행의 {field} 값이 올바르지 않습니다: {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(
            f"Naver 벤치마크 행의 {field} 값이 유한수가 아닙니다: {value!r}"
        )
    return number


def _parse_naver_row(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("Naver 벤치마크 이력은 객체 행의 목록이어야 합니다")

    raw_date = item.get("localTradedAt")
    if not isinstance(raw_date, str):
        raise ValueError("Naver 벤치마크 행에 localTradedAt이 없습니다")
    try:
        trading_date = date.fromisoformat(raw_date[:10])
    except ValueError as exc:
        raise ValueError(
            f"Naver 벤치마크 행의 localTradedAt이 올바르지 않습니다: {raw_date!r}"
        ) from exc

    open_value = _finite_number(item.get("openPrice"), field="openPrice")
    high_value = _finite_number(item.get("highPrice"), field="highPrice")
    low_value = _finite_number(item.get("lowPrice"), field="lowPrice")
    close_value = _finite_number(item.get("closePrice"), field="closePrice")
    raw_volume = item.get("accumulatedTradingVolume")
    # Naver 지수 일봉은 주식 일봉과 달리 거래량을 생략한다. 지수 OHLC의
    # 결측으로 오인하지 않도록 저장 스키마의 unavailable sentinel인 0을 쓴다.
    volume = (
        _finite_number(raw_volume, field="accumulatedTradingVolume")
        if raw_volume is not None
        else 0.0
    )
    if min(open_value, high_value, low_value, close_value) <= 0:
        raise ValueError("Naver 벤치마크 OHLC 값은 양수여야 합니다")
    if volume < 0:
        raise ValueError("Naver 벤치마크 거래량은 음수일 수 없습니다")
    if low_value > min(open_value, close_value) or high_value < max(
        open_value, close_value
    ):
        raise ValueError("Naver 벤치마크 행이 OHLC 범위를 위반했습니다")

    raw_value = item.get("accumulatedTradingValue")
    value = (
        _finite_number(raw_value, field="accumulatedTradingValue")
        if raw_value is not None
        else close_value * volume
    )
    if value < 0:
        raise ValueError("Naver 벤치마크 거래대금은 음수일 수 없습니다")

    return {
        "date": trading_date,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "volume": volume,
        "value": value,
    }


async def fetch_kr_benchmark_daily(
    *,
    symbol: str,
    n: int,
    client: _HttpClient | None = None,
) -> pd.DataFrame:
    """네이버 지수 일봉을 페이지 순회하고 거래일 기준 중복을 제거한다.

    형성 중인 최신 행을 제거한 뒤에도 ``n``개를 남길 수 있도록 최대 ``n + 1``개를
    반환한다. 실제 완료 세션 판정과 최종 ``n``개 절단은 sync service가 담당한다.
    """

    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in _SUPPORTED_KR_BENCHMARKS:
        raise ValueError(f"지원하지 않는 KR 벤치마크입니다: {symbol!r}")
    if n <= 0:
        raise ValueError("n은 양수여야 합니다")

    requested_rows = n + 1
    page_size = min(_NAVER_PAGE_SIZE, requested_rows)
    max_pages = math.ceil(requested_rows / page_size) + 2
    owned_client = client is None
    http_client: _HttpClient = client or httpx.AsyncClient(
        timeout=10, follow_redirects=False
    )
    rows_by_date: dict[date, dict[str, object]] = {}

    try:
        for page in range(1, max_pages + 1):
            response = await http_client.get(
                _NAVER_INDEX_PRICE_URL.format(symbol=normalized_symbol),
                params={"pageSize": page_size, "page": page, "timeframe": "day"},
                headers=_NAVER_HEADERS,
            )
            response.raise_for_status()
            payload: Any = response.json()
            if not isinstance(payload, list):
                raise ValueError("Naver 벤치마크 이력 응답은 목록이어야 합니다")
            if not payload:
                break

            previous_count = len(rows_by_date)
            for item in payload:
                row = _parse_naver_row(item)
                trading_date = cast(date, row["date"])
                existing = rows_by_date.get(trading_date)
                if existing is not None and existing != row:
                    raise ValueError(
                        "Naver 벤치마크 이력의 중복 거래일 값이 충돌합니다: "
                        f"{trading_date.isoformat()}"
                    )
                rows_by_date[trading_date] = row

            if len(rows_by_date) >= requested_rows:
                break
            if len(payload) < page_size or len(rows_by_date) == previous_count:
                break
    finally:
        if owned_client:
            await http_client.aclose()

    if not rows_by_date:
        raise ValueError(f"Naver가 {normalized_symbol} 이력을 반환하지 않았습니다")
    if len(rows_by_date) < n:
        raise ValueError(
            f"Naver 벤치마크 이력 수가 부족합니다: requested={n} "
            f"received={len(rows_by_date)}"
        )

    rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    return pd.DataFrame(rows[-requested_rows:]).reset_index(drop=True)
