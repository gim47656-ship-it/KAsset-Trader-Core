"""ops_dashboard panel builders against the real schema.

The point of running these against ``db_session`` rather than a stubbed
session is that the SQL itself is the risk: every panel is hand-written SQL
over four schemas (``public``/``review``/``research``/``paper``). An empty
database therefore proves two things at once — the statements are valid, and
"no rows" resolves to ``IDLE`` with real zeros instead of ``ERROR``.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text

from app.extensions.kasset.models import AndroidRuntimeState
from app.models.trading import User, UserRole
from app.services import ops_dashboard
from app.services.ops_dashboard import (
    OpsContext,
    PanelStatus,
    _ai_usage_panel,
    _funnel_panel,
    _news_panel,
    _paper_portfolio_panel,
    _reconcile_panel,
    _strategy_panel,
    _system_panel,
    build_ops_dashboard,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)

# Panels whose SQL must run unchanged against the real schema.
_SQL_PANELS = (
    ("system", _system_panel),
    ("ai_usage", _ai_usage_panel),
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
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

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
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    panel = await _funnel_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert panel.rows == ()
    assert [metric.value for metric in panel.metrics[:2]] == ["0", "0"]
    # The "마지막 체결" metric has nothing to report — unmeasured, not zero.
    assert panel.metrics[2].value is None
    assert panel.metrics[2].hint == "체결 이력 없음"


async def test_strategy_panel_without_bypass_names_auto_paper_block(db_session):
    """No approval evidence and no bypass means AUTO_PAPER cannot place orders."""
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    panel = await _strategy_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert "PAPER_APPROVED" in panel.summary
    assert "우회 ON 소유자도 0명" in panel.summary
    assert "자동발주 불가" in panel.summary
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["PAPER_APPROVED"] == "0"
    assert values["승격 우회 ON 소유자"] == "0"


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
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    panel = await _strategy_panel(ctx)

    assert panel.status is PanelStatus.WARN
    assert "승격 레지스트리 0건" in panel.summary
    assert "자동발주 가능" in panel.summary
    assert "PAPER 모드이고 kill switch OFF이면" in panel.summary
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["승격 우회 ON 소유자"] == "1"


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
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=summary)
    ):
        panel = await _ai_usage_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["논리 호출"] == "3"
    assert values["provider 시도"] == "7"
    assert values["실패 시도"] == "2"
    assert values["총 토큰"] == "1,200"
    assert values["토큰 미제공 시도"] == "4"
    # Cost the provider never reported must stay unmeasured, not 0.
    assert values["비용"] is None
    hints = {metric.label: metric.hint for metric in panel.metrics}
    assert hints["총 토큰"] == "토큰 미제공 4건 제외"
    assert panel.status is PanelStatus.WARN
    assert panel.rows[0].cells[:2] == ("provider", "openai")
    assert panel.rows[0].cells[-1] is None


async def test_ai_panel_with_no_calls_is_idle_with_real_zeros(db_session):
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=_ai_summary())
    ):
        panel = await _ai_usage_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert panel.rows == ()
    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["논리 호출"] == "0"
    assert values["provider 시도"] == "0"
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
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=summary)
    ):
        panel = await _ai_usage_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    hints = {metric.label: metric.hint for metric in panel.metrics}
    assert values["총 토큰"] is None
    assert hints["총 토큰"] == "토큰 미제공 2건 제외"
    from app.core.templates import templates

    html = templates.get_template("admin_ops.html").render(
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
        metric for metric in panel.metrics if metric.label == "논리 호출"
    ).hint == ("routed client 호출 수 (모델 티어당 1개, terra→sol escalation은 2회)")


async def test_ai_panel_discloses_the_uninstrumented_paths(db_session):
    """0 calls must not read as "AI was not used".

    run_skill-family providers never create a ledger row, so their usage is
    absent from these numbers — the panel has to say so.
    """
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    with patch.object(
        ops_dashboard, "summarize_ai_usage", AsyncMock(return_value=_ai_summary())
    ):
        panel = await _ai_usage_panel(ctx)

    assert "AI를 쓰지 않았다는 뜻은 아니다" in panel.summary
    assert panel.note == ops_dashboard.AI_COVERAGE_NOTE
    assert "run_skill" in panel.note
    assert "MCP" in panel.note
    assert "AvailabilityRoutedJsonClient" in panel.note


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
    await db_session.flush()
    ctx = OpsContext(db=db_session, admin_user_id=914, now=_NOW)

    panel = await _system_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert panel.status is PanelStatus.WARN
    assert "소유자 kill switch ON 1명" in panel.summary
    assert values["내 kill switch"] == "OFF"
    assert values["소유자 kill switch ON"] == "1"
    assert values["전역 kill switch"] == "OFF"
    assert any(
        row.cells[:3] == ("소유자 runtime", "PAPER", "kill switch ON")
        for row in panel.rows
    )
    assert "현재 관리자 id=914 기준" in panel.note
    assert "소유자 전체 상태 및 전역 상태와 분리" in panel.note


async def test_system_panel_distinguishes_execution_master_switches(
    db_session, monkeypatch
):
    monkeypatch.setattr(ops_dashboard.settings, "TRADING_ENABLED", True)
    monkeypatch.setattr(
        ops_dashboard.settings, "AI_PAPER_AUTO_EXECUTION_ENABLED", False
    )
    ctx = OpsContext(db=db_session, admin_user_id=42, now=_NOW)

    panel = await _system_panel(ctx)

    values = {metric.label: metric.value for metric in panel.metrics}
    assert values["Core 거래 기능 (TRADING_ENABLED)"] == "ON"
    assert values["PAPER 자동실행 (AI_PAPER_AUTO_EXECUTION_ENABLED)"] == "OFF"
    assert "내 trading mode" in values
    assert "전역 kill switch" in values


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
        dashboard = await build_ops_dashboard(db_session, admin_user_id=921, now=_NOW)
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

    dashboard = await build_ops_dashboard(db_session, admin_user_id=1, now=_NOW)

    html = templates.get_template("admin_ops.html").render(
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
    assert "run_skill 계열" in html
    assert "Core 거래 기능 (TRADING_ENABLED)" in html
    assert "PAPER 자동실행 (AI_PAPER_AUTO_EXECUTION_ENABLED)" in html
    assert "전역 kill switch" in html


async def test_readiness_panel_uses_the_cache_before_measuring(db_session):
    """A cache hit must not re-run the 7-statement readiness measurement."""
    snapshot = {
        "as_of": "2026-08-30T02:50:00+00:00",
        "daily_history_ready": True,
        "promotion_ready": True,
        "blockers": [],
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
                "benchmark_symbol": "069500",
                "benchmark_status": "available",
                "benchmark_count": 300,
                "blockers": [],
            }
        ],
    }
    import json

    client = AsyncMock()
    client.get.return_value = json.dumps(snapshot)
    measure = AsyncMock()
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

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
    assert panel.rows[0].cells[0] == "KR"
    # missing_expected_trading_day_count is NULL upstream — stays unmeasured.
    assert panel.rows[0].cells[6] is None
    assert panel.metrics[2].hint == "캐시"


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
        total_symbol_count=0,
        cohort_active_member_count=0,
        forced_member_count=0,
        benchmark_member_count=0,
        active_symbol_count=0,
        inactive_symbol_count=0,
        symbols_with_exactly_251_bars=0,
        symbols_with_at_least_252_bars=0,
        eligible_symbol_count=0,
        stale_bar_count=0,
        future_bar_count=0,
        duplicate_timestamp_count=0,
        ohlc_anomaly_count=0,
        missing_expected_trading_day_count=None,
        calendar_status="unavailable",
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
        daily_history_blockers=("kr:cohort_missing",),
        blockers=("kr:cohort_missing",),
        reasons=(),
    )
    readiness = DailyCandlesReadiness(
        as_of=_NOW,
        required_history_bars=252,
        markets=(market,),
        daily_history_ready=False,
        promotion_ready=False,
        daily_history_blockers=("kr:cohort_missing",),
        blockers=("kr:cohort_missing",),
        reasons=(),
    )
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)
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
    assert panel.metrics[2].hint == "방금 측정"
    # The write-back is what keeps the 7-statement measurement off the 2 vCPU
    # production database on every page load.
    client.set.assert_awaited_once()
    _, kwargs = client.set.await_args
    assert kwargs["ex"] == ops_dashboard._READINESS_CACHE_TTL_SECONDS
