"""KAsset AI route 정책의 서버 allowlist catalog와 immutable snapshot.

이 모듈은 데이터베이스를 알지 못한다. ``settings``에서 provider slot이 채워져
있는지만 읽고, 저장·직렬화 가능한 값은 **고정된 route ID**뿐이다.

의도적으로 절대 노출/저장하지 않는 것:

* API key, MCP token 등 credential 값
* direct/OpenRouter/MCP base URL
* ``KASSET_AI_SUBSCRIPTION_CMD`` 명령 원문(argv[0] 경로 포함)
* 운영자가 입력한 임의 model 문자열

``model`` 필드는 **현재 서버 설정에서 resolve된 표시용 문자열**이다. 운영자가
model을 입력하는 경로는 존재하지 않으며, 정책에는 route ID만 남는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from app.core.config import settings


class AiLane(StrEnum):
    """정책을 독립적으로 적용하는 실행 경로."""

    SUMMARY_LUNA = "summary_luna"
    REVIEW_LUNA = "review_luna"
    REVIEW_TERRA = "review_terra"
    REVIEW_SOL = "review_sol"
    COMPAT_SKILL = "compat_skill"


class AiRouteId(StrEnum):
    """저장 가능한 고정 route 식별자. 절대 model 문자열이 아니다."""

    DIRECT_LUNA = "direct_luna"
    DIRECT_TERRA = "direct_terra"
    DIRECT_SOL = "direct_sol"
    OPENROUTER_FLASH = "openrouter_flash"
    OPENROUTER_PRO = "openrouter_pro"
    MCP_TOOL = "mcp_tool"
    SUBSCRIPTION_CLI = "subscription_cli"


type AiProviderName = Literal["mcp", "direct-api", "openrouter", "subscription"]

type AiRoutePolicy = Mapping[AiLane, tuple[AiRouteId, ...]]

#: ``build_ai_routes_view``가 노출하는 정책 출처.
#:
#: * ``persisted`` — 저장된 singleton을 검증해 사용
#: * ``default`` — singleton이 아직 없어 env 동등 기본값을 읽기 전용으로 사용
#: * ``invalid`` — 저장된 정책이 손상되어 모든 lane을 차단(fail closed)
#: * ``unavailable`` — 정책 조회 자체가 실패해 모든 lane을 차단(fail closed)
type AiPolicySource = Literal["persisted", "default", "invalid", "unavailable"]


_ROUTE_PROVIDERS: Final[Mapping[AiRouteId, AiProviderName]] = MappingProxyType(
    {
        AiRouteId.DIRECT_LUNA: "direct-api",
        AiRouteId.DIRECT_TERRA: "direct-api",
        AiRouteId.DIRECT_SOL: "direct-api",
        AiRouteId.OPENROUTER_FLASH: "openrouter",
        AiRouteId.OPENROUTER_PRO: "openrouter",
        AiRouteId.MCP_TOOL: "mcp",
        AiRouteId.SUBSCRIPTION_CLI: "subscription",
    }
)


#: lane별 허용 route allowlist. 여기 없는 조합은 읽기/쓰기 모두에서 거부한다.
LANE_ROUTE_IDS: Final[AiRoutePolicy] = MappingProxyType(
    {
        AiLane.SUMMARY_LUNA: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_LUNA,
            AiRouteId.OPENROUTER_FLASH,
        ),
        AiLane.REVIEW_LUNA: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_LUNA,
            AiRouteId.OPENROUTER_FLASH,
        ),
        AiLane.REVIEW_TERRA: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
        AiLane.REVIEW_SOL: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_SOL,
            AiRouteId.OPENROUTER_PRO,
        ),
        AiLane.COMPAT_SKILL: (
            AiRouteId.SUBSCRIPTION_CLI,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
    }
)


#: 환경변수만 있던 시절의 순서를 그대로 재현하는 기본 정책.
#:
#: 마이그레이션이 삽입하는 값이자, singleton이 없을 때 읽기 전용으로 쓰는 값이다.
#: 오늘은 lane allowlist와 순서가 일치하지만(그 시절 코드가 설정된 route를
#: catalog 순서대로 모두 시도했으므로) 의미가 다르므로 따로 적는다.
DEFAULT_ROUTE_POLICY: Final[AiRoutePolicy] = MappingProxyType(
    {
        AiLane.SUMMARY_LUNA: (
            AiRouteId.DIRECT_LUNA,
            AiRouteId.OPENROUTER_FLASH,
        ),
        AiLane.REVIEW_LUNA: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_LUNA,
            AiRouteId.OPENROUTER_FLASH,
        ),
        AiLane.REVIEW_TERRA: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
        AiLane.REVIEW_SOL: (
            AiRouteId.MCP_TOOL,
            AiRouteId.DIRECT_SOL,
            AiRouteId.OPENROUTER_PRO,
        ),
        AiLane.COMPAT_SKILL: (
            AiRouteId.SUBSCRIPTION_CLI,
            AiRouteId.DIRECT_TERRA,
            AiRouteId.OPENROUTER_PRO,
        ),
    }
)


#: 초보 운영자용 lane 표시명. ID는 그대로 두고 화면 문구만 한국어로 준다.
LANE_LABELS: Final[Mapping[AiLane, str]] = MappingProxyType(
    {
        AiLane.SUMMARY_LUNA: "뉴스·공시 요약",
        AiLane.REVIEW_LUNA: "1차 검토 (빠른 판단)",
        AiLane.REVIEW_TERRA: "2차 검토 (표준 판단)",
        AiLane.REVIEW_SOL: "최종 검토 (정밀 판단)",
        AiLane.COMPAT_SKILL: "호환 Skill 실행",
    }
)

#: 초보 운영자용 route 표시명. provider 종류와 등급만 알려주고 URL/키는 없다.
ROUTE_LABELS: Final[Mapping[AiRouteId, str]] = MappingProxyType(
    {
        AiRouteId.DIRECT_LUNA: "직접 API · 경량 모델",
        AiRouteId.DIRECT_TERRA: "직접 API · 표준 모델",
        AiRouteId.DIRECT_SOL: "직접 API · 정밀 모델",
        AiRouteId.OPENROUTER_FLASH: "OpenRouter · 빠른 모델",
        AiRouteId.OPENROUTER_PRO: "OpenRouter · 고급 모델",
        AiRouteId.MCP_TOOL: "MCP 도구 서버",
        AiRouteId.SUBSCRIPTION_CLI: "구독 CLI 브리지",
    }
)


#: 사용 불가 사유 코드. 설정 값을 되돌려주지 않고 "무엇이 비었는지"만 말한다.
REASON_MISSING_MODEL: Final = "missing_model"
REASON_MISSING_API_KEY: Final = "missing_api_key"
REASON_MISSING_MCP_URL: Final = "missing_mcp_url"
REASON_MISSING_SUBSCRIPTION_CMD: Final = "missing_subscription_command"

REASON_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        REASON_MISSING_MODEL: "서버에 모델 이름이 설정되어 있지 않습니다.",
        REASON_MISSING_API_KEY: "서버에 API 키가 설정되어 있지 않습니다.",
        REASON_MISSING_MCP_URL: "서버에 MCP 주소가 설정되어 있지 않습니다.",
        REASON_MISSING_SUBSCRIPTION_CMD: "서버에 구독 CLI 실행 설정이 없습니다.",
    }
)


#: 검토 tier(``luna``/``terra``/``sol``)가 정책을 읽어오는 lane.
#: 추천 cycle이 실제로 호출하는 경로이므로 cycle 게이트의 기준이 된다.
AI_REVIEW_LANES: Final[tuple[AiLane, ...]] = (
    AiLane.REVIEW_LUNA,
    AiLane.REVIEW_TERRA,
    AiLane.REVIEW_SOL,
)

#: 앱 AI 기능이 실제로 의존하는 lane 전체(요약 + 검토).
#:
#: ``compat_skill``은 운영 caller가 없으므로 유효 가용성 판정에서 제외한다.
#: 정책에는 그대로 남지만 앱이 보는 AI 상태를 좌우하지 않는다.
AI_APP_LANES: Final[tuple[AiLane, ...]] = (AiLane.SUMMARY_LUNA, *AI_REVIEW_LANES)


#: 유효 AI 가용성의 사용 불가 사유 코드. route 단위 사유와 층이 다르다.
#:
#: * ``policy_unreadable`` — 정책이 손상되었거나 조회 자체가 실패(fail closed)
#: * ``no_active_route`` — 앱 lane에 활성 route가 하나도 없음(명시적 비활성화)
#: * ``routes_unavailable`` — 활성 route는 있으나 현재 설정으로 전부 호출 불가
REASON_POLICY_UNREADABLE: Final = "policy_unreadable"
REASON_NO_ACTIVE_ROUTE: Final = "no_active_route"
REASON_ROUTES_UNAVAILABLE: Final = "routes_unavailable"

#: 정책을 신뢰할 수 없는 snapshot 출처. 이 상태에서는 무조건 사용 불가다.
_UNREADABLE_POLICY_SOURCES: Final[frozenset[str]] = frozenset(
    {"invalid", "unavailable"}
)


#: ``review.ai_call_events`` 원장에 attempt 행이 남는 lane.
#:
#: 요약/검토 lane은 ``AvailabilityRoutedJsonClient``를 통과하므로 attempt slot이
#: 생긴다. ``compat_skill``은 ``OpenAiCompatibleProvider``/``ChainedApiProvider``/
#: subscription CLI를 직접 호출해 원장을 우회한다. 그 경로의 latest success는
#: "실패"가 아니라 "측정 불가"이므로 항상 ``None``으로 보고한다.
_LEDGER_LANES: Final[frozenset[AiLane]] = frozenset(
    {
        AiLane.SUMMARY_LUNA,
        AiLane.REVIEW_LUNA,
        AiLane.REVIEW_TERRA,
        AiLane.REVIEW_SOL,
    }
)

#: MCP 표시 model의 접두사. 원장이 기록하는 ``tool:<tool_name>``과 동일해야 한다.
MCP_MODEL_PREFIX: Final = "tool:"

#: subscription CLI는 model 개념이 없다. 명령 원문 대신 고정 문자열만 노출한다.
SUBSCRIPTION_MODEL_LABEL: Final = "subscription-agent"


class AiRoutePolicyError(ValueError):
    """정책 payload가 allowlist 계약을 위반했다."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AiRouteCatalogEntry:
    """한 route의 secret-free 설명."""

    route_id: AiRouteId
    provider: AiProviderName
    label: str
    model: str
    configured: bool
    available: bool
    unavailable_reason: str | None
    #: ``review.ai_call_events``에서 이 route를 찾는 (provider, model_name) 쌍.
    #: 원장을 우회하는 route는 ``None``.
    ledger_provider: str | None
    ledger_model: str | None


@dataclass(frozen=True, slots=True)
class AiRuntimeSnapshot:
    """한 cycle/batch가 공유하는 불변 정책 스냅샷."""

    revision: int
    updated_at: datetime | None
    updated_by_user_id: int | None
    source: AiPolicySource
    lanes: AiRoutePolicy

    def routes(self, lane: AiLane | str) -> tuple[AiRouteId, ...]:
        """lane의 순서화된 route ID. 비활성 lane은 빈 tuple(호출 차단)."""

        return self.lanes.get(AiLane(lane), ())


@dataclass(frozen=True, slots=True)
class AiAvailability:
    """앱과 시스템 상태가 쓰는 **유효** AI 가용성.

    "선택 사항인 MCP relay가 붙었는지"가 아니라 "지금 정책에 활성인 route로 실제
    호출이 가능한지"를 말한다. direct API나 OpenRouter 하나만 설정되어 있어도
    사용 가능이며, 쓸 수 있는 route가 하나도 없으면 fail closed로 사용 불가다.

    credential, base URL, 명령 원문은 어떤 필드에도 들어가지 않는다.
    """

    source: AiPolicySource
    configured: bool
    available: bool
    unavailable_reason: str | None
    #: 사용자에게 그대로 보여줄 한국어 설명.
    message: str
    usable_lanes: frozenset[AiLane]

    def lane_usable(self, lane: AiLane) -> bool:
        """해당 lane에 지금 호출 가능한 route가 있는지."""

        return lane in self.usable_lanes

    def any_lane_usable(self, lanes: Iterable[AiLane]) -> bool:
        """주어진 lane 중 하나라도 호출 가능한지."""

        return any(lane in self.usable_lanes for lane in lanes)


def ai_route_provider(route_id: AiRouteId) -> AiProviderName:
    """route ID의 transport 종류."""

    return _ROUTE_PROVIDERS[route_id]


def lane_telemetry_covered(lane: AiLane | str) -> bool:
    """lane의 호출이 AI 호출 원장에 남는지."""

    return AiLane(lane) in _LEDGER_LANES


def freeze_route_policy(
    lanes: Mapping[AiLane, Sequence[AiRouteId]],
) -> AiRoutePolicy:
    """수정 불가능한 정책 mapping을 만든다."""

    return MappingProxyType(
        {lane: tuple(routes) for lane, routes in lanes.items()},
    )


def default_snapshot() -> AiRuntimeSnapshot:
    """singleton이 없을 때 쓰는 읽기 전용 env 동등 기본 정책."""

    return AiRuntimeSnapshot(
        revision=0,
        updated_at=None,
        updated_by_user_id=None,
        source="default",
        lanes=DEFAULT_ROUTE_POLICY,
    )


def fail_closed_snapshot(
    source: AiPolicySource,
    *,
    revision: int = 0,
    updated_at: datetime | None = None,
    updated_by_user_id: int | None = None,
) -> AiRuntimeSnapshot:
    """모든 lane을 차단한다. env 기본값으로 몰래 되돌아가지 않는다."""

    return AiRuntimeSnapshot(
        revision=revision,
        updated_at=updated_at,
        updated_by_user_id=updated_by_user_id,
        source=source,
        lanes=freeze_route_policy(dict.fromkeys(AiLane, ())),
    )


def _secret_present(value: object) -> bool:
    """SecretStr slot이 비어 있지 않은지. 값 자체는 반환하지 않는다."""

    if value is None:
        return False
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    return bool(str(raw).strip())


def _api_entry(
    route_id: AiRouteId,
    *,
    provider: AiProviderName,
    ledger_provider: str,
    model: str,
    key_present: bool,
) -> AiRouteCatalogEntry:
    normalized_model = model.strip()
    configured = bool(normalized_model)
    if not configured:
        reason: str | None = REASON_MISSING_MODEL
    elif not key_present:
        reason = REASON_MISSING_API_KEY
    else:
        reason = None
    return AiRouteCatalogEntry(
        route_id=route_id,
        provider=provider,
        label=ROUTE_LABELS[route_id],
        model=normalized_model,
        configured=configured,
        available=reason is None,
        unavailable_reason=reason,
        ledger_provider=ledger_provider,
        ledger_model=normalized_model or None,
    )


def build_ai_route_catalog() -> Mapping[AiRouteId, AiRouteCatalogEntry]:
    """현재 서버 설정에서 secret-free catalog를 만든다.

    credential은 "있다/없다"로만 접히고, 값은 어떤 필드에도 들어가지 않는다.
    """

    direct_key = _secret_present(settings.KASSET_AI_API_KEY)
    openrouter_key = _secret_present(settings.KASSET_AI_OPENROUTER_API_KEY)

    mcp_configured = bool(settings.KASSET_AI_MCP_URL.strip())
    mcp_model = f"{MCP_MODEL_PREFIX}{settings.KASSET_AI_MCP_TOOL_NAME.strip()}"
    subscription_configured = bool(settings.KASSET_AI_SUBSCRIPTION_CMD.strip())

    entries: dict[AiRouteId, AiRouteCatalogEntry] = {
        AiRouteId.DIRECT_LUNA: _api_entry(
            AiRouteId.DIRECT_LUNA,
            provider="direct-api",
            ledger_provider="direct-api",
            model=settings.KASSET_AI_MODEL_LUNA,
            key_present=direct_key,
        ),
        AiRouteId.DIRECT_TERRA: _api_entry(
            AiRouteId.DIRECT_TERRA,
            provider="direct-api",
            ledger_provider="direct-api",
            model=settings.KASSET_AI_MODEL_TERRA,
            key_present=direct_key,
        ),
        AiRouteId.DIRECT_SOL: _api_entry(
            AiRouteId.DIRECT_SOL,
            provider="direct-api",
            ledger_provider="direct-api",
            model=settings.KASSET_AI_MODEL_SOL,
            key_present=direct_key,
        ),
        AiRouteId.OPENROUTER_FLASH: _api_entry(
            AiRouteId.OPENROUTER_FLASH,
            provider="openrouter",
            ledger_provider="openrouter",
            model=settings.KASSET_AI_OPENROUTER_MODEL_FLASH,
            key_present=openrouter_key,
        ),
        AiRouteId.OPENROUTER_PRO: _api_entry(
            AiRouteId.OPENROUTER_PRO,
            provider="openrouter",
            ledger_provider="openrouter",
            model=settings.KASSET_AI_OPENROUTER_MODEL_PRO,
            key_present=openrouter_key,
        ),
        AiRouteId.MCP_TOOL: AiRouteCatalogEntry(
            route_id=AiRouteId.MCP_TOOL,
            provider="mcp",
            label=ROUTE_LABELS[AiRouteId.MCP_TOOL],
            model=mcp_model,
            configured=mcp_configured,
            available=mcp_configured,
            unavailable_reason=None if mcp_configured else REASON_MISSING_MCP_URL,
            ledger_provider="mcp",
            ledger_model=mcp_model,
        ),
        AiRouteId.SUBSCRIPTION_CLI: AiRouteCatalogEntry(
            route_id=AiRouteId.SUBSCRIPTION_CLI,
            provider="subscription",
            label=ROUTE_LABELS[AiRouteId.SUBSCRIPTION_CLI],
            model=SUBSCRIPTION_MODEL_LABEL,
            configured=subscription_configured,
            available=subscription_configured,
            unavailable_reason=(
                None if subscription_configured else REASON_MISSING_SUBSCRIPTION_CMD
            ),
            ledger_provider=None,
            ledger_model=None,
        ),
    }
    return MappingProxyType(entries)


def build_ai_availability(
    snapshot: AiRuntimeSnapshot,
    catalog: Mapping[AiRouteId, AiRouteCatalogEntry],
) -> AiAvailability:
    """정책 snapshot과 catalog에서 유효 AI 가용성을 계산한다.

    lane 하나라도 "정책에 활성이고 현재 설정으로 사용 가능한" route를 가지면 AI를
    쓸 수 있다고 본다. 판정 기준은 관리자 화면(``build_ai_routes_view``)이 route
    단위로 노출하는 ``available``과 같으므로 두 화면이 어긋나지 않는다.
    """

    if snapshot.source in _UNREADABLE_POLICY_SOURCES:
        return AiAvailability(
            source=snapshot.source,
            configured=False,
            available=False,
            unavailable_reason=REASON_POLICY_UNREADABLE,
            message="AI 경로 설정을 읽을 수 없어 AI 기능을 사용할 수 없습니다.",
            usable_lanes=frozenset(),
        )

    usable: set[AiLane] = set()
    configured = False
    reasons: list[str] = []
    active_routes = 0
    for lane in AI_APP_LANES:
        for route_id in snapshot.routes(lane):
            entry = catalog[route_id]
            active_routes += 1
            configured = configured or entry.configured
            if entry.available:
                usable.add(lane)
            elif (
                entry.unavailable_reason is not None
                and entry.unavailable_reason not in reasons
            ):
                reasons.append(entry.unavailable_reason)

    if active_routes == 0:
        return AiAvailability(
            source=snapshot.source,
            configured=False,
            available=False,
            unavailable_reason=REASON_NO_ACTIVE_ROUTE,
            message="사용할 AI 경로가 설정되어 있지 않습니다.",
            usable_lanes=frozenset(),
        )

    if not usable:
        detail = " ".join(
            label for reason in reasons if (label := REASON_LABELS.get(reason))
        )
        return AiAvailability(
            source=snapshot.source,
            configured=configured,
            available=False,
            unavailable_reason=REASON_ROUTES_UNAVAILABLE,
            message=f"설정된 AI 경로를 지금 사용할 수 없습니다. {detail}".strip(),
            usable_lanes=frozenset(),
        )

    unusable_labels = [LANE_LABELS[lane] for lane in AI_APP_LANES if lane not in usable]
    return AiAvailability(
        source=snapshot.source,
        configured=True,
        available=True,
        unavailable_reason=None,
        message=(
            "AI를 사용할 수 있습니다."
            if not unusable_labels
            else "AI 일부 기능만 사용할 수 있습니다. 사용할 수 없는 기능: "
            + ", ".join(unusable_labels)
        ),
        usable_lanes=frozenset(usable),
    )


def normalize_route_policy(
    raw: object,
    *,
    catalog: Mapping[AiRouteId, AiRouteCatalogEntry] | None = None,
) -> dict[AiLane, tuple[AiRouteId, ...]]:
    """정책 payload를 검증해 lane별 route ID tuple로 정규화한다.

    ``catalog``를 주면 현재 사용 불가한 route까지 거부한다(쓰기 경로). 읽기
    경로는 ``None``으로 호출한다. credential이 잠시 빠졌다는 이유로 저장된
    정책을 손상 처리하면 안 되고, 실행 시점에 unavailable route를 건너뛰는
    것으로 충분하기 때문이다.

    빈 lane은 손상이 아니라 **명시적 비활성화**다.
    """

    if not isinstance(raw, Mapping):
        raise AiRoutePolicyError(
            "policy_shape",
            "정책은 lane 이름을 키로 가지는 객체여야 합니다.",
        )

    expected = {lane.value for lane in AiLane}
    supplied = {str(key) for key in raw}
    missing = sorted(expected - supplied)
    if missing:
        raise AiRoutePolicyError(
            "lane_missing",
            f"lane이 빠졌습니다: {', '.join(missing)}",
        )
    unknown = sorted(supplied - expected)
    if unknown:
        raise AiRoutePolicyError(
            "unknown_lane",
            f"알 수 없는 lane입니다: {', '.join(unknown)}",
        )

    normalized: dict[AiLane, tuple[AiRouteId, ...]] = {}
    for lane in AiLane:
        value = raw[lane.value]
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise AiRoutePolicyError(
                "route_list_shape",
                f"{lane.value}의 route 목록은 배열이어야 합니다.",
            )
        allowed = LANE_ROUTE_IDS[lane]
        seen: list[AiRouteId] = []
        for element in value:
            if not isinstance(element, str):
                raise AiRoutePolicyError(
                    "unknown_route",
                    f"{lane.value}에 문자열이 아닌 route가 있습니다.",
                )
            try:
                route_id = AiRouteId(element)
            except ValueError:
                raise AiRoutePolicyError(
                    "unknown_route",
                    f"{lane.value}에 알 수 없는 route가 있습니다: {element}",
                ) from None
            if route_id not in allowed:
                raise AiRoutePolicyError(
                    "lane_route_mismatch",
                    f"{route_id.value}는 {lane.value}에서 쓸 수 없습니다.",
                )
            if route_id in seen:
                raise AiRoutePolicyError(
                    "duplicate_route",
                    f"{lane.value}에 {route_id.value}가 중복되었습니다.",
                )
            if catalog is not None and not catalog[route_id].available:
                raise AiRoutePolicyError(
                    "route_unavailable",
                    f"{route_id.value}는 현재 서버 설정으로 사용할 수 없습니다.",
                )
            seen.append(route_id)
        normalized[lane] = tuple(seen)
    return normalized


def serialize_route_policy(
    lanes: Mapping[AiLane, Sequence[AiRouteId]],
) -> dict[str, list[str]]:
    """JSONB 저장용 표현. route ID만 남고 provider/model/URL은 없다."""

    return {
        lane.value: [route_id.value for route_id in lanes.get(lane, ())]
        for lane in AiLane
    }


__all__ = [
    "AI_APP_LANES",
    "AI_REVIEW_LANES",
    "DEFAULT_ROUTE_POLICY",
    "LANE_LABELS",
    "LANE_ROUTE_IDS",
    "MCP_MODEL_PREFIX",
    "REASON_LABELS",
    "REASON_MISSING_API_KEY",
    "REASON_MISSING_MCP_URL",
    "REASON_MISSING_MODEL",
    "REASON_MISSING_SUBSCRIPTION_CMD",
    "REASON_NO_ACTIVE_ROUTE",
    "REASON_POLICY_UNREADABLE",
    "REASON_ROUTES_UNAVAILABLE",
    "ROUTE_LABELS",
    "SUBSCRIPTION_MODEL_LABEL",
    "AiAvailability",
    "AiLane",
    "AiPolicySource",
    "AiProviderName",
    "AiRouteCatalogEntry",
    "AiRouteId",
    "AiRoutePolicy",
    "AiRoutePolicyError",
    "AiRuntimeSnapshot",
    "ai_route_provider",
    "build_ai_availability",
    "build_ai_route_catalog",
    "default_snapshot",
    "fail_closed_snapshot",
    "freeze_route_policy",
    "lane_telemetry_covered",
    "normalize_route_policy",
    "serialize_route_policy",
]
