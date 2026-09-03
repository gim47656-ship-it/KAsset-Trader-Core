"""Contract tests for the mobile AI hub briefing endpoint.

The reader is exercised through the Android facade with a statement-dispatching
fake session, so the observable contract (stored-data-only mapping, sanitized
errors, truthful empty state) is asserted without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.db import get_db
from app.extensions.kasset.api.auth import get_mobile_session
from app.extensions.kasset.api.daily_routine_schemas import DailyRoutineAlert
from app.extensions.kasset.api.installation import install_android_compat_api
from app.extensions.kasset.daily_routine_service import daily_routine_service
from app.models.investment_reports import InvestmentReport
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.models.research_reports import ResearchReport

NOW = datetime(2026, 8, 26, 3, 0, 0, tzinfo=UTC)

NEWS_ITEM_KEYS = {
    "id",
    "headline",
    "source",
    "publishedAt",
    "market",
    "symbols",
    "canonicalUrl",
    "summary",
    "dataUpdatedAt",
}
RESEARCH_ITEM_KEYS = {
    "id",
    "title",
    "provider",
    "publishedAt",
    "publishedAtText",
    "market",
    "symbols",
    "canonicalUrl",
    "excerpt",
    "dataUpdatedAt",
}
BRIEFING_KEYS = {
    "status",
    "id",
    "title",
    "summary",
    "provider",
    "market",
    "asOf",
    "validUntil",
    "dataStatus",
    "unavailableReason",
}


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any | None:
        if len(self._rows) > 1:
            raise RuntimeError("expected at most one fake row")
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        if len(self._rows) != 1:
            raise RuntimeError("expected exactly one fake row")
        return self._rows[0]


class _FakeSession:
    """Dispatches each ORM statement to canned rows keyed by entity name."""

    def __init__(
        self,
        rows: dict[str, list[Any]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._rows = rows or {}
        self._error = error
        self.statements: dict[str, Any] = {}

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        if self._error is not None:
            raise self._error
        descriptions = statement.column_descriptions
        entity = descriptions[0]["entity"].__name__
        self.statements[entity] = statement
        rows = self._rows.get(entity, [])
        if entity == "NewsAnalysisResult":
            rows = [row[: len(descriptions)] for row in rows]
        return _FakeResult(rows)

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any | None:
        return (await self.execute(statement, *args, **kwargs)).first()

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        return (await self.execute(statement, *args, **kwargs)).scalars()


def _client(
    session: _FakeSession | None = None, *, authenticated: bool = True
) -> TestClient:
    app = FastAPI()
    install_android_compat_api(app)

    async def _db_override() -> AsyncIterator[Any]:
        yield session if session is not None else _FakeSession()

    app.dependency_overrides[get_db] = _db_override
    if authenticated:

        async def _session_override() -> object:
            return SimpleNamespace(user=SimpleNamespace(id=42))

        app.dependency_overrides[get_mobile_session] = _session_override
    return TestClient(app)


def _compiled(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _article(
    article_id: int,
    *,
    market: str = "kr",
    published_at: datetime | None = None,
    summary: str | None = None,
) -> NewsArticle:
    return NewsArticle(
        id=article_id,
        url=f"https://news.example.com/{article_id}",
        title=f"헤드라인 {article_id}",
        source="매일경제",
        market=market,
        article_content="기사 본문 전체 — 절대 노출되면 안 된다.",
        summary=summary,
        article_published_at=published_at or NOW - timedelta(hours=1),
        scraped_at=NOW - timedelta(minutes=30),
        created_at=NOW - timedelta(minutes=30),
        updated_at=NOW - timedelta(minutes=10),
    )


def _related(
    article_id: int,
    *,
    symbol: str,
    market: str = "kr",
    source: str = "matcher",
    rank: int | None = None,
) -> NewsArticleRelatedSymbol:
    return NewsArticleRelatedSymbol(
        id=article_id * 100 + (rank or 0),
        article_id=article_id,
        market=market,
        symbol=symbol,
        source=source,
        rank=rank,
        created_at=NOW,
    )


def _report(
    report_id: int,
    *,
    published_at: datetime | None = None,
    excerpt: str | None = "발췌문",
    candidates: list[dict[str, Any]] | None = None,
) -> ResearchReport:
    if candidates is None:
        candidates = [{"symbol": "005930", "market": "kr", "source": "title"}]
    return ResearchReport(
        id=report_id,
        dedup_key=f"dedup-{report_id}",
        report_type="company",
        source="kis_research",
        title=f"리서치 {report_id}",
        analyst="김분석",
        # Research staleness is measured against the real read clock, so these
        # rows stay relative to "now" instead of the frozen fixture instant.
        published_at=published_at or datetime.now(UTC) - timedelta(hours=5),
        published_at_text="2026.08.26",
        summary_text="요약 텍스트",
        detail_excerpt=excerpt,
        detail_url=f"https://research.example.com/{report_id}",
        pdf_url=f"https://research.example.com/{report_id}.pdf",
        symbol_candidates=candidates,
        attribution_publisher="한국투자증권",
        attribution_copyright_notice="© 한국투자증권",
        created_at=NOW - timedelta(hours=5),
        updated_at=NOW - timedelta(hours=4),
    )


def _investment_report(
    report_id: int = 7,
    *,
    freshness: dict[str, Any] | None = None,
    valid_until: datetime | None = None,
) -> InvestmentReport:
    return InvestmentReport(
        id=report_id,
        idempotency_key=f"idem-{report_id}",
        report_type="daily_briefing",
        market="kr",
        account_scope=None,
        execution_mode="advisory_only",
        created_by_profile="hermes_daily",
        title="KR 데일리 브리핑",
        summary="지수는 보합, 반도체 강세.",
        market_snapshot={"kospi": 2700},
        portfolio_snapshot={"cash": 1000000},
        status="published",
        report_metadata={},
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=3),
        published_at=NOW - timedelta(hours=2),
        valid_until=valid_until,
        snapshot_freshness_summary=freshness,
    )


def test_briefing_requires_an_authenticated_mobile_session() -> None:
    with _client(authenticated=False) as client:
        response = client.get("/api/v1/ai/briefing?market=kr")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_briefing_rejects_unknown_market_and_out_of_range_limit() -> None:
    with _client() as client:
        bad_market = client.get("/api/v1/ai/briefing?market=jp")
        zero_limit = client.get("/api/v1/ai/briefing?market=kr&limit=0")
        over_limit = client.get("/api/v1/ai/briefing?market=kr&limit=51")
        bad_symbol = client.get("/api/v1/ai/briefing?market=kr&symbol=005930%20OR%201")

    for response in (bad_market, zero_limit, over_limit, bad_symbol):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_empty_stores_return_http_200_with_truthful_empty_state() -> None:
    with _client(_FakeSession()) as client:
        response = client.get("/api/v1/ai/briefing?market=us")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["asOf"].endswith("Z")
    assert body["news"] == {"status": "empty", "refreshedAt": None, "items": []}
    assert body["research"] == {"status": "empty", "refreshedAt": None, "items": []}
    assert body["briefing"]["status"] == "unavailable"
    assert body["routineAlerts"] == []
    assert (
        body["briefing"]["unavailableReason"]
        == "저장된 AI 브리핑 제공자가 아직 연결되지 않았습니다."
    )
    assert body["briefing"]["id"] is None
    assert body["briefing"]["dataStatus"] == "unknown"


def test_daily_routine_alerts_join_owner_ai_context_as_read_only_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_owner_ids: list[int] = []

    async def load_alerts(
        _db: Any, owner_user_id: int, *, now: datetime
    ) -> list[DailyRoutineAlert]:
        observed_owner_ids.append(owner_user_id)
        return [
            DailyRoutineAlert(
                id="news:701",
                kind="TRUMP_POLICY",
                headline="Trump tariff policy changes",
                summary=None,
                translated_title="Trump 관세 정책 변경",
                translated_excerpt=(
                    "Trump 행정부는 저장된 영문 기사에서 관세 정책 변경을 밝혔다."
                ),
                source="Reuters",
                occurred_at=now,
            )
        ]

    monkeypatch.setattr(daily_routine_service, "get_alerts", load_alerts)
    with _client(_FakeSession()) as client:
        body = client.get("/api/v1/ai/briefing?market=us").json()

    assert observed_owner_ids == [42]
    assert body["status"] == "available"
    assert body["routineAlerts"] == [
        {
            "id": "news:701",
            "kind": "TRUMP_POLICY",
            "headline": "Trump tariff policy changes",
            "summary": None,
            "translatedTitle": "Trump 관세 정책 변경",
            "translatedExcerpt": (
                "Trump 행정부는 저장된 영문 기사에서 관세 정책 변경을 밝혔다."
            ),
            "symbol": None,
            "market": None,
            "source": "Reuters",
            "url": None,
            "occurredAt": body["routineAlerts"][0]["occurredAt"],
            "detectedRatePct": None,
            "currentRatePct": None,
            "recovered": False,
            "lastSeenAt": None,
        }
    ]


def test_query_failure_is_a_sanitized_5xx_not_an_empty_payload() -> None:
    session = _FakeSession(
        error=SQLAlchemyError("relation news_articles does not exist")
    )
    with _client(session) as client:
        response = client.get("/api/v1/ai/briefing?market=kr")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "AI_BRIEFING_UNAVAILABLE"
    assert "news_articles" not in error["message"]


def test_news_items_expose_stored_rows_persisted_symbols_and_stored_summary() -> None:
    session = _FakeSession(
        {
            "NewsArticle": [_article(11)],
            "NewsArticleRelatedSymbol": [
                _related(11, symbol="005930", rank=1),
                _related(11, symbol="005930", source="body", rank=2),
                _related(11, symbol="000660", rank=3),
            ],
            "NewsAnalysisResult": [
                (11, "저장된 분석 요약", "번역된 헤드라인 11", "헤드라인 11"),
                (11, "오래된 분석 요약", "이전 헤드라인 11", "헤드라인 11"),
            ],
        }
    )
    with _client(session) as client:
        body = client.get("/api/v1/ai/briefing?market=kr").json()

    assert body["status"] == "available"
    assert body["news"]["status"] == "available"
    assert body["news"]["refreshedAt"] == "2026-08-26T02:50:00Z"
    item = body["news"]["items"][0]
    assert set(item) == NEWS_ITEM_KEYS
    assert item == {
        "id": "news:11",
        "headline": "번역된 헤드라인 11",
        "source": "매일경제",
        "publishedAt": "2026-08-26T02:00:00Z",
        "market": "kr",
        "symbols": [
            {"symbol": "005930", "market": "kr"},
            {"symbol": "000660", "market": "kr"},
        ],
        "canonicalUrl": "https://news.example.com/11",
        "summary": "저장된 분석 요약",
        "dataUpdatedAt": "2026-08-26T02:50:00Z",
    }


def test_news_without_complete_korean_analysis_is_not_exposed() -> None:
    session = _FakeSession()
    with _client(session) as client:
        body = client.get("/api/v1/ai/briefing?market=kr").json()

    assert body["news"] == {"status": "empty", "refreshedAt": None, "items": []}


def test_news_query_is_newest_first_limit_bounded_and_symbol_scoped() -> None:
    session = _FakeSession(
        {
            "NewsArticle": [_article(13)],
            "NewsAnalysisResult": [
                (13, "저장된 요약", "번역된 헤드라인 13", "헤드라인 13")
            ],
        }
    )
    with _client(session) as client:
        client.get("/api/v1/ai/briefing?market=kr&symbol=005930&limit=3")

    sql, params = _compiled(session.statements["NewsArticle"])
    assert "news_articles.article_published_at DESC NULLS LAST" in sql
    assert "news_articles.id DESC" in sql
    assert "LIMIT" in sql
    assert 3 in params.values()
    assert "kr" in params.values()
    assert "005930" in params.values()
    assert "news_article_related_symbols" in sql
    assert "news_analysis_results" in sql
    assert "news_articles.feed_source NOT IN" in sql


def test_research_items_are_citation_only_and_limit_bounded() -> None:
    long_excerpt = "가" * 900
    session = _FakeSession(
        {
            "ResearchReport": [
                _report(21, excerpt=long_excerpt),
                _report(22),
                _report(23),
            ]
        }
    )
    with _client(session) as client:
        body = client.get("/api/v1/ai/briefing?market=kr&limit=2").json()

    research = body["research"]
    assert research["status"] == "available"
    assert [item["id"] for item in research["items"]] == [
        "research-report:21",
        "research-report:22",
    ]
    first = research["items"][0]
    assert set(first) == RESEARCH_ITEM_KEYS
    assert first["provider"] == "한국투자증권"
    assert first["canonicalUrl"] == "https://research.example.com/21"
    assert first["excerpt"] == "가" * 500
    assert first["publishedAtText"] == "2026.08.26"
    assert first["symbols"] == [{"symbol": "005930", "market": "kr", "source": "title"}]


def test_research_section_is_stale_when_the_newest_report_is_old() -> None:
    session = _FakeSession(
        {
            "ResearchReport": [
                _report(24, published_at=datetime.now(UTC) - timedelta(days=10))
            ]
        }
    )
    with _client(session) as client:
        body = client.get("/api/v1/ai/briefing?market=kr").json()

    assert body["research"]["status"] == "stale"
    assert body["research"]["items"][0]["id"] == "research-report:24"


def test_briefing_projects_an_eligible_report_without_snapshot_payloads() -> None:
    session = _FakeSession(
        {"InvestmentReport": [_investment_report(freshness={"overall": "fresh"})]}
    )
    with _client(session) as client:
        body = client.get("/api/v1/ai/briefing?market=kr").json()

    briefing = body["briefing"]
    assert set(briefing) == BRIEFING_KEYS
    assert briefing == {
        "status": "available",
        "id": "investment-report:7",
        "title": "KR 데일리 브리핑",
        "summary": "지수는 보합, 반도체 강세.",
        "provider": "hermes_daily",
        "market": "kr",
        "asOf": "2026-08-26T01:00:00Z",
        "validUntil": None,
        "dataStatus": "fresh",
        "unavailableReason": None,
    }
    # A briefing alone is not enough to claim the hub has content.
    assert body["status"] == "empty"


def test_briefing_marks_soft_stale_snapshot_data_as_stale() -> None:
    session = _FakeSession(
        {"InvestmentReport": [_investment_report(freshness={"overall": "soft_stale"})]}
    )
    with _client(session) as client:
        briefing = client.get("/api/v1/ai/briefing?market=kr").json()["briefing"]

    assert briefing["status"] == "stale"
    assert briefing["dataStatus"] == "soft_stale"


def test_briefing_query_only_accepts_advisory_unexpired_account_free_reports() -> None:
    session = _FakeSession({"InvestmentReport": [_investment_report()]})
    with _client(session) as client:
        client.get("/api/v1/ai/briefing?market=kr")

    sql, params = _compiled(session.statements["InvestmentReport"])
    assert "review.investment_reports.account_scope IS NULL" in sql
    assert "review.investment_reports.valid_until IS NULL" in sql
    assert "review.investment_reports.valid_until >" in sql
    assert "advisory_only" in params.values()
    assert any(
        isinstance(value, list | tuple) and tuple(value) == ("published", "decided")
        for value in params.values()
    )
    assert "LIMIT" in sql


def test_existing_ai_status_route_still_exists_next_to_the_briefing_route() -> None:
    with _client() as client:
        schema = client.app.openapi()

    paths = schema["paths"]
    assert "get" in paths["/api/v1/ai/status"]
    assert "get" in paths["/api/v1/ai/briefing"]

    # `/system/status`의 `ai`와 `/ai/status`는 **같은 스키마**를 쓴다. 두 화면이 서로
    # 다른 AI 상태 모양을 읽던 시절(`configured` vs `relayConfigured`)로 돌아가지 않게
    # 계약 수준에서 못박는다.
    ai_status_ref = paths["/api/v1/ai/status"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ai_status_ref.endswith("/AiAvailabilityStatus")
    system_ai = schema["components"]["schemas"]["SystemStatus"]["properties"]["ai"]
    assert system_ai["$ref"] == ai_status_ref


def test_ai_status_reports_available_when_direct_api_works_without_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영 실제 상태: MCP relay는 미설정이고 direct API/OpenRouter만 설정되어 있다.

    예전 라우트는 이 상황에서도 ``relayConfigured=false``를 상수로 돌려주어 앱이 AI를
    영구 차단했다. 이제는 정책에 활성인 route의 실제 사용 가능 여부를 본다.
    """

    _configure_direct_api_only(monkeypatch)

    with _client() as client:
        body = client.get("/api/v1/ai/status").json()

    assert body == {
        "configured": True,
        "available": True,
        "unavailableReason": None,
        "message": "AI를 사용할 수 있습니다.",
    }


def test_ai_status_fails_closed_with_a_korean_reason_when_no_route_is_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_direct_api_only(monkeypatch)
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", None)
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_API_KEY", None)

    with _client() as client:
        body = client.get("/api/v1/ai/status").json()

    assert body["configured"] is True
    assert body["available"] is False
    assert body["unavailableReason"] == "routes_unavailable"
    assert body["message"].startswith("설정된 AI 경로를 지금 사용할 수 없습니다.")
    assert "서버에 API 키가 설정되어 있지 않습니다." in body["message"]


def test_ai_status_never_leaks_credentials_or_internal_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_direct_api_only(monkeypatch)

    with _client() as client:
        blob = client.get("/api/v1/ai/status").text

    for secret in ("direct-secret-key", "openrouter-secret-key"):
        assert secret not in blob
    assert "direct.invalid" not in blob
    assert "router.invalid" not in blob


def _configure_direct_api_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """direct API + OpenRouter는 설정되고 선택 사항인 MCP/구독은 비어 있는 서버."""

    monkeypatch.setattr(settings, "KASSET_AI_API_BASE_URL", "https://direct.invalid/v1")
    monkeypatch.setattr(settings, "KASSET_AI_API_KEY", SecretStr("direct-secret-key"))
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_LUNA", "model-luna")
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_TERRA", "model-terra")
    monkeypatch.setattr(settings, "KASSET_AI_MODEL_SOL", "model-sol")
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_BASE_URL", "https://router.invalid/api/v1"
    )
    monkeypatch.setattr(
        settings, "KASSET_AI_OPENROUTER_API_KEY", SecretStr("openrouter-secret-key")
    )
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_FLASH", "model-flash")
    monkeypatch.setattr(settings, "KASSET_AI_OPENROUTER_MODEL_PRO", "model-pro")
    monkeypatch.setattr(settings, "KASSET_AI_MCP_URL", "")
    monkeypatch.setattr(settings, "KASSET_AI_SUBSCRIPTION_CMD", "")
