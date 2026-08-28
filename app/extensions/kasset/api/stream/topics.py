"""스트림 토픽 모델과 상향 토픽 예산 배분.

앱이 쓰는 토픽 키는 REST 시세 계약과 같은 표기를 쓴다(`quote:US:TQQQ`,
`orderbook:KRX:005930`). 상향 토스 토픽(`trade:us:TQQQ`)과는 1:1로 대응하며,
변환은 이 모듈 하나만 안다. 앱이 토스 표기를 알아야 할 이유가 없기 때문이다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

TopicKind = Literal["quote", "orderbook"]
TopicMarket = Literal["KRX", "US"]

# 토스 AsyncAPI 명세(웹소켓 1.2.2) "한도" 표의 확정값이다.
# - 연결당 구독 수 100건(`codes` 합산). 초과 선언은 `too-many-topics`로 전체 실패.
# - 구독 수는 채널×종목 조합 기준: `trade:us:AAPL` + `orderbook:us:AAPL` = 2건.
MAX_UPSTREAM_TOPICS: Final[int] = 100

# 클라이언트 1개가 잡을 수 있는 토픽 상한. 관심종목 상한 20개(`MAX_WATCHLIST_ITEMS`)
# 전체 + 상세 시세 1 + 주문 호가 1 + 여유 2다. 한 연결이 상향 예산 100건을
# 독점하지 못하게 하는 것이 목적이다.
MAX_CLIENT_TOPICS: Final[int] = 24

_KRX_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^\d{6}$")
# 미국 티커 표기는 종목 마스터 그대로(대문자). 소문자는 토스가
# `stock-not-found`로 거부하므로 여기서 미리 걸러 왕복을 아낀다.
_US_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")

_KINDS: Final[frozenset[str]] = frozenset({"quote", "orderbook"})
_MARKETS: Final[frozenset[str]] = frozenset({"KRX", "US"})
# 앱 시장 표기 → 토스 시장 표기. `trade:kr`은 KRX+NXT 통합 시세다.
_UPSTREAM_MARKET: Final[dict[str, str]] = {"KRX": "kr", "US": "us"}
_UPSTREAM_FAMILY: Final[dict[str, str]] = {"quote": "trade", "orderbook": "orderbook"}
_DOWNSTREAM_MARKET: Final[dict[str, str]] = {"kr": "KRX", "us": "US"}
_DOWNSTREAM_KIND: Final[dict[str, str]] = {"trade": "quote", "orderbook": "orderbook"}

# 거부 사유 코드. 앱은 이 값으로 "고칠 수 있는 요청 오류"와 "서버 예산 부족"을
# 구분한다.
REJECT_MALFORMED: Final[str] = "MALFORMED_TOPIC"
REJECT_UNKNOWN_KIND: Final[str] = "UNKNOWN_KIND"
REJECT_UNKNOWN_MARKET: Final[str] = "UNKNOWN_MARKET"
REJECT_BAD_SYMBOL: Final[str] = "BAD_SYMBOL"
REJECT_TOO_MANY: Final[str] = "TOO_MANY_TOPICS"


class TopicRejected(ValueError):
    """토픽 키 하나를 거부한다. 연결 전체를 끊지 않고 그 항목만 거부한다."""

    __slots__ = ("code", "topic")

    def __init__(self, topic: str, code: str) -> None:
        super().__init__(f"{topic}: {code}")
        self.topic = topic
        self.code = code


@dataclass(frozen=True, slots=True)
class Topic:
    """`quote|orderbook` × `KRX|US` × 종목 하나."""

    kind: TopicKind
    market: TopicMarket
    symbol: str

    @property
    def key(self) -> str:
        """앱↔서버 토픽 키."""
        return f"{self.kind}:{self.market}:{self.symbol}"

    @property
    def upstream_key(self) -> str:
        """토스 full key. ack의 `subscribed`/`rejected[].target`과 같은 표기다."""
        family = _UPSTREAM_FAMILY[self.kind]
        return f"{family}:{_UPSTREAM_MARKET[self.market]}:{self.symbol}"

    @property
    def upstream_type(self) -> str:
        """토스 구독 선언 원소의 `type` 값(`trade:us` 등)."""
        return f"{_UPSTREAM_FAMILY[self.kind]}:{_UPSTREAM_MARKET[self.market]}"


def parse_topic(raw: object) -> Topic:
    """앱이 보낸 토픽 키를 검증한다. 잘못된 항목은 `TopicRejected`."""

    if not isinstance(raw, str):
        raise TopicRejected(str(raw), REJECT_MALFORMED)
    text = raw.strip()
    parts = text.split(":")
    if len(parts) != 3 or not all(parts):
        raise TopicRejected(text, REJECT_MALFORMED)
    kind, market, symbol = parts
    if kind not in _KINDS:
        raise TopicRejected(text, REJECT_UNKNOWN_KIND)
    if market not in _MARKETS:
        raise TopicRejected(text, REJECT_UNKNOWN_MARKET)
    pattern = _US_SYMBOL_RE if market == "US" else _KRX_SYMBOL_RE
    if not pattern.match(symbol):
        raise TopicRejected(text, REJECT_BAD_SYMBOL)
    return Topic(kind=kind, market=market, symbol=symbol)  # type: ignore[arg-type]


def parse_upstream_key(raw: object) -> Topic | None:
    """토스 full key를 앱 토픽으로 되돌린다. 우리가 안 쓰는 계열은 `None`.

    `personal:order:3` 처럼 이 채널이 구독하지 않는 계열도 같은 연결로 올 수
    있으므로, 모르는 계열은 예외 없이 조용히 버린다.
    """

    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    family, market, symbol = parts
    kind = _DOWNSTREAM_KIND.get(family)
    downstream_market = _DOWNSTREAM_MARKET.get(market)
    if kind is None or downstream_market is None or not symbol:
        return None
    return Topic(kind=kind, market=downstream_market, symbol=symbol)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TopicBudget:
    """상향 100건 예산 배분 결과. 두 집합은 서로 배타적이다."""

    # 실제로 토스에 선언할 토픽 키(구독자 많은 순으로 뽑은 뒤 정렬해 고정).
    streaming: tuple[str, ...]
    # 예산을 넘겨 스트림에서 제외한 토픽 키. 앱은 이 토픽만 REST 폴링으로 강등한다.
    demoted: tuple[str, ...]


def allocate_topics(
    demand: Mapping[str, int],
    *,
    capacity: int = MAX_UPSTREAM_TOPICS,
    blocked: Iterable[str] = (),
) -> TopicBudget:
    """구독자 수가 많은 토픽부터 예산 안에 넣고, 나머지는 강등한다.

    예산을 넘겼을 때 조용히 누락시키면 앱은 "시세가 멈춘 종목"을 보게 된다.
    그래서 제외분을 `demoted`로 반환해 호출부가 앱에 폴링 강등을 통보하게 한다.

    `blocked`는 토스가 이미 `stock-not-found`·`symbol-market-mismatch`로 거부한
    토픽이다. 명세상 원인을 고치지 않은 재선언은 같은 이유로 다시 거부되므로
    선언 목록에서 빼고, 앱에는 강등으로 알려 REST가 값을 내게 한다.
    """

    blocked_keys = frozenset(blocked)
    ranked = sorted(
        (
            (key, count)
            for key, count in demand.items()
            if count > 0 and key not in blocked_keys
        ),
        # 구독자 수 내림차순, 동수는 키 오름차순. 동수 구간에서 순서가 흔들리면
        # 선언이 매 주기마다 바뀌어 선언 빈도 한도(5회/초)를 태운다.
        key=lambda row: (-row[1], row[0]),
    )
    limit = max(capacity, 0)
    streaming = sorted(key for key, _ in ranked[:limit])
    demoted = sorted(
        [key for key, _ in ranked[limit:]]
        + [key for key, count in demand.items() if count > 0 and key in blocked_keys]
    )
    return TopicBudget(streaming=tuple(streaming), demoted=tuple(demoted))
