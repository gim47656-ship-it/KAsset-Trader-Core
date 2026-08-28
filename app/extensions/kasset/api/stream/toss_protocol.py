"""토스 실시간 웹소켓 와이어 계약.

근거는 토스 AsyncAPI 명세(`openapi-ws` 1.2.2)다. 이 모듈은 프레임 직렬화/파싱과
에러 코드 분류만 담당하고 연결·재시도는 `upstream.py`가 담당한다.

명세에서 설계에 직접 영향을 준 사실:

- 구독은 **선언형 full-replace**다. JSON 배열 1개가 곧 현재 구독 전체이고,
  빠진 항목은 자동 해제되며 `[]`는 전체 해제다. `subscribe`/`unsubscribe`
  액션은 존재하지 않는다.
- 인증은 handshake 1회다. 연결 유지 중 access token이 만료되어도 연결은 끊기지
  않는다. 즉 토큰 재발급은 재연결 시점에만 필요하다.
- keepalive는 JSON이 아닌 순수 텍스트 프레임 `PING`(대문자)이고 응답은
  `{"type":"pong"}`이다. 서버는 **클라이언트로부터의 수신**이 180초 없으면
  연결을 끊는다(서버가 보내는 데이터는 타이머를 리셋하지 않는다).
- 구독 직후 초기 스냅샷은 오지 않는다. 연결 시점 상태는 REST로 받아야 한다.
  그래서 하향 계약은 REST 스냅샷 위에 갱신을 얹는 모양이다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.extensions.kasset.api.stream.topics import Topic, parse_upstream_key

logger = logging.getLogger(__name__)

TOSS_STREAM_URL: Final[str] = "wss://openapi-ws.tossinvest.com/ws/v1"
# 스트림으로 만든 시세·호가의 `source`. REST 배치 경로(`TOSS_API_PRICES`)와
# 구분해 앱이 "이 값이 어느 채널에서 왔는지"를 그대로 알 수 있게 한다.
TOSS_STREAM_SOURCE: Final[str] = "TOSS_API_WS"

# keepalive. 명세: 순수 텍스트 대문자 4글자, 60초 간격 권장, 서버 idle 한도 180초.
PING_TEXT: Final[str] = "PING"
KEEPALIVE_INTERVAL_SECONDS: Final[float] = 60.0
IDLE_LIMIT_SECONDS: Final[float] = 180.0

# 선언 빈도 한도는 5회/초다. 여유를 두고 최소 간격을 0.5초로 잡는다.
DECLARE_MIN_INTERVAL_SECONDS: Final[float] = 0.5
# `rate-limit-exceeded` 를 받으면 명세 권고대로 약 1초 대기 후 재선언한다.
RATE_LIMIT_BACKOFF_SECONDS: Final[float] = 1.0

# 선언 원소 `type` 을 항상 같은 순서로 내보낸다. 순서가 흔들리면 같은 구독
# 집합인데도 프레임이 달라져 진단이 어려워진다.
_DECLARE_TYPE_ORDER: Final[tuple[str, ...]] = (
    "trade:kr",
    "trade:us",
    "orderbook:kr",
    "orderbook:us",
)

# --- 에러 코드 분류 (명세 errorFrame.error.code 열거값 기준) -------------------
# 우리 프레임이 틀렸다는 뜻이므로 재시도해도 같은 결과다. 무한 재시도 금지.
FATAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"wrong-format", "no-type", "invalid-type", "no-codes"}
)
# 선언 자체가 과했다. 예산을 줄이고 다시 선언한다(연결은 유지된다).
CAPACITY_ERROR_CODES: Final[frozenset[str]] = frozenset({"too-many-topics"})
# 선언 빈도 초과. 잠시 쉬고 다시 선언한다(연결은 유지된다).
THROTTLE_ERROR_CODES: Final[frozenset[str]] = frozenset({"rate-limit-exceeded"})
# 프레임 직후 연결이 닫힌다. 재연결 후 다시 선언한다.
RECONNECT_ERROR_CODES: Final[frozenset[str]] = frozenset({"server-shutdown"})
# 동시 연결 한도. 다른 소유자가 살아 있을 수 있으니 길게 쉬고 재확인한다.
CONNECTION_LIMIT_ERROR_CODES: Final[frozenset[str]] = frozenset({"too-many"})
# 엣지 차단(허용 IP 등). 재시도로 풀리지 않으므로 길게 쉰다.
BLOCKED_ERROR_CODES: Final[frozenset[str]] = frozenset({"edge-blocked"})

# ack 의 `rejected[].code`. 원인을 고치기 전에는 재선언해도 같은 이유로 거부된다.
REJECT_CODES: Final[frozenset[str]] = frozenset(
    {"stock-not-found", "symbol-market-mismatch", "account-not-found"}
)


def declare_frame(
    upstream_keys: Iterable[str], *, request_id: str | None = None
) -> str:
    """구독 선언 프레임 한 개를 만든다. 이 배열이 곧 현재 구독 전체다.

    `upstream_keys`는 토스 full key(`trade:us:AAPL`)다. 같은 `type`끼리 묶어
    `codes` 배열로 접는다. 빈 입력은 `[]`(전체 해제)이고, 명세가 그것을 정상
    입력으로 정의한다.
    """

    grouped: dict[str, list[str]] = {}
    for key in upstream_keys:
        family_market, _, code = key.rpartition(":")
        if not family_market or not code:
            # 우리가 만든 키만 들어오는 자리다. 여기 걸리면 상위 로직 결함이다.
            raise ValueError(f"malformed upstream topic key: {key!r}")
        grouped.setdefault(family_market, []).append(code)

    elements: list[dict[str, Any]] = []
    if request_id is not None:
        elements.append({"id": request_id})
    for declare_type in _DECLARE_TYPE_ORDER:
        codes = grouped.pop(declare_type, None)
        if codes:
            elements.append({"type": declare_type, "codes": sorted(set(codes))})
    for declare_type in sorted(grouped):
        elements.append(
            {"type": declare_type, "codes": sorted(set(grouped[declare_type]))}
        )
    return json.dumps(elements, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RejectedTopic:
    """ack 의 `rejected[]` 한 건."""

    target: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SubscriptionsAck:
    """`{"type":"subscriptions", ...}`. 데이터보다 먼저 도착한다."""

    request_id: str | None
    subscribed: tuple[str, ...]
    rejected: tuple[RejectedTopic, ...]


@dataclass(frozen=True, slots=True)
class TradeFrame:
    """`trade:{시장}:{symbol}` 체결 한 건."""

    topic: Topic
    price: Decimal
    volume: Decimal
    currency: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    price: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookFrame:
    """`orderbook:{시장}:{symbol}` 호가 스냅샷. `asks`는 낮은 가격순이다."""

    topic: Topic
    currency: str
    # 명세상 `timestamp`는 null 가능(데이터 미제공). 서버 시각으로 위조하지 않는다.
    as_of: datetime | None
    asks: tuple[OrderbookLevel, ...]
    bids: tuple[OrderbookLevel, ...]


@dataclass(frozen=True, slots=True)
class UpstreamError:
    """`{"type":"error","error":{"code","message"}}`."""

    code: str
    message: str
    request_id: str | None


@dataclass(frozen=True, slots=True)
class PongFrame:
    """`{"type":"pong"}`. keepalive 왕복이 살아 있다는 신호로만 쓴다."""


InboundFrame = (
    SubscriptionsAck | TradeFrame | OrderbookFrame | UpstreamError | PongFrame
)


def parse_inbound(raw: str | bytes) -> InboundFrame | None:
    """수신 프레임 하나를 파싱한다. 우리가 안 쓰는 프레임은 `None`.

    명세대로 top-level `type`으로만 디스패치한다. 같은 연결에
    `personal:order` 등 다른 계열이 섞여 와도 조용히 버린다.
    """

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    frame_type = payload.get("type")
    if frame_type == "subscriptions":
        return _parse_ack(payload)
    if frame_type == "error":
        return _parse_error(payload)
    if frame_type == "pong":
        return PongFrame()
    if frame_type != "message":
        return None

    topic = parse_upstream_key(payload.get("topic"))
    data = payload.get("data")
    if topic is None or not isinstance(data, dict):
        return None
    if topic.kind == "quote":
        return _parse_trade(topic, data)
    return _parse_orderbook(topic, data)


def _parse_ack(payload: Mapping[str, Any]) -> SubscriptionsAck:
    subscribed = tuple(item for item in _string_list(payload.get("subscribed")) if item)
    rejected_raw = payload.get("rejected")
    rejected: list[RejectedTopic] = []
    if isinstance(rejected_raw, list):
        for item in rejected_raw:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            code = item.get("code")
            if not isinstance(target, str) or not isinstance(code, str):
                continue
            message = item.get("message")
            rejected.append(
                RejectedTopic(
                    target=target,
                    code=code,
                    message=message if isinstance(message, str) else "",
                )
            )
    request_id = payload.get("id")
    return SubscriptionsAck(
        request_id=request_id if isinstance(request_id, str) else None,
        subscribed=subscribed,
        rejected=tuple(rejected),
    )


def _parse_error(payload: Mapping[str, Any]) -> UpstreamError:
    error = payload.get("error")
    code = ""
    message = ""
    if isinstance(error, dict):
        raw_code = error.get("code")
        raw_message = error.get("message")
        code = raw_code if isinstance(raw_code, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""
    request_id = payload.get("id")
    return UpstreamError(
        code=code,
        message=message,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _parse_trade(topic: Topic, data: Mapping[str, Any]) -> TradeFrame | None:
    price = _decimal(data.get("price"))
    as_of = _timestamp(data.get("timestamp"))
    if price is None or price <= 0 or as_of is None:
        # 체결가나 체결 시각을 신뢰할 수 없으면 버린다. 서버 시각으로 채우면
        # 오래된 값이 실시간처럼 보인다(REST 경로와 같은 규칙).
        return None
    volume = _decimal(data.get("volume"))
    return TradeFrame(
        topic=topic,
        price=price,
        volume=volume if volume is not None and volume >= 0 else Decimal(0),
        currency=_currency(data.get("currency"), topic),
        as_of=as_of,
    )


def _parse_orderbook(topic: Topic, data: Mapping[str, Any]) -> OrderbookFrame | None:
    asks = _levels(data.get("asks"))
    bids = _levels(data.get("bids"))
    if asks is None or bids is None:
        return None
    return OrderbookFrame(
        topic=topic,
        currency=_currency(data.get("currency"), topic),
        as_of=_timestamp(data.get("timestamp")),
        asks=asks,
        bids=bids,
    )


def _levels(raw: object) -> tuple[OrderbookLevel, ...] | None:
    if not isinstance(raw, list):
        return None
    levels: list[OrderbookLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        price = _decimal(item.get("price"))
        volume = _decimal(item.get("volume"))
        if price is None or volume is None or price < 0 or volume < 0:
            continue
        levels.append(OrderbookLevel(price=price, volume=volume))
    return tuple(levels)


def _currency(raw: object, topic: Topic) -> str:
    """통화 코드. 명세가 unknown enum 허용을 요구하므로 죽지 않고 시장값으로 채운다."""

    if isinstance(raw, str):
        text = raw.strip().upper()
        if text:
            return text
    return "USD" if topic.market == "US" else "KRW"


def _decimal(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _timestamp(raw: object) -> datetime | None:
    """명세 `format: date-time` 문자열을 tz-aware UTC로 바꾼다.

    오프셋이 없는 문자열은 신뢰하지 않고 `None`으로 둔다. 서버 로컬 시각으로
    해석하면 9시간 틀린 시세 시각이 실시간처럼 보인다.
    """

    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _string_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def classify_error(code: str) -> str:
    """에러 코드를 대응 전략으로 분류한다. 모르는 코드는 재시도 대상이다."""

    if code in FATAL_ERROR_CODES:
        return "fatal"
    if code in CAPACITY_ERROR_CODES:
        return "capacity"
    if code in THROTTLE_ERROR_CODES:
        return "throttle"
    if code in RECONNECT_ERROR_CODES:
        return "reconnect"
    if code in CONNECTION_LIMIT_ERROR_CODES:
        return "connection-limit"
    if code in BLOCKED_ERROR_CODES:
        return "blocked"
    return "retry"


def upstream_keys(topics: Sequence[Topic]) -> tuple[str, ...]:
    return tuple(topic.upstream_key for topic in topics)
