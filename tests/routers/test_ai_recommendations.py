from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.extensions.kasset.automation.contracts import PaperExecutionOutcome
from app.middleware.auth import AuthMiddleware
from app.models.ai_recommendations import AIRecommendation
from app.models.symbol_master import SymbolMaster
from app.models.trading import User, UserRole
from app.routers.ai_recommendations import _service, router
from app.routers.dependencies import get_authenticated_user
from app.services.ai_recommendations import (
    AIRecommendationService,
    RecommendationStateConflictError,
)

_NOW = datetime(2026, 8, 27, 1, 0, 0, tzinfo=UTC)
_TEST_OWNER_ID: int | None = None


def _test_owner_id() -> int:
    assert _TEST_OWNER_ID is not None
    return _TEST_OWNER_ID


def _recommendation(
    recommendation_id: str,
    *,
    owner_user_id: int | None = None,
    action: str = "BUY",
    decision: str = "PENDING",
    created_at: datetime = _NOW,
    valid_until: datetime | None = _NOW + timedelta(days=1),
    decided_at: datetime | None = None,
    rationale: list[str] | None = None,
    confidence: str | None = "0.7200",
    reference_price: str | None = "71500.00",
    suggested_quantity: str | None = "10.000",
    evidence: list[dict[str, object]] | None = None,
) -> AIRecommendation:
    return AIRecommendation(
        owner_user_id=owner_user_id or _test_owner_id(),
        id=recommendation_id,
        action=action,
        decision=decision,
        market="KRX",
        symbol="005930",
        name="삼성전자",
        currency="KRW",
        headline="실적 회복 신호",
        rationale=["실적 개선", "수급 유입"] if rationale is None else rationale,
        risks=["업황 변동"],
        evidence=evidence
        if evidence is not None
        else [
            {
                "title": "분기보고서",
                "source": "거래소 공시",
                "publishedAt": "2026-08-27T00:30:00Z",
            }
        ],
        confidence=confidence,
        reference_price=reference_price,
        suggested_quantity=suggested_quantity,
        source="KAsset AI",
        created_at=created_at,
        valid_until=valid_until,
        decided_at=decided_at,
        updated_at=decided_at or created_at,
    )


async def _seed(session: AsyncSession, *rows: AIRecommendation) -> None:
    session.add_all(rows)
    await session.commit()


def _app(session: AsyncSession, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_service] = lambda: AIRecommendationService(
        session,
        clock=lambda: _NOW,
    )
    if authenticated:
        app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
            id=_test_owner_id(),
            is_active=True,
            role=UserRole.trader,
        )
    return app


async def _request(app: FastAPI, method: str, url: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, url, **kwargs)


@pytest_asyncio.fixture(autouse=True)
async def _clean_recommendations(db_session: AsyncSession, user: User):
    global _TEST_OWNER_ID
    _TEST_OWNER_ID = user.id
    await db_session.execute(delete(AIRecommendation))
    await db_session.commit()
    try:
        yield
    finally:
        await db_session.execute(delete(AIRecommendation))
        await db_session.commit()
        _TEST_OWNER_ID = None


@pytest.mark.asyncio
async def test_get_filters_orders_limits_and_preserves_android_shape(
    db_session: AsyncSession,
) -> None:
    await _seed(
        db_session,
        _recommendation("pending-old", created_at=_NOW - timedelta(hours=2)),
        _recommendation("pending-new", created_at=_NOW - timedelta(hours=1)),
        _recommendation(
            "approved",
            decision="APPROVED",
            created_at=_NOW - timedelta(minutes=30),
            decided_at=_NOW - timedelta(minutes=10),
        ),
        _recommendation(
            "rejected-newest",
            decision="REJECTED",
            created_at=_NOW,
            decided_at=_NOW + timedelta(minutes=1),
        ),
    )
    app = _app(db_session)

    pending = await _request(
        app,
        "GET",
        "/api/v1/ai/recommendations?status=PENDING&limit=1",
    )
    assert pending.status_code == 200
    assert pending.json() == {
        "recommendations": [
            {
                "id": "pending-new",
                "action": "BUY",
                "decision": "PENDING",
                "market": "KRX",
                "symbol": "005930",
                "name": "삼성전자",
                "currency": "KRW",
                "headline": "실적 회복 신호",
                "rationale": ["실적 개선", "수급 유입"],
                "risks": ["업황 변동"],
                "evidence": [
                    {
                        "title": "분기보고서",
                        "source": "거래소 공시",
                        "publishedAt": "2026-08-27T00:30:00Z",
                    }
                ],
                "confidence": "0.7200",
                "referencePrice": "71500.00",
                "suggestedQuantity": "10.000",
                "source": "KAsset AI",
                "createdAt": "2026-08-27T00:00:00Z",
                "validUntil": "2026-08-28T01:00:00Z",
                "decidedAt": None,
            }
        ]
    }

    resolved = await _request(
        app,
        "GET",
        "/api/v1/ai/recommendations?status=RESOLVED&limit=50",
    )
    assert resolved.status_code == 200
    assert [item["id"] for item in resolved.json()["recommendations"]] == [
        "rejected-newest",
        "approved",
    ]


@pytest.mark.asyncio
async def test_get_preserves_nullable_fields_and_empty_snapshots(
    db_session: AsyncSession,
) -> None:
    row = _recommendation(
        "nullable",
        action="WATCH",
        rationale=[],
        confidence=None,
        reference_price=None,
        suggested_quantity=None,
        valid_until=None,
    )
    row.name = None
    row.currency = None
    row.headline = None
    row.risks = []
    row.evidence = []
    row.source = None
    await _seed(db_session, row)
    app = _app(db_session)

    response = await _request(app, "GET", "/api/v1/ai/recommendations")

    assert response.status_code == 200
    item = response.json()["recommendations"][0]
    assert item["name"] is None
    assert item["currency"] is None
    assert item["headline"] is None
    assert item["confidence"] is None
    assert item["referencePrice"] is None
    assert item["suggestedQuantity"] is None
    assert item["source"] is None
    assert item["validUntil"] is None
    assert item["decidedAt"] is None
    assert item["rationale"] == []
    assert item["risks"] == []
    assert item["evidence"] == []


@pytest.mark.asyncio
async def test_existing_row_get_enriches_name_and_localizes_legacy_vote_rationale(
    db_session: AsyncSession,
) -> None:
    symbol = f"T{uuid4().hex[:10].upper()}"
    master = SymbolMaster(
        market="KRX",
        symbol=symbol,
        name="테스트 실제 종목명",
        name_en=None,
        security_type="COMMON_STOCK",
        is_active=True,
        updated_at=_NOW,
    )
    row = _recommendation("legacy-display")
    row.symbol = symbol
    row.name = symbol
    row.rationale = [
        (
            "Deterministic strategy votes: momentum=BUY, mean_reversion=HOLD, "
            "breakout=SELL, volatility_trend=HOLD."
        )
    ]
    row.evidence = [
        {
            "title": "AI vertical slice",
            "source": "kasset-automation",
            "kind": "ai_vertical_slice",
            "strategyVotes": [
                {
                    "strategy": "MOMENTUM",
                    "vote": "BUY",
                    "weight": "0.250000",
                    "score": "0.200000",
                },
                {
                    "strategy": "MEAN_REVERSION",
                    "vote": "HOLD",
                    "weight": "0.250000",
                    "score": "0.000000",
                },
                {
                    "strategy": "BREAKOUT",
                    "vote": "SELL",
                    "weight": "0.250000",
                    "score": "-0.200000",
                },
                {
                    "strategy": "VOLATILITY_TREND",
                    "vote": "HOLD",
                    "weight": "0.250000",
                    "score": "0.000000",
                },
            ],
        }
    ]
    db_session.add_all([master, row])
    await db_session.commit()
    try:
        response = await _request(
            _app(db_session),
            "GET",
            "/api/v1/ai/recommendations/legacy-display",
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["name"] == "테스트 실제 종목명"
        assert payload["rationale"] == [
            "전략 투표 결과는 모멘텀=매수, 평균회귀=관망, 돌파=매도, 변동성추세=관망입니다."
        ]
        assert payload["strategyVotes"] == row.evidence[0]["strategyVotes"]
    finally:
        await db_session.execute(
            delete(SymbolMaster).where(
                SymbolMaster.market == "KRX",
                SymbolMaster.symbol == symbol,
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_get_defaults_to_pending_and_rejects_unbounded_limit(
    db_session: AsyncSession,
) -> None:
    await _seed(db_session, _recommendation("pending-default"))
    app = _app(db_session)

    defaulted = await _request(app, "GET", "/api/v1/ai/recommendations")
    assert defaulted.status_code == 200
    assert [item["id"] for item in defaulted.json()["recommendations"]] == [
        "pending-default"
    ]

    invalid = await _request(
        app,
        "GET",
        "/api/v1/ai/recommendations?status=PENDING&limit=101",
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    invalid_status = await _request(
        app,
        "GET",
        "/api/v1/ai/recommendations?status=UNKNOWN&limit=50",
    )
    assert invalid_status.status_code == 400
    assert invalid_status.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"decision": "PENDING"},
        {"decision": "APPROVED", "broker": "KIS"},
        {"decision": 1},
        ["APPROVED"],
    ],
)
async def test_post_body_is_strict(
    db_session: AsyncSession,
    payload: object,
) -> None:
    await _seed(db_session, _recommendation("strict-body"))
    app = _app(db_session)

    response = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/strict-body/decision",
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    db_session.expire_all()
    row = await db_session.get(AIRecommendation, "strict-body")
    assert row is not None and row.decision == "PENDING"


@pytest.mark.asyncio
async def test_post_returns_not_found_error_envelope(
    db_session: AsyncSession,
) -> None:
    app = _app(db_session)

    response = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/missing/decision",
        json={"decision": "REJECTED"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "추천을 찾을 수 없습니다.",
            "details": {},
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "rationale", "valid_until", "reason"),
    [
        ("HOLD", ["근거"], _NOW + timedelta(hours=1), "action_not_approvable"),
        ("BUY", [], _NOW + timedelta(hours=1), "rationale_required"),
        ("SELL", ["  "], _NOW + timedelta(hours=1), "rationale_required"),
        ("BUY", ["근거"], None, "valid_until_required"),
        ("SELL", ["근거"], _NOW, "recommendation_expired"),
    ],
)
async def test_approval_guards_leave_recommendation_pending(
    db_session: AsyncSession,
    action: str,
    rationale: list[str],
    valid_until: datetime | None,
    reason: str,
) -> None:
    await _seed(
        db_session,
        _recommendation(
            "guarded",
            action=action,
            rationale=rationale,
            valid_until=valid_until,
        ),
    )
    app = _app(db_session)

    response = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/guarded/decision",
        json={"decision": "APPROVED"},
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"reason": reason}
    assert isinstance(error["message"], str) and error["message"]
    db_session.expire_all()
    row = await db_session.get(AIRecommendation, "guarded")
    assert row is not None and row.decision == "PENDING"
    assert row.decided_at is None


@pytest.mark.asyncio
async def test_approval_returns_the_paper_execution_outcome_when_submission_is_blocked(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(
        db_session,
        _recommendation(
            "approval-blocked",
            evidence=[{"kind": "ai_vertical_slice"}],
        ),
    )

    async def _blocked(owner_user_id: int, recommendation_id: str):
        assert owner_user_id == _test_owner_id()
        assert recommendation_id == "approval-blocked"
        return PaperExecutionOutcome(
            status="BLOCKED",
            reason="global_kill_switch_enabled",
            recommendation_id=recommendation_id,
        )

    monkeypatch.setattr(
        "app.routers.ai_recommendations.run_approved_recommendation_once",
        _blocked,
    )

    response = await _request(
        _app(db_session),
        "POST",
        "/api/v1/ai/recommendations/approval-blocked/decision",
        json={"decision": "APPROVED"},
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert response.json()["paperExecution"] == {
        "status": "BLOCKED",
        "reason": "global_kill_switch_enabled",
        "recommendationId": "approval-blocked",
        "replayed": False,
    }
    assert "paperOrder" not in response.json()


@pytest.mark.asyncio
async def test_approval_returns_structured_failure_when_execution_raises(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(
        db_session,
        _recommendation(
            "approval-error",
            evidence=[{"kind": "ai_vertical_slice"}],
        ),
    )

    async def _raise(_owner_user_id: int, _recommendation_id: str):
        raise RuntimeError("synthetic execution failure")

    monkeypatch.setattr(
        "app.routers.ai_recommendations.run_approved_recommendation_once",
        _raise,
    )

    response = await _request(
        _app(db_session),
        "POST",
        "/api/v1/ai/recommendations/approval-error/decision",
        json={"decision": "APPROVED"},
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert response.json()["paperExecution"] == {
        "status": "FAILED",
        "reason": "approval_execution_failed:RuntimeError",
        "recommendationId": "approval-error",
        "replayed": False,
    }
    assert "paperOrder" not in response.json()


@pytest.mark.asyncio
async def test_same_decision_replay_is_unchanged_and_different_decision_conflicts(
    db_session: AsyncSession,
) -> None:
    seeded = _recommendation("idempotent")
    immutable_before = {
        field: deepcopy(getattr(seeded, field))
        for field in (
            "id",
            "action",
            "market",
            "symbol",
            "name",
            "currency",
            "headline",
            "rationale",
            "risks",
            "evidence",
            "confidence",
            "reference_price",
            "suggested_quantity",
            "source",
            "created_at",
            "valid_until",
        )
    }
    await _seed(db_session, seeded)
    app = _app(db_session)

    first = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/idempotent/decision",
        json={"decision": "APPROVED"},
    )
    replay = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/idempotent/decision",
        json={"decision": "APPROVED"},
    )
    conflict = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/idempotent/decision",
        json={"decision": "REJECTED"},
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["decidedAt"] == "2026-08-27T01:00:00Z"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RECOMMENDATION_STATE_CONFLICT"
    db_session.expire_all()
    row = await db_session.get(AIRecommendation, "idempotent")
    assert row is not None
    assert row.decision == "APPROVED"
    assert row.decided_at == _NOW
    assert row.updated_at == _NOW
    immutable_after = {
        field: deepcopy(getattr(row, field)) for field in immutable_before
    }
    assert immutable_after == immutable_before


@pytest.mark.asyncio
async def test_rejected_decision_does_not_require_approval_guards(
    db_session: AsyncSession,
) -> None:
    await _seed(
        db_session,
        _recommendation(
            "reject-hold",
            action="HOLD",
            rationale=[],
            valid_until=_NOW - timedelta(days=1),
        ),
    )
    app = _app(db_session)

    response = await _request(
        app,
        "POST",
        "/api/v1/ai/recommendations/reject-hold/decision",
        json={"decision": "REJECTED"},
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "REJECTED"


@pytest.mark.asyncio
async def test_decision_persists_across_new_database_sessions(
    db_session: AsyncSession,
) -> None:
    await _seed(db_session, _recommendation("reconnect"))

    async with AsyncSessionLocal() as deciding_session:
        decided = await AIRecommendationService(
            deciding_session,
            clock=lambda: _NOW,
        ).decide(
            _test_owner_id(),
            recommendation_id="reconnect",
            decision="APPROVED",
        )
        assert decided.decision == "APPROVED"

    async with AsyncSessionLocal() as reconnect_session:
        rows = await AIRecommendationService(reconnect_session).list_recommendations(
            _test_owner_id(),
            status="RESOLVED",
            limit=50,
        )
        persisted = next(row for row in rows if row.id == "reconnect")
        assert persisted.decision == "APPROVED"
        assert persisted.decided_at == _NOW
        assert persisted.confidence == "0.7200"
        assert persisted.reference_price == "71500.00"
        assert persisted.suggested_quantity == "10.000"


@pytest.mark.asyncio
async def test_concurrent_different_decisions_have_one_winner(
    db_session: AsyncSession,
) -> None:
    await _seed(db_session, _recommendation("concurrent"))

    async def decide(decision: Literal["APPROVED", "REJECTED"]):
        async with AsyncSessionLocal() as session:
            service = AIRecommendationService(session, clock=lambda: _NOW)
            return await service.decide(
                _test_owner_id(),
                recommendation_id="concurrent",
                decision=decision,
            )

    results = await asyncio.gather(
        decide("APPROVED"),
        decide("REJECTED"),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, AIRecommendation)]
    conflicts = [
        result
        for result in results
        if isinstance(result, RecommendationStateConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with AsyncSessionLocal() as session:
        persisted = await session.scalar(
            select(AIRecommendation).where(AIRecommendation.id == "concurrent")
        )
    assert persisted is not None
    assert persisted.decision == successes[0].decision


@pytest.mark.asyncio
async def test_detail_exposes_android_vertical_slice_evidence(
    db_session: AsyncSession,
) -> None:
    row = _recommendation("vertical-detail")
    row.evidence.append(
        {
            "kind": "ai_vertical_slice",
            "regime": "TRENDING_UP",
            "regimeDetail": "상승 추세",
            "strategyVotes": [
                {
                    "strategy": strategy,
                    "vote": "BUY",
                    "weight": "0.250000",
                    "score": "0.200000",
                }
                for strategy in (
                    "MOMENTUM",
                    "MEAN_REVERSION",
                    "BREAKOUT",
                    "VOLATILITY_TREND",
                )
            ],
            "aiRationale": ["뉴스와 공시를 함께 확인했습니다."],
            "eventEvidence": [
                {
                    "kind": "DISCLOSURE",
                    "title": "분기보고서",
                    "source": "DART",
                    "publishedAt": "2026-08-27T00:30:00Z",
                    "summary": "실적 개선",
                }
            ],
            "entryPrice": "71500",
            "stopPrice": "70000",
            "targetPrice": "74500",
            "ranking": {
                "score": "0.82",
                "position": 1,
                "total": 60,
                "note": "상위 후보",
            },
            "portfolio": {
                "targetWeight": "0.20",
                "targetQuantity": "10",
                "cashAfter": "9285000",
                "note": "운영 한도 내",
            },
            "hardRisk": {
                "passed": True,
                "checks": [
                    {"rule": rule, "passed": True, "detail": "통과"}
                    for rule in (
                        "DAILY_MAX_LOSS",
                        "BUDGET",
                        "POSITION",
                        "ORDER_COUNT",
                        "AI",
                        "DAILY_GOAL",
                    )
                ],
                "blockedReason": None,
            },
        }
    )
    await _seed(db_session, row)

    response = await _request(
        _app(db_session),
        "GET",
        "/api/v1/ai/recommendations/vertical-detail",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["regime"] == "TRENDING_UP"
    assert [vote["strategy"] for vote in body["strategyVotes"]] == [
        "MOMENTUM",
        "MEAN_REVERSION",
        "BREAKOUT",
        "VOLATILITY_TREND",
    ]
    assert body["eventEvidence"][0]["kind"] == "DISCLOSURE"
    assert [check["rule"] for check in body["hardRisk"]["checks"]] == [
        "DAILY_MAX_LOSS",
        "BUDGET",
        "POSITION",
        "ORDER_COUNT",
        "AI",
        "DAILY_GOAL",
    ]


@pytest.mark.asyncio
async def test_router_requires_authentication(db_session: AsyncSession) -> None:
    app = _app(db_session, authenticated=False)

    response = await _request(app, "GET", "/api/v1/ai/recommendations")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_middleware_accepts_valid_bearer_token(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(db_session, _recommendation("bearer-auth"))
    app = _app(db_session, authenticated=False)
    app.add_middleware(AuthMiddleware)
    user = SimpleNamespace(id=1, is_active=True)
    authenticate = AsyncMock(return_value=user)
    monkeypatch.setattr("app.middleware.auth.get_current_user", authenticate)

    response = await _request(
        app,
        "GET",
        "/api/v1/ai/recommendations",
        headers={"Authorization": "Bearer android-access-token"},
    )

    assert response.status_code == 200
    authenticate.assert_awaited_once()
    assert authenticate.await_args.args[0] == "android-access-token"


def test_main_registers_exact_recommendation_paths() -> None:
    from app.main import api

    methods_by_path = {
        route.path: getattr(route, "methods", set())
        for route in api.routes
        if hasattr(route, "path")
    }
    assert "GET" in methods_by_path["/api/v1/ai/recommendations"]
    assert "GET" in methods_by_path["/api/v1/ai/recommendations/{recommendation_id}"]
    assert (
        "POST"
        in methods_by_path["/api/v1/ai/recommendations/{recommendation_id}/decision"]
    )


def test_recommendation_runtime_has_no_order_surface_imports_or_calls() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "app/models/ai_recommendations.py",
        root / "app/schemas/ai_recommendations.py",
        root / "app/services/ai_recommendations/repository.py",
        root / "app/services/ai_recommendations/service.py",
        root / "app/routers/ai_recommendations.py",
    ]
    forbidden_import_prefixes = (
        "app.models.order",
        "app.models.watch",
        "app.services.brokers",
        "app.services.order",
        "app.services.watch",
        "app.services.ledger",
        "app.services.reconcile",
        "app.services.scheduler",
    )
    forbidden_call_roots = {
        "place_order",
        "preview_order",
        "submit_order",
        "modify_order",
        "cancel_order",
        "route_order",
        "reconcile_orders",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = []
        call_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    call_names.append(function.id)
                elif isinstance(function, ast.Attribute):
                    call_names.append(function.attr)
        assert not any(
            module.startswith(forbidden_import_prefixes) for module in imported_modules
        ), path
        assert forbidden_call_roots.isdisjoint(call_names), path
