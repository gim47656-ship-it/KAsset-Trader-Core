"""OpenDART 공시목록 ``list.json`` 전용 비동기 클라이언트.

고유번호나 문서 API 및 POSIX 전용 OpenDartReader 모듈을 임포트하지 않는다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.core.config import settings

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_LIST_PAGE_COUNT = 100
DART_NO_DATA_STATUS = "013"
_DART_STATUS_MESSAGES = {
    "010": "등록되지 않은 인증키입니다.",
    "011": "사용할 수 없는 인증키입니다.",
    DART_NO_DATA_STATUS: "조회된 데이터가 없습니다.",
    "020": "요청 제한을 초과하였습니다.",
}
_DART_PLACEHOLDER_PREFIXES = (
    "UNUSED",
    "DUMMY",
    "PLACEHOLDER",
    "CHANGEME",
    "YOUR_",
)


class DartListError(RuntimeError):
    """DART 공시목록 실패와 실패 전 수신한 행을 함께 전달한다."""

    def __init__(
        self,
        status: str,
        message: str,
        *,
        partial_filings: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.status = status
        self.message = message
        self.partial_filings = [dict(row) for row in partial_filings]
        super().__init__(f"DART list status {status}: {message}")


def _validated_list_api_key() -> str:
    api_key = settings.opendart_api_key.strip()
    if len(api_key) != 40 or api_key.upper().startswith(_DART_PLACEHOLDER_PREFIXES):
        raise DartListError(
            "configuration",
            "OPENDART_API_KEY is missing or placeholder; a 40-character key is required",
        )
    return api_key


def _dart_payload_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
    partial_filings: Sequence[Mapping[str, Any]],
) -> int:
    value = payload.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DartListError(
            "response",
            f"invalid {key}: {value!r}",
            partial_filings=partial_filings,
        ) from exc
    if parsed < 0:
        raise DartListError(
            "response",
            f"invalid {key}: {value!r}",
            partial_filings=partial_filings,
        )
    return parsed


async def _fetch_disclosure_list_pages(
    client: Any,
    *,
    api_key: str,
    start_date: dt.date,
    end_date: dt.date,
    wanted_stock_symbols: set[str] | None,
) -> list[dict[str, Any]]:
    filings: list[dict[str, Any]] = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": api_key,
            "bgn_de": start_date.strftime("%Y%m%d"),
            "end_de": end_date.strftime("%Y%m%d"),
            "page_no": page_no,
            "page_count": DART_LIST_PAGE_COUNT,
        }
        try:
            response = await client.get(DART_LIST_URL, params=params)
        except httpx.HTTPError as exc:
            raise DartListError(
                "transport",
                f"DART request failed: {type(exc).__name__}",
                partial_filings=filings,
            ) from exc
        if response.status_code != 200:
            raise DartListError(
                f"http_{response.status_code}",
                "DART list request returned a non-200 response",
                partial_filings=filings,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DartListError(
                "response",
                "DART list response is not valid JSON",
                partial_filings=filings,
            ) from exc
        if not isinstance(payload, Mapping):
            raise DartListError(
                "response",
                "DART list response must be a JSON object",
                partial_filings=filings,
            )

        status = str(payload.get("status", ""))
        message = str(
            payload.get("message")
            or _DART_STATUS_MESSAGES.get(status)
            or "unknown DART API error"
        )
        if status == DART_NO_DATA_STATUS:
            return filings
        if status != "000":
            raise DartListError(
                status or "response",
                message,
                partial_filings=filings,
            )

        page_rows = payload.get("list")
        if not isinstance(page_rows, list):
            raise DartListError(
                "response",
                "DART list response field 'list' must be an array",
                partial_filings=filings,
            )
        for row in page_rows:
            if not isinstance(row, Mapping):
                raise DartListError(
                    "response",
                    "DART list row must be a JSON object",
                    partial_filings=filings,
                )
            stock_code = str(row.get("stock_code") or "").strip()
            if wanted_stock_symbols is None or stock_code in wanted_stock_symbols:
                filings.append(dict(row))

        response_page = _dart_payload_int(
            payload,
            "page_no",
            default=page_no,
            partial_filings=filings,
        )
        total_page = _dart_payload_int(
            payload,
            "total_page",
            default=response_page,
            partial_filings=filings,
        )
        if response_page < page_no:
            raise DartListError(
                "response",
                "DART list page_no did not advance",
                partial_filings=filings,
            )
        if response_page >= total_page:
            return filings
        page_no = response_page + 1


async def fetch_disclosure_list(
    start_date: dt.date,
    end_date: dt.date,
    *,
    stock_symbols: Sequence[str] | None = None,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """날짜 범위의 DART 공시목록을 마지막 페이지까지 가져온다.

    전체 시장 ``list.json`` 응답만 사용한다. 선택 종목은 응답의 ``stock_code``로
    로컬 필터링하므로 중지 가능한 고유번호 API나 별도 법인코드 캐시에 의존하지 않는다.
    """
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    api_key = _validated_list_api_key()
    wanted_stock_symbols = (
        None
        if stock_symbols is None
        else {symbol.strip() for symbol in stock_symbols if symbol.strip()}
    )

    if client is None:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as owned:
            return await _fetch_disclosure_list_pages(
                owned,
                api_key=api_key,
                start_date=start_date,
                end_date=end_date,
                wanted_stock_symbols=wanted_stock_symbols,
            )
    return await _fetch_disclosure_list_pages(
        client,
        api_key=api_key,
        start_date=start_date,
        end_date=end_date,
        wanted_stock_symbols=wanted_stock_symbols,
    )
