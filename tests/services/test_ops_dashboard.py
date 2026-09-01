"""ops_dashboard panel builders against the real schema.

The point of running these against ``db_session`` rather than a stubbed
session is that the SQL itself is the risk: every panel is hand-written SQL
over four schemas (``public``/``review``/``research``/``paper``). An empty
database therefore proves two things at once — the statements are valid, and
"no rows" resolves to ``IDLE`` with real zeros instead of ``ERROR``.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text

from app.extensions.kasset.models import AndroidRuntimeState
from app.models.trading import User, UserRole
from app.models.user_settings import UserSetting
from app.services import ops_dashboard
from app.services.kasset_automation_audit import build_automation_cycle_event
from app.services.ops_dashboard import (
    OpsContext,
    PanelStatus,
    _ai_reviews_panel,
    _ai_usage_panel,
    _collection_panel,
    _funnel_panel,
    _news_panel,
    _paper_portfolio_panel,
    _reconcile_panel,
    _strategy_panel,
    _system_panel,
    build_ops_dashboard,
)

pytestmark = pytest.mark.asyncio

_REQUEST = SimpleNamespace(state=SimpleNamespace(csrftoken="test-csrf-token"))
_NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)

# Panels whose SQL must run unchanged against the real schema.
_SQL_PANELS = (
    ("system", _system_panel),
    ("ai_usage", _ai_usage_panel),
    ("collection", _collection_panel),
    ("ai_reviews", _ai_reviews_panel),
    ("funnel", _funnel_panel),
    ("paper_portfolio", _paper_portfolio_panel),
    ("reconcile", _reconcile_panel),
    ("news", _news_panel),
    ("strategy", _strategy_panel),
)


@pytest.fixture
def no_redis():
    """Disable the readiness cache so nothing reaches a real Redis."""
    with patch.object(ops_dashboard, "_get_redis_client", AsyncMock(return_value=None)):
        yield


@pytest.mark.parametrize("key,builder", _SQL_PANELS, ids=[k for k, _ in _SQL_PANELS])
async def test_sql_panel_runs_against_real_schema(db_session, key, builder):
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await builder(ctx)

    assert panel.status is not PanelStatus.ERROR, panel.error
    assert panel.error is None
    assert panel.summary
    # A metric that could not be measured must explain itself rather than
    # render a fake zero.
    for metric in panel.metrics:
        assert metric.value is not None or metric.hint, metric.label


async def test_empty_database_is_idle_not_error(db_session):
    """0 rows is a successful measurement, so counts are real zeros."""
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await _funnel_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert panel.rows == ()
    assert [metric.value for metric in panel.metrics[:2]] == ["0", "0"]
    # The "마지막 체결" metric has nothing to report — unmeasured, not zero.
    assert panel.metrics[2].value is None
    assert panel.metrics[2].hint == "체결 이력 없음"


async def test_strategy_panel_without_bypass_names_auto_paper_block(db_session):
    """No approval evidence and no bypass means AUTO_PAPER cannot place orders."""
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await _strategy_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert "모의투자 승인 전략이 없고" in panel.summary
    assert "승격 우회가 켜진 사용자도 없어" in panel.summary
    assert "자동 모의주문 불가" in panel.summary
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["모의투자 승인"] == "0"
    assert values["승격 우회 사용자"] == "0"


async def test_strategy_panel_warns_when_bypass_allows_unpromoted_orders(db_session):
    db_session.add(
        User(
            id=901,
            username="ops-bypass-owner",
            email="ops-bypass-owner@example.com",
            role=UserRole.trader,
        )
    )
    await db_session.flush()
    db_session.add(
        AndroidRuntimeState(
            owner_user_id=901,
            trading_mode="PAPER",
            kill_switch_enabled=False,
            promotion_bypass_enabled=True,
        )
    )
    await db_session.flush()
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await _strategy_panel(ctx)

    assert panel.status is PanelStatus.WARN
    assert "승격 기록 0건" in panel.summary
    assert "자동 모의주문 가능" in panel.summary
    assert "모의투자 모드이고" in panel.summary
    assert "긴급 중지가 꺼져 있으면" in panel.summary
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["승격 우회 사용자"] == "1"


def _ai_summary(**overrides):
    from app.services.ai_usage_service import AiUsageSummary

    defaults = {
        "since": _NOW,
        "until": _NOW,
        "logical_calls": 0,
        "attempts": 0,
        "success_attempts": 0,
        "failure_attempts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "attempts_without_usage": 0,
        "cost_amount": None,
        "cost_currency": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "by_provider": (),
        "by_model": (),
        "by_feature": (),
    }
    return AiUsageSummary(**{**defaults, **overrides})


async def test_ai_panel_separates_logical_calls_from_provider_attempts(db_session):
    """fallback / tier escalation only shows up in attempts."""
    from app.services.ai_usage_service import AiUsageBreakdown

    summary = _ai_summary(
        logical_calls=3,
        attempts=7,
        success_attempts=5,
        failure_attempts=2,
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        attempts_without_usage=4,
        p50_latency_ms=810,
        p95_latency_ms=4200,
        by_provider=(
            AiUsageBreakdown(
                key="openai",
                attempts=5,
                logical_calls=3,
                success_attempts=5,
                failure_attempts=0,
                success_rate=1.0,
                prompt_tokens=1000,
                completion_tokens=200,
                total_tokens=1200,
                attempts_without_usage=0,
                cost_amount=None,
                cost_currency=None,
            ),
        ),
    )
    ctx = OpsContext(db=db_session, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=summary)
    ):
        panel = await _ai_usage_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["AI 요청"] == "3"
    assert values["AI 제공사 시도"] == "7"
    assert values["실패 시도"] == "2"
    assert values["총 토큰"] == "1,200"
    assert values["토큰 미제공 시도"] == "4"
    # Cost the provider never reported must stay unmeasured, not 0.
    assert values["비용"] is None
    hints = {metric.label: metric.hint for metric in panel.metrics}
    assert hints["총 토큰"] == "사용량을 보내지 않은 시도 4건 제외"
    assert panel.status is PanelStatus.WARN
    assert panel.rows[0].cells[:2] == ("AI 제공사", "openai")
    assert panel.rows[0].cells[-1] is None


async def test_ai_panel_with_no_calls_is_idle_with_real_zeros(db_session):
    ctx = OpsContext(db=db_session, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=_ai_summary())
    ):
        panel = await _ai_usage_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert panel.rows == ()
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["AI 요청"] == "0"
    assert values["AI 제공사 시도"] == "0"
    assert values["총 토큰"] == "0"
    # No latency samples and no cost: unmeasured, never 0.
    assert values["p50 지연"] is None
    assert values["p95 지연"] is None
    assert values["비용"] is None


async def test_ai_panel_marks_all_unreported_tokens_unmeasured(db_session):
    summary = _ai_summary(
        logical_calls=2,
        attempts=2,
        success_attempts=2,
        total_tokens=0,
        attempts_without_usage=2,
    )
    ctx = OpsContext(db=db_session, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=summary)
    ):
        panel = await _ai_usage_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    hints = {metric.label: metric.hint for metric in panel.metrics}
    assert values["총 토큰"] is None
    assert hints["총 토큰"] == "사용량을 보내지 않은 시도 2건 제외"
    from app.core.templates import templates

    html = templates.get_template("admin_ops.html").render(
        request=_REQUEST,
        user=None,
        dashboard=ops_dashboard.OpsDashboard(
            generated_at=_NOW,
            panels=(panel,),
        ),
        generated_at="2026-08-30 12:00:00 KST",
        unmeasured_text=ops_dashboard.UNMEASURED_TEXT,
    )
    token_card = html.split(
        '<div class="ops-metric-label">총 토큰</div>',
        1,
    )[1].split(
        '<div class="ops-metric-label">토큰 미제공 시도</div>',
        1,
    )[0]
    assert 'data-measured="false">—</div>' in token_card
    assert next(
        metric for metric in panel.metrics if metric.label == "AI 요청"
    ).hint == (
        "기능별 요청 수입니다. 한 요청이 정밀 검토로 넘어가면 모델을 두 번 호출할 수 있습니다."
    )


async def test_ai_panel_discloses_the_uninstrumented_paths(db_session):
    """0 calls must not read as "AI was not used".

    run_skill-family providers never create a ledger row, so their usage is
    absent from these numbers — the panel has to say so.
    """
    ctx = OpsContext(db=db_session, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=_ai_summary())
    ):
        panel = await _ai_usage_panel(ctx)

    assert "별도 AI 경로는 이 화면에 표시되지 않습니다" in panel.summary
    assert panel.note == ops_dashboard.AI_COVERAGE_NOTE
    assert "구독형 CLI" in panel.note
    assert "AI MCP" in panel.note
    assert "0건이 곧 AI 미사용을 뜻하지 않습니다" in panel.note


async def test_collection_and_ai_review_panels_explain_candidate_rejection(
    db_session,
):
    db_session.add(
        build_automation_cycle_event(
            owner_user_id=4,
            observed_at=_NOW,
            finished_at=_NOW,
            result={
                "candidateCount": 100,
                "rankedCount": 99,
                "strategyEvaluatedCount": 12,
                "strategyActionableCount": 1,
                "aiReviewedCount": 1,
                "aiFailureCount": 0,
                "candidateMarkets": {"KR": 94, "US": 6},
                "candidateSources": {"tvscreener_kr": 94, "watchlist": 9},
                "collectionPolicy": {
                    "candidateLimit": 100,
                    "minimumCandidateTarget": 50,
                    "strategyReviewLimit": 12,
                    "recommendationLimit": 5,
                    "aiReviewActions": ["BUY", "SELL"],
                },
                "rankedCandidates": [
                    {
                        "symbol": "003230",
                        "market": "KR",
                        "rankPosition": 1,
                        "totalScore": "0.809801",
                    }
                ],
                "candidateExclusions": [
                    {
                        "symbol": "0126Z0",
                        "market": "KR",
                        "exclusionReason": "insufficient_history",
                    },
                    *[
                        {
                            "symbol": f"X{index:04d}",
                            "market": "KR",
                            "exclusionReason": "insufficient_history",
                        }
                        for index in range(50)
                    ],
                ],
                "aiReviewRejections": {"action_mismatch": 1},
                "aiReviewOutcomes": [
                    {
                        "symbol": "003230",
                        "market": "KR",
                        "strategyAction": "BUY",
                        "aiAction": "HOLD",
                        "confidence": "0.72",
                        "reason": "action_mismatch",
                        "observedAt": "2026-08-30T03:00:00Z",
                        "provider": "mcp",
                        "tier": "terra",
                        "modelId": "gpt-5.6-terra",
                        "rationaleTags": ["breakout_not_confirmed"],
                    }
                ],
                "recommendationIds": [],
                "skipped": "no_ai_confirmed_signal",
            },
        )
    )
    await db_session.flush()
    ctx = OpsContext(db=db_session, now=_NOW)

    collection = await _collection_panel(ctx)
    reviews = await _ai_reviews_panel(ctx)

    assert collection.status is PanelStatus.OK
    assert "후보 최대 100개" in collection.summary
    assert collection.rows[0].cells[2] == "KR 94, US 6"
    assert "003230(KR)" in str(collection.rows[0].cells[5])
    assert "0126Z0: insufficient_history" in str(collection.rows[0].cells[6])
    assert "외 1건" in str(collection.rows[0].cells[6])
    assert reviews.status is PanelStatus.OK
    assert reviews.summary == "AI 후보 검토 1건 · 합의 0건 · 거절 1건"
    assert reviews.rows[0].cells[:6] == (
        "003230",
        "국내 (KR)",
        "매수 (BUY)",
        "보류 (HOLD)",
        "72.0%",
        "전략과 AI 방향 불일치 (action_mismatch)",
    )
    assert reviews.rows[0].cells[6] == "mcp · terra · gpt-5.6-terra"
    assert reviews.rows[0].cells[7] == "breakout_not_confirmed"


async def test_system_panel_reads_grouped_runtime_state_from_real_db(db_session):
    """A non-admin owner's kill switch must be visible to the viewing admin."""
    db_session.add_all(
        [
            User(
                id=911,
                username="ops-trader-owner",
                email="ops-trader-owner@example.com",
                role=UserRole.trader,
            ),
            User(
                id=914,
                username="ops-viewing-admin",
                email="ops-viewing-admin@example.com",
                role=UserRole.admin,
            ),
        ]
    )
    await db_session.flush()
    db_session.add(
        AndroidRuntimeState(
            owner_user_id=911,
            trading_mode="PAPER",
            kill_switch_enabled=True,
            promotion_bypass_enabled=False,
        )
    )
    db_session.add(
        UserSetting(
            user_id=911,
            key="kasset.ai_trading",
            value={"mode": "AUTO_PAPER"},
        )
    )
    await db_session.flush()
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await _system_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert panel.status is PanelStatus.WARN
    assert "사용자 긴급 중지 켜짐 1명" in panel.summary
    assert values["활성 거래 사용자"] == "1"
    assert values["승인 없는 자동 주문 사용자"] == "1"
    assert values["승인 후 주문 사용자"] == "0"
    assert values["사용자 긴급 중지 켜짐"] == "1"
    assert values["전체 긴급 중지"] == "꺼짐"
    assert any(
        row.cells[:3] == ("사용자 거래 설정", "모의투자 (PAPER)", "긴급 중지 켜짐")
        for row in panel.rows
    )
    assert any(
        row.cells[:3]
        == ("AI 주문 방식", "승인 없는 자동 모의주문 (AUTO_PAPER)", "설정됨")
        for row in panel.rows
    )
    assert "활성 trader와 거래 설정·상태·추천이 있는 admin만 집계" in panel.note


async def test_system_panel_counts_active_admin_execution_owner(db_session):
    """승인·자동 실행이 허용된 admin owner도 운영 집계에서 빠지지 않는다."""
    db_session.add(
        User(
            id=915,
            username="ops-admin-owner",
            email="ops-admin-owner@example.com",
            role=UserRole.admin,
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            AndroidRuntimeState(
                owner_user_id=915,
                trading_mode="PAPER",
                kill_switch_enabled=False,
                promotion_bypass_enabled=False,
            ),
            UserSetting(
                user_id=915,
                key="kasset.ai_trading",
                value={"mode": "AUTO_PAPER"},
            ),
        ]
    )
    await db_session.flush()

    panel = await _system_panel(OpsContext(db=db_session, now=_NOW))

    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["활성 거래 사용자"] == "1"
    assert values["승인 없는 자동 주문 사용자"] == "1"
    assert values["승인 후 주문 사용자"] == "0"


async def test_system_panel_distinguishes_execution_master_switches(
    db_session, monkeypatch
):
    monkeypatch.setattr(ops_dashboard.settings, "TRADING_ENABLED", True)
    monkeypatch.setattr(
        ops_dashboard.settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", False
    )
    ctx = OpsContext(db=db_session, now=_NOW)

    panel = await _system_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["Core 거래 기능"] == "켜짐"
    assert values["모의투자 자동 실행"] == "꺼짐"
    assert "활성 거래 사용자" in values
    assert "승인 없는 자동 주문 사용자" in values
    assert "승인 후 주문 사용자" in values
    assert "전체 긴급 중지" in values


async def test_full_dashboard_renders_every_panel_without_committing(
    db_session, no_redis
):
    """A first admin dashboard read must neither commit nor create runtime state."""
    db_session.add(
        User(
            id=921,
            username="ops-read-only-admin",
            email="ops-read-only-admin@example.com",
            role=UserRole.admin,
        )
    )
    await db_session.flush()
    commit_count = 0

    def record_commit(_session):
        nonlocal commit_count
        commit_count += 1

    event.listen(db_session.sync_session, "after_commit", record_commit)
    try:
        dashboard = await build_ops_dashboard(db_session, now=_NOW)
    finally:
        event.remove(db_session.sync_session, "after_commit", record_commit)

    keys = [panel.key for panel in dashboard.panels]
    assert keys == [key for key, _, _ in ops_dashboard.PANEL_BUILDERS]
    failures = {
        panel.key: panel.error
        for panel in dashboard.panels
        if panel.status is PanelStatus.ERROR
    }
    assert failures == {}
    assert dashboard.failed_panel_titles == ()
    assert commit_count == 0
    runtime_rows = await db_session.execute(
        text(
            "SELECT count(*) FROM kasset_android_runtime_state "
            "WHERE owner_user_id = :owner"
        ),
        {"owner": 921},
    )
    assert runtime_rows.scalar_one() == 0


async def test_template_renders_the_real_dashboard(db_session, no_redis):
    """admin_ops.html must survive the shapes the real builders produce."""
    from app.core.templates import templates
    from app.services.ops_dashboard import UNMEASURED_TEXT

    dashboard = await build_ops_dashboard(db_session, now=_NOW)

    html = templates.get_template("admin_ops.html").render(
        request=_REQUEST,
        user=None,
        dashboard=dashboard,
        generated_at="2026-08-30 12:00:00 KST",
        unmeasured_text=UNMEASURED_TEXT,
    )

    for key, title, _ in ops_dashboard.PANEL_BUILDERS:
        assert f'id="panel-{key}"' in html
        assert title in html
    # Empty database: every panel measured successfully, so no 조회실패 banner
    # and no unmeasured cells in the count columns.
    assert 'id="ops-degraded"' not in html
    assert 'data-status="error"' not in html
    assert "조회 성공 · 해당 기간 0건" in html
    # The AI panel's standing coverage caveat must reach the page.
    assert 'data-role="panel-note"' in html
    assert "구독형 CLI" in html
    assert "Core 거래 기능" in html
    assert "모의투자 자동 실행" in html
    assert "전체 긴급 중지" in html
    assert 'id="ai-route-panel"' in html
    assert 'id="ai-route-save"' in html
    assert '"X-CSRFToken": csrfToken' in html


async def test_readiness_panel_uses_the_cache_before_measuring(db_session):
    """A cache hit must not re-run the 7-statement readiness measurement."""
    snapshot = {
        "as_of": "2026-08-30T02:50:00+00:00",
        "daily_history_ready": True,
        "promotion_ready": True,
        "historical_evidence_ready": False,
        "blockers": [],
        "unresolved_evidence": ["kr:cohort_not_historical_pit"],
        "markets": [
            {
                "market": "kr",
                "cohort_id": "cohort-1",
                "daily_history_ready": True,
                "promotion_ready": True,
                "eligible_symbol_count": 120,
                "symbols_with_at_least_252_bars": 130,
                "stale_bar_count": 0,
                "duplicate_timestamp_count": 0,
                "ohlc_anomaly_count": 0,
                "missing_expected_trading_day_count": None,
                "ingest_lag_session_count": 1,
                "unevidenced_session_count": 0,
                "benchmark_symbol": "069500",
                "benchmark_status": "available",
                "benchmark_count": 300,
                "blockers": [],
                "unresolved_evidence": ["kr:cohort_not_historical_pit"],
            }
        ],
    }
    import json

    client = AsyncMock()
    client.get.return_value = json.dumps(snapshot)
    measure = AsyncMock()
    ctx = OpsContext(db=db_session, now=_NOW)

    with (
        patch.object(
            ops_dashboard, "_get_redis_client", AsyncMock(return_value=client)
        ),
        patch.object(ops_dashboard.DailyCandlesReadinessService, "measure", measure),
    ):
        panel = await ops_dashboard._readiness_panel(ctx)

    measure.assert_not_awaited()
    client.set.assert_not_awaited()
    assert panel.status is PanelStatus.OK
    assert panel.rows[0].cells[0] == "국내 (KR)"
    # missing_expected_trading_day_count is NULL upstream — stays unmeasured.
    assert panel.rows[0].cells[6] is None
    # The rolled-back ingest lag is reported next to it.
    assert panel.rows[0].cells[7] == "1"
    assert panel.rows[0].cells[-1] == "kr:cohort_not_historical_pit"
    assert panel.metrics[3].hint == "캐시"


async def test_readiness_cache_miss_measures_and_writes_back(db_session):
    from app.services.daily_candles.readiness import (
        BenchmarkCoverage,
        DailyCandlesReadiness,
        MarketReadiness,
    )

    market = MarketReadiness(
        market="kr",
        cohort=None,
        evaluated_window_start=None,
        evaluated_window_end=None,
        latest_completed_session=None,
        ingest_lag_session_count=0,
        unevidenced_session_count=0,
        unevidenced_sessions=(),
        total_symbol_count=0,
        cohort_active_member_count=0,
        forced_member_count=0,
        benchmark_member_count=0,
        active_symbol_count=0,
        inactive_symbol_count=0,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=0,
        eligible_symbol_count=0,
        eligible_symbols=(),
        excluded_symbols=(),
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=None,
        calendar_status="unavailable",
        price_adjustment_status="incomplete",
        corporate_action_status="unknown",
        corporate_action_covered_symbol_count=0,
        adjustment_covered_symbol_count=0,
        list_date_covered_symbol_count=0,
        members_listed_after_cohort_start=0,
        delist_date_covered_inactive_count=0,
        point_in_time_available=False,
        inactive_with_candles_count=0,
        delisted_symbol_count=0,
        delisted_with_candles_count=0,
        includes_delisted=False,
        fallback_only=False,
        benchmark=BenchmarkCoverage(
            market="kr",
            symbol="",
            start=None,
            end=None,
            count=0,
            source=None,
            sources=(),
            status="unavailable",
        ),
        daily_history_ready=False,
        promotion_ready=False,
        historical_evidence_ready=False,
        daily_history_blockers=("kr:cohort_missing",),
        blockers=("kr:cohort_missing",),
        historical_evidence_blockers=("kr:cohort_not_found",),
        unresolved_evidence=("kr:cohort_not_found",),
        reasons=(),
    )
    readiness = DailyCandlesReadiness(
        as_of=_NOW,
        required_history_bars=252,
        markets=(market,),
        daily_history_ready=False,
        promotion_ready=False,
        historical_evidence_ready=False,
        daily_history_blockers=("kr:cohort_missing",),
        blockers=("kr:cohort_missing",),
        historical_evidence_blockers=("kr:cohort_not_found",),
        unresolved_evidence=("kr:cohort_not_found",),
        reasons=(),
    )
    ctx = OpsContext(db=db_session, now=_NOW)
    client = AsyncMock()
    client.get.return_value = None

    with (
        patch.object(
            ops_dashboard, "_get_redis_client", AsyncMock(return_value=client)
        ),
        patch.object(
            ops_dashboard.DailyCandlesReadinessService,
            "measure",
            AsyncMock(return_value=readiness),
        ),
    ):
        panel = await ops_dashboard._readiness_panel(ctx)

    assert panel.status is PanelStatus.WARN
    assert "kr:cohort_missing" in panel.summary
    assert panel.metrics[3].hint == "방금 측정"
    # The write-back is what keeps the 7-statement measurement off the 2 vCPU
    # production database on every page load.
    client.set.assert_awaited_once()
    _, kwargs = client.set.await_args
    assert kwargs["ex"] == ops_dashboard._READINESS_CACHE_TTL_SECONDS
