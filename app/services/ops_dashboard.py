"""Read-only operations aggregation behind ``GET /admin/ops``.

Design constraints (production DB is 2 vCPU and shares the box with the live
trading stack):

* **No unbounded ``COUNT(*)``.**  Every event-table aggregate carries an
  explicit time window and rides an existing index.  The two exceptions are
  named and justified at their call sites.
* **One panel failing never takes the page down.**  Each builder runs inside
  its own ``try``; a failure rolls the session back (a failed statement
  poisons the transaction — same reasoning as ``_applied_migration_revision``
  in ``app/extensions/kasset/api/router.py``) and is rendered as a
  ``조회실패`` panel while the rest of the page still renders.
* **"0 rows" and "could not measure" are different states.**  A successful
  query that found nothing yields :data:`PanelStatus.IDLE` with real ``0``
  values.  A failed query yields :data:`PanelStatus.ERROR` and ``None``
  values, which the template renders as ``—``.  A metric the source simply
  does not provide (NULL cost, NULL realized P&L, AI usage the provider never
  reported) is also ``None`` — never silently coerced to ``0``.

Query budget for one page load — 24 statements cold, 16 warm::

    운영 상태          4  alembic_version (5분 캐시) + brokers + runtime_state x2
    AI 사용량          4  summarize_ai_usage() 합계 + provider/model/feature — 24시간
    자동매매 funnel    2  추천 24시간 / PAPER 주문 24시간
    PAPER 포트폴리오   2  보유 포지션(계좌 한정) / 체결 30일
    체결 대사          1  최근 7일, broker별 1건
    데이터 readiness   7  DailyCandlesReadinessService — Redis 900초 캐시 시 0
    뉴스 파이프라인    2  수집 run 7일 / 기사 24시간
    전략 승격          2  승격 레지스트리(전량) / 후보 30일

Warm = alembic revision still cached (-1) and readiness served from Redis (-7).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.extensions.kasset.api.router import _build_system_status
from app.services.ai_usage_service import summarize_ai_usage
from app.services.daily_candles.readiness import (
    DailyCandlesReadiness,
    DailyCandlesReadinessService,
)
from app.services.ohlcv_cache_common import create_redis_client

logger = logging.getLogger(__name__)

# Windows. Each is a hard bound on the rows a panel may touch.
_RECENT_WINDOW = timedelta(hours=24)
_RUN_WINDOW = timedelta(days=7)
_LEDGER_WINDOW = timedelta(days=30)

# readiness is the only genuinely expensive panel (7 statements, full cohort
# scans). It reflects daily candle ingestion, which moves a few times a day at
# most, so a 15-minute shared TTL bounds it to <=4 heavy runs per hour across
# every worker while keeping the number fresh enough to act on.
_READINESS_CACHE_TTL_SECONDS = 900
_READINESS_CACHE_KEY = "ops_dashboard:daily_candles_readiness:v1"

_ERROR_DETAIL_MAX_CHARS = 200


class PanelStatus(StrEnum):
    """How much the operator can trust one panel's numbers."""

    OK = "ok"
    IDLE = "idle"
    WARN = "warn"
    ERROR = "error"


PANEL_STATUS_LABELS: Mapping[PanelStatus, str] = {
    PanelStatus.OK: "정상",
    PanelStatus.IDLE: "대기",
    PanelStatus.WARN: "주의",
    PanelStatus.ERROR: "조회실패",
}

# Rendered wherever a value could not be measured. Never used for a real zero.
UNMEASURED_TEXT = "—"


@dataclass(frozen=True, slots=True)
class OpsMetric:
    """One headline number. ``value is None`` means "측정 불가", not zero."""

    label: str
    value: str | None
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class OpsRow:
    """One table row. A ``None`` cell means "측정 불가", not zero."""

    cells: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class OpsPanel:
    """One dashboard panel.

    ``key``/``title`` are stamped by :func:`build_ops_dashboard` from the
    registry so a builder never repeats them.
    """

    status: PanelStatus
    summary: str
    metrics: tuple[OpsMetric, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[OpsRow, ...] = ()
    #: Standing caveat about what the panel's numbers do and do not cover.
    #: Unlike ``summary`` this does not change with the data.
    note: str | None = None
    window: str | None = None
    source: str | None = None
    error: str | None = None
    key: str = ""
    title: str = ""

    @property
    def status_label(self) -> str:
        return PANEL_STATUS_LABELS[self.status]


@dataclass(frozen=True, slots=True)
class OpsDashboard:
    generated_at: datetime
    panels: tuple[OpsPanel, ...]

    @property
    def failed_panel_titles(self) -> tuple[str, ...]:
        return tuple(
            panel.title for panel in self.panels if panel.status is PanelStatus.ERROR
        )


@dataclass(frozen=True, slots=True)
class OpsContext:
    db: AsyncSession
    admin_user_id: int
    now: datetime

    @property
    def since_recent(self) -> datetime:
        return self.now - _RECENT_WINDOW

    @property
    def since_runs(self) -> datetime:
        return self.now - _RUN_WINDOW

    @property
    def since_ledger(self) -> datetime:
        return self.now - _LEDGER_WINDOW


# --------------------------------------------------------------------------
# formatting — the single place that decides zero vs unmeasured
# --------------------------------------------------------------------------


def _count(value: object) -> str:
    return f"{int(value or 0):,}"


def _opt_count(value: object) -> str | None:
    """Format a nullable count. ``None`` stays ``None`` (미제공)."""
    return None if value is None else f"{int(value):,}"


def _amount(value: object) -> str | None:
    """Format a nullable money/quantity value without inventing a zero."""
    if value is None:
        return None
    number = Decimal(str(value))
    quantized = number.quantize(Decimal("0.01")) if number % 1 else number.to_integral()
    return f"{quantized:,}"


def _naive_utc(value: datetime) -> datetime:
    """Bind value for a ``TIMESTAMP WITHOUT TIME ZONE`` column.

    ``news_ingestion_runs`` and ``research.promotion_candidates`` store naive
    UTC (the writers use ``datetime.now(UTC).replace(tzinfo=None)`` and the
    server's ``now()`` under ``TimeZone=Etc/UTC``). asyncpg refuses an aware
    datetime for those columns, so the window bound has to be naive too.
    """
    return value.astimezone(UTC).replace(tzinfo=None)


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def _age(value: datetime | None, now: datetime) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return "미래 시각"
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 172800:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def _flag(value: bool) -> str:
    return "ON" if value else "OFF"


def _window_label(since: datetime, now: datetime) -> str:
    hours = round((now - since).total_seconds() / 3600)
    if hours % 24 == 0 and hours >= 24:
        return f"최근 {hours // 24}일"
    return f"최근 {hours}시간"


def _error_detail(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) > _ERROR_DETAIL_MAX_CHARS:
        message = message[:_ERROR_DETAIL_MAX_CHARS] + "…"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def _rows(
    ctx: OpsContext, sql: str, params: Mapping[str, Any] | None = None
) -> Sequence[Mapping[str, Any]]:
    result = await ctx.db.execute(text(sql), dict(params or {}))
    return result.mappings().all()


# --------------------------------------------------------------------------
# 1. 운영 상태
# --------------------------------------------------------------------------


async def _system_panel(ctx: OpsContext) -> OpsPanel:
    # Reuses the exact builder behind GET /api/v1/system/status so the web
    # dashboard and the Android client can never disagree about the same fact.
    status = await _build_system_status(ctx.db, ctx.admin_user_id)

    database_ok = status.database.status == "ok"
    revision = status.database.migration_revision
    warnings: list[str] = []
    if status.kill_switch_enabled:
        warnings.append("kill switch ON")
    if not database_ok:
        warnings.append(f"DB status={status.database.status}")
    if revision is None:
        warnings.append("migration revision 조회 불가")
    if status.live_trading_enabled:
        warnings.append("LIVE 매매 허용")

    metrics = (
        OpsMetric("서버 버전", status.server_version),
        OpsMetric("서버 시각", status.server_time),
        OpsMetric("DB", status.database.status),
        OpsMetric("migration revision", revision, hint="alembic_version"),
        OpsMetric("trading mode", status.trading_mode),
        OpsMetric("trading enabled", _flag(status.trading_enabled)),
        OpsMetric("live trading", _flag(status.live_trading_enabled)),
        OpsMetric("kill switch", _flag(status.kill_switch_enabled)),
    )
    rows = tuple(
        OpsRow(
            (
                broker.provider,
                "연결됨" if broker.connected else "미연결",
                broker.last_verified_at or UNMEASURED_TEXT,
            )
        )
        for broker in status.brokers
    )
    summary = (
        "주의: " + ", ".join(warnings)
        if warnings
        else f"정상 · 등록 broker {len(status.brokers)}건"
    )
    return OpsPanel(
        status=PanelStatus.WARN if warnings else PanelStatus.OK,
        summary=summary,
        metrics=metrics,
        columns=("broker", "연결", "마지막 확인"),
        rows=rows,
        source="app/extensions/kasset/api/router.py::_build_system_status",
    )


# --------------------------------------------------------------------------
# 2. AI 사용량
# --------------------------------------------------------------------------

# The ledger only has a row where the structured router creates an attempt
# slot. ``run_skill``-family providers (OpenAiCompatibleProvider →
# ChainedApiProvider, SubscriptionAgentProvider / subscription_cli,
# CloudflareAiProvider, hermes_client) bypass it entirely, so their usage is
# absent from these numbers — not zero, not NULL, absent. Saying so on screen
# is the difference between "AI를 안 썼다" and "여기서는 안 보인다".
AI_COVERAGE_NOTE = (
    "계측 범위: AvailabilityRoutedJsonClient를 거치는 구조화 호출만 "
    "(MCP 포함, 단 MCP는 usage를 주지 않아 토큰이 '미제공'으로 잡힘). "
    "run_skill 계열 경로(구독형 CLI·Cloudflare·Hermes·ChainedApiProvider)는 "
    "원장에 행 자체가 남지 않으므로 이 숫자에 전혀 나타나지 않는다. "
    "비용은 provider가 금액과 통화를 함께 줄 때만 기록되며 현재 운영 경로는 "
    "아무도 주지 않는다."
)


def _cost_cell(amount: Decimal | None, currency: str | None) -> str | None:
    """``None`` when the provider reported no cost — never a fabricated 0."""
    if amount is None or currency is None:
        return None
    return f"{_amount(amount)} {currency}"


def _success_rate(rate: float) -> str:
    return f"{rate * 100:.1f}%"


async def _ai_usage_panel(ctx: OpsContext) -> OpsPanel:
    summary_data = await summarize_ai_usage(
        ctx.db, since=ctx.since_recent, until=ctx.now
    )

    breakdowns = (
        ("provider", summary_data.by_provider),
        ("model", summary_data.by_model),
        ("feature", summary_data.by_feature),
    )
    rows = tuple(
        OpsRow(
            (
                dimension,
                item.key,
                _count(item.logical_calls),
                _count(item.attempts),
                _success_rate(item.success_rate),
                _count(item.total_tokens),
                _count(item.attempts_without_usage),
                _cost_cell(item.cost_amount, item.cost_currency),
            )
        )
        for dimension, items in breakdowns
        for item in items
    )

    if summary_data.attempts == 0:
        status = PanelStatus.IDLE
        summary = (
            "최근 24시간 계측된 AI 호출 0건 (조회 성공). "
            "계측 범위 밖 경로가 있으므로 AI를 쓰지 않았다는 뜻은 아니다."
        )
    elif summary_data.failure_attempts:
        status = PanelStatus.WARN
        summary = (
            f"논리 호출 {summary_data.logical_calls:,}건 / provider 시도 "
            f"{summary_data.attempts:,}건 중 실패 "
            f"{summary_data.failure_attempts:,}건"
        )
    else:
        status = PanelStatus.OK
        summary = (
            f"논리 호출 {summary_data.logical_calls:,}건 · provider 시도 "
            f"{summary_data.attempts:,}건 전부 성공"
        )

    # attempts_without_usage is the honest denominator caveat: MCP /
    # subscription / Cloudflare / Hermes routes return no usage at all, so
    # their tokens are missing, not zero.
    usage_hint = (
        f"토큰 미제공 {summary_data.attempts_without_usage:,}건 제외"
        if summary_data.attempts_without_usage
        else "모든 시도가 usage 보고"
    )
    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric(
                "논리 호출",
                _count(summary_data.logical_calls),
                hint="기능이 요청한 횟수",
            ),
            OpsMetric(
                "provider 시도",
                _count(summary_data.attempts),
                hint="fallback·티어 escalation 포함",
            ),
            OpsMetric("실패 시도", _count(summary_data.failure_attempts)),
            OpsMetric("총 토큰", _count(summary_data.total_tokens), hint=usage_hint),
            OpsMetric(
                "토큰 미제공 시도",
                _count(summary_data.attempts_without_usage),
                hint="usage를 주지 않는 경로",
            ),
            OpsMetric(
                "비용",
                _cost_cell(summary_data.cost_amount, summary_data.cost_currency),
                hint="provider가 보고한 값만",
            ),
            OpsMetric("p50 지연", _opt_count(summary_data.p50_latency_ms), hint="ms"),
            OpsMetric("p95 지연", _opt_count(summary_data.p95_latency_ms), hint="ms"),
        ),
        columns=(
            "구분",
            "키",
            "논리 호출",
            "시도",
            "성공률",
            "총 토큰",
            "토큰 미제공",
            "비용",
        ),
        rows=rows,
        note=AI_COVERAGE_NOTE,
        window=_window_label(ctx.since_recent, ctx.now),
        source="review.ai_call_events (app/services/ai_usage_service.py)",
    )


# --------------------------------------------------------------------------
# 3. 자동매매 funnel
# --------------------------------------------------------------------------

# ``owner_user_id IN (SELECT id FROM users)`` keeps the leading column of
# ix_ai_recommendations_owner_decision_created_at / of
# ix_kasset_android_paper_order_owner_created bound, so Postgres drives the
# aggregate off the index instead of scanning the event table. ``users`` is a
# 3-row table in production.
_RECOMMENDATION_FUNNEL_SQL = """/* ops_dashboard:recommendations */
SELECT decision,
       count(*) AS attempt_count,
       max(created_at) AS last_created_at,
       count(*) FILTER (WHERE decided_at IS NOT NULL) AS decided_count
FROM review.ai_recommendations
WHERE owner_user_id IN (SELECT id FROM users)
  AND created_at >= :since
GROUP BY decision
ORDER BY attempt_count DESC, decision
"""

_PAPER_ORDER_FUNNEL_SQL = """/* ops_dashboard:paper_orders */
SELECT status,
       count(*) AS order_count,
       sum(filled_quantity) AS filled_quantity,
       max(updated_at) AS last_update_at,
       max(updated_at) FILTER (WHERE filled_quantity > 0) AS last_fill_at
FROM kasset_android_paper_orders
WHERE owner_user_id IN (SELECT id FROM users)
  AND created_at >= :since
GROUP BY status
ORDER BY order_count DESC, status
"""


async def _funnel_panel(ctx: OpsContext) -> OpsPanel:
    params = {"since": ctx.since_recent}
    recommendations = await _rows(ctx, _RECOMMENDATION_FUNNEL_SQL, params)
    orders = await _rows(ctx, _PAPER_ORDER_FUNNEL_SQL, params)

    recommendation_total = sum(int(row["attempt_count"]) for row in recommendations)
    order_total = sum(int(row["order_count"]) for row in orders)
    last_fill = max(
        (row["last_fill_at"] for row in orders if row["last_fill_at"] is not None),
        default=None,
    )

    rows = tuple(
        OpsRow(
            (
                "추천",
                str(row["decision"]),
                _count(row["attempt_count"]),
                _count(row["decided_count"]),
                _ts(row["last_created_at"]),
            )
        )
        for row in recommendations
    ) + tuple(
        OpsRow(
            (
                "PAPER 주문",
                str(row["status"]),
                _count(row["order_count"]),
                _amount(row["filled_quantity"]),
                _ts(row["last_update_at"]),
            )
        )
        for row in orders
    )

    if recommendation_total == 0 and order_total == 0:
        status = PanelStatus.IDLE
        summary = "최근 24시간 추천 0건 · PAPER 주문 0건 (조회 성공, 실행 이력 없음)"
    else:
        status = PanelStatus.OK
        summary = f"추천 {recommendation_total:,}건 · PAPER 주문 {order_total:,}건"

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("추천 생성", _count(recommendation_total)),
            OpsMetric("PAPER 주문", _count(order_total)),
            OpsMetric(
                "마지막 체결",
                _ts(last_fill),
                hint=_age(last_fill, ctx.now) or "체결 이력 없음",
            ),
        ),
        columns=("구분", "상태", "건수", "결정/체결수량", "최근 시각"),
        rows=rows,
        window=_window_label(ctx.since_recent, ctx.now),
        source="review.ai_recommendations · kasset_android_paper_orders",
    )


# --------------------------------------------------------------------------
# 4. PAPER 포트폴리오
# --------------------------------------------------------------------------

# Scoped to the accounts the KAsset surface actually owns, never the whole
# paper schema. total_invested has no currency column, so it is grouped by
# instrument_type instead of summed across KRW and USD.
_PAPER_POSITIONS_SQL = """/* ops_dashboard:paper_positions */
SELECT instrument_type::text AS instrument_type,
       count(*) AS position_count,
       sum(total_invested) AS invested_amount
FROM paper.paper_positions
WHERE account_id IN (SELECT paper_account_id FROM kasset_android_paper_accounts)
  AND quantity <> 0
GROUP BY instrument_type
ORDER BY instrument_type
"""

_PAPER_TRADES_SQL = """/* ops_dashboard:paper_trades */
SELECT currency,
       count(*) AS trade_count,
       sum(total_amount) AS traded_amount,
       sum(realized_pnl) AS realized_pnl,
       max(executed_at) AS last_executed_at
FROM paper.paper_trades
WHERE account_id IN (SELECT paper_account_id FROM kasset_android_paper_accounts)
  AND executed_at >= :since
GROUP BY currency
ORDER BY currency
"""


async def _paper_portfolio_panel(ctx: OpsContext) -> OpsPanel:
    positions = await _rows(ctx, _PAPER_POSITIONS_SQL)
    trades = await _rows(ctx, _PAPER_TRADES_SQL, {"since": ctx.since_ledger})

    position_total = sum(int(row["position_count"]) for row in positions)
    trade_total = sum(int(row["trade_count"]) for row in trades)
    last_trade = max(
        (
            row["last_executed_at"]
            for row in trades
            if row["last_executed_at"] is not None
        ),
        default=None,
    )

    rows = tuple(
        OpsRow(
            (
                "보유",
                str(row["instrument_type"]),
                _count(row["position_count"]),
                _amount(row["invested_amount"]),
                None,
            )
        )
        for row in positions
    ) + tuple(
        OpsRow(
            (
                "체결",
                str(row["currency"]),
                _count(row["trade_count"]),
                _amount(row["traded_amount"]),
                _amount(row["realized_pnl"]),
            )
        )
        for row in trades
    )

    if position_total == 0 and trade_total == 0:
        status = PanelStatus.IDLE
        summary = "보유 포지션 0건 · 최근 30일 체결 0건 (조회 성공, 거래 이력 없음)"
    else:
        status = PanelStatus.OK
        summary = f"보유 {position_total:,}건 · 최근 30일 체결 {trade_total:,}건"

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("보유 포지션", _count(position_total)),
            OpsMetric("체결", _count(trade_total)),
            OpsMetric(
                "마지막 체결",
                _ts(last_trade),
                hint=_age(last_trade, ctx.now) or "체결 이력 없음",
            ),
        ),
        columns=("구분", "통화/종류", "건수", "금액", "실현손익"),
        rows=rows,
        window=f"보유 전량 · 체결 {_window_label(ctx.since_ledger, ctx.now)}",
        source="paper.paper_positions · paper.paper_trades (KAsset 계좌 한정)",
    )


# --------------------------------------------------------------------------
# 5. 체결 대사(reconcile)
# --------------------------------------------------------------------------

_RECONCILE_SQL = """/* ops_dashboard:reconcile_runs */
SELECT DISTINCT ON (broker)
       broker,
       started_at,
       finished_at,
       dry_run,
       committed_insert,
       committed_update,
       error_summary
FROM review.execution_ledger_reconcile_runs
WHERE started_at >= :since
ORDER BY broker, started_at DESC
"""


async def _reconcile_panel(ctx: OpsContext) -> OpsPanel:
    runs = await _rows(ctx, _RECONCILE_SQL, {"since": ctx.since_runs})

    failing = [
        row for row in runs if row["error_summary"] or row["finished_at"] is None
    ]
    stale = [
        row
        for row in runs
        if row["finished_at"] is not None
        and (ctx.now - _aware(row["finished_at"])) > _RECENT_WINDOW
    ]
    committed = sum(
        int(row["committed_insert"]) + int(row["committed_update"]) for row in runs
    )

    rows = tuple(
        OpsRow(
            (
                str(row["broker"]),
                _ts(row["started_at"]),
                _age(row["finished_at"], ctx.now) or "미완료",
                "dry-run" if row["dry_run"] else "commit",
                _count(int(row["committed_insert"]) + int(row["committed_update"])),
                str(row["error_summary"]) if row["error_summary"] else "-",
            )
        )
        for row in runs
    )

    if not runs:
        status = PanelStatus.IDLE
        summary = "최근 7일 대사 실행 0건 (조회 성공, 실행 이력 없음)"
    elif failing or stale:
        status = PanelStatus.WARN
        parts = []
        if failing:
            parts.append(f"오류/미완료 {len(failing)}건")
        if stale:
            parts.append(f"24시간 초과 {len(stale)}건")
        summary = "주의: " + ", ".join(parts)
    else:
        status = PanelStatus.OK
        summary = f"broker {len(runs)}곳 대사 정상 · 반영 {committed:,}건"

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("대사한 broker", _count(len(runs))),
            OpsMetric("오류/미완료", _count(len(failing))),
            OpsMetric("반영 행", _count(committed)),
        ),
        columns=("broker", "시작", "완료", "모드", "반영", "오류"),
        rows=rows,
        window=_window_label(ctx.since_runs, ctx.now),
        source="review.execution_ledger_reconcile_runs",
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# 6. KR/US 데이터 readiness (캐시)
# --------------------------------------------------------------------------

_REDIS_CLIENT: redis.Redis | None = None


async def _get_redis_client() -> redis.Redis | None:
    """Lazily create the shared Redis client; ``None`` disables the cache.

    Mirrors ``app/core/analyze_cache.py``: every caller fails open to the live
    measurement when Redis is unavailable, and tests patch this function to
    keep the cache out of the way.
    """
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        _REDIS_CLIENT = await create_redis_client()
    except Exception as exc:
        logger.debug("ops_dashboard: redis client init failed: %s", exc)
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


async def close_ops_dashboard_redis() -> None:
    """Close the module-level Redis client (test/teardown helper)."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        with suppress(Exception):
            await _REDIS_CLIENT.aclose()
        _REDIS_CLIENT = None


def _readiness_snapshot(readiness: DailyCandlesReadiness) -> dict[str, Any]:
    """Reduce the readiness evidence to the small payload the panel renders."""
    return {
        "as_of": readiness.as_of.isoformat(),
        "daily_history_ready": readiness.daily_history_ready,
        "promotion_ready": readiness.promotion_ready,
        "blockers": list(readiness.blockers),
        "markets": [
            {
                "market": market.market,
                "cohort_id": market.cohort.cohort_id if market.cohort else None,
                "daily_history_ready": market.daily_history_ready,
                "promotion_ready": market.promotion_ready,
                "eligible_symbol_count": market.eligible_symbol_count,
                "symbols_with_at_least_252_bars": (
                    market.symbols_with_at_least_252_bars
                ),
                "stale_bar_count": market.stale_bar_count,
                "duplicate_timestamp_count": market.duplicate_timestamp_count,
                "ohlc_anomaly_count": market.ohlc_anomaly_count,
                "missing_expected_trading_day_count": (
                    market.missing_expected_trading_day_count
                ),
                "benchmark_symbol": market.benchmark.symbol,
                "benchmark_status": market.benchmark.status,
                "benchmark_count": market.benchmark.count,
                "blockers": list(market.blockers),
            }
            for market in readiness.markets
        ],
    }


async def _cached_readiness_snapshot(
    ctx: OpsContext,
) -> tuple[dict[str, Any], bool]:
    """Return ``(snapshot, from_cache)``; always falls open to a live measure."""
    client = await _get_redis_client()
    if client is not None:
        try:
            raw = await client.get(_READINESS_CACHE_KEY)
        except Exception as exc:
            logger.debug("ops_dashboard: readiness cache GET failed: %s", exc)
        else:
            if isinstance(raw, str):
                try:
                    cached = json.loads(raw)
                except ValueError:
                    cached = None
                if isinstance(cached, dict) and isinstance(cached.get("markets"), list):
                    return cached, True

    readiness = await DailyCandlesReadinessService(ctx.db).measure(as_of=ctx.now)
    snapshot = _readiness_snapshot(readiness)
    if client is not None:
        with suppress(Exception):
            await client.set(
                _READINESS_CACHE_KEY,
                json.dumps(snapshot),
                ex=_READINESS_CACHE_TTL_SECONDS,
            )
    return snapshot, False


async def _readiness_panel(ctx: OpsContext) -> OpsPanel:
    snapshot, from_cache = await _cached_readiness_snapshot(ctx)
    markets = snapshot["markets"]

    rows = tuple(
        OpsRow(
            (
                str(market["market"]).upper(),
                "준비됨" if market["daily_history_ready"] else "미달",
                "준비됨" if market["promotion_ready"] else "미달",
                _count(market["eligible_symbol_count"]),
                _count(market["symbols_with_at_least_252_bars"]),
                _count(market["stale_bar_count"]),
                _opt_count(market["missing_expected_trading_day_count"]),
                _count(market["duplicate_timestamp_count"]),
                _count(market["ohlc_anomaly_count"]),
                f"{market['benchmark_symbol'] or '-'} ({market['benchmark_status']})",
                ", ".join(market["blockers"]) if market["blockers"] else "-",
            )
        )
        for market in markets
    )

    if not markets:
        status = PanelStatus.IDLE
        summary = "측정된 코호트 0건 (조회 성공, 코호트 미구성)"
    elif not snapshot["promotion_ready"]:
        status = PanelStatus.WARN
        blockers = snapshot["blockers"]
        summary = "승격 기준 미달: " + (
            ", ".join(blockers[:4]) if blockers else "사유 미기재"
        )
    else:
        status = PanelStatus.OK
        summary = "KR/US 일봉 이력과 승격 기준 모두 충족"

    measured_at = snapshot.get("as_of")
    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric(
                "일봉 이력",
                "준비됨" if snapshot["daily_history_ready"] else "미달",
            ),
            OpsMetric(
                "승격 준비",
                "준비됨" if snapshot["promotion_ready"] else "미달",
            ),
            OpsMetric(
                "측정 시각",
                _ts(datetime.fromisoformat(measured_at)) if measured_at else None,
                hint="캐시" if from_cache else "방금 측정",
            ),
        ),
        columns=(
            "시장",
            "일봉 이력",
            "승격",
            "적격 종목",
            "252봉 이상",
            "stale",
            "결측 거래일",
            "중복",
            "이상치",
            "벤치마크",
            "blocker",
        ),
        rows=rows,
        window=f"캐시 TTL {_READINESS_CACHE_TTL_SECONDS // 60}분",
        source="app/services/daily_candles/readiness.py",
    )


# --------------------------------------------------------------------------
# 7. 뉴스 파이프라인
# --------------------------------------------------------------------------

_NEWS_RUNS_SQL = """/* ops_dashboard:news_runs */
SELECT DISTINCT ON (market)
       market,
       feed_set,
       finished_at,
       status,
       inserted_count,
       skipped_count,
       error_message
FROM news_ingestion_runs
WHERE finished_at >= :since
ORDER BY market, finished_at DESC
"""

# ``article_published_at`` is a naive column with two live writer conventions:
# llm_news_service/news_payload_normalizer store naive KST, symbol_news_store
# stores naive UTC. A naive-UTC bound is the only one that cannot silently
# drop rows (a KST-written row just looks 9h newer), so this window is
# over-inclusive by up to 9 hours rather than under-inclusive. The panel never
# renders an absolute clock off this column for the same reason.
_NEWS_ARTICLES_SQL = """/* ops_dashboard:news_articles */
SELECT market,
       count(*) AS article_count,
       count(*) FILTER (WHERE summary IS NOT NULL) AS summarized_count,
       count(*) FILTER (WHERE is_analyzed) AS analyzed_count
FROM news_articles
WHERE article_published_at >= :since
GROUP BY market
ORDER BY market
"""


async def _news_panel(ctx: OpsContext) -> OpsPanel:
    runs = await _rows(ctx, _NEWS_RUNS_SQL, {"since": _naive_utc(ctx.since_runs)})
    articles = await _rows(
        ctx, _NEWS_ARTICLES_SQL, {"since": _naive_utc(ctx.since_recent)}
    )

    failing = [row for row in runs if row["status"] not in ("success", "dry_run_ok")]
    article_total = sum(int(row["article_count"]) for row in articles)
    summarized_total = sum(int(row["summarized_count"]) for row in articles)
    analyzed_total = sum(int(row["analyzed_count"]) for row in articles)
    latest_run_finished = max(
        (row["finished_at"] for row in runs if row["finished_at"] is not None),
        default=None,
    )

    rows = tuple(
        OpsRow(
            (
                "수집 run",
                str(row["market"]),
                str(row["status"]),
                _count(row["inserted_count"]),
                _count(row["skipped_count"]),
                _age(row["finished_at"], ctx.now) or "미완료",
                str(row["error_message"]) if row["error_message"] else "-",
            )
        )
        for row in runs
    ) + tuple(
        OpsRow(
            (
                "기사",
                str(row["market"]),
                "-",
                _count(row["article_count"]),
                f"요약 {_count(row['summarized_count'])} / 분석 "
                f"{_count(row['analyzed_count'])}",
                "-",
                "-",
            )
        )
        for row in articles
    )

    if not runs and article_total == 0:
        status = PanelStatus.IDLE
        summary = "최근 7일 수집 run 0건 · 최근 24시간 기사 0건 (조회 성공)"
    elif failing:
        status = PanelStatus.WARN
        summary = f"주의: 실패/부분 성공 run {len(failing)}건"
    else:
        status = PanelStatus.OK
        summary = (
            f"수집 run {len(runs)}건 · 최근 24시간 기사 {article_total:,}건 "
            f"(요약 {summarized_total:,} / 분석 {analyzed_total:,})"
        )

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("수집 run", _count(len(runs))),
            OpsMetric("기사", _count(article_total)),
            OpsMetric(
                "최근 수집",
                _ts(latest_run_finished),
                hint=_age(latest_run_finished, ctx.now) or "완료된 run 없음",
            ),
        ),
        columns=(
            "구분",
            "시장",
            "상태",
            "수집/기사",
            "건너뜀 · 커버리지",
            "최근",
            "오류",
        ),
        rows=rows,
        window=(
            f"run {_window_label(ctx.since_runs, ctx.now)} · "
            "기사 최근 24시간(발행시각 기준, 수집기별 타임존 혼재로 최대 +9시간 과대집계)"
        ),
        source="news_ingestion_runs · news_articles",
    )


# --------------------------------------------------------------------------
# 8. 전략 승격
# --------------------------------------------------------------------------

# Deliberately unwindowed. review.kasset_strategy_promotions is a registry
# keyed by (strategy_key, version), not an event log — one row per strategy
# version, index-only over ix_kasset_strategy_promotion_state_updated. A time
# window here would hide an old-but-still-PAPER_APPROVED strategy, which is
# exactly the fact that decides whether AUTO_PAPER may place an order.
_PROMOTION_STATE_SQL = """/* ops_dashboard:promotion_states */
SELECT state,
       count(*) AS promotion_count,
       max(updated_at) AS last_updated_at
FROM review.kasset_strategy_promotions
GROUP BY state
ORDER BY state
"""

_PROMOTION_CANDIDATE_SQL = """/* ops_dashboard:promotion_candidates */
SELECT status,
       count(*) AS candidate_count,
       max(evaluated_at) AS last_evaluated_at
FROM research.promotion_candidates
WHERE evaluated_at >= :since
GROUP BY status
ORDER BY candidate_count DESC, status
"""

_APPROVED_STATES = frozenset({"PAPER_APPROVED"})


async def _strategy_panel(ctx: OpsContext) -> OpsPanel:
    states = await _rows(ctx, _PROMOTION_STATE_SQL)
    candidates = await _rows(
        ctx, _PROMOTION_CANDIDATE_SQL, {"since": _naive_utc(ctx.since_ledger)}
    )

    promotion_total = sum(int(row["promotion_count"]) for row in states)
    approved_total = sum(
        int(row["promotion_count"])
        for row in states
        if str(row["state"]) in _APPROVED_STATES
    )
    candidate_total = sum(int(row["candidate_count"]) for row in candidates)

    rows = tuple(
        OpsRow(
            (
                "승격",
                str(row["state"]),
                _count(row["promotion_count"]),
                _ts(row["last_updated_at"]),
            )
        )
        for row in states
    ) + tuple(
        OpsRow(
            (
                "후보",
                str(row["status"]),
                _count(row["candidate_count"]),
                _ts(row["last_evaluated_at"]),
            )
        )
        for row in candidates
    )

    if promotion_total == 0:
        status = PanelStatus.IDLE
        summary = (
            "승격 레지스트리 0건 (조회 성공) — PAPER_APPROVED 전략이 없어 "
            "AUTO_PAPER 자동발주 근거가 없고 APPROVAL 경로만 열립니다."
        )
    elif approved_total == 0:
        status = PanelStatus.WARN
        summary = (
            f"승격 {promotion_total:,}건 중 PAPER_APPROVED 0건 — "
            "AUTO_PAPER 자동발주 근거 없음, APPROVAL 경로만 열림"
        )
    else:
        status = PanelStatus.OK
        summary = (
            f"PAPER_APPROVED {approved_total:,}건 · 전체 승격 {promotion_total:,}건"
        )

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("PAPER_APPROVED", _count(approved_total)),
            OpsMetric("승격 레지스트리", _count(promotion_total)),
            OpsMetric("최근 30일 후보", _count(candidate_total)),
        ),
        columns=("구분", "상태", "건수", "최근 시각"),
        rows=rows,
        window="승격 전량 · 후보 최근 30일",
        source="review.kasset_strategy_promotions · research.promotion_candidates",
    )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

PanelBuilder = Callable[[OpsContext], Awaitable[OpsPanel]]

PANEL_BUILDERS: tuple[tuple[str, str, PanelBuilder], ...] = (
    ("system", "운영 상태", _system_panel),
    ("ai_usage", "AI 사용량", _ai_usage_panel),
    ("funnel", "자동매매 funnel", _funnel_panel),
    ("paper_portfolio", "PAPER 포트폴리오", _paper_portfolio_panel),
    ("reconcile", "체결 대사", _reconcile_panel),
    ("data_readiness", "KR/US 데이터 readiness", _readiness_panel),
    ("news", "뉴스 파이프라인", _news_panel),
    ("strategy", "전략 승격", _strategy_panel),
)


async def build_ops_dashboard(
    db: AsyncSession,
    *,
    admin_user_id: int,
    now: datetime | None = None,
) -> OpsDashboard:
    """Measure every panel sequentially, isolating each one's failures.

    Panels share a single ``AsyncSession`` and therefore a single transaction;
    a failed statement poisons it, so a failing panel is rolled back before the
    next one runs. That is what keeps the other panels renderable.
    """
    moment = now or datetime.now(UTC)
    ctx = OpsContext(db=db, admin_user_id=admin_user_id, now=moment)

    panels: list[OpsPanel] = []
    for key, title, builder in PANEL_BUILDERS:
        try:
            panel = await builder(ctx)
        except Exception as exc:
            logger.warning(
                "ops dashboard panel failed: %s",
                key,
                exc_info=True,
                extra={"panel": key},
            )
            with suppress(Exception):
                await db.rollback()
            panel = OpsPanel(
                status=PanelStatus.ERROR,
                summary="지표를 가져오지 못했습니다. 아래 값은 0이 아니라 미측정입니다.",
                error=_error_detail(exc),
            )
        panels.append(replace(panel, key=key, title=title))

    return OpsDashboard(generated_at=moment, panels=tuple(panels))


__all__ = [
    "OpsDashboard",
    "OpsMetric",
    "OpsPanel",
    "OpsRow",
    "PANEL_BUILDERS",
    "PANEL_STATUS_LABELS",
    "PanelStatus",
    "UNMEASURED_TEXT",
    "build_ops_dashboard",
    "close_ops_dashboard_redis",
]
