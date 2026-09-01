"""완료 bar 판정을 위한 정규장 세션 경계.

Daily Setup과 장중 Trigger는 둘 다 "이 bar가 정말 닫혔는가"를 같은 기준으로
물어야 한다. 그 기준을 여기 한 곳에만 두고, 달력은 이미 있는 공용
:mod:`app.services.market_events.session_calendar`만 쓴다. 새 달력이나 두 번째
휴장일 표를 만들지 않는다.

일봉 timestamp 규약은 공급자마다 다르다. KIS 경로는 세션 날짜를 UTC 자정으로
적고 Toss 경로는 세션 날짜를 시장 현지 자정으로 적는다. 두 규약이 공유하는
성질은 하나다: timestamp는 자기 세션의 종료 시각보다 앞에 있다. 그래서 완료
판정은 날짜 산술이 아니라 "가장 최근에 닫힌 세션의 종료 시각" 하나로 한다.
timestamp가 그 시각보다 앞이면 그 bar의 세션은 이미 닫혀 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.extensions.kasset.automation.contracts import PriceBar
from app.services.market_events.session_calendar import (
    Market,
    regular_session_bounds,
    trading_sessions_in_range,
)

#: 자동화가 쓰는 시장 라벨과 공용 달력 키의 대응.
CALENDAR_MARKET: Final[dict[str, Market]] = {
    "KRX": "kr",
    "KR": "kr",
    "US": "us",
}

#: 세션을 찾을 때 ``as_of`` 앞뒤로 훑는 최대 일수. 한국의 연휴 묶음과 공급자
#: timestamp 규약 차이(±1일)를 모두 덮으면서 탐색을 유한하게 유지한다.
_SESSION_SEARCH_DAYS: Final = 12


class MarketSessionError(ValueError):
    """지원하지 않는 시장 라벨이거나 시간대 없는 timestamp."""


@dataclass(frozen=True, slots=True)
class RegularSession:
    """정규장 한 세션의 UTC 경계."""

    market: Market
    session_date: date
    opens_at: datetime
    closes_at: datetime

    def contains(self, moment: datetime) -> bool:
        return self.opens_at <= moment <= self.closes_at


def calendar_market(market: str) -> Market:
    """``KRX``/``KR``/``US`` 라벨을 공용 달력 키로 좁힌다."""

    resolved = CALENDAR_MARKET.get(str(market).strip().upper())
    if resolved is None:
        raise MarketSessionError(
            f"unsupported market for the session calendar: {market!r}"
        )
    return resolved


def aware_utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MarketSessionError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _sessions_around(market: Market, moment: datetime) -> tuple[RegularSession, ...]:
    """``moment`` 주변의 확정된 정규장 세션을 시간순으로 모은다."""

    day = moment.date()
    sessions: list[RegularSession] = []
    for session_date in trading_sessions_in_range(
        market,
        day - timedelta(days=_SESSION_SEARCH_DAYS),
        day + timedelta(days=_SESSION_SEARCH_DAYS),
    ):
        bounds = regular_session_bounds(market, session_date)
        if bounds is None:
            continue
        opens_at, closes_at = bounds
        sessions.append(
            RegularSession(
                market=market,
                session_date=session_date,
                opens_at=opens_at.astimezone(UTC),
                closes_at=closes_at.astimezone(UTC),
            )
        )
    sessions.sort(key=lambda item: item.opens_at)
    return tuple(sessions)


def latest_completed_session(market: str, as_of: datetime) -> RegularSession | None:
    """``as_of`` 기준으로 가장 최근에 닫힌 정규장 세션.

    달력이 세션을 확정하지 못하면 ``None``을 돌려 호출자가 fail-closed하게 만든다.
    """

    resolved_market = calendar_market(market)
    moment = aware_utc(as_of, "as_of")
    completed = [
        session
        for session in _sessions_around(resolved_market, moment)
        if session.closes_at <= moment
    ]
    return completed[-1] if completed else None


def current_regular_session(market: str, as_of: datetime) -> RegularSession | None:
    """``as_of`` 시점에 열려 있는 정규장 세션. 장외면 ``None``."""

    resolved_market = calendar_market(market)
    moment = aware_utc(as_of, "as_of")
    for session in _sessions_around(resolved_market, moment):
        if session.contains(moment):
            return session
    return None


def completed_bar_cutoff(market: str, as_of: datetime) -> datetime | None:
    """이 시각보다 앞선 timestamp의 bar는 닫힌 세션에 속한다."""

    session = latest_completed_session(market, as_of)
    return session.closes_at if session is not None else None


def completed_daily_bars(
    bars: Sequence[PriceBar],
    *,
    market: str,
    as_of: datetime,
    cutoff: datetime | None = None,
) -> tuple[PriceBar, ...]:
    """완료된 세션의 일봉만, timestamp 오름차순으로 남긴다.

    진행 중인 세션의 부분 일봉과 시간대 없는 bar는 조용히 값을 섞지 않고
    제외한다. Daily Setup은 이 결과만 본다. ``cutoff``를 미리 계산해 넘기면
    같은 cycle의 여러 후보가 달력 조회를 한 번만 한다.
    """

    resolved_cutoff = (
        cutoff if cutoff is not None else completed_bar_cutoff(market, as_of)
    )
    if resolved_cutoff is None:
        return ()
    boundary = aware_utc(resolved_cutoff, "cutoff")
    retained: list[PriceBar] = []
    for bar in bars:
        try:
            timestamp = aware_utc(bar.timestamp, "bar timestamp")
        except MarketSessionError:
            continue
        if timestamp < boundary:
            retained.append(bar)
    retained.sort(key=lambda bar: bar.timestamp)
    return tuple(retained)


__all__ = [
    "CALENDAR_MARKET",
    "MarketSessionError",
    "RegularSession",
    "aware_utc",
    "calendar_market",
    "completed_bar_cutoff",
    "completed_daily_bars",
    "current_regular_session",
    "latest_completed_session",
]
