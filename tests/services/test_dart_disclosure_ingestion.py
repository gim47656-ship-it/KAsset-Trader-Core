"""DART 공시 수집과 통합 뉴스 저장 계약 테스트."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.models.news import NewsArticle, NewsIngestionRun
from app.services.disclosures import dart_list
from app.services.disclosures.ingestion import (
    DartPartialIngestionError,
    ingest_dart_disclosures,
)
from app.tasks import TASKIQ_TASK_MODULES, dart_disclosure_ingestion_tasks


class FakeDartClient:
    def __init__(self, *responses: dict[str, Any] | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: dict[str, Any]) -> httpx.Response:
        self.calls.append({"url": url, "params": dict(params)})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json=response, request=request)


@pytest.fixture(autouse=True)
def _valid_dart_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dart_list.settings, "opendart_api_key", "a" * 40)


def _rcept_no() -> str:
    return f"20260829{uuid.uuid4().int % 1_000_000:06d}"


def _row(
    *,
    rcept_no: str | None = None,
    report_nm: str = "주요사항보고서",
    corp_name: str = "테스트상장사",
    stock_code: str | None = "005930",
    rcept_dt: str = "20260829",
) -> dict[str, str]:
    row = {
        "corp_code": "00126380",
        "corp_name": corp_name,
        "report_nm": report_nm,
        "rcept_no": rcept_no or _rcept_no(),
        "rcept_dt": rcept_dt,
    }
    if stock_code is not None:
        row["stock_code"] = stock_code
    return row


def _success_payload(
    rows: list[dict[str, str]],
    *,
    page_no: int = 1,
    total_page: int = 1,
) -> dict[str, Any]:
    return {
        "status": "000",
        "message": "정상",
        "page_no": page_no,
        "total_page": total_page,
        "total_count": len(rows),
        "list": rows,
    }


async def _load_run(db_session, run_uuid: str) -> NewsIngestionRun:
    return (
        await db_session.execute(
            select(NewsIngestionRun).where(NewsIngestionRun.run_uuid == run_uuid)
        )
    ).scalar_one()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_inserts_dart_article_and_success_run(db_session) -> None:
    row = _row()
    run_uuid = str(uuid.uuid4())

    counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=run_uuid,
        dart_client=FakeDartClient(_success_payload([row])),
    )

    assert counts == (1, 0, 0)
    article = (
        await db_session.execute(
            select(NewsArticle).where(
                NewsArticle.url
                == f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']}"
            )
        )
    ).scalar_one()
    assert article.title == "주요사항보고서"
    assert article.source == "DART"
    assert article.feed_source == "dart"
    assert article.market == "kr"
    assert article.stock_symbol == "005930"
    assert article.stock_name == "테스트상장사"
    assert article.article_published_at == datetime(2026, 8, 29)
    assert article.is_analyzed is False
    assert article.article_content is None
    assert article.summary is None

    run = await _load_run(db_session, run_uuid)
    assert run.status == "success"
    assert run.finished_at is not None
    assert run.inserted_count == 1
    assert run.skipped_count == 0
    assert run.source_counts["dart"] == {
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reingest_updates_title_and_company_without_changing_id(
    db_session,
) -> None:
    rcept_no = _rcept_no()
    original = _row(rcept_no=rcept_no)
    await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=str(uuid.uuid4()),
        dart_client=FakeDartClient(_success_payload([original])),
    )
    before = (
        await db_session.execute(
            select(NewsArticle).where(
                NewsArticle.url
                == f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            )
        )
    ).scalar_one()
    original_id = before.id
    unchanged_counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=str(uuid.uuid4()),
        dart_client=FakeDartClient(_success_payload([original])),
    )
    assert unchanged_counts == (0, 0, 1)
    await db_session.refresh(before)
    assert before.id == original_id

    changed = _row(
        rcept_no=rcept_no,
        report_nm="정정 주요사항보고서",
        corp_name="테스트상장사 새이름",
        stock_code="000000",
    )
    counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=str(uuid.uuid4()),
        dart_client=FakeDartClient(_success_payload([changed])),
    )

    assert counts == (0, 1, 0)
    await db_session.refresh(before)
    assert before.id == original_id
    assert before.title == "정정 주요사항보고서"
    assert before.stock_name == "테스트상장사 새이름"
    assert before.stock_symbol == "005930"
    article_count = (
        await db_session.execute(
            select(func.count())
            .select_from(NewsArticle)
            .where(
                NewsArticle.url
                == f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            )
        )
    ).scalar_one()
    assert article_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_run_deduplicates_rcept_no_once(db_session) -> None:
    rcept_no = _rcept_no()
    first = _row(rcept_no=rcept_no, report_nm="첫 제목")
    last = _row(rcept_no=rcept_no, report_nm="마지막 제목")

    counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=str(uuid.uuid4()),
        dart_client=FakeDartClient(_success_payload([first, last])),
    )

    assert counts == (1, 0, 1)
    article = (
        await db_session.execute(
            select(NewsArticle).where(
                NewsArticle.url
                == f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            )
        )
    ).scalar_one()
    assert article.title == "마지막 제목"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disclosure_without_stock_code_is_saved_without_symbol(
    db_session,
) -> None:
    row = _row(stock_code=None, corp_name="비상장테스트법인")

    counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=str(uuid.uuid4()),
        dart_client=FakeDartClient(_success_payload([row])),
    )

    assert counts == (1, 0, 0)
    article = (
        await db_session.execute(
            select(NewsArticle).where(
                NewsArticle.url
                == f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']}"
            )
        )
    ).scalar_one()
    assert article.stock_symbol is None
    assert article.stock_name == "비상장테스트법인"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exception_closes_run_as_failed(db_session) -> None:
    run_uuid = str(uuid.uuid4())
    client = FakeDartClient(RuntimeError("fake DART client failure"))

    with pytest.raises(RuntimeError, match="fake DART client failure"):
        await ingest_dart_disclosures(
            db_session,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            run_uuid=run_uuid,
            dart_client=client,
        )

    run = await _load_run(db_session, run_uuid)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.inserted_count == 0
    assert run.error_message == "fake DART client failure"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_013_is_successful_empty_run(db_session) -> None:
    run_uuid = str(uuid.uuid4())
    counts = await ingest_dart_disclosures(
        db_session,
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        run_uuid=run_uuid,
        dart_client=FakeDartClient(
            {"status": "013", "message": "조회된 데이터가 없습니다."}
        ),
    )

    assert counts == (0, 0, 0)
    run = await _load_run(db_session, run_uuid)
    assert run.status == "success"
    assert run.error_message is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["010", "011", "020"])
async def test_fatal_dart_status_closes_failed_run(db_session, status: str) -> None:
    run_uuid = str(uuid.uuid4())

    with pytest.raises(dart_list.DartListError, match=rf"status {status}"):
        await ingest_dart_disclosures(
            db_session,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            run_uuid=run_uuid,
            dart_client=FakeDartClient(
                {"status": status, "message": f"fake status {status}"}
            ),
        )

    run = await _load_run(db_session, run_uuid)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert status in (run.error_message or "")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "UNUSED_PLACEHOLDER_KEY_1234567890123"])
async def test_missing_or_placeholder_key_closes_failed_run_before_http(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
) -> None:
    monkeypatch.setattr(dart_list.settings, "opendart_api_key", api_key)
    run_uuid = str(uuid.uuid4())
    client = FakeDartClient(_success_payload([_row()]))

    with pytest.raises(dart_list.DartListError, match="missing or placeholder"):
        await ingest_dart_disclosures(
            db_session,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            run_uuid=run_uuid,
            dart_client=client,
        )

    assert client.calls == []
    run = await _load_run(db_session, run_uuid)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "OPENDART_API_KEY" in (run.error_message or "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_page_failure_commits_counts_and_partial_run(db_session) -> None:
    row = _row()
    run_uuid = str(uuid.uuid4())
    client = FakeDartClient(
        _success_payload([row], page_no=1, total_page=2),
        {"status": "020", "message": "요청 제한을 초과하였습니다."},
    )

    with pytest.raises(DartPartialIngestionError) as caught:
        await ingest_dart_disclosures(
            db_session,
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            run_uuid=run_uuid,
            dart_client=client,
        )

    assert caught.value.counts.inserted == 1
    run = await _load_run(db_session, run_uuid)
    assert run.status == "partial"
    assert run.finished_at is not None
    assert run.inserted_count == 1
    assert "020" in (run.error_message or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_json_pagination_stops_at_last_page_and_uses_page_count_100() -> (
    None
):
    first = _row()
    second = _row()
    client = FakeDartClient(
        _success_payload([first], page_no=1, total_page=2),
        _success_payload([second], page_no=2, total_page=2),
    )

    rows = await dart_list.fetch_disclosure_list(
        date(2026, 8, 29),
        date(2026, 8, 29),
        client=client,
    )

    assert rows == [first, second]
    assert [call["params"]["page_no"] for call in client.calls] == [1, 2]
    assert all(call["params"]["page_count"] == 100 for call in client.calls)
    assert all(call["url"] == dart_list.DART_LIST_URL for call in client.calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_json_symbol_scope_filters_on_stock_code() -> None:
    wanted = _row(stock_code="005930")
    other = _row(stock_code="000660")
    client = FakeDartClient(_success_payload([wanted, other]))

    rows = await dart_list.fetch_disclosure_list(
        date(2026, 8, 29),
        date(2026, 8, 29),
        stock_symbols=["005930"],
        client=client,
    )

    assert rows == [wanted]


@pytest.mark.unit
def test_dart_ingestion_task_is_registered_without_recurring_schedule() -> None:
    assert dart_disclosure_ingestion_tasks in TASKIQ_TASK_MODULES
    task = dart_disclosure_ingestion_tasks.ingest_dart_disclosures_task
    assert task.task_name == "news.dart.ingest"
    assert "schedule" not in task.labels
