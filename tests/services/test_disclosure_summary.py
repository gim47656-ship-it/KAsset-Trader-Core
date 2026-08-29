"""공시 AI 요약의 strict output, 멱등 저장, 수집/TaskIQ 배선 테스트."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.news import NewsArticle
from app.services import symbol_news_store
from app.services.disclosures.summary_service import (
    DisclosureSummaryInput,
    OpenAiDisclosureSummaryGenerator,
    summarize_pending_disclosures,
)
from app.services.symbol_news_store import DisclosureArticleInput


class FakeResponsesClient:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[dict[str, object]] = []

    async def request_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"summary": self.summary}


class SequenceResponsesClient:
    def __init__(self, summaries: list[str]) -> None:
        self.summaries = summaries
        self.calls: list[dict[str, object]] = []

    async def request_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"summary": self.summaries[len(self.calls) - 1]}


class FakeBodyFetcher:
    def __init__(self, outcomes: dict[str, str | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def fetch(self, url: str) -> str:
        self.calls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSummaryGenerator:
    def __init__(self) -> None:
        self.calls: list[DisclosureSummaryInput] = []

    async def summarize(self, disclosure: DisclosureSummaryInput) -> str:
        self.calls.append(disclosure)
        return (
            "회사는 공급 계약 체결 사실을 공시했다. "
            "계약 이행 일정은 원문에 기재되어 있다고 밝혔다."
        )


async def _insert_disclosure(
    db_session,
    *,
    url: str,
    title: str,
    published_at: datetime,
) -> None:
    await symbol_news_store.upsert_disclosures(
        db_session,
        [
            DisclosureArticleInput(
                url=url,
                title=title,
                source="DART",
                feed_source="dart",
                market="kr",
                stock_symbol="005930",
                stock_name="테스트상장사",
                published_at=published_at,
            )
        ],
    )
    await db_session.commit()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_uses_strict_low_cost_summary_contract() -> None:
    client = FakeResponsesClient(
        "매출은 100억원으로 공시됐다. 회사는 기존 가이던스를 유지했다."
    )
    generator = OpenAiDisclosureSummaryGenerator(client, model="gpt-5.6-luna")

    result = await generator.summarize(
        DisclosureSummaryInput(
            title="분기보고서",
            company="테스트상장사",
            form="10-Q",
            body_excerpt="분기 매출은 100억원이다. 기존 가이던스를 유지한다.",
        )
    )

    assert result == "매출은 100억원으로 공시됐다. 회사는 기존 가이던스를 유지했다."
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning_effort"] == "low"
    assert call["schema_name"] == "kasset_disclosure_summary"
    assert call["schema"]["additionalProperties"] is False
    assert call["input_payload"] == {
        "title": "분기보고서",
        "company": "테스트상장사",
        "form": "10-Q",
        "body_excerpt": "분기 매출은 100억원이다. 기존 가이던스를 유지한다.",
    }
    assert "목표주가" in call["additional_instructions"]
    assert "추측" in call["additional_instructions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_accepts_english_month_translated_to_korean_number() -> (
    None
):
    client = FakeResponsesClient(
        "회사는 2026년 7월 26일 분기 실적을 발표했다. 매출과 순이익을 공시했다."
    )
    generator = OpenAiDisclosureSummaryGenerator(client, model="gpt-5.6-luna")

    result = await generator.summarize(
        DisclosureSummaryInput(
            title="분기 실적",
            company="테스트상장사",
            form="8-K",
            body_excerpt=(
                "The quarter ended July 26, 2026. "
                "Revenue and net income were disclosed."
            ),
        )
    )

    assert "2026년 7월 26일" in result
    assert len(client.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_retries_numeric_unit_conversion_once() -> None:
    client = SequenceResponsesClient(
        [
            "매출은 96.2 billion으로 공시됐다. 회사는 분기 실적을 발표했다.",
            "매출은 96,221 million으로 공시됐다. 회사는 분기 실적을 발표했다.",
        ]
    )
    generator = OpenAiDisclosureSummaryGenerator(client, model="gpt-5.6-luna")

    result = await generator.summarize(
        DisclosureSummaryInput(
            title="분기 실적",
            company="테스트상장사",
            form="8-K",
            body_excerpt="분기 매출은 96,221 million이다.",
        )
    )

    assert result.startswith("매출은 96,221 million")
    assert len(client.calls) == 2
    assert client.calls[1]["schema_name"] == "kasset_disclosure_summary_retry"
    assert "million/billion 환산" in client.calls[1]["additional_instructions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_generator_rejects_number_absent_from_source() -> None:
    generator = OpenAiDisclosureSummaryGenerator(
        FakeResponsesClient(
            "매출은 999억원으로 공시됐다. 회사는 기존 가이던스를 유지했다."
        ),
        model="gpt-5.6-luna",
    )

    with pytest.raises(ValueError, match="numbers absent from source"):
        await generator.summarize(
            DisclosureSummaryInput(
                title="분기보고서",
                company="테스트상장사",
                form="10-Q",
                body_excerpt="회사는 매출과 기존 가이던스를 공시했다.",
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_batch_isolates_failure_persists_success_and_is_idempotent(
    db_session,
) -> None:
    suffix = uuid.uuid4().hex
    successful_url = (
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260829{suffix[:6]}"
    )
    failed_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260829{suffix[6:12]}"
    await _insert_disclosure(
        db_session,
        url=successful_url,
        title="공급계약 체결",
        published_at=datetime(2026, 8, 29, 10, 0),
    )
    await _insert_disclosure(
        db_session,
        url=failed_url,
        title="신규시설 투자",
        published_at=datetime(2026, 8, 29, 9, 0),
    )
    fetcher = FakeBodyFetcher(
        {
            successful_url: "회사는 공급 계약 체결 사실과 이행 일정을 공시했다.",
            failed_url: RuntimeError("fake body fetch failure"),
        }
    )
    generator = FakeSummaryGenerator()

    first = await summarize_pending_disclosures(
        db_session,
        batch_size=2,
        fetcher=fetcher,
        generator=generator,
    )

    assert first.status == "partial"
    assert first.selected == 2
    assert first.summarized == 1
    assert first.failed == 1
    successful = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.url == successful_url)
    )
    failed = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.url == failed_url)
    )
    assert successful is not None
    assert successful.summary == (
        "회사는 공급 계약 체결 사실을 공시했다. "
        "계약 이행 일정은 원문에 기재되어 있다고 밝혔다."
    )
    assert successful.is_analyzed is True
    assert failed is not None
    assert failed.summary is None
    assert failed.is_analyzed is False

    fetcher.outcomes[failed_url] = "회사는 신규시설 투자 사실과 일정을 공시했다."
    second = await summarize_pending_disclosures(
        db_session,
        batch_size=2,
        fetcher=fetcher,
        generator=generator,
    )

    assert second.status == "success"
    assert second.selected == 1
    assert second.summarized == 1
    await db_session.refresh(successful)
    await db_session.refresh(failed)
    assert successful.is_analyzed is True
    assert failed.is_analyzed is True
    assert len(generator.calls) == 2
    assert fetcher.calls.count(successful_url) == 1
    assert fetcher.calls.count(failed_url) == 2

    third = await summarize_pending_disclosures(
        db_session,
        batch_size=2,
        fetcher=fetcher,
        generator=generator,
    )
    assert third.selected == 0
    assert len(generator.calls) == 2


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingestion_jobs_enable_exact_post_ingest_summary_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import dart_disclosure_ingestion as dart_job
    from app.jobs import sec_edgar_ingestion as sec_job

    dart_kwargs: dict[str, object] = {}
    sec_kwargs: dict[str, object] = {}

    async def fake_dart_ingest(db, **kwargs):
        dart_kwargs.update(kwargs)
        return 1, 0, 0

    async def fake_load_symbols(db, **kwargs):
        return ["NVDA"]

    async def fake_sec_ingest(db, **kwargs):
        sec_kwargs.update(kwargs)
        return SimpleNamespace(
            status="success",
            run_uuid=kwargs["run_uuid"],
            successful_symbols=1,
            inserted=1,
            updated=0,
            skipped=0,
            skipped_symbols=(),
            failed_symbols=(),
            form_counts={"10-Q": 1},
        )

    monkeypatch.setattr(dart_job, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(dart_job, "ingest_dart_disclosures", fake_dart_ingest)
    monkeypatch.setattr(sec_job, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(sec_job, "load_sec_symbols", fake_load_symbols)
    monkeypatch.setattr(sec_job, "ingest_sec_edgar", fake_sec_ingest)

    dart_result = await dart_job.run_dart_disclosure_ingestion(
        from_date=date(2026, 8, 29),
        to_date=date(2026, 8, 29),
    )
    sec_result = await sec_job.run_sec_edgar_ingestion(
        since_date=date(2026, 8, 29),
    )

    assert dart_result["status"] == "success"
    assert sec_result["status"] == "success"
    assert dart_kwargs["summarize_after_ingest"] is True
    assert sec_kwargs["summarize_after_ingest"] is True


@pytest.mark.unit
def test_disclosure_summary_task_is_registered_without_schedule() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, disclosure_summary_tasks

    assert disclosure_summary_tasks in TASKIQ_TASK_MODULES
    task = disclosure_summary_tasks.summarize_disclosures_task
    assert task.task_name == "news.disclosures.summarize"
    assert "schedule" not in task.labels
