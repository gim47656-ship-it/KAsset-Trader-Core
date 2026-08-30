from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.daily_candles.benchmark_fetcher import (
    fetch_kr_benchmark_daily,
    fetch_kr_benchmark_daily_kis,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = iter(payloads)
        self.calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(next(self._payloads))


class _KISClient:
    def __init__(
        self,
        responses: list[tuple[dict[str, Any], dict[str, str]]],
    ) -> None:
        self._responses = iter(responses)
        self._hdr_base = {"appkey": "key", "appsecret": "secret"}
        self._settings = SimpleNamespace(kis_access_token="token")
        self._token_manager = SimpleNamespace(clear_token=AsyncMock())
        self._ensure_token = AsyncMock()
        self.calls: list[dict[str, Any]] = []

    def _kis_url(self, path: str) -> str:
        return f"https://example.test{path}"

    async def _request_with_rate_limit_with_headers(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self.calls.append({"method": method, "url": url, **kwargs})
        return next(self._responses)


def _kis_row(day: str, close: float) -> dict[str, object]:
    return {
        "stck_bsop_date": day,
        "bstp_nmix_prpr": str(close),
        "bstp_nmix_oprc": str(close - 1),
        "bstp_nmix_hgpr": str(close + 2),
        "bstp_nmix_lwpr": str(close - 2),
        "acml_vol": "1000",
        "acml_tr_pbmn": "2000000",
    }


def _row(day: date, close: float) -> dict[str, object]:
    return {
        "localTradedAt": day.isoformat(),
        "openPrice": close - 1,
        "highPrice": close + 2,
        "lowPrice": close - 2,
        "closePrice": close,
        "accumulatedTradingVolume": "1,000",
        "accumulatedTradingValue": "2,000,000",
    }


@pytest.mark.asyncio
async def test_kis_index_history_uses_header_continuation_and_deduplicates() -> None:
    duplicated = _kis_row("20240502", 2600.0)
    kis = _KISClient(
        [
            (
                {
                    "rt_cd": "0",
                    "output2": [
                        _kis_row("20240503", 2610.0),
                        duplicated,
                    ],
                },
                {"Tr-Cont": "M"},
            ),
            (
                {
                    "rt_cd": "0",
                    "output2": [
                        duplicated,
                        _kis_row("20240501", 2590.0),
                    ],
                },
                {"TR_CONT": "D"},
            ),
        ]
    )

    frame = await fetch_kr_benchmark_daily_kis(
        kis=kis,
        symbol="KOSPI",
        n=3,
        input_date=date(2024, 5, 4),
    )

    assert frame["date"].tolist() == [
        date(2024, 5, 1),
        date(2024, 5, 2),
        date(2024, 5, 3),
    ]
    assert [call["headers"]["tr_cont"] for call in kis.calls] == ["", "N"]
    assert {call["params"]["FID_INPUT_DATE_1"] for call in kis.calls} == {"20240504"}
    assert all(
        call["tr_id"] == "FHPUP02120000"
        and call["params"]["FID_PERIOD_DIV_CODE"] == "D"
        and call["params"]["FID_COND_MRKT_DIV_CODE"] == "U"
        and call["params"]["FID_INPUT_ISCD"] == "0001"
        for call in kis.calls
    )


@pytest.mark.asyncio
async def test_kis_index_history_rejects_repeated_page_state() -> None:
    page = {
        "rt_cd": "0",
        "output2": [_kis_row("20240503", 2610.0)],
    }
    kis = _KISClient(
        [
            (page, {"tr_cont": "M"}),
            (page, {"tr_cont": "M"}),
        ]
    )

    with pytest.raises(ValueError, match="페이지 상태가 반복"):
        await fetch_kr_benchmark_daily_kis(
            kis=kis,
            symbol="KOSPI",
            n=1,
            input_date=date(2024, 5, 4),
        )


@pytest.mark.asyncio
async def test_kis_index_history_reports_insufficient_rows() -> None:
    kis = _KISClient(
        [
            (
                {
                    "rt_cd": "0",
                    "output2": [_kis_row("20240503", 2610.0)],
                },
                {"tr_cont": "D"},
            )
        ]
    )

    with pytest.raises(ValueError, match="수가 부족"):
        await fetch_kr_benchmark_daily_kis(
            kis=kis,
            symbol="KOSPI",
            n=2,
            input_date=date(2024, 5, 4),
        )


@pytest.mark.asyncio
async def test_kis_index_history_rejects_missing_output2() -> None:
    kis = _KISClient([({"rt_cd": "0", "output1": {}}, {"tr_cont": "D"})])

    with pytest.raises(ValueError, match="output2가 없습니다"):
        await fetch_kr_benchmark_daily_kis(
            kis=kis,
            symbol="KOSPI",
            n=1,
            input_date=date(2024, 5, 4),
        )


@pytest.mark.asyncio
async def test_naver_history_paginates_and_deduplicates_by_trading_date() -> None:
    first_day = date(2024, 1, 1)
    rows = [
        _row(first_day + timedelta(days=index), 2500.0 + index) for index in range(101)
    ]
    client = _Client([rows[:60], [rows[59], *rows[60:]]])

    frame = await fetch_kr_benchmark_daily(symbol="KOSPI", n=100, client=client)

    assert len(frame) == 101
    assert frame["date"].tolist() == [
        first_day + timedelta(days=index) for index in range(101)
    ]
    assert [call["params"]["page"] for call in client.calls] == [1, 2]
    assert all(call["params"]["timeframe"] == "day" for call in client.calls)
    assert all(call["params"]["pageSize"] == 60 for call in client.calls)


@pytest.mark.asyncio
async def test_naver_history_accepts_index_rows_without_volume() -> None:
    row = _row(date(2024, 1, 1), 2500.0)
    row.pop("accumulatedTradingVolume")
    row.pop("accumulatedTradingValue")
    client = _Client([[row]])

    frame = await fetch_kr_benchmark_daily(symbol="KOSPI", n=1, client=client)

    assert frame.iloc[0]["volume"] == 0.0
    assert frame.iloc[0]["value"] == 0.0


@pytest.mark.asyncio
async def test_naver_history_rejects_conflicting_duplicate_date() -> None:
    first_day = date(2024, 1, 1)
    rows = [
        _row(first_day + timedelta(days=index), 2500.0 + index) for index in range(100)
    ]
    conflicting = _row(first_day + timedelta(days=59), 9999.0)
    client = _Client(
        [
            rows[:60],
            [conflicting, *rows[60:], _row(first_day + timedelta(days=100), 2600.0)],
        ]
    )

    with pytest.raises(ValueError, match="중복 거래일 값이 충돌"):
        await fetch_kr_benchmark_daily(symbol="KOSPI", n=100, client=client)


@pytest.mark.asyncio
async def test_naver_history_rejects_malformed_successful_row() -> None:
    malformed = _row(date(2024, 1, 1), 2500.0)
    malformed["highPrice"] = 2400.0
    client = _Client([[malformed]])

    with pytest.raises(ValueError, match="OHLC 범위를 위반"):
        await fetch_kr_benchmark_daily(symbol="KOSPI", n=1, client=client)
