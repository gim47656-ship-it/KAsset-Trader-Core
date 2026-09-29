"""NH PLUG 국내주식 실시간(WebSocket) 프로토콜.

정본은 NH ``krstock/openapi.json``의 ``x-realtime-channels``와 공식 SDK
``PLUG-OpenAPI/nhplug-sdk``(MIT) ``nhplug/realtime.py``다. SDK는 동기식
``websocket-client`` 스레드 모델이고 푸시가 없으면 세션을 끝내므로 장시간
asyncio 프로세스에는 쓰지 않고, 같은 규칙(경로·한도·ACK 판정)만 따른다.

- 접속: ``wss://api.nhplug.com:7070/websocket`` — 경로 ``/websocket`` 필수.
- 구독: ``{"header":{"token":…,"tr_type":"1"},"body":{"tr_cd":…,"tr_key":…}}``,
  해제는 ``tr_type="2"``. 인증은 ``header.token``뿐이다.
- 푸시: ``{"header":{"tr_cd","tr_key"},"body":{…}}``. ACK는 header에
  ``tr_type`` 또는 ``rsp_cd``가 있고 정상 코드는 ``00000``이다.
- 한도: 앱키당 세션 2, 세션당 등록 30, 구독 전송 초당 10건.
- 채널: ``oc`` KRX 체결, ``ob`` KRX 호가. 통합(``mc``/``mb``)·NXT는 쓰지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Final

from app.extensions.kasset.automation.realtime_tape import (
    BookLevel,
    BookTick,
    TradeTick,
)

WS_URL: Final = "wss://api.nhplug.com:7070/websocket"
MAX_SESSIONS: Final = 2
MAX_KEYS_PER_SESSION: Final = 30
MAX_SUBSCRIBE_PER_SEC: Final = 10
#: 한도보다 느리게 보낸다(초당 5건).
SUBSCRIBE_INTERVAL_SECONDS: Final = 0.2
WS_ACK_OK: Final = "00000"

TRADE_CHANNEL: Final = "oc"
BOOK_CHANNEL: Final = "ob"
CHANNELS: Final = (TRADE_CHANNEL, BOOK_CHANNEL)
#: 종목 하나가 쓰는 등록 수(체결+호가).
REGISTRATIONS_PER_SYMBOL: Final = len(CHANNELS)
SYMBOLS_PER_SESSION: Final = MAX_KEYS_PER_SESSION // REGISTRATIONS_PER_SYMBOL
MAX_SYMBOLS: Final = SYMBOLS_PER_SESSION * MAX_SESSIONS


def subscribe_message(token: str, channel: str, symbol: str, *, register: bool) -> str:
    return json.dumps(
        {
            "header": {"token": token, "tr_type": "1" if register else "2"},
            "body": {"tr_cd": channel, "tr_key": symbol},
        },
        separators=(",", ":"),
    )


def is_ack(message: Mapping[str, object]) -> bool:
    header = message.get("header")
    return isinstance(header, Mapping) and ("tr_type" in header or "rsp_cd" in header)


def ack_result(message: Mapping[str, object]) -> tuple[bool, str, str]:
    """ACK의 (정상 여부, rsp_cd, rsp_msg). 토큰 값은 담지 않는다."""

    header = message.get("header")
    if not isinstance(header, Mapping):
        return False, "", ""
    code = str(header.get("rsp_cd") or "")
    text = str(header.get("rsp_msg") or "")
    return (not code or code == WS_ACK_OK), code, text


def _text(body: Mapping[str, object], key: str) -> str | None:
    raw = body.get(key)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _positive_decimal(body: Mapping[str, object], key: str) -> Decimal | None:
    raw = _text(body, key)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _non_negative_int(body: Mapping[str, object], key: str) -> int | None:
    raw = _text(body, key)
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _exchange_time(raw: str | None) -> time | None:
    """``HH:MM:SS`` 또는 ``HHMMSS``. 날짜는 없으므로 수신일을 붙인다."""

    if raw is None:
        return None
    digits = raw.replace(":", "")
    if len(digits) != 6 or not digits.isdigit():
        return None
    try:
        return time(int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError:
        return None


def parse_push(
    message: Mapping[str, object],
    *,
    received_at: datetime,
) -> TradeTick | BookTick | None:
    """KRX ``oc``/``ob`` 푸시를 정규화한다. 필수 필드가 없거나 틀리면 ``None``.

    선택 필드(누적 매수/매도 체결량, VWAP, 잔량)는 없으면 ``None``으로 두고
    값을 발명하지 않는다. 음수·0 가격은 받지 않는다.
    """

    header = message.get("header")
    body = message.get("body")
    if not isinstance(header, Mapping) or not isinstance(body, Mapping):
        return None
    channel = header.get("tr_cd")
    symbol = _text(body, "code") or (
        str(header.get("tr_key")).strip() if header.get("tr_key") else None
    )
    if symbol is None or len(symbol) != 6:
        return None
    if channel == TRADE_CHANNEL:
        exchange_time = _exchange_time(_text(body, "time"))
        price = _positive_decimal(body, "price")
        volume = _non_negative_int(body, "volume")
        if exchange_time is None or price is None or volume is None:
            return None
        return TradeTick(
            symbol=symbol,
            received_at=received_at,
            exchange_time=exchange_time,
            price=price,
            cumulative_volume=volume,
            cumulative_buy_volume=_non_negative_int(body, "bidvolall"),
            cumulative_sell_volume=_non_negative_int(body, "offvolall"),
            session_vwap=_positive_decimal(body, "avgprice"),
        )
    if channel == BOOK_CHANNEL:
        exchange_time = _exchange_time(_text(body, "hotime"))
        if exchange_time is None:
            return None
        return BookTick(
            symbol=symbol,
            received_at=received_at,
            exchange_time=exchange_time,
            best_bid=_positive_decimal(body, "bid"),
            best_ask=_positive_decimal(body, "offer"),
            best_bid_size=_non_negative_int(body, "bidrem"),
            best_ask_size=_non_negative_int(body, "offerrem"),
            total_bid_size=_non_negative_int(body, "T_bidrem"),
            total_ask_size=_non_negative_int(body, "T_offerrem"),
            depth=tuple(
                BookLevel(
                    bid=_positive_decimal(body, f"{prefix}bid"),
                    ask=_positive_decimal(body, f"{prefix}offer"),
                    bid_size=_non_negative_int(body, f"{prefix}bidrem"),
                    ask_size=_non_negative_int(body, f"{prefix}offerrem"),
                )
                for prefix in (
                    "",
                    "P_",
                    "S_",
                    "S4_",
                    "S5_",
                    "S6_",
                    "S7_",
                    "S8_",
                    "S9_",
                    "S10_",
                )
            ),
        )
    return None


__all__ = [
    "BOOK_CHANNEL",
    "CHANNELS",
    "MAX_SESSIONS",
    "MAX_SYMBOLS",
    "SUBSCRIBE_INTERVAL_SECONDS",
    "SYMBOLS_PER_SESSION",
    "TRADE_CHANNEL",
    "WS_URL",
    "ack_result",
    "is_ack",
    "parse_push",
    "subscribe_message",
]
