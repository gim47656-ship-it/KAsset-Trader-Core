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

from app.extensions.kasset.api.schemas import (
    AiRelayStatus,
    DatabaseStatus,
    SystemBrokerStatus,
    SystemStatus,
)
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


async def test_strategy_panel_names_the_auto_paper_consequence(db_session):
    """0 promotions is the fact that decides AUTO_PAPER cannot place orders."""
    ctx = OpsContext(db=db_session, admin_user_id=1, now=_NOW)

    panel = await _strategy_panel(ctx)

    assert panel.status is PanelStatus.IDLE
    assert "PAPER_APPROVED" in panel.summary
    assert "APPROVAL" in panel.summary
    assert panel.metrics[0].value == "0"


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


async def test_system_panel_reuses_the_android_status_builder(db_session):
    """The web page and /api/v1/system/status must not diverge."""
    status = SystemStatus(
        server_version="9.9.9",
        server_time="2026-08-30T03:00:00Z",
        database=DatabaseStatus(status="ok", migration_revision="rev-abc"),
        trading_mode="PAPER",
        trading_enabled=True,
        live_trading_enabled=False,
        kill_switch_enabled=False,
        brokers=[
            SystemBrokerStatus(provider="kis", connected=True, last_verified_at=None)
        ],
        ai_relay=AiRelayStatus(configured=False, reachable=False, message="-"),
    )
    builder = AsyncMock(return_value=status)
    ctx = OpsContext(db=db_session, admin_user_id=42, now=_NOW)

    with patch.object(ops_dashboard, "_build_system_status", builder):
        panel = await _system_panel(ctx)

    builder.assert_awaited_once_with(db_session, 42)
    assert panel.status is PanelStatus.OK
    assert ("migration revision", "rev-abc") in [
        (metric.label, metric.value) for metric in panel.metrics
    ]
    assert panel.rows[0].cells[0] == "kis"


async def test_kill_switch_and_live_trading_raise_the_panel_to_warn(db_session):
    status = SystemStatus(
        server_version="9.9.9",
        server_time="2026-08-30T03:00:00Z",
        database=DatabaseStatus(status="ok", migration_revision=None),
        trading_mode="LIVE",
        trading_enabled=True,
        live_trading_enabled=True,
        kill_switch_enabled=True,
        brokers=[],
        ai_relay=AiRelayStatus(configured=False, reachable=False, message="-"),
    )
    ctx = OpsContext(db=db_session, admin_user_id=42, now=_NOW)

    with patch.object(
        ops_dashboard, "_build_system_status", AsyncMock(return_value=status)
    ):
        panel = await _system_panel(ctx)

    assert panel.status is PanelStatus.WARN
    assert "kill switch ON" in panel.summary
    assert "LIVE 매매 허용" in panel.summary
    assert "migration revision 조회 불가" in panel.summary


async def test_full_dashboard_renders_every_panel(db_session, no_redis):
    """End-to-end over the real schema: no panel may report 조회실패."""
    status = SystemStatus(
        server_version="9.9.9",
        server_time="2026-08-30T03:00:00Z",
        database=DatabaseStatus(status="ok", migration_revision="rev-abc"),
        trading_mode="PAPER",
        trading_enabled=True,
        live_trading_enabled=False,
        kill_switch_enabled=False,
        brokers=[],
        ai_relay=AiRelayStatus(configured=False, reachable=False, message="-"),
    )
    with patch.object(
        ops_dashboard, "_build_system_status", AsyncMock(return_value=status)
    ):
        dashboard = await build_ops_dashboard(db_session, admin_user_id=1, now=_NOW)

    keys = [panel.key for panel in dashboard.panels]
    assert keys == [key for key, _, _ in ops_dashboard.PANEL_BUILDERS]
    failures = {
        panel.key: panel.error
        for panel in dashboard.panels
        if panel.status is PanelStatus.ERROR
    }
    assert failures == {}
    assert dashboard.failed_panel_titles == ()


async def test_template_renders_the_real_dashboard(db_session, no_redis):
    """admin_ops.html must survive the shapes the real builders produce."""
    from app.core.templates import templates
    from app.services.ops_dashboard import UNMEASURED_TEXT

    status = SystemStatus(
        server_version="9.9.9",
        server_time="2026-08-30T03:00:00Z",
        database=DatabaseStatus(status="ok", migration_revision="rev-abc"),
        trading_mode="PAPER",
        trading_enabled=True,
        live_trading_enabled=False,
        kill_switch_enabled=False,
        brokers=[],
        ai_relay=AiRelayStatus(configured=False, reachable=False, message="-"),
    )
    with patch.object(
        ops_dashboard, "_build_system_status", AsyncMock(return_value=status)
    ):
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
