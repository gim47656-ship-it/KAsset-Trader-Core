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

Query budget for one page load — 28 statements cold, 20 warm::

    운영 상태          5  alembic_version (5분 캐시) + 활성 사용자/AI 모드/증권사/전역 kill switch
    AI 사용량          4  summarize_ai_usage() 합계 + provider/model/feature — 24시간
    추천 수집/AI 판정 2  동일한 bounded cycle 원장 최근 24시간, 패널별 1회
    자동매매 funnel    2  추천 24시간 / PAPER 주문 24시간
    PAPER 포트폴리오   2  보유 포지션(계좌 한정) / 체결 30일
    체결 대사          1  최근 7일, broker별 1건
    데이터 readiness   7  DailyCandlesReadinessService — Redis 900초 캐시 시 0
    뉴스 파이프라인    2  수집 run 7일 / 기사 24시간
    전략 승격          3  승격 레지스트리(전량) / 후보 30일 / 승격 우회 소유자

Warm = alembic revision still cached (-1) and readiness served from Redis (-7).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import KST
from app.extensions.kasset.api.router import _applied_migration_revision
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
    return "켜짐" if value else "꺼짐"


_TRADING_MODE_LABELS = {
    "PAPER": "모의투자 (PAPER)",
    "AUTO_PAPER": "자동 모의투자 (AUTO_PAPER)",
    "LIVE": "실거래 (LIVE)",
}

_STATE_LABELS = {
    "PENDING": "대기 중 (PENDING)",
    "APPROVED": "승인됨 (APPROVED)",
    "REJECTED": "거절됨 (REJECTED)",
    "PAPER_APPROVED": "모의투자 승인 (PAPER_APPROVED)",
    "FILLED": "체결 완료 (FILLED)",
    "PARTIALLY_FILLED": "일부 체결 (PARTIALLY_FILLED)",
    "CANCELLED": "취소됨 (CANCELLED)",
    "FAILED": "실패 (FAILED)",
    "success": "성공 (success)",
    "dry_run_ok": "모의 실행 성공 (dry_run_ok)",
}


def _trading_mode(value: object) -> str:
    normalized = str(value)
    return _TRADING_MODE_LABELS.get(normalized, normalized)


def _ai_mode(value: object) -> str:
    normalized = str(value)
    return {
        "APPROVAL": "승인 후 모의주문 (APPROVAL)",
        "AUTO_PAPER": "승인 없는 자동 모의주문 (AUTO_PAPER)",
    }.get(normalized, normalized)


def _state(value: object) -> str:
    normalized = str(value)
    return _STATE_LABELS.get(normalized, normalized)


def _market(value: object) -> str:
    normalized = str(value).lower()
    return {
        "kr": "국내 (KR)",
        "us": "미국 (US)",
        "crypto": "가상자산",
    }.get(normalized, str(value))


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


_OWNER_RUNTIME_SUMMARY_SQL = """/* ops_dashboard:owner_runtime_summary */
SELECT state.trading_mode,
       state.kill_switch_enabled,
       count(*) AS owner_count
FROM kasset_android_runtime_state AS state
JOIN users AS owner
  ON owner.id = state.owner_user_id
 AND owner.is_active IS TRUE
 AND owner.role IN ('trader', 'admin')
GROUP BY state.trading_mode, state.kill_switch_enabled
ORDER BY state.trading_mode, state.kill_switch_enabled
"""

_AI_MODE_SUMMARY_SQL = """/* ops_dashboard:ai_mode_summary */
SELECT COALESCE(NULLIF(setting.value ->> 'mode', ''), 'APPROVAL') AS ai_mode,
       count(*) AS owner_count
FROM users AS owner
LEFT JOIN user_settings AS setting
  ON setting.user_id = owner.id
 AND setting.key = 'kasset.ai_trading'
WHERE owner.is_active IS TRUE
  AND (
    owner.role = 'trader'
    OR (
      owner.role = 'admin'
      AND (
        setting.user_id IS NOT NULL
        OR EXISTS (
          SELECT 1
          FROM kasset_android_runtime_state AS runtime
          WHERE runtime.owner_user_id = owner.id
        )
        OR EXISTS (
          SELECT 1
          FROM kasset_broker_credentials AS credentials
          WHERE credentials.owner_user_id = owner.id
        )
        OR EXISTS (
          SELECT 1
          FROM review.ai_recommendations AS recommendation
          WHERE recommendation.owner_user_id = owner.id
        )
      )
    )
  )
GROUP BY COALESCE(NULLIF(setting.value ->> 'mode', ''), 'APPROVAL')
ORDER BY ai_mode
"""

_ACTIVE_BROKER_SUMMARY_SQL = """/* ops_dashboard:active_broker_summary */
SELECT credentials.provider,
       count(*) AS owner_count,
       max(credentials.last_verified_at) AS last_verified_at
FROM kasset_broker_credentials AS credentials
JOIN users AS owner
  ON owner.id = credentials.owner_user_id
 AND owner.is_active IS TRUE
 AND owner.role IN ('trader', 'admin')
GROUP BY credentials.provider
ORDER BY credentials.provider
"""

_GLOBAL_RUNTIME_SQL = """/* ops_dashboard:global_runtime */
SELECT kill_switch_enabled
FROM kasset_global_runtime_state
WHERE id = 1
"""


async def _system_panel(ctx: OpsContext) -> OpsPanel:
    # These read-only SELECTs do not create default runtime or user-setting
    # rows. Missing runtime means PAPER/OFF; missing AI mode means APPROVAL.
    revision = await _applied_migration_revision(ctx.db)
    owner_states = await _rows(ctx, _OWNER_RUNTIME_SUMMARY_SQL)
    ai_modes = await _rows(ctx, _AI_MODE_SUMMARY_SQL)
    active_brokers = await _rows(ctx, _ACTIVE_BROKER_SUMMARY_SQL)
    global_rows = await _rows(ctx, _GLOBAL_RUNTIME_SQL)

    owner_total = sum(int(row["owner_count"]) for row in ai_modes)
    owner_kill_switch_total = sum(
        int(row["owner_count"])
        for row in owner_states
        if bool(row["kill_switch_enabled"])
    )
    ai_mode_counts = {str(row["ai_mode"]): int(row["owner_count"]) for row in ai_modes}
    auto_owner_total = ai_mode_counts.get("AUTO_PAPER", 0)
    approval_owner_total = ai_mode_counts.get("APPROVAL", 0)
    broker_connection_total = sum(int(row["owner_count"]) for row in active_brokers)
    global_kill_switch = bool(global_rows and global_rows[0]["kill_switch_enabled"])

    warnings: list[str] = []
    if global_kill_switch:
        warnings.append("전체 긴급 중지 켜짐")
    if owner_kill_switch_total:
        warnings.append(f"사용자 긴급 중지 켜짐 {owner_kill_switch_total:,}명")
    if not owner_total:
        warnings.append("활성 거래 사용자 없음")
    elif settings.AI_PAPER_AUTO_EXECUTION_ENABLED and not auto_owner_total:
        warnings.append("승인 없는 자동 주문 사용자 없음")
    if revision is None:
        warnings.append("DB 스키마 버전 조회 불가")
    if settings.LIVE_TRADING_ENABLED:
        warnings.append("실거래 허용됨")

    metrics = (
        OpsMetric("서버 버전", settings.KASSET_SERVER_VERSION),
        OpsMetric(
            "서버 시각",
            ctx.now.astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        ),
        OpsMetric("데이터베이스", "정상"),
        OpsMetric("DB 스키마 버전", revision, hint="alembic_version"),
        OpsMetric(
            "활성 거래 사용자",
            _count(owner_total),
            hint="활성 trader + 거래 상태가 있는 admin",
        ),
        OpsMetric(
            "승인 없는 자동 주문 사용자",
            _count(auto_owner_total),
            hint="AUTO_PAPER",
        ),
        OpsMetric(
            "승인 후 주문 사용자",
            _count(approval_owner_total),
            hint="APPROVAL",
        ),
        OpsMetric(
            "Core 거래 기능",
            _flag(settings.TRADING_ENABLED),
            hint="TRADING_ENABLED",
        ),
        OpsMetric(
            "실거래 허용",
            _flag(settings.LIVE_TRADING_ENABLED),
            hint="LIVE_TRADING_ENABLED",
        ),
        OpsMetric(
            "모의투자 자동 실행",
            _flag(settings.AI_PAPER_AUTO_EXECUTION_ENABLED),
            hint="AI_PAPER_AUTO_EXECUTION_ENABLED",
        ),
        OpsMetric(
            "사용자 긴급 중지 켜짐",
            _count(owner_kill_switch_total),
            hint=f"활성 거래 사용자 {owner_total:,}명",
        ),
        OpsMetric("전체 긴급 중지", _flag(global_kill_switch)),
    )
    rows = (
        tuple(
            OpsRow(
                (
                    "사용자 거래 설정",
                    _trading_mode(row["trading_mode"]),
                    f"긴급 중지 {_flag(bool(row['kill_switch_enabled']))}",
                    _count(row["owner_count"]),
                )
            )
            for row in owner_states
        )
        + tuple(
            OpsRow(
                (
                    "AI 주문 방식",
                    _ai_mode(row["ai_mode"]),
                    "설정됨",
                    _count(row["owner_count"]),
                )
            )
            for row in ai_modes
        )
        + tuple(
            OpsRow(
                (
                    "증권사 연결",
                    str(row["provider"]),
                    "인증정보 등록됨",
                    _ts(row["last_verified_at"]) or _count(row["owner_count"]),
                )
            )
            for row in active_brokers
        )
    )
    summary = (
        "확인 필요: " + ", ".join(warnings)
        if warnings
        else (
            f"운영 안전 설정 정상 · 활성 거래 사용자 {owner_total:,}명 · "
            f"증권사 연결 {broker_connection_total:,}건"
        )
    )
    return OpsPanel(
        status=PanelStatus.WARN if warnings else PanelStatus.OK,
        summary=summary,
        metrics=metrics,
        columns=("구분", "대상", "상태", "수/최근 확인"),
        rows=rows,
        note=(
            "활성 trader와 거래 설정·상태·추천이 있는 admin만 집계합니다. 저장된 runtime 행이 없으면 모의투자/긴급 중지 꺼짐, "
            "AI 주문 방식 행이 없으면 승인 후 주문(APPROVAL)이 안전 기본값입니다. "
            "이 조회는 기본 행을 만들거나 설정을 바꾸지 않습니다."
        ),
        source=(
            "alembic_version · users · user_settings · kasset_broker_credentials · "
            "kasset_android_runtime_state · kasset_global_runtime_state"
        ),
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
    "이 화면에는 앱이 기록한 구조화 AI 호출(일반 API·OpenRouter·AI MCP)만 "
    "집계됩니다. 구독형 CLI와 알림형 연동은 별도 경로라 여기에 표시되지 않으므로 "
    "0건이 곧 AI 미사용을 뜻하지 않습니다. AI MCP는 토큰 사용량을 보내지 않아 "
    "'토큰 미제공'으로 표시됩니다. 비용도 AI 제공사가 금액과 통화를 함께 보낼 때만 "
    "표시됩니다."
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
        ("AI 제공사", summary_data.by_provider),
        ("모델", summary_data.by_model),
        ("기능", summary_data.by_feature),
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
            "최근 24시간 화면에 집계된 AI 호출 0건 (조회 성공). "
            "구독형 CLI 등 별도 AI 경로는 이 화면에 표시되지 않습니다."
        )
    elif summary_data.failure_attempts:
        status = PanelStatus.WARN
        summary = (
            f"AI 요청 {summary_data.logical_calls:,}건 / 제공사 연결 시도 "
            f"{summary_data.attempts:,}건 중 실패 "
            f"{summary_data.failure_attempts:,}건"
        )
    else:
        status = PanelStatus.OK
        summary = (
            f"AI 요청 {summary_data.logical_calls:,}건 · 제공사 연결 시도 "
            f"{summary_data.attempts:,}건 전부 성공"
        )

    # attempts_without_usage is the honest denominator caveat: MCP /
    # subscription / Cloudflare / Hermes routes return no usage at all, so
    # their tokens are missing, not zero.
    all_tokens_unreported = (
        summary_data.attempts > 0
        and summary_data.attempts_without_usage == summary_data.attempts
    )
    usage_hint = (
        f"사용량을 보내지 않은 시도 {summary_data.attempts_without_usage:,}건 제외"
        if summary_data.attempts_without_usage
        else "모든 시도가 토큰 사용량을 보냄"
    )
    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric(
                "AI 요청",
                _count(summary_data.logical_calls),
                hint=(
                    "기능별 요청 수입니다. 한 요청이 정밀 검토로 넘어가면 "
                    "모델을 두 번 호출할 수 있습니다."
                ),
            ),
            OpsMetric(
                "AI 제공사 시도",
                _count(summary_data.attempts),
                hint="대체 제공사 호출·모델 단계 상승 포함",
            ),
            OpsMetric("실패 시도", _count(summary_data.failure_attempts)),
            OpsMetric(
                "총 토큰",
                None if all_tokens_unreported else _count(summary_data.total_tokens),
                hint=usage_hint,
            ),
            OpsMetric(
                "토큰 미제공 시도",
                _count(summary_data.attempts_without_usage),
                hint="토큰 사용량을 보내지 않은 AI 경로",
            ),
            OpsMetric(
                "비용",
                _cost_cell(summary_data.cost_amount, summary_data.cost_currency),
                hint="AI 제공사가 보고한 값만",
            ),
            OpsMetric("p50 지연", _opt_count(summary_data.p50_latency_ms), hint="ms"),
            OpsMetric("p95 지연", _opt_count(summary_data.p95_latency_ms), hint="ms"),
        ),
        columns=(
            "분류",
            "제공사·모델·기능",
            "AI 요청",
            "연결 시도",
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
# 3. 후보 수집과 AI 판정
# --------------------------------------------------------------------------

_AUTOMATION_CYCLE_SQL = """/* ops_dashboard:kasset_automation_cycles */
SELECT id,
       owner_user_id,
       observed_at,
       status,
       skipped_reason,
       candidate_count,
       ranked_count,
       candidate_exclusion_count,
       strategy_evaluated_count,
       strategy_actionable_count,
       ai_reviewed_count,
       ai_failure_count,
       recommendation_count,
       candidate_markets,
       candidate_sources,
       collection_policy,
       ranked_candidates,
       candidate_exclusions,
       ai_review_rejections,
       ai_review_outcomes
FROM review.kasset_automation_cycle_events
WHERE observed_at >= :since
  AND observed_at <= :now
ORDER BY observed_at DESC, id DESC
LIMIT 100
"""

_REVIEW_REASON_LABELS = {
    "accepted": "전략과 AI 합의 (accepted)",
    "action_mismatch": "전략과 AI 방향 불일치 (action_mismatch)",
    "low_confidence": "AI 확신도 50% 미만 (low_confidence)",
    "expired": "판정 유효시간 만료 (expired)",
    "provider_unavailable": "AI 제공사 연결 실패 (provider_unavailable)",
    "ranking_unavailable": "랭킹 근거 없음 (ranking_unavailable)",
}


def _json_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def _count_breakdown(value: object) -> str:
    mapping = _json_mapping(value)
    if not mapping:
        return "없음"
    return ", ".join(f"{key} {_count(count)}" for key, count in sorted(mapping.items()))


def _cycle_state(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "")
    skipped = str(row.get("skipped_reason") or "")
    if status == "failed":
        return f"실패 · {skipped or 'owner_cycle_failed'}"
    if skipped:
        return f"{'건너뜀' if status == 'skipped' else '완료'} · {skipped}"
    return "완료"


def _ranked_symbols(value: object) -> str:
    symbols: list[str] = []
    for item in _json_sequence(value):
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        market = str(item.get("market") or "").strip()
        if symbol:
            symbols.append(f"{symbol}({market})" if market else symbol)
    return ", ".join(symbols) if symbols else "없음"


def _exclusion_summary(value: object, total: object) -> str:
    exclusions: list[str] = []
    for item in _json_sequence(value):
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if symbol and reason:
            exclusions.append(f"{symbol}: {reason}")
    hidden = max(0, int(total or 0) - len(exclusions))
    detail = ", ".join(exclusions) if exclusions else "없음"
    return f"{detail}, … 외 {hidden}건" if hidden else detail


def _policy_summary(value: object) -> str:
    policy = _json_mapping(value)
    if not policy:
        return "수집 설정 미기록"
    actions = "/".join(
        str(item) for item in _json_sequence(policy.get("aiReviewActions"))
    )
    return (
        f"후보 최대 {_count(policy.get('candidateLimit'))}개 · "
        f"랭킹 목표 {_count(policy.get('minimumCandidateTarget'))}개 · "
        f"전략 평가 상위 {_count(policy.get('strategyReviewLimit'))}개 · "
        f"{actions or 'BUY/SELL'}만 AI 검토 · "
        f"추천 최대 {_count(policy.get('recommendationLimit'))}개"
    )


def _pipeline_summary(row: Mapping[str, Any]) -> str:
    return (
        f"후보 {_count(row.get('candidate_count'))} → "
        f"랭킹 {_count(row.get('ranked_count'))} → "
        f"전략 평가 {_count(row.get('strategy_evaluated_count'))} → "
        f"전략 합의 {_count(row.get('strategy_actionable_count'))} → "
        f"AI 검토 {_count(row.get('ai_reviewed_count'))} → "
        f"추천 {_count(row.get('recommendation_count'))}"
    )


async def _collection_panel(ctx: OpsContext) -> OpsPanel:
    cycle_rows = await _rows(
        ctx,
        _AUTOMATION_CYCLE_SQL,
        {"since": ctx.since_recent, "now": ctx.now},
    )
    if not cycle_rows:
        return OpsPanel(
            status=PanelStatus.IDLE,
            summary="최근 24시간에 기록된 추천 수집 cycle이 없습니다.",
            metrics=(
                OpsMetric("수집 후보", "0"),
                OpsMetric("랭킹 통과", "0"),
                OpsMetric("수집 제외", "0"),
                OpsMetric("전략 평가", "0"),
                OpsMetric("전략 합의", "0"),
                OpsMetric("마지막 수집", None, hint="수집 이력 없음"),
            ),
            columns=(
                "수집 시각",
                "결과",
                "시장",
                "수집 경로",
                "단계별 흐름",
                "상위 검토 종목",
                "수집 제외",
            ),
            rows=(),
            note=(
                "추천 producer는 평일 KST 09:10~16:10에 매시 10분 실행됩니다. "
                "AI prompt와 provider 원문 응답은 저장하지 않습니다."
            ),
            window=_window_label(ctx.since_recent, ctx.now),
            source="review.kasset_automation_cycle_events",
        )

    latest = cycle_rows[0]
    failures = sum(1 for row in cycle_rows if row.get("status") == "failed")
    provider_failures = sum(int(row.get("ai_failure_count") or 0) for row in cycle_rows)
    status = PanelStatus.WARN if failures or provider_failures else PanelStatus.OK
    collection_rows = tuple(
        OpsRow(
            (
                _ts(row.get("observed_at")),
                _cycle_state(row),
                _count_breakdown(row.get("candidate_markets")),
                _count_breakdown(row.get("candidate_sources")),
                _pipeline_summary(row),
                _ranked_symbols(row.get("ranked_candidates")),
                _exclusion_summary(
                    row.get("candidate_exclusions"),
                    row.get("candidate_exclusion_count"),
                ),
            )
        )
        for row in cycle_rows[:12]
    )
    return OpsPanel(
        status=status,
        summary=(
            f"추천 수집 cycle {len(cycle_rows)}회 · "
            f"{_policy_summary(latest.get('collection_policy'))}"
        ),
        metrics=(
            OpsMetric(
                "수집 후보",
                _count(sum(int(row.get("candidate_count") or 0) for row in cycle_rows)),
                hint="최근 24시간 cycle 합계",
            ),
            OpsMetric(
                "랭킹 통과",
                _count(sum(int(row.get("ranked_count") or 0) for row in cycle_rows)),
            ),
            OpsMetric(
                "수집 제외",
                _count(
                    sum(
                        int(row.get("candidate_exclusion_count") or 0)
                        for row in cycle_rows
                    )
                ),
            ),
            OpsMetric(
                "전략 평가",
                _count(
                    sum(
                        int(row.get("strategy_evaluated_count") or 0)
                        for row in cycle_rows
                    )
                ),
            ),
            OpsMetric(
                "전략 합의",
                _count(
                    sum(
                        int(row.get("strategy_actionable_count") or 0)
                        for row in cycle_rows
                    )
                ),
                hint="BUY/SELL로 합의해 AI에 보낸 후보",
            ),
            OpsMetric("마지막 수집", _ts(latest.get("observed_at"))),
        ),
        columns=(
            "수집 시각",
            "결과",
            "시장",
            "수집 경로",
            "단계별 흐름",
            "상위 검토 종목",
            "수집 제외",
        ),
        rows=collection_rows,
        note=(
            "평일 KST 09:10~16:10, 매시 10분 실행입니다. 표는 최근 12회만 "
            "보여주며 집계는 최근 24시간 최대 100회입니다."
        ),
        window=_window_label(ctx.since_recent, ctx.now),
        source="review.kasset_automation_cycle_events",
    )


def _action_label(value: object) -> str | None:
    if value is None:
        return None
    action = str(value).strip().upper()
    return {
        "BUY": "매수 (BUY)",
        "SELL": "매도 (SELL)",
        "HOLD": "보류 (HOLD)",
        "IGNORE": "제외 (IGNORE)",
        "REVIEW": "재검토 (REVIEW)",
    }.get(action, action)


def _confidence_percent(value: object) -> str | None:
    if value is None:
        return None
    try:
        return f"{Decimal(str(value)) * Decimal('100'):.1f}%"
    except (InvalidOperation, ValueError):
        return None


def _review_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return _ts(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return value[:40]


def _ai_route(outcome: Mapping[str, Any]) -> str | None:
    parts = [
        str(outcome.get(key)).strip()
        for key in ("provider", "tier", "modelId")
        if outcome.get(key)
    ]
    return " · ".join(parts) if parts else None


async def _ai_reviews_panel(ctx: OpsContext) -> OpsPanel:
    cycle_rows = await _rows(
        ctx,
        _AUTOMATION_CYCLE_SQL,
        {"since": ctx.since_recent, "now": ctx.now},
    )
    outcomes: list[Mapping[str, Any]] = []
    recommendation_count = 0
    for cycle in cycle_rows:
        recommendation_count += int(cycle.get("recommendation_count") or 0)
        outcomes.extend(
            item
            for item in _json_sequence(cycle.get("ai_review_outcomes"))
            if isinstance(item, Mapping)
        )
    rejected = [outcome for outcome in outcomes if outcome.get("reason") != "accepted"]
    provider_failures = sum(
        1 for outcome in outcomes if outcome.get("reason") == "provider_unavailable"
    )
    if provider_failures:
        status = PanelStatus.WARN
    elif outcomes:
        status = PanelStatus.OK
    else:
        status = PanelStatus.IDLE

    review_rows = tuple(
        OpsRow(
            (
                str(outcome.get("symbol") or ""),
                _market(outcome.get("market")),
                _action_label(outcome.get("strategyAction")),
                _action_label(outcome.get("aiAction")),
                _confidence_percent(outcome.get("confidence")),
                _REVIEW_REASON_LABELS.get(
                    str(outcome.get("reason")),
                    str(outcome.get("reason") or "판정 사유 없음"),
                ),
                _ai_route(outcome),
                ", ".join(
                    str(tag) for tag in _json_sequence(outcome.get("rationaleTags"))
                )
                or None,
                _review_time(outcome.get("observedAt")),
            )
        )
        for outcome in outcomes[:100]
    )
    return OpsPanel(
        status=status,
        summary=(
            f"AI 후보 검토 {len(outcomes)}건 · 합의 "
            f"{len(outcomes) - len(rejected)}건 · 거절 {len(rejected)}건"
            if outcomes
            else "최근 24시간 AI 검토 대상이 된 BUY/SELL 전략 후보가 없습니다."
        ),
        metrics=(
            OpsMetric("AI 검토", _count(len(outcomes))),
            OpsMetric("추천 생성", _count(recommendation_count)),
            OpsMetric("AI 거절", _count(len(rejected))),
        ),
        columns=(
            "종목",
            "시장",
            "전략 판정",
            "AI 판정",
            "확신도",
            "사유",
            "AI 경로",
            "AI 근거",
            "판정 시각",
        ),
        rows=review_rows,
        note=(
            "표는 최신 AI 판정 100건까지만 보여줍니다. "
            "AI가 반환한 구조화 action·확신도·짧은 rationale tag만 표시합니다. "
            "prompt, 원문 응답, API key와 토큰은 저장하거나 표시하지 않습니다."
        ),
        window=_window_label(ctx.since_recent, ctx.now),
        source="review.kasset_automation_cycle_events.ai_review_outcomes",
    )


# --------------------------------------------------------------------------
# 3. 자동매매 funnel
# --------------------------------------------------------------------------

# The check constraint fixes the decision domain to these three values.
# Splitting them into equality-bound UNION ALL branches lets the existing
# (owner_user_id, decision, created_at DESC, id) index constrain all
# leading columns, including the 24-hour created_at bound. With production's
# three owners this is at most 3 decisions × 3 owners = 9 bounded index probes;
# the outer aggregate sees only rows from the requested window.
_RECOMMENDATION_FUNNEL_SQL = """/* ops_dashboard:recommendations */
SELECT decision,
       count(*) AS attempt_count,
       max(created_at) AS last_created_at,
       count(*) FILTER (WHERE decided_at IS NOT NULL) AS decided_count
FROM (
    SELECT decision, created_at, decided_at
    FROM review.ai_recommendations
    WHERE owner_user_id IN (SELECT id FROM users)
      AND decision = 'PENDING'
      AND created_at >= :since
    UNION ALL
    SELECT decision, created_at, decided_at
    FROM review.ai_recommendations
    WHERE owner_user_id IN (SELECT id FROM users)
      AND decision = 'APPROVED'
      AND created_at >= :since
    UNION ALL
    SELECT decision, created_at, decided_at
    FROM review.ai_recommendations
    WHERE owner_user_id IN (SELECT id FROM users)
      AND decision = 'REJECTED'
      AND created_at >= :since
) AS recent
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
                "AI 추천",
                _state(row["decision"]),
                _count(row["attempt_count"]),
                _count(row["decided_count"]),
                _ts(row["last_created_at"]),
            )
        )
        for row in recommendations
    ) + tuple(
        OpsRow(
            (
                "모의투자 주문",
                _state(row["status"]),
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
            OpsMetric("모의투자 주문", _count(order_total)),
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
                "확인만 함" if row["dry_run"] else "실제 반영",
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
        summary = f"증권사 {len(runs)}곳 대사 정상 · 반영 {committed:,}건"

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("대사한 증권사", _count(len(runs))),
            OpsMetric("오류/미완료", _count(len(failing))),
            OpsMetric("반영 행", _count(committed)),
        ),
        columns=("증권사", "시작", "완료", "처리 방식", "반영", "오류"),
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
                _market(market["market"]),
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
            "오래된 봉",
            "결측 거래일",
            "중복",
            "이상치",
            "기준 종목",
            "차단 사유",
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
                "뉴스 수집",
                _market(row["market"]),
                _state(row["status"]),
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
                _market(row["market"]),
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
        summary = "최근 7일 수집 실행 0건 · 최근 24시간 기사 0건 (조회 성공)"
    elif failing:
        status = PanelStatus.WARN
        summary = f"확인 필요: 실패/부분 성공 수집 {len(failing)}건"
    else:
        status = PanelStatus.OK
        summary = (
            f"수집 실행 {len(runs)}건 · 최근 24시간 기사 {article_total:,}건 "
            f"(요약 {summarized_total:,} / 분석 {analyzed_total:,})"
        )

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("수집 실행", _count(len(runs))),
            OpsMetric("기사", _count(article_total)),
            OpsMetric(
                "최근 수집",
                _ts(latest_run_finished),
                hint=_age(latest_run_finished, ctx.now) or "완료된 수집 없음",
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
            f"수집 {_window_label(ctx.since_runs, ctx.now)} · "
            "기사 최근 24시간(발행시각 기준, 수집기별 시간대 혼재로 최대 +9시간 과대집계)"
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

_PROMOTION_BYPASS_SQL = """/* ops_dashboard:promotion_bypass */
SELECT count(*) AS enabled_owner_count
FROM kasset_android_runtime_state
WHERE promotion_bypass_enabled = true
"""

_APPROVED_STATES = frozenset({"PAPER_APPROVED"})


async def _strategy_panel(ctx: OpsContext) -> OpsPanel:
    states = await _rows(ctx, _PROMOTION_STATE_SQL)
    candidates = await _rows(
        ctx, _PROMOTION_CANDIDATE_SQL, {"since": _naive_utc(ctx.since_ledger)}
    )
    bypass_rows = await _rows(ctx, _PROMOTION_BYPASS_SQL)

    promotion_total = sum(int(row["promotion_count"]) for row in states)
    approved_total = sum(
        int(row["promotion_count"])
        for row in states
        if str(row["state"]) in _APPROVED_STATES
    )
    candidate_total = sum(int(row["candidate_count"]) for row in candidates)
    bypass_owner_total = int(bypass_rows[0]["enabled_owner_count"])

    rows = tuple(
        OpsRow(
            (
                "승격",
                _state(row["state"]),
                _count(row["promotion_count"]),
                _ts(row["last_updated_at"]),
            )
        )
        for row in states
    ) + tuple(
        OpsRow(
            (
                "후보",
                _state(row["status"]),
                _count(row["candidate_count"]),
                _ts(row["last_evaluated_at"]),
            )
        )
        for row in candidates
    )

    if bypass_owner_total:
        status = PanelStatus.WARN
        if approved_total == 0:
            summary = (
                f"승격 기록 {promotion_total:,}건 · 모의투자 승인 0건이지만 "
                f"승격 우회 켜짐 {bypass_owner_total:,}명 — 해당 사용자가 모의투자 모드이고 "
                "긴급 중지가 꺼져 있으면 승격 근거 없이 자동 모의주문 가능"
            )
        else:
            summary = (
                f"모의투자 승인 {approved_total:,}건 · 전체 승격 "
                f"{promotion_total:,}건 · 승격 우회 켜짐 {bypass_owner_total:,}명 — "
                "해당 사용자는 조건 충족 시 승격 근거 검사를 건너뜀"
            )
    elif promotion_total == 0:
        status = PanelStatus.IDLE
        summary = (
            "승격 기록 0건 (조회 성공) — 모의투자 승인 전략이 없고 "
            "승격 우회가 켜진 사용자도 없어 자동 모의주문 불가"
        )
    elif approved_total == 0:
        status = PanelStatus.WARN
        summary = (
            f"승격 {promotion_total:,}건 중 모의투자 승인 0건이고 승격 우회 꺼짐 — "
            "자동 모의주문 불가"
        )
    else:
        status = PanelStatus.OK
        summary = (
            f"모의투자 승인 {approved_total:,}건 · 전체 승격 {promotion_total:,}건 · "
            "승격 우회 꺼짐"
        )

    return OpsPanel(
        status=status,
        summary=summary,
        metrics=(
            OpsMetric("모의투자 승인", _count(approved_total), hint="PAPER_APPROVED"),
            OpsMetric("승격 기록", _count(promotion_total)),
            OpsMetric("승격 우회 사용자", _count(bypass_owner_total)),
            OpsMetric("최근 30일 후보", _count(candidate_total)),
        ),
        columns=("구분", "상태", "건수", "최근 시각"),
        rows=rows,
        window="승격 전량 · 후보 최근 30일",
        source=(
            "review.kasset_strategy_promotions · research.promotion_candidates · "
            "kasset_android_runtime_state"
        ),
    )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

PanelBuilder = Callable[[OpsContext], Awaitable[OpsPanel]]

PANEL_BUILDERS: tuple[tuple[str, str, PanelBuilder], ...] = (
    ("system", "운영 상태", _system_panel),
    ("ai_usage", "AI 연결과 사용량", _ai_usage_panel),
    ("collection", "후보 수집과 전략 판정", _collection_panel),
    ("ai_reviews", "AI 후보 판정 상세", _ai_reviews_panel),
    ("funnel", "자동매매 진행 흐름", _funnel_panel),
    ("paper_portfolio", "모의투자 포트폴리오", _paper_portfolio_panel),
    ("reconcile", "체결 내역 대조", _reconcile_panel),
    ("data_readiness", "국내/미국 데이터 준비", _readiness_panel),
    ("news", "뉴스 수집과 AI 분석", _news_panel),
    ("strategy", "전략 승인", _strategy_panel),
)


async def build_ops_dashboard(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> OpsDashboard:
    """Measure every panel sequentially, isolating each one's failures.

    Panels share a single ``AsyncSession`` and therefore a single transaction;
    a failed statement poisons it, so a failing panel is rolled back before the
    next one runs. That is what keeps the other panels renderable.
    """
    moment = now or datetime.now(UTC)
    ctx = OpsContext(db=db, now=moment)

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
