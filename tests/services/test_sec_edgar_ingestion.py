"""SEC EDGAR 공시 수집·정규화·회차 상태 계약 테스트."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.jobs.sec_edgar_ingestion import load_sec_symbols
from app.models.news import NewsArticle, NewsIngestionRun
from app.models.symbol_master import SymbolMaster
from app.services.disclosures.sec_edgar import (
    SEC_COMPANY_TICKERS_URL,
    CompanyTickerCache,
    SecEdgarError,
    build_archive_url,
    build_submission_url,
    parse_submissions,
)
from app.services.disclosures.sec_ingestion import ingest_sec_edgar
from app.tasks import TASKIQ_TASK_MODULES, sec_edgar_ingestion_tasks

_TEST_USER_AGENT = "KAsset Trader tests test@example.com"
_SINCE_DATE = date(2026, 8, 1)


@dataclass(frozen=True)
class _ResponseSpec:
    status_code: int
    payload: object


class _NoopRateLimiter:
    def __init__(self) -> None:
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1


class FakeTickerRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: list[int] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expirations.append(ex)


class FakeSecClient:
    def __init__(self, *responses: _ResponseSpec) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        self.calls.append({"url": url, "headers": dict(headers)})
        response = self.responses.pop(0)
        request = httpx.Request("GET", url, headers=headers)
        if isinstance(response.payload, bytes):
            return httpx.Response(
                response.status_code,
                content=response.payload,
                request=request,
            )
        return httpx.Response(
            response.status_code,
            json=response.payload,
            request=request,
        )


class ExplodingSecClient:
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        raise RuntimeError("fake SEC transport crash")


def _ok(payload: object) -> _ResponseSpec:
    return _ResponseSpec(200, payload)


def _ticker_payload(**ticker_to_cik: int) -> dict[str, dict[str, object]]:
    return {
        str(index): {
            "ticker": ticker,
            "cik_str": cik,
            "title": f"{ticker} issuer",
        }
        for index, (ticker, cik) in enumerate(ticker_to_cik.items())
    }


def _accession(cik: int = 1_045_810) -> str:
    suffix = uuid.uuid4().int % 1_000_000
    return f"{cik:010d}-26-{suffix:06d}"


def _row(
    *,
    cik: int = 1_045_810,
    form: str = "8-K",
    description: str | None = "Current report",
    items: str | None = "2.02,9.01",
    acceptance: str | None = "2026-08-26T16:22:57.000Z",
    filing_date: str = "2026-08-26",
    accession: str | None = None,
    document: str | None = None,
) -> dict[str, object]:
    current_accession = accession or _accession(cik)
    return {
        "accessionNumber": current_accession,
        "form": form,
        "primaryDocument": document or f"filing-{uuid.uuid4().hex}.htm",
        "filingDate": filing_date,
        "acceptanceDateTime": acceptance,
        "primaryDocDescription": description,
        "items": items,
    }


def _submissions(
    rows: list[dict[str, object]],
    *,
    name: str = "NVIDIA CORP",
) -> dict[str, object]:
    keys = (
        "accessionNumber",
        "form",
        "primaryDocument",
        "filingDate",
        "acceptanceDateTime",
        "primaryDocDescription",
        "items",
    )
    return {
        "name": name,
        "filings": {"recent": {key: [row.get(key) for row in rows] for key in keys}},
    }


async def _load_run(db_session: Any, run_uuid: str) -> NewsIngestionRun:
    return (
        await db_session.execute(
            select(NewsIngestionRun).where(NewsIngestionRun.run_uuid == run_uuid)
        )
    ).scalar_one()


def test_column_arrays_parse_all_forms_and_stop_at_date_lower_bound() -> None:
    first = _row(form="4", description="Statement of changes")
    cutoff = _row(
        form="144",
        description=None,
        items="Sale notice",
        filing_date="2026-07-31",
    )
    unreachable = _row(form="10-Q", filing_date="2026-08-20")

    parsed = parse_submissions(
        _submissions([first, cutoff, unreachable]),
        symbol="nvda",
        cik="0001045810",
        since_date=_SINCE_DATE,
    )

    assert len(parsed.items) == 1
    assert parsed.items[0].title == "4 — Statement of changes"
    assert parsed.items[0].keywords == ["sec_form:4"]
    assert parsed.form_counts == {"4": 1}


def test_column_array_length_mismatch_is_a_symbol_error() -> None:
    payload = _submissions([_row(), _row()])
    recent = payload["filings"]["recent"]  # type: ignore[index]
    recent["form"].pop()  # type: ignore[index,union-attr]

    with pytest.raises(SecEdgarError, match="column length mismatch"):
        parse_submissions(
            payload,
            symbol="NVDA",
            cik=1_045_810,
            since_date=_SINCE_DATE,
        )


def test_url_normalizes_cik_and_accession_hyphens() -> None:
    assert build_submission_url("1045810") == (
        "https://data.sec.gov/submissions/CIK0001045810.json"
    )
    assert build_archive_url(
        cik="0001045810",
        accession_number="0001045810-26-000075",
        primary_document="nvda-20260726.htm",
    ) == (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581026000075/nvda-20260726.htm"
    )


def test_acceptance_time_converts_to_naive_kst_and_missing_stays_none() -> None:
    parsed = parse_submissions(
        _submissions(
            [
                _row(acceptance="2026-08-26T16:22:57.000Z"),
                _row(
                    form="4",
                    description="Insider transaction",
                    acceptance=None,
                ),
            ]
        ),
        symbol="NVDA",
        cik=1_045_810,
        since_date=_SINCE_DATE,
    )

    assert parsed.items[0].published_at == datetime(2026, 8, 27, 1, 22, 57)
    assert parsed.items[0].published_at.tzinfo is None
    assert parsed.items[1].published_at is None


def test_title_uses_items_then_company_name_without_inventing_description() -> None:
    parsed = parse_submissions(
        _submissions(
            [
                _row(form="8-K", description="", items="2.02,9.01"),
                _row(form="10-Q", description="FORM 10-Q", items=""),
            ]
        ),
        symbol="NVDA",
        cik=1_045_810,
        since_date=_SINCE_DATE,
    )

    assert [item.title for item in parsed.items] == [
        "8-K — 2.02,9.01",
        "10-Q — NVIDIA CORP",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_reingest_and_update_preserve_one_row_and_id(
    db_session: Any,
) -> None:
    accession = _accession()
    document = f"stable-{uuid.uuid4().hex}.htm"
    first_payload = _submissions(
        [_row(accession=accession, document=document, description="Current report")]
    )
    changed_payload = _submissions(
        [
            _row(
                accession=accession,
                document=document,
                description="Amended current report",
            )
        ]
    )
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810)),
        _ok(first_payload),
        _ok(first_payload),
        _ok(changed_payload),
    )
    shared_cache = FakeTickerRedis()

    async def redis_factory() -> FakeTickerRedis:
        return shared_cache

    limiter = _NoopRateLimiter()

    first = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA"],
        since_date=_SINCE_DATE,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=limiter,
        ticker_cache=CompanyTickerCache(redis_factory=redis_factory),
    )
    url = build_archive_url(
        cik=1_045_810,
        accession_number=accession,
        primary_document=document,
    )
    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    article_id = article.id

    unchanged = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA"],
        since_date=_SINCE_DATE,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=limiter,
        ticker_cache=CompanyTickerCache(redis_factory=redis_factory),
    )
    changed = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA"],
        since_date=_SINCE_DATE,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=limiter,
        ticker_cache=CompanyTickerCache(redis_factory=redis_factory),
    )

    await db_session.refresh(article)
    assert (first.inserted, first.updated, first.skipped) == (1, 0, 0)
    assert (unchanged.inserted, unchanged.updated, unchanged.skipped) == (0, 0, 1)
    assert (changed.inserted, changed.updated, changed.skipped) == (0, 1, 0)
    assert article.id == article_id
    assert article.title == "8-K — Amended current report"
    assert article.source == "SEC EDGAR"
    assert article.feed_source == "sec"
    assert article.market == "us"
    assert article.stock_symbol == "NVDA"
    assert article.stock_name == "NVIDIA CORP"
    assert article.article_published_at == datetime(2026, 8, 27, 1, 22, 57)
    assert article.keywords == ["sec_form:8-K"]
    assert article.summary is None
    assert article.article_content is None
    assert article.is_analyzed is False
    assert (
        await db_session.scalar(
            select(func.count()).select_from(NewsArticle).where(NewsArticle.url == url)
        )
        == 1
    )
    assert [call["url"] for call in client.calls].count(SEC_COMPANY_TICKERS_URL) == 1
    assert limiter.acquisitions == 4
    assert shared_cache.expirations == [24 * 60 * 60]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmapped_etf_is_skipped_without_failing_run(db_session: Any) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810)),
        _ok(_submissions([])),
    )

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA", "TQQQ"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert result.status == "success"
    assert result.skipped == 1
    assert result.failed_symbols == ()
    assert [(issue.symbol, issue.reason) for issue in result.skipped_symbols] == [
        ("TQQQ", "ticker_not_in_company_tickers")
    ]
    assert run.status == "success"
    assert run.source_counts["sec"] == {
        "inserted": 0,
        "updated": 0,
        "skipped": 1,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_403_is_symbol_failure_and_other_success_makes_partial(
    db_session: Any,
) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810, GOOGL=1_652_044)),
        _ok(_submissions([_row()])),
        _ResponseSpec(403, b"missing or rejected User-Agent"),
    )

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA", "GOOGL"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert result.status == "partial"
    assert result.successful_symbols == 1
    assert len(result.failed_symbols) == 1
    assert result.failed_symbols[0].symbol == "GOOGL"
    assert "HTTP 403" in result.failed_symbols[0].reason
    assert run.status == "partial"
    assert "GOOGL: SEC EDGAR HTTP 403" in (run.error_message or "")
    assert all(
        call["headers"]
        == {
            "User-Agent": _TEST_USER_AGENT,
            "Accept": "application/json",
        }
        for call in client.calls
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_user_agent_fails_closed_before_http(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(_ok(_ticker_payload(NVDA=1_045_810)))

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert result.status == "failed"
    assert client.calls == []
    assert "SEC_EDGAR_USER_AGENT" in result.failed_symbols[0].reason
    assert run.status == "failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_json_is_a_symbol_failure_not_a_zero_result(
    db_session: Any,
) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810, AMD=2488)),
        _ok(_submissions([])),
        _ResponseSpec(200, b"not-json"),
    )

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA", "AMD"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert result.status == "partial"
    assert result.successful_symbols == 1
    assert result.failed_symbols[0].symbol == "AMD"
    assert "invalid JSON" in result.failed_symbols[0].reason
    assert run.status == "partial"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unexpected_exception_still_closes_failed_run(db_session: Any) -> None:
    run_uuid = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="fake SEC transport crash"):
        await ingest_sec_edgar(
            db_session,
            symbols=["NVDA"],
            since_date=_SINCE_DATE,
            run_uuid=run_uuid,
            http_client=ExplodingSecClient(),
            user_agent=_TEST_USER_AGENT,
            rate_limiter=_NoopRateLimiter(),
            ticker_cache=CompanyTickerCache(),
        )

    run = await _load_run(db_session, run_uuid)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.error_message == "fake SEC transport crash"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_failed_symbols_close_run_as_failed(db_session: Any) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810, GOOGL=1_652_044)),
        _ResponseSpec(503, b"unavailable"),
        _ResponseSpec(403, b"forbidden"),
    )

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA", "GOOGL"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert result.status == "failed"
    assert result.successful_symbols == 0
    assert len(result.failed_symbols) == 2
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.inserted_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_true_zero_filings_is_successful_zero_run(db_session: Any) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeSecClient(
        _ok(_ticker_payload(NVDA=1_045_810)),
        _ok(_submissions([])),
    )

    result = await ingest_sec_edgar(
        db_session,
        symbols=["NVDA"],
        since_date=_SINCE_DATE,
        run_uuid=run_uuid,
        http_client=client,
        user_agent=_TEST_USER_AGENT,
        rate_limiter=_NoopRateLimiter(),
        ticker_cache=CompanyTickerCache(),
    )

    run = await _load_run(db_session, run_uuid)
    assert (result.status, result.inserted, result.updated, result.skipped) == (
        "success",
        0,
        0,
        0,
    )
    assert run.status == "success"
    assert run.error_message is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_loader_uses_active_us_symbol_master(db_session: Any) -> None:
    active = f"Z{uuid.uuid4().hex[:9].upper()}"
    inactive = f"Y{uuid.uuid4().hex[:9].upper()}"
    db_session.add_all(
        [
            SymbolMaster(
                market="US",
                symbol=active,
                name="활성 미국 테스트",
                name_en="ACTIVE TEST CORP",
                security_type="COMMON_STOCK",
                is_active=True,
            ),
            SymbolMaster(
                market="US",
                symbol=inactive,
                name="비활성 미국 테스트",
                name_en="INACTIVE TEST CORP",
                security_type="COMMON_STOCK",
                is_active=False,
            ),
        ]
    )
    await db_session.flush()

    assert await load_sec_symbols(db_session, stock_symbols=[active]) == [active]
    with pytest.raises(ValueError, match="inactive or missing us symbols"):
        await load_sec_symbols(db_session, stock_symbols=[inactive])


def test_sec_task_is_registered_without_recurring_schedule() -> None:
    assert sec_edgar_ingestion_tasks in TASKIQ_TASK_MODULES
    task = sec_edgar_ingestion_tasks.ingest_sec_edgar_task
    assert task.task_name == "news.sec.ingest"
    assert "schedule" not in task.labels
