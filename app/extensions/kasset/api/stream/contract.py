"""앱 ↔ 서버 WebSocket 와이어 계약.

설계 원칙 두 개다.

1. **페이로드는 REST와 같은 모델이다.** 시세는 REST `Quote`를 그대로 쓰고, 호가는
   REST `OrderbookResponse`와 필드 이름·표기를 맞춘다. 앱이 폴링 결과와 스트림
   결과를 같은 데이터 클래스로 다룰 수 있어야 한다. 가격·수량은 항상 문자열이다.
2. **구독은 선언형 full-replace다.** 클라이언트가 보내는 `topics` 배열 하나가 그
   연결의 구독 전체다. 화면을 나가면 새 배열(또는 빈 배열)을 보내면 되고, 놓친
   `unsubscribe` 때문에 상향 예산이 새는 경로가 아예 없다. 상향 토스 프로토콜과
   같은 모델이라 변환 계층도 얇다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import Field

from app.extensions.kasset.api.krx_quotes import build_quote
from app.extensions.kasset.api.paper import decimal_text, iso_z
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.schemas import AndroidWireModel
from app.extensions.kasset.api.stream.topics import (
    MAX_CLIENT_TOPICS,
    Topic,
    TopicMarket,
)

# 와이어 프로토콜 버전. 앱이 서버 배포 시점 차이를 감지할 수 있게 `ready`에 담는다.
STREAM_PROTOCOL_VERSION: Final[int] = 1

# 앱이 스트림에서 빠진 토픽을 폴링으로 메울 때 쓸 권장 간격. 지금 홈 화면 폴링
# 주기(2초)와 같은 값이며, 서버가 값을 바꾸면 앱이 따라온다.
POLL_FALLBACK_INTERVAL_SECONDS: Final[int] = 2

# --- close code ---------------------------------------------------------------
# 4000번대 애플리케이션 코드를 쓴다. 앱은 이 값으로 "토큰 갱신 후 재접속"과
# "그냥 백오프 재접속"을 구분한다.
CLOSE_UNAUTHORIZED: Final[int] = 4401
CLOSE_AUTH_TIMEOUT: Final[int] = 4408
CLOSE_BAD_PROTOCOL: Final[int] = 4400
CLOSE_SLOW_CONSUMER: Final[int] = 4409
CLOSE_SERVER_SHUTDOWN: Final[int] = 4503

# --- 서버 → 앱 메시지 type ------------------------------------------------------
MESSAGE_READY: Final[str] = "ready"
MESSAGE_SUBSCRIBED: Final[str] = "subscribed"
MESSAGE_QUOTE: Final[str] = "quote"
MESSAGE_ORDERBOOK: Final[str] = "orderbook"
MESSAGE_STATUS: Final[str] = "status"
MESSAGE_ERROR: Final[str] = "error"
MESSAGE_PONG: Final[str] = "pong"

UpstreamState = Literal["LIVE", "DEGRADED"]

# 강등 사유. 앱은 사유별로 배너를 다르게 낼 수 있고, 아무 경우에도
# `pollingTopics`만 보면 무엇을 폴링해야 하는지 알 수 있다.
REASON_UPSTREAM_DOWN: Final[str] = "UPSTREAM_UNAVAILABLE"
REASON_TOPIC_BUDGET: Final[str] = "TOPIC_BUDGET_EXCEEDED"
REASON_TOPIC_REJECTED: Final[str] = "TOPIC_REJECTED_BY_PROVIDER"

# 클라이언트 요청 오류 코드.
ERROR_BAD_FRAME: Final[str] = "BAD_FRAME"
ERROR_UNKNOWN_TYPE: Final[str] = "UNKNOWN_TYPE"

# 상태 메시지는 최신값만 의미가 있으므로 세션 mailbox에서 이 예약 키로 합쳐진다.
STATUS_MAILBOX_KEY: Final[str] = "\x00status"


class StreamOrderbookLevel(AndroidWireModel):
    """호가 한 단. REST `OrderbookLevel`과 같은 필드 이름·decimal 표기다."""

    price: str = Field(pattern=r"^\d+(?:\.\d+)?$")
    volume: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class StreamOrderbook(AndroidWireModel):
    """스트림 호가 스냅샷.

    REST `OrderbookResponse`를 그대로 재사용하지 않는 이유는 하나다. 그 모델은
    NH PLUG KRX 채널의 계약이라 `market: Literal["KRX"]`,
    `source: Literal["NH_PLUG_WS"]`, 6자리 심볼 패턴으로 좁혀져 있고, 스트림은
    미국 종목과 토스 소스를 함께 실어야 한다. REST 계약을 넓히면 기존 앱 폴링
    경로의 계약이 흔들리므로, 필드 이름·표기만 1:1로 맞춘 별도 모델을 둔다.
    """

    symbol: str
    market: TopicMarket
    # 첫 푸시가 오기 전에는 `false`다. 토스는 구독 직후 초기 스냅샷을 주지 않는다.
    ready: bool
    as_of: str | None
    source: str
    asks: list[StreamOrderbookLevel]
    bids: list[StreamOrderbookLevel]
    total_ask_volume: str
    total_bid_volume: str


@dataclass(frozen=True, slots=True)
class SubscribeRequest:
    """`{"type":"subscribe","topics":[...]}`. 이 배열이 그 연결의 구독 전체다."""

    topics: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class PingRequest:
    """`{"type":"ping"}`."""


@dataclass(frozen=True, slots=True)
class AuthRequest:
    """`{"type":"auth","accessToken":"..."}`. 헤더로 토큰을 못 싣는 경우만 쓴다."""

    access_token: str


@dataclass(frozen=True, slots=True)
class ClientFrameError:
    code: str
    message: str


ClientFrame = SubscribeRequest | PingRequest | AuthRequest | ClientFrameError


def parse_client_frame(raw: str | bytes) -> ClientFrame:
    """앱이 보낸 프레임 하나를 해석한다. 오류도 값으로 돌려준다.

    잘못된 프레임 하나로 연결을 끊지 않는다. 끊으면 앱이 재접속 → 재구독을
    반복하며 상향 선언 빈도 한도를 태운다.
    """

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ClientFrameError(ERROR_BAD_FRAME, "JSON 프레임이 아닙니다.")
    if not isinstance(payload, dict):
        return ClientFrameError(ERROR_BAD_FRAME, "프레임은 JSON 객체여야 합니다.")

    frame_type = payload.get("type")
    if frame_type == "subscribe":
        topics = payload.get("topics")
        if not isinstance(topics, list):
            return ClientFrameError(
                ERROR_BAD_FRAME, "`topics`는 문자열 배열이어야 합니다."
            )
        return SubscribeRequest(topics=tuple(topics))
    if frame_type == "ping":
        return PingRequest()
    if frame_type == "auth":
        token = payload.get("accessToken")
        if not isinstance(token, str) or not token.strip():
            return ClientFrameError(ERROR_BAD_FRAME, "`accessToken`이 필요합니다.")
        return AuthRequest(access_token=token.strip())
    return ClientFrameError(
        ERROR_UNKNOWN_TYPE, "지원하지 않는 `type`입니다: subscribe|ping|auth"
    )


def ready_message(*, upstream: UpstreamState) -> str:
    """연결 직후 1회. 연결 파라미터와 접속 시점 상향 상태를 함께 알린다.

    구독이 없는 상태에서 `status`를 따로 보내지 않는 이유는, 구독이 없으면
    폴링할 토픽도 없어서 그 프레임에 담을 내용이 없기 때문이다. 접속 시점에
    필요한 정보는 "상향이 살아 있는가" 하나이므로 여기에 담는다.
    """

    return _dump(
        {
            "type": MESSAGE_READY,
            "protocol": STREAM_PROTOCOL_VERSION,
            "upstream": upstream,
            "maxTopics": MAX_CLIENT_TOPICS,
            # 상향 keepalive와 별개로 앱이 유휴 연결을 살려 두는 기준.
            "pingIntervalSeconds": 30,
            "pollIntervalSeconds": POLL_FALLBACK_INTERVAL_SECONDS,
        }
    )


def subscribed_message(
    *,
    accepted: Sequence[str],
    rejected: Sequence[tuple[str, str]],
) -> str:
    return _dump(
        {
            "type": MESSAGE_SUBSCRIBED,
            "topics": list(accepted),
            "rejected": [{"topic": topic, "code": code} for topic, code in rejected],
        }
    )


def quote_message(topic: Topic, quote: Quote) -> str:
    return _dump(
        {
            "type": MESSAGE_QUOTE,
            "topic": topic.key,
            "quote": quote.model_dump(by_alias=True),
        }
    )


def orderbook_message(topic: Topic, orderbook: StreamOrderbook) -> str:
    return _dump(
        {
            "type": MESSAGE_ORDERBOOK,
            "topic": topic.key,
            "orderbook": orderbook.model_dump(by_alias=True),
        }
    )


def status_message(
    *,
    upstream: UpstreamState,
    reason: str | None,
    polling_topics: Iterable[str],
) -> str:
    """스트림 강등 시그널.

    `pollingTopics`는 "이 연결이 구독했지만 서버가 지금 스트리밍하지 않는" 토픽
    이다. 상향 단절·예산 초과·공급자 거부를 한 필드로 흡수하므로, 앱은 사유를
    분기하지 않고 이 목록만 REST 폴링으로 돌리면 된다. 목록이 비면 폴링을 끈다.
    """

    return _dump(
        {
            "type": MESSAGE_STATUS,
            "upstream": upstream,
            "reason": reason,
            "pollingTopics": sorted(polling_topics),
            "pollIntervalSeconds": POLL_FALLBACK_INTERVAL_SECONDS,
        }
    )


def error_message(code: str, message: str) -> str:
    return _dump({"type": MESSAGE_ERROR, "code": code, "message": message})


def pong_message() -> str:
    return _dump({"type": MESSAGE_PONG})


def quote_from_tick(
    baseline: Quote,
    *,
    price: Decimal,
    as_of: datetime,
    source: str,
) -> Quote:
    """체결 tick 하나를 REST와 동일한 `Quote`로 만든다.

    `previousClose`·`name`·`currency`는 REST 해석 결과(baseline)를 재사용한다.
    토스 체결 프레임에는 전일 종가가 없고, 등락 계산을 스트림 쪽에서 따로 구현
    하면 폴링 값과 스트림 값이 갈라진다. 그래서 생성은 REST와 같은
    `krx_quotes.build_quote` 하나만 쓴다.
    """

    previous_close = (
        Decimal(baseline.previous_close)
        if baseline.previous_close is not None
        else None
    )
    return build_quote(
        market=baseline.market,
        symbol=baseline.symbol,
        name=baseline.name,
        currency=baseline.currency,
        price=price,
        previous_close=previous_close,
        as_of=as_of,
        source=source,
    )


def orderbook_from_frame(
    topic: Topic,
    *,
    asks: Sequence[tuple[Decimal, Decimal]],
    bids: Sequence[tuple[Decimal, Decimal]],
    as_of: datetime | None,
    source: str,
) -> StreamOrderbook:
    """호가 프레임을 REST 호가와 같은 표기로 만든다."""

    total_ask = sum((volume for _, volume in asks), Decimal(0))
    total_bid = sum((volume for _, volume in bids), Decimal(0))
    return StreamOrderbook(
        symbol=topic.symbol,
        market=topic.market,
        ready=bool(asks or bids),
        as_of=iso_z(as_of) if as_of is not None else None,
        source=source,
        asks=[
            StreamOrderbookLevel(price=decimal_text(price), volume=decimal_text(volume))
            for price, volume in asks
        ],
        bids=[
            StreamOrderbookLevel(price=decimal_text(price), volume=decimal_text(volume))
            for price, volume in bids
        ],
        total_ask_volume=decimal_text(total_ask),
        total_bid_volume=decimal_text(total_bid),
    )


def _dump(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
