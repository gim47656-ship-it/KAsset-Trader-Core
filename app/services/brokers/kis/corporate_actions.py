"""KIS public reads for KR lifecycle and corporate-action evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from . import constants

if TYPE_CHECKING:
    from .protocols import KISClientProtocol

SEARCH_STOCK_INFO_ENDPOINT = "/uapi/domestic-stock/v1/quotations/search-stock-info"
SEARCH_STOCK_INFO_TR = "CTPF1002R"
REV_SPLIT_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/rev-split"
REV_SPLIT_TR = "HHKDB669105C0"
PAIDIN_CAPIN_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/paidin-capin"
PAIDIN_CAPIN_TR = "HHKDB669100C0"
BONUS_ISSUE_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/bonus-issue"
BONUS_ISSUE_TR = "HHKDB669101C0"
DIVIDEND_ENDPOINT = "/uapi/domestic-stock/v1/ksdinfo/dividend"
DIVIDEND_TR = "HHKDB669102C0"


class KISCorporateActionPayloadError(RuntimeError):
    """KIS returned a successful envelope with an unusable row/cursor shape."""


class KISCorporateActionPaginationError(RuntimeError):
    """KIS continuation did not demonstrate forward progress."""


class KISPageResult(list[dict[str, Any]]):
    """List-compatible rows plus truthful provider pagination evidence."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        page_count: int,
        last_cursor: str | None,
    ) -> None:
        super().__init__(rows)
        self.page_count = page_count
        self.last_cursor = last_cursor


def _annotate_progress(
    error: Exception,
    *,
    page_count: int,
    last_cursor: str | None,
) -> None:
    """Attach bounded pagination facts without replacing the original error type."""

    try:
        error.page_count = page_count  # type: ignore[attr-defined]
        error.last_cursor = last_cursor  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _date_param(value: date | str, name: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != text:
        raise ValueError(f"{name} must use YYYYMMDD")
    return text


def _validated_rows(payload: dict[str, Any], container: str) -> list[dict[str, Any]]:
    if container not in payload:
        raise KISCorporateActionPayloadError(
            f"KIS response is missing required row container: {container}"
        )
    raw_rows = payload[container]
    if isinstance(raw_rows, dict):
        rows: list[object] = [raw_rows]
    elif isinstance(raw_rows, list):
        rows = raw_rows
    else:
        raise KISCorporateActionPayloadError(
            f"KIS response row container {container} must be a dict or list"
        )
    if any(not isinstance(row, dict) or not row for row in rows):
        raise KISCorporateActionPayloadError(
            f"KIS response row container {container} contains a non-object or blank row"
        )
    return [dict(row) for row in rows]


def _page_fingerprint(rows: list[dict[str, Any]]) -> str:
    try:
        encoded = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KISCorporateActionPayloadError(
            "KIS response rows are not canonical JSON values"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _extract_cts(payload: dict[str, Any]) -> str | None:
    candidates: list[str] = []

    def add_from(value: object) -> None:
        if not isinstance(value, dict):
            return
        for key in ("CTS", "cts"):
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                candidates.append(str(raw).strip())

    add_from(payload)
    output2 = payload.get("output2")
    if isinstance(output2, list):
        for item in output2:
            add_from(item)
    else:
        add_from(output2)

    unique = list(dict.fromkeys(candidates))
    if len(unique) > 1:
        raise KISCorporateActionPayloadError(
            "KIS response contains conflicting CTS continuation values"
        )
    return unique[0] if unique else None


class CorporateActionsClient:
    """Focused sub-client for the five official KIS public endpoints."""

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self) -> Any:
        return self._parent._settings

    async def _request_page(
        self,
        *,
        endpoint: str,
        tr_id: str,
        params: dict[str, Any],
        tr_cont: str,
        api_name: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        for attempt in range(2):
            await self._parent._ensure_token()
            headers = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
                "tr_cont": tr_cont,
            }
            (
                payload,
                response_headers,
            ) = await self._parent._request_with_rate_limit_with_headers(
                "GET",
                self._parent._kis_url(endpoint),
                headers=headers,
                params=params,
                timeout=10.0,
                api_name=api_name,
                tr_id=tr_id,
            )
            if payload.get("rt_cd") == "0":
                return payload, response_headers
            if attempt == 0 and payload.get("msg_cd") in constants.TOKEN_EXPIRED_CODES:
                await self._parent._token_manager.clear_token()
                continue
            message = payload.get("msg1")
            code = payload.get("msg_cd", "unknown")
            raise RuntimeError(message or f"KIS API error (msg_cd={code})")
        raise RuntimeError("KIS API token retry exhausted")

    async def _fetch_paginated(
        self,
        *,
        endpoint: str,
        tr_id: str,
        params: dict[str, Any],
        container: str,
        api_name: str,
        cts_parameter: bool,
    ) -> KISPageResult:
        all_rows: list[dict[str, Any]] = []
        cts = str(params.get("CTS") or "")
        request_tr_cont = ""
        page_count = 0
        last_cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_continuing_pages: set[str] = set()

        while True:
            page_params = dict(params)
            if cts_parameter:
                page_params["CTS"] = cts
            try:
                payload, response_headers = await self._request_page(
                    endpoint=endpoint,
                    tr_id=tr_id,
                    params=page_params,
                    tr_cont=request_tr_cont,
                    api_name=api_name,
                )
            except Exception as exc:
                _annotate_progress(
                    exc,
                    page_count=page_count,
                    last_cursor=last_cursor,
                )
                raise
            try:
                rows = _validated_rows(payload, container)
            except KISCorporateActionPayloadError as exc:
                _annotate_progress(
                    exc,
                    page_count=page_count + 1,
                    last_cursor=last_cursor,
                )
                raise
            all_rows.extend(rows)
            page_count += 1
            next_cts = _extract_cts(payload) if cts_parameter else None
            if next_cts is not None:
                last_cursor = next_cts

            headers_lower = {
                str(key).lower(): value for key, value in response_headers.items()
            }
            continuation = str(headers_lower.get("tr_cont") or "").strip().upper()
            if continuation not in {"F", "M"}:
                return KISPageResult(
                    all_rows,
                    page_count=page_count,
                    last_cursor=last_cursor,
                )

            if next_cts is None:
                fingerprint = _page_fingerprint(rows)
                if fingerprint in seen_continuing_pages:
                    error = KISCorporateActionPaginationError(
                        f"KIS repeated a continuing page for {tr_id}"
                    )
                    _annotate_progress(
                        error,
                        page_count=page_count,
                        last_cursor=last_cursor,
                    )
                    raise error
                seen_continuing_pages.add(fingerprint)

            if next_cts is not None:
                if next_cts in seen_cursors:
                    error = KISCorporateActionPaginationError(
                        f"KIS repeated CTS cursor for {tr_id}"
                    )
                    _annotate_progress(
                        error,
                        page_count=page_count,
                        last_cursor=last_cursor,
                    )
                    raise error
                seen_cursors.add(next_cts)
                cts = next_cts
            request_tr_cont = "N"

    async def search_stock_info(
        self,
        pdno: str,
        *,
        prdt_type_cd: str = "300",
    ) -> KISPageResult:
        product_number = _required_text(pdno, "pdno")
        product_type = _required_text(prdt_type_cd, "prdt_type_cd")
        return await self._fetch_paginated(
            endpoint=SEARCH_STOCK_INFO_ENDPOINT,
            tr_id=SEARCH_STOCK_INFO_TR,
            params={"PRDT_TYPE_CD": product_type, "PDNO": product_number},
            container="output",
            api_name="search_stock_info",
            cts_parameter=False,
        )

    async def ksdinfo_rev_split(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        market_gb: str = "0",
    ) -> KISPageResult:
        symbol = _required_text(sht_cd, "sht_cd")
        start, end = self._date_window(from_date, to_date)
        market = str(market_gb).strip()
        if market not in {"0", "1", "2"}:
            raise ValueError("market_gb must be one of 0, 1, 2")
        return await self._fetch_paginated(
            endpoint=REV_SPLIT_ENDPOINT,
            tr_id=REV_SPLIT_TR,
            params={
                "SHT_CD": symbol,
                "CTS": "",
                "F_DT": start,
                "T_DT": end,
                "MARKET_GB": market,
            },
            container="output1",
            api_name="ksdinfo_rev_split",
            cts_parameter=True,
        )

    async def ksdinfo_paidin_capin(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        gb1: str = "2",
    ) -> KISPageResult:
        symbol = _required_text(sht_cd, "sht_cd")
        start, end = self._date_window(from_date, to_date)
        query_kind = str(gb1).strip()
        if query_kind not in {"1", "2"}:
            raise ValueError("gb1 must be one of 1, 2")
        return await self._fetch_paginated(
            endpoint=PAIDIN_CAPIN_ENDPOINT,
            tr_id=PAIDIN_CAPIN_TR,
            params={
                "CTS": "",
                "GB1": query_kind,
                "F_DT": start,
                "T_DT": end,
                "SHT_CD": symbol,
            },
            container="output1",
            api_name="ksdinfo_paidin_capin",
            cts_parameter=True,
        )

    async def ksdinfo_bonus_issue(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
    ) -> KISPageResult:
        symbol = _required_text(sht_cd, "sht_cd")
        start, end = self._date_window(from_date, to_date)
        return await self._fetch_paginated(
            endpoint=BONUS_ISSUE_ENDPOINT,
            tr_id=BONUS_ISSUE_TR,
            params={
                "CTS": "",
                "F_DT": start,
                "T_DT": end,
                "SHT_CD": symbol,
            },
            container="output1",
            api_name="ksdinfo_bonus_issue",
            cts_parameter=True,
        )

    async def ksdinfo_dividend(
        self,
        sht_cd: str,
        from_date: date | str,
        to_date: date | str,
        *,
        gb1: str = "0",
        high_gb: str = "",
    ) -> KISPageResult:
        symbol = _required_text(sht_cd, "sht_cd")
        start, end = self._date_window(from_date, to_date)
        query_kind = str(gb1).strip()
        if query_kind not in {"0", "1", "2"}:
            raise ValueError("gb1 must be one of 0, 1, 2")
        return await self._fetch_paginated(
            endpoint=DIVIDEND_ENDPOINT,
            tr_id=DIVIDEND_TR,
            params={
                "CTS": "",
                "GB1": query_kind,
                "F_DT": start,
                "T_DT": end,
                "SHT_CD": symbol,
                "HIGH_GB": str(high_gb or ""),
            },
            container="output1",
            api_name="ksdinfo_dividend",
            cts_parameter=True,
        )

    @staticmethod
    def _date_window(
        from_date: date | str,
        to_date: date | str,
    ) -> tuple[str, str]:
        start = _date_param(from_date, "from_date")
        end = _date_param(to_date, "to_date")
        if start > end:
            raise ValueError("from_date must be on or before to_date")
        return start, end
