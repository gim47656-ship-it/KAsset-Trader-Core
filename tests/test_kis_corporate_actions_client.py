from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.brokers.kis.corporate_actions import (
    CorporateActionsClient,
    KISCorporateActionPaginationError,
    KISCorporateActionPayloadError,
)


class _TokenManager:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear_token(self) -> None:
        self.cleared += 1


class _Parent:
    def __init__(self, responses: list[tuple[dict[str, Any], dict[str, str]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._settings = SimpleNamespace(kis_access_token="token")
        self._hdr_base = {"appkey": "key", "appsecret": "secret"}
        self._token_manager = _TokenManager()

    async def _ensure_token(self) -> None:
        return None

    def _kis_url(self, path: str) -> str:
        return f"https://kis.invalid{path}"

    async def _request_with_rate_limit_with_headers(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_header_pagination_continues_without_fixed_depth_or_cts_guess() -> None:
    responses = [
        (
            {"rt_cd": "0", "output1": [{"sht_cd": "005930", "page": str(page)}]},
            {"tr_cont": "M" if page < 12 else ""},
        )
        for page in range(13)
    ]
    parent = _Parent(responses)

    rows = await CorporateActionsClient(parent).ksdinfo_bonus_issue(
        "005930", "20260101", "20260131"
    )

    assert [row["page"] for row in rows] == [str(page) for page in range(13)]
    assert rows.page_count == 13
    assert rows.last_cursor is None
    assert len(parent.calls) == 13
    assert [call["headers"]["tr_cont"] for call in parent.calls] == [
        "",
        *("N" for _ in range(12)),
    ]
    assert all(call["params"]["CTS"] == "" for call in parent.calls)


@pytest.mark.asyncio
async def test_cts_is_forwarded_and_repeated_cursor_fails_closed() -> None:
    parent = _Parent(
        [
            (
                {
                    "rt_cd": "0",
                    "CTS": "cursor-1",
                    "output1": [{"sht_cd": "005930", "record_date": "20260101"}],
                },
                {"tr_cont": "M"},
            ),
            (
                {
                    "rt_cd": "0",
                    "CTS": "cursor-1",
                    "output1": [{"sht_cd": "005930", "record_date": "20260102"}],
                },
                {"tr_cont": "M"},
            ),
        ]
    )

    with pytest.raises(KISCorporateActionPaginationError, match="repeated CTS"):
        await CorporateActionsClient(parent).ksdinfo_rev_split(
            "005930", "20260101", "20260131"
        )

    assert parent.calls[1]["params"]["CTS"] == "cursor-1"
    assert parent.calls[1]["headers"]["tr_cont"] == "N"


@pytest.mark.asyncio
async def test_advancing_cts_allows_duplicate_provider_rows_across_pages() -> None:
    page = {"sht_cd": "005930", "record_date": "20260101"}
    parent = _Parent(
        [
            (
                {"rt_cd": "0", "CTS": "cursor-1", "output1": [page]},
                {"tr_cont": "M"},
            ),
            (
                {"rt_cd": "0", "CTS": "cursor-2", "output1": [page]},
                {"tr_cont": "M"},
            ),
            (
                {"rt_cd": "0", "CTS": "cursor-3", "output1": [page]},
                {"tr_cont": ""},
            ),
        ]
    )

    rows = await CorporateActionsClient(parent).ksdinfo_bonus_issue(
        "005930", "20260101", "20260131"
    )

    assert rows == [page, page, page]
    assert parent.calls[2]["params"]["CTS"] == "cursor-2"


@pytest.mark.asyncio
async def test_repeated_page_without_cts_fails_closed() -> None:
    page = {"sht_cd": "005930", "record_date": "20260101"}
    parent = _Parent(
        [
            ({"rt_cd": "0", "output1": [page]}, {"tr_cont": "M"}),
            ({"rt_cd": "0", "output1": [page]}, {"tr_cont": "M"}),
        ]
    )
    with pytest.raises(
        KISCorporateActionPaginationError,
        match="repeated a continuing page",
    ):
        await CorporateActionsClient(parent).ksdinfo_dividend(
            "005930", "20260101", "20260131"
        )


@pytest.mark.asyncio
async def test_empty_row_container_is_a_successful_completed_page() -> None:
    parent = _Parent([({"rt_cd": "0", "output1": []}, {"tr_cont": ""})])

    rows = await CorporateActionsClient(parent).ksdinfo_dividend(
        "005930", "20260101", "20260131"
    )

    assert rows == []
    assert rows.page_count == 1
    assert rows.last_cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"rt_cd": "0"},
        {"rt_cd": "0", "output1": "not-a-row-container"},
        {"rt_cd": "0", "output1": [None]},
        {"rt_cd": "0", "output1": [{}]},
    ],
)
async def test_malformed_row_containers_fail_closed(payload: dict[str, Any]) -> None:
    parent = _Parent([(payload, {})])

    with pytest.raises(KISCorporateActionPayloadError):
        await CorporateActionsClient(parent).ksdinfo_paidin_capin(
            "005930", "20260101", "20260131"
        )


@pytest.mark.asyncio
async def test_provider_row_fields_are_returned_exactly() -> None:
    provider_row = {
        "record_date": "20260102",
        "sht_cd": "005930",
        "inter_bf_face_amt": "5000",
        "inter_af_face_amt": "1000",
        "unmapped_provider_field": " 그대로 ",
    }
    parent = _Parent([({"rt_cd": "0", "output1": provider_row}, {"tr_cont": ""})])

    rows = await CorporateActionsClient(parent).ksdinfo_rev_split(
        "005930", "20260101", "20260131"
    )

    assert rows == [provider_row]
