"""일봉 저장소의 KR 지수 벤치마크 전용 fetcher."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Protocol, cast

import httpx
import pandas as pd

from app.core.timezone import now_kst
from app.services.brokers.kis.constants import TOKEN_EXPIRED_CODES

_KIS_INDEX_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price"
_KIS_INDEX_DAILY_TR_ID = "FHPUP02120000"
_KIS_INDEX_CODE_BY_SYMBOL = {"KOSPI": "0001"}
_KIS_INDEX_MAX_PAGES = 20
_NAVER_INDEX_PRICE_URL = "https://m.stock.naver.com/api/index/{symbol}/price"
_NAVER_PAGE_SIZE = 100
_NAVER_HEADERS = {"User-Agent": "Mozilla/5.0"}
_SUPPORTED_KR_BENCHMARKS = frozenset({"KOSPI"})


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpClient(Protocol):
    async def get(self, url: str, **kwargs: object) -> _Response: ...

    async def aclose(self) -> None: ...


class _KISIndexClient(Protocol):
    _hdr_base: dict[str, str]
    _settings: Any
    _token_manager: Any

    async def _ensure_token(self) -> None: ...

    def _kis_url(self, path: str) -> str: ...

    async def _request_with_rate_limit_with_headers(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]: ...


def _finite_number(value: object, *, field: str, provider: str = "Naver") -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(
            f"{provider} 벤치마크 행의 {field} 값이 올바르지 않습니다: {value!r}"
        )
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{provider} 벤치마크 행의 {field} 값이 올바르지 않습니다: {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(
            f"{provider} 벤치마크 행의 {field} 값이 유한수가 아닙니다: {value!r}"
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
    volume = _finite_number(
        item.get("accumulatedTradingVolume"), field="accumulatedTradingVolume"
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


def _parse_kis_index_row(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("KIS 지수 일봉 이력은 객체 행의 목록이어야 합니다")
    raw_date = item.get("stck_bsop_date")
    try:
        trading_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"KIS 지수 일봉의 stck_bsop_date가 올바르지 않습니다: {raw_date!r}"
        ) from exc

    close_value = _finite_number(
        item.get("bstp_nmix_prpr"),
        field="bstp_nmix_prpr",
        provider="KIS",
    )
    open_value = _finite_number(
        item.get("bstp_nmix_oprc"),
        field="bstp_nmix_oprc",
        provider="KIS",
    )
    high_value = _finite_number(
        item.get("bstp_nmix_hgpr"),
        field="bstp_nmix_hgpr",
        provider="KIS",
    )
    low_value = _finite_number(
        item.get("bstp_nmix_lwpr"),
        field="bstp_nmix_lwpr",
        provider="KIS",
    )
    volume = _finite_number(
        item.get("acml_vol"),
        field="acml_vol",
        provider="KIS",
    )
    value = _finite_number(
        item.get("acml_tr_pbmn"),
        field="acml_tr_pbmn",
        provider="KIS",
    )
    if min(open_value, high_value, low_value, close_value) <= 0:
        raise ValueError("KIS 지수 일봉 OHLC 값은 양수여야 합니다")
    if volume < 0 or value < 0:
        raise ValueError("KIS 지수 일봉 거래량·거래대금은 음수일 수 없습니다")
    if low_value > min(open_value, close_value) or high_value < max(
        open_value, close_value
    ):
        raise ValueError("KIS 지수 일봉이 OHLC 범위를 위반했습니다")
    return {
        "date": trading_date,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "volume": volume,
        "value": value,
    }


async def _request_kis_index_page(
    *,
    kis: _KISIndexClient,
    params: dict[str, Any],
    tr_cont: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    for attempt in range(2):
        await kis._ensure_token()
        headers = dict(kis._hdr_base)
        headers.update(
            {
                "authorization": f"Bearer {kis._settings.kis_access_token}",
                "tr_id": _KIS_INDEX_DAILY_TR_ID,
                "tr_cont": tr_cont,
            }
        )
        payload, response_headers = await kis._request_with_rate_limit_with_headers(
            "GET",
            kis._kis_url(_KIS_INDEX_DAILY_PATH),
            headers=headers,
            params=params,
            timeout=10,
            api_name="inquire_index_daily_price",
            tr_id=_KIS_INDEX_DAILY_TR_ID,
        )
        if payload.get("rt_cd") == "0":
            return payload, response_headers
        if attempt == 0 and payload.get("msg_cd") in TOKEN_EXPIRED_CODES:
            await kis._token_manager.clear_token()
            continue
        message = payload.get("msg1") or payload.get("msg_cd") or "unknown"
        raise RuntimeError(f"KIS 지수 일봉 API 오류: {message}")
    raise RuntimeError("KIS 지수 일봉 토큰 재시도 한도를 초과했습니다")


async def fetch_kr_benchmark_daily_kis(
    *,
    kis: _KISIndexClient,
    symbol: str,
    n: int,
    input_date: date | None = None,
) -> pd.DataFrame:
    """KIS 공식 지수 일봉 endpoint를 ``tr_cont`` 종료까지 순회한다."""

    normalized_symbol = str(symbol or "").strip().upper()
    index_code = _KIS_INDEX_CODE_BY_SYMBOL.get(normalized_symbol)
    if index_code is None:
        raise ValueError(f"지원하지 않는 KIS KR 벤치마크입니다: {symbol!r}")
    if n <= 0:
        raise ValueError("n은 양수여야 합니다")

    request_date = input_date or now_kst().date()
    params = {
        "FID_PERIOD_DIV_CODE": "D",
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": index_code,
        "FID_INPUT_DATE_1": request_date.strftime("%Y%m%d"),
    }
    rows_by_date: dict[date, dict[str, object]] = {}
    seen_page_states: set[tuple[str, tuple[date, ...]]] = set()
    previous_oldest: date | None = None
    tr_cont_request = ""
    terminated = False

    for _page in range(1, _KIS_INDEX_MAX_PAGES + 1):
        payload, response_headers = await _request_kis_index_page(
            kis=kis,
            params=params,
            tr_cont=tr_cont_request,
        )
        if "output2" not in payload:
            raise ValueError("KIS 지수 일봉 응답에 output2가 없습니다")
        raw_rows = payload["output2"]
        if not isinstance(raw_rows, list):
            raise ValueError("KIS 지수 일봉 output2는 목록이어야 합니다")
        normalized_headers = {
            str(key).lower().replace("-", "_"): value
            for key, value in response_headers.items()
        }
        tr_cont_response = str(normalized_headers.get("tr_cont") or "").strip().upper()

        parsed_rows = [_parse_kis_index_row(item) for item in raw_rows]
        if not parsed_rows:
            if tr_cont_response in {"F", "M"}:
                raise ValueError("KIS 지수 일봉 연속 응답이 빈 페이지를 반환했습니다")
            terminated = True
            break

        page_dates = tuple(sorted(cast(date, row["date"]) for row in parsed_rows))
        state = (tr_cont_response, page_dates)
        if state in seen_page_states:
            raise ValueError("KIS 지수 일봉 페이지 상태가 반복되었습니다")
        seen_page_states.add(state)

        page_oldest = page_dates[0]
        if previous_oldest is not None and page_oldest >= previous_oldest:
            raise ValueError("KIS 지수 일봉 연속 페이지의 최저일이 감소하지 않았습니다")
        previous_oldest = page_oldest

        for row in parsed_rows:
            trading_date = cast(date, row["date"])
            existing = rows_by_date.get(trading_date)
            if existing is not None and existing != row:
                raise ValueError(
                    "KIS 지수 일봉의 중복 거래일 값이 충돌합니다: "
                    f"{trading_date.isoformat()}"
                )
            rows_by_date[trading_date] = row

        if tr_cont_response not in {"F", "M"}:
            terminated = True
            break
        tr_cont_request = "N"

    if not terminated:
        raise RuntimeError(
            f"KIS 지수 일봉 페이지가 안전상한 {_KIS_INDEX_MAX_PAGES}회 안에 "
            "종료되지 않았습니다"
        )
    if len(rows_by_date) < n:
        raise ValueError(
            f"KIS 지수 일봉 수가 부족합니다: requested={n} received={len(rows_by_date)}"
        )

    requested_rows = n + 1
    rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    return pd.DataFrame(rows[-requested_rows:]).reset_index(drop=True)


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
