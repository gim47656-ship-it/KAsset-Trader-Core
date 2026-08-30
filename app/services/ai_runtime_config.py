"""AI route 정책 singleton의 읽기/쓰기와 secret-free 화면 표현.

한 cycle/batch는 ``get_ai_runtime_snapshot``을 **한 번만** 호출해 불변 snapshot을
공유한다. 호출마다 DB를 읽지 않으므로 실행 중 정책이 바뀌어도 provider가 섞이지
않고, 다음 invocation부터 재시작 없이 새 정책이 적용된다.

fail-closed 규칙: 저장된 정책이 손상되었거나 조회 자체가 실패하면 모든 lane을
비활성으로 만든다. 환경변수 기본값으로 조용히 되돌아가지 않는다. 기본값은
singleton 행이 **아예 없을 때만** 읽기 전용으로 쓰인다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.ai.runtime_config import (
    LANE_LABELS,
    LANE_ROUTE_IDS,
    REASON_LABELS,
    AiLane,
    AiRouteCatalogEntry,
    AiRouteId,
    AiRoutePolicyError,
    AiRuntimeSnapshot,
    build_ai_route_catalog,
    default_snapshot,
    fail_closed_snapshot,
    freeze_route_policy,
    lane_telemetry_covered,
    normalize_route_policy,
    serialize_route_policy,
)
from app.models.ai_call_events import AiCallEvent
from app.models.ai_runtime_config import AI_RUNTIME_CONFIG_ID, AiRuntimeConfig

logger = logging.getLogger(__name__)


class AiRoutePolicyRevisionConflict(Exception):
    """다른 관리자가 먼저 저장했다. 현재 revision만 알려준다."""

    __slots__ = ("current_revision",)

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"stale revision; current={current_revision}")
        self.current_revision = current_revision


async def get_ai_runtime_snapshot(db: AsyncSession) -> AiRuntimeSnapshot:
    """실행 경로가 쓰는 불변 정책 snapshot을 한 번 읽는다."""

    try:
        # session이 재사용되어도 이전에 읽어둔 값이 아니라 커밋된 현재 정책을
        # 본다. "다음 cycle/batch부터 재시작 없이 반영"이 성립하는 지점이다.
        row = (
            await db.execute(
                select(AiRuntimeConfig)
                .where(AiRuntimeConfig.id == AI_RUNTIME_CONFIG_ID)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        # 실패한 트랜잭션을 정리해 호출자의 session을 다시 쓸 수 있게 한다.
        logger.error(
            "AI route 정책을 조회할 수 없어 모든 lane을 차단한다: error_type=%s",
            type(exc).__name__,
        )
        try:
            await db.rollback()
        except SQLAlchemyError:
            logger.error("AI route 정책 조회 실패 후 rollback도 실패했다")
        return fail_closed_snapshot("unavailable")

    if row is None:
        # 행이 없으면 마이그레이션 이전과 동일한 env 동등 기본값. 쓰지 않는다.
        return default_snapshot()

    try:
        lanes = normalize_route_policy(row.route_policy)
    except AiRoutePolicyError as exc:
        logger.error(
            "저장된 AI route 정책이 유효하지 않아 모든 lane을 차단한다: "
            "revision=%s code=%s",
            row.revision,
            exc.code,
        )
        return fail_closed_snapshot(
            "invalid",
            revision=row.revision,
            updated_at=row.updated_at,
            updated_by_user_id=row.updated_by_user_id,
        )

    return AiRuntimeSnapshot(
        revision=row.revision,
        updated_at=row.updated_at,
        updated_by_user_id=row.updated_by_user_id,
        source="persisted",
        lanes=freeze_route_policy(lanes),
    )


async def _latest_success_at(
    db: AsyncSession,
    catalog: Mapping[AiRouteId, AiRouteCatalogEntry],
) -> dict[tuple[str, str], datetime]:
    """원장에 남은 route별 최근 성공 시각. 계측이며 게이트가 아니다."""

    pairs = sorted(
        {
            (entry.ledger_provider, entry.ledger_model)
            for entry in catalog.values()
            if entry.ledger_provider and entry.ledger_model
        }
    )
    if not pairs:
        return {}
    try:
        rows = (
            await db.execute(
                select(
                    AiCallEvent.provider,
                    AiCallEvent.model_name,
                    func.max(AiCallEvent.finished_at),
                )
                .where(
                    AiCallEvent.status == "success",
                    tuple_(AiCallEvent.provider, AiCallEvent.model_name).in_(pairs),
                )
                .group_by(AiCallEvent.provider, AiCallEvent.model_name)
            )
        ).all()
    except SQLAlchemyError as exc:
        logger.warning(
            "AI 호출 원장 최근 성공 조회 실패: error_type=%s",
            type(exc).__name__,
        )
        return {}
    return {
        (provider, model_name): finished_at
        for provider, model_name, finished_at in rows
        if finished_at is not None
    }


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _route_view(
    lane: AiLane,
    route_id: AiRouteId,
    *,
    entry: AiRouteCatalogEntry,
    active: tuple[AiRouteId, ...],
    latest_success: Mapping[tuple[str, str], datetime],
) -> dict[str, object]:
    telemetry_covered = lane_telemetry_covered(lane)
    success_at: datetime | None = None
    if telemetry_covered and entry.ledger_provider and entry.ledger_model:
        success_at = latest_success.get((entry.ledger_provider, entry.ledger_model))
    is_active = route_id in active
    return {
        "routeId": route_id.value,
        "label": entry.label,
        "provider": entry.provider,
        "model": entry.model,
        "configured": entry.configured,
        "available": entry.available,
        "active": is_active,
        "fallbackOrder": active.index(route_id) + 1 if is_active else None,
        "latestSuccessAt": _isoformat(success_at),
        "telemetryCovered": telemetry_covered,
        "unavailableReason": entry.unavailable_reason,
        "unavailableReasonLabel": (
            REASON_LABELS.get(entry.unavailable_reason)
            if entry.unavailable_reason
            else None
        ),
    }


async def build_ai_routes_view(db: AsyncSession) -> dict[str, object]:
    """관리자 화면/JSON용 정책 표현.

    lane마다 **허용된 모든 route**를 반환한다. 현재 사용 불가한 route도 비활성
    상태로 포함해 화면에서 이유를 보여줄 수 있게 한다. 어떤 필드에도 credential,
    base URL, subscription 명령 원문은 들어가지 않는다.
    """

    snapshot = await get_ai_runtime_snapshot(db)
    catalog = build_ai_route_catalog()
    latest_success = await _latest_success_at(db, catalog)

    lanes: list[dict[str, object]] = []
    for lane in AiLane:
        active = snapshot.routes(lane)
        ordered = [*active, *(r for r in LANE_ROUTE_IDS[lane] if r not in active)]
        lanes.append(
            {
                "lane": lane.value,
                "label": LANE_LABELS[lane],
                "telemetryCovered": lane_telemetry_covered(lane),
                # 기존 호환 provider factory는 운영 caller가 없다. API에는 정책을
                # 보존하되 운영자 화면에서 효과가 있는 제어처럼 노출하지 않는다.
                "operatorControllable": lane is not AiLane.COMPAT_SKILL,
                "routes": [
                    _route_view(
                        lane,
                        route_id,
                        entry=catalog[route_id],
                        active=active,
                        latest_success=latest_success,
                    )
                    for route_id in ordered
                ],
            }
        )

    return {
        "revision": snapshot.revision,
        "updatedAt": _isoformat(snapshot.updated_at),
        "updatedByUserId": snapshot.updated_by_user_id,
        "source": snapshot.source,
        "lanes": lanes,
    }


async def _current_revision(db: AsyncSession) -> int:
    revision = await db.scalar(
        select(AiRuntimeConfig.revision).where(
            AiRuntimeConfig.id == AI_RUNTIME_CONFIG_ID
        )
    )
    return int(revision) if revision is not None else 0


async def apply_ai_routes_update(
    db: AsyncSession,
    *,
    expected_revision: int,
    lanes: Mapping[str, Sequence[str]],
    admin_user_id: int | None,
) -> dict[str, object]:
    """정책 전체를 검증하고 낙관적 잠금 아래에서 한 번에 교체한다.

    ``AiRoutePolicyError``는 422, ``AiRoutePolicyRevisionConflict``는 409로
    매핑된다. 검증은 잠금 전에 끝내 잘못된 payload가 행을 잡고 있지 않게 한다.
    """

    catalog = build_ai_route_catalog()
    normalized = normalize_route_policy(lanes, catalog=catalog)
    serialized = serialize_route_policy(normalized)

    # ``populate_existing``이 없으면 같은 session이 이미 이 행을 읽어둔 경우
    # identity map의 낡은 ``revision``이 그대로 남아 낙관적 잠금 검사가
    # 무의미해진다. 잠근 행의 현재 값으로 반드시 덮어쓴다.
    row = (
        await db.execute(
            select(AiRuntimeConfig)
            .where(AiRuntimeConfig.id == AI_RUNTIME_CONFIG_ID)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if row is None:
        # 행이 없는 상태의 revision은 0(기본 정책)이다.
        if expected_revision != 0:
            await db.rollback()
            raise AiRoutePolicyRevisionConflict(0)
        db.add(
            AiRuntimeConfig(
                id=AI_RUNTIME_CONFIG_ID,
                revision=1,
                route_policy=serialized,
                updated_by_user_id=admin_user_id,
                updated_at=datetime.now(UTC),
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # 동시에 다른 관리자가 첫 행을 만들었다.
            await db.rollback()
            raise AiRoutePolicyRevisionConflict(await _current_revision(db)) from None
        new_revision = 1
    else:
        if row.revision != expected_revision:
            current = int(row.revision)
            await db.rollback()
            raise AiRoutePolicyRevisionConflict(current)
        new_revision = int(row.revision) + 1
        row.revision = new_revision
        row.route_policy = serialized
        row.updated_by_user_id = admin_user_id
        # ``expire_on_commit=False``이므로 서버 기본값에 의존하지 않고 값을 적는다.
        row.updated_at = datetime.now(UTC)
        await db.commit()

    logger.info(
        "AI route 정책 갱신: revision=%s admin_user_id=%s lanes=%s",
        new_revision,
        admin_user_id,
        {lane: len(routes) for lane, routes in serialized.items()},
    )
    return await build_ai_routes_view(db)


__all__ = [
    "AiRoutePolicyRevisionConflict",
    "apply_ai_routes_update",
    "build_ai_routes_view",
    "get_ai_runtime_snapshot",
]
