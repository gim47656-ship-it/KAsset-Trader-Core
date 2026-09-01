"""MCP 읽기 도구의 KR/US 시장 세션 상태 판정.

KRX 정규장이 열리지 않은 시간에는 전일 종가가 실시간 값처럼 보이거나
``change_pct == 0``인 행이 순위에 노출될 수 있다. 이 모듈은 현재 세션 상태를
표시해 오래됐거나 사용할 수 없는 데이터를 최신 데이터로 오인하지 않게 한다.

공유 거래소 캘린더 경계를 사용하므로 휴일·주말과 미국 조기 폐장을 같은
기준으로 처리한다.
"""

from __future__ import annotations

import datetime as _dt
from datetime import time as _time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.market_events.session_calendar import (
    is_trading_session,
    previous_trading_session,
    regular_session_bounds,
)

# MCP 호출자에게 노출하는 data_state 값.
DATA_STATE_FRESH = "fresh"
DATA_STATE_STALE = "stale"
DATA_STATE_PREMARKET_UNAVAILABLE = "premarket_unavailable"
DATA_STATE_MARKET_CLOSED = "market_closed"

US_SESSION_PREMARKET = "premarket"
US_SESSION_REGULAR = "regular"
US_SESSION_AFTERHOURS = "afterhours"
US_SESSION_CLOSED = "closed"

_UTC = ZoneInfo("UTC")
_ET = ZoneInfo("America/New_York")
_US_PRE_OPEN = _time(4, 0)
_US_AFTER_CLOSE = _time(20, 0)


def kr_market_data_state(now: Any = None) -> str:
    """현재 KRX 정규장 세션 상태를 판정한다.

    반환값:

    - ``DATA_STATE_FRESH`` — XKRX 정규장 거래 중.
    - ``DATA_STATE_PREMARKET_UNAVAILABLE`` — XKRX 거래일의 정규장 개장
      전(09:00 KST).
    - ``DATA_STATE_MARKET_CLOSED`` — 장 마감 뒤, 주말 또는 휴일.

    ``now``는 pandas가 해석할 수 있는 시각이며 기본값은 현재 UTC다.
    timezone-naive 입력은 UTC로 간주한다.
    """
    ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now("UTC")
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    current = ts.to_pydatetime().astimezone(_UTC)
    local = current.astimezone(ZoneInfo("Asia/Seoul"))
    bounds = regular_session_bounds("kr", local.date())
    if bounds is None:
        return DATA_STATE_MARKET_CLOSED
    if bounds[0] <= current < bounds[1]:
        return DATA_STATE_FRESH
    if current < bounds[0]:
        return DATA_STATE_PREMARKET_UNAVAILABLE
    return DATA_STATE_MARKET_CLOSED


def us_market_session(now: Any = None) -> str:
    """XNYS 정규장 경계를 기준으로 현재 미국 equity quote 세션을 판정한다.

    04:00 ET부터 XNYS 개장 전까지는 ``premarket``, 정규장 중에는
    ``regular``, XNYS 폐장부터 20:00 ET 전까지는 ``afterhours``다.
    그 밖의 시간과 비거래일은 ``closed``다. timezone-naive 입력은 UTC로
    간주하며 ``regular_session_bounds``의 조기 폐장도 반영한다.
    """
    current = now if now is not None else _dt.datetime.now(_dt.UTC)
    if not isinstance(current, _dt.datetime):
        current = pd.Timestamp(current).to_pydatetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC)

    local = current.astimezone(_ET)
    bounds = regular_session_bounds("us", local.date())
    if bounds is None:
        return US_SESSION_CLOSED

    open_utc, close_utc = bounds
    current_utc = current.astimezone(_UTC)
    if open_utc <= current_utc < close_utc:
        return US_SESSION_REGULAR

    open_local = open_utc.astimezone(_ET)
    close_local = close_utc.astimezone(_ET)
    pre_open = local.replace(hour=_US_PRE_OPEN.hour, minute=0, second=0, microsecond=0)
    after_close = local.replace(
        hour=_US_AFTER_CLOSE.hour, minute=0, second=0, microsecond=0
    )
    if pre_open <= local < open_local:
        return US_SESSION_PREMARKET
    if close_local <= local < after_close:
        return US_SESSION_AFTERHOURS
    return US_SESSION_CLOSED


def is_kr_session_day(date: Any) -> bool:
    """``date``가 KST 기준 XKRX 거래일이면 ``True``를 반환한다."""
    return is_trading_session("kr", pd.Timestamp(date).date())


def previous_kr_session(date: Any) -> _dt.date:
    """``date``보다 앞선 가장 최근 XKRX 거래일을 반환한다.

    ``date``는 KST 달력 날짜이며 그 자체가 거래일일 필요는 없다. 주말이나
    휴일이면 가장 최근 거래일로 되돌아가며, 거래일을 넘겨도 같은 날짜가
    아니라 그 전 거래일을 반환한다. 따라서 연휴와 월요일 경계에서도 항상
    입력 날짜보다 앞선 결과를 보장한다.
    """
    target = pd.Timestamp(date).date()
    session = previous_trading_session("kr", target)
    if session is None:
        raise ValueError(f"could not resolve previous XKRX session before {target}")
    return session
