"""KRX 정규장 일봉 override에 사용하는 시간·행 계약."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models.kr_candles_1m_toss import TOSS_MINUTE_VALUE_SEMANTICS
from app.services.market_events.session_calendar import (
    is_trading_session,
    previous_trading_session,
)

KST = ZoneInfo("Asia/Seoul")
KR_REGULAR_SOURCE = "toss_regular"
KR_REGULAR_PARTITION = "KRX"
KR_REGULAR_WINDOW_START = time(9, 0)
KR_REGULAR_SEGMENT_END = time(15, 30)
KR_REGULAR_WINDOW_END = time(15, 31)
# 지연 개장과 오전 누락 수집을 구분할 근거가 없으므로 둘 다 fail-closed한다.
KR_REGULAR_FIRST_TRADE_LATEST = time(9, 10)
KR_REGULAR_LAST_TRADE_EARLIEST = time(15, 20)
KR_REGULAR_MIN_TRADE_BARS = 60
KR_REGULAR_VALUE_SEMANTICS = TOSS_MINUTE_VALUE_SEMANTICS


@dataclass(frozen=True, slots=True)
class KrTossMinuteCandle:
    """정규장 일봉 집계에 필요한 Toss 1분봉 필드."""

    time_utc: datetime
    session_date_kst: date
    symbol: str
    session_segment: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal
    value_semantics: str
    is_padding: bool


def latest_completed_kr_session_date(now: datetime) -> date | None:
    """현재 시각에 저장 대상으로 삼을 수 있는 가장 최근 KRX 거래일을 반환한다.

    Toss 15:31 봉에 마감 동시호가 체결이 들어오므로 그 시각 전에는 당일을
    선택하지 않는다. 거래일을 확인할 수 없으면 이전 거래일도 fail-closed로
    탐색하며, 확인 가능한 날짜가 없으면 ``None``을 반환한다.
    """

    now_kst = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    today = now_kst.date()
    clock = now_kst.time().replace(tzinfo=None)
    if clock >= KR_REGULAR_WINDOW_END and is_trading_session("kr", today):
        return today
    return previous_trading_session("kr", today)
