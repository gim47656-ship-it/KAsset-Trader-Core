from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.ai.base import AiProviderUnavailable
from app.extensions.kasset.ai.model_router import AnalysisKind
from app.extensions.kasset.automation.market_pipeline import MarketEventPipeline
from app.models.ai_recommendations import AIRecommendation
from app.models.trading import User, UserRole
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)

_NOW = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)


class FakeRouter:
    def __init__(self, verdict: SimpleNamespace | None = None) -> None:
        self.verdict = verdict
        self.calls: list[tuple[AnalysisKind, dict[str, object], str | None]] = []

    async def analyze(
        self,
        kind: AnalysisKind,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((kind, payload, correlation_id))
        assert self.verdict is not None
        return self.verdict


class UnavailableRouter(FakeRouter):
    async def analyze(
        self,
        kind: AnalysisKind,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((kind, payload, correlation_id))
        raise AiProviderUnavailable("not configured")


@pytest_asyncio.fixture
async def owner(db_session: AsyncSession):
    username = f"market-pipeline-{uuid4().hex}"
    row = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="unused-test-hash",
        role=UserRole.trader,
        is_active=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    owner_id = row.id
    try:
        yield owner_id
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.id == owner_id))
        await db_session.commit()


def _candles(*, latest_close: float = 100.0) -> list[DailyCandleRow]:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    closes = [100.0] * 20 + [latest_close]
    return [
        DailyCandleRow(
            time_utc=start + timedelta(days=index),
            symbol="005930",
            partition="KRX",
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            adj_close=close,
            volume=100.0,
            value=0.0,
            source="test",
        )
        for index, close in enumerate(closes)
    ]


def _stub_candles(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[DailyCandleRow],
) -> None:
    async def fetch_recent(
        _repository: DailyCandlesRepository,
        *,
        market: MarketKey,
        symbol: str,
        partition: str,
        count: int,
    ) -> list[DailyCandleRow]:
        assert (market, symbol, partition, count) == (
            MarketKey.KR,
            "005930",
            "KRX",
            40,
        )
        return rows

    monkeypatch.setattr(DailyCandlesRepository, "fetch_recent", fetch_recent)


@pytest.mark.asyncio
async def test_no_trigger_skips_without_calling_router(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_candles(monkeypatch, _candles())
    router = FakeRouter()

    result = await MarketEventPipeline(
        db_session, router, lambda: _NOW
    ).run_symbol_scan(
        1,
        "KRX",
        "005930",
    )

    assert result == {"skipped": "no_triggers"}
    assert router.calls == []


@pytest.mark.asyncio
async def test_high_confidence_buy_persists_owner_scoped_pending_recommendation(
    db_session: AsyncSession,
    owner: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_candles(monkeypatch, _candles(latest_close=103.0))
    router = FakeRouter(
        SimpleNamespace(
            action="BUY",
            confidence=0.91,
            risk="LOW",
            bullish_score=88,
            bearish_score=12,
            escalate=False,
            rationale_tags=["price_break", "volume_confirmed"],
            tier_used="gpt-5.6-terra",
            kind=AnalysisKind.CANDIDATE_SCAN,
            correlation_id=None,
        )
    )

    result = await MarketEventPipeline(
        db_session, router, lambda: _NOW
    ).run_symbol_scan(
        owner,
        "KRX",
        "005930",
    )

    recommendation = await db_session.scalar(
        select(AIRecommendation).where(AIRecommendation.owner_user_id == owner)
    )
    assert recommendation is not None
    assert result == {
        "action": "BUY",
        "confidence": 0.91,
        "tier_used": "gpt-5.6-terra",
        "recommendation_id": recommendation.id,
    }
    assert recommendation.decision == "PENDING"
    assert recommendation.market == "KRX"
    assert recommendation.symbol == "005930"
    assert recommendation.rationale == ["price_break", "volume_confirmed"]
    assert recommendation.risks == ["LOW"]
    assert recommendation.confidence == "0.91"
    assert recommendation.valid_until == _NOW + timedelta(hours=1)
    assert recommendation.evidence == [
        {
            "title": "탐지 신호: price_spike, rsi_extreme, breakout",
            "source": "event_detector",
        },
        {
            "title": "AI 판정 모델: gpt-5.6-terra",
            "source": "model_router",
        },
    ]
    # The stored row must round-trip through the mobile API response schema;
    # a shape drift here breaks GET /api/v1/ai/recommendations for real rows.
    from app.schemas.ai_recommendations import RecommendationResponse

    RecommendationResponse.model_validate(recommendation)
    assert len(router.calls) == 1
    kind, payload, correlation_id = router.calls[0]
    assert kind == AnalysisKind.CANDIDATE_SCAN
    assert payload["symbol"] == "005930"
    assert payload["market"] == "KRX"
    assert payload["triggers"] == ["price_spike", "rsi_extreme", "breakout"]
    assert payload["news"] == []
    features = payload["features"]
    assert features["insufficient"] is False
    assert features["change_pct"] == 3.0
    assert features["rsi14"] == 100.0
    assert features["high20_break"] is True
    assert features["low20_break"] is False
    assert features["sma20"] == 100.15
    assert features["sma20_distance_pct"] == pytest.approx(
        ((103.0 - 100.15) / 100.15) * 100.0
    )
    assert features["volume_ratio"] == 1.0
    assert correlation_id is not None and correlation_id.startswith(
        f"market-scan:{owner}:KRX:005930:"
    )


@pytest.mark.parametrize(
    ("action", "confidence"),
    [
        ("HOLD", 0.99),
        ("IGNORE", 0.99),
        ("REVIEW", 0.99),
        ("BUY", 0.59),
        ("SELL", 0.59),
    ],
)
@pytest.mark.asyncio
async def test_non_actionable_verdicts_are_not_persisted(
    db_session: AsyncSession,
    owner: int,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    confidence: float,
) -> None:
    _stub_candles(monkeypatch, _candles(latest_close=103.0))
    router = FakeRouter(
        SimpleNamespace(
            action=action,
            confidence=confidence,
            risk="LOW",
            rationale_tags=[],
            tier_used="gpt-5.6-luna",
        )
    )

    result = await MarketEventPipeline(
        db_session, router, lambda: _NOW
    ).run_symbol_scan(
        owner,
        "KRX",
        "005930",
    )

    stored = await db_session.scalar(
        select(func.count())
        .select_from(AIRecommendation)
        .where(AIRecommendation.owner_user_id == owner)
    )
    assert result == {
        "action": action,
        "confidence": confidence,
        "tier_used": "gpt-5.6-luna",
    }
    assert stored == 0


@pytest.mark.asyncio
async def test_ai_unavailable_skips_without_persisting(
    db_session: AsyncSession,
    owner: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_candles(monkeypatch, _candles(latest_close=103.0))
    router = UnavailableRouter()
    before = await db_session.scalar(
        select(func.count())
        .select_from(AIRecommendation)
        .where(AIRecommendation.owner_user_id == owner)
    )

    result = await MarketEventPipeline(
        db_session, router, lambda: _NOW
    ).run_symbol_scan(
        owner,
        "KRX",
        "005930",
    )

    after = await db_session.scalar(
        select(func.count())
        .select_from(AIRecommendation)
        .where(AIRecommendation.owner_user_id == owner)
    )
    assert result == {"skipped": "ai_unavailable"}
    assert before == after == 0
    assert len(router.calls) == 1


def test_market_event_task_is_registered() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, kasset_market_events_tasks

    assert kasset_market_events_tasks in TASKIQ_TASK_MODULES
    assert (
        kasset_market_events_tasks.kasset_market_events_run.task_name
        == "kasset_market_events.run"
    )


@pytest.mark.asyncio
async def test_disabled_market_event_task_is_database_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.tasks import kasset_market_events_tasks

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", False)

    def forbidden_session() -> None:
        raise AssertionError("disabled task must not open a database session")

    monkeypatch.setattr(
        kasset_market_events_tasks,
        "AsyncSessionLocal",
        forbidden_session,
    )
    assert await kasset_market_events_tasks.kasset_market_events_run() == {
        "enabled": False,
        "owner_user_id": None,
        "scanned": 0,
        "results": [],
    }


def test_market_route_maps_kr_alternate_venue_to_ntx_partition() -> None:
    from app.extensions.kasset.automation.market_pipeline import _market_route

    # DB candle partitions use "NTX"; the display spelling "NXT" must map to it.
    assert _market_route("NTX") == (MarketKey.KR, "NTX", "KRX")
    assert _market_route("NXT") == (MarketKey.KR, "NTX", "KRX")
    assert _market_route("KRX") == (MarketKey.KR, "KRX", "KRX")
    with pytest.raises(ValueError):
        _market_route("LSE")


@pytest.mark.asyncio
async def test_unsupported_market_skips_symbol_without_aborting_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from app.core.config import settings
    from app.extensions.kasset.ai import factory
    from app.models.trading import InstrumentType
    from app.tasks import kasset_market_events_tasks

    monkeypatch.setattr(settings, "KASSET_MARKET_EVENTS_ENABLED", True)

    class _Scalars:
        def all(self) -> list[int]:
            return [101]

    class _FakeSession:
        async def scalars(self, _query: object) -> _Scalars:
            return _Scalars()

    @asynccontextmanager
    async def _fake_session():
        yield _FakeSession()

    async def _fake_watch_items(
        _session: object, _owner: int
    ) -> list[tuple[str, InstrumentType, str | None]]:
        return [
            ("BAD", InstrumentType.equity_kr, "LSE"),
            ("005930", InstrumentType.equity_kr, "KRX"),
        ]

    calls: list[str] = []

    class _FakePipeline:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run_symbol_scan(
            self, owner_user_id: int, market: str, symbol: str
        ) -> dict[str, object]:
            calls.append(symbol)
            if market == "LSE":
                raise ValueError("unsupported market")
            return {"skipped": "no_events"}

    monkeypatch.setattr(kasset_market_events_tasks, "_session", _fake_session)
    monkeypatch.setattr(
        kasset_market_events_tasks, "_active_watch_items", _fake_watch_items
    )
    monkeypatch.setattr(
        kasset_market_events_tasks, "MarketEventPipeline", _FakePipeline
    )
    monkeypatch.setattr(factory, "build_model_router", lambda: object())

    outcome = await kasset_market_events_tasks.kasset_market_events_run()

    assert calls == ["BAD", "005930"]
    assert outcome["scanned"] == 2
    results = outcome["results"]
    assert results[0] == {
        "symbol": "BAD",
        "market": "LSE",
        "skipped": "unsupported_market",
    }
    assert results[1] == {
        "symbol": "005930",
        "market": "KRX",
        "skipped": "no_events",
    }
