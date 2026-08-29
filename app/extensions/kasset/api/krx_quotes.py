"""KRX 시세 우선순위 해석: 토스 실시간 → NH 공용 채널 → 저장 일봉.

단일 조회(`GET /api/v1/market/quote`)와 배치 조회
(`GET /api/v1/market/quotes`)가 같은 우선순위를 공유한다. 앞 단계가 실패하면
조용히 다음 단계로 강등하며, 어떤 단계도 서버 현재 시각을 시세 시각으로
위조하지 않는다. `source`와 `asOf`가 그 응답이 실제로 어느 채널에서 언제
나온 값인지 알려준다.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.nh_adapter import nh_market_data
from app.extensions.kasset.api.paper import decimal_text, iso_z, paper_account_adapter
from app.extensions.kasset.api.paper_schemas import Quote
from app.extensions.kasset.api.schemas import MarketSessionState
from app.extensions.kasset.api.toss_market_data import (
    TOSS_QUOTE_SOURCE,
    TossQuotePoint,
    toss_market_data,
)
from app.extensions.kasset.automation.market_pipeline import _market_route
from app.models.trading import Instrument
from app.services.brokers.toss.market_calendar import (
    TossSessionWindow,
    get_kr_toss_session_from_toss,
    get_latest_completed_regular_window_from_toss,
    get_us_toss_session_from_toss,
)
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
)
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.nxt_preflight import NxtTradability

logger = logging.getLogger(__name__)

MAX_BATCH_SYMBOLS = 50
CANDLE_QUOTE_SOURCE = "PAPER_CANDLES"

_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")
_KRX_MARKETS = frozenset({"KRX", "KR"})
# 미국 종목은 토스가 티커를 그대로 받는다(`GET /api/v1/prices`). 앱은 관심종목
# 와이어 값 `US`를 보내고, 거래소 표기로 들어오는 경우도 같은 경로로 받는다.
_US_MARKETS = frozenset({"US", "NASDAQ", "NYSE", "AMEX"})
_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_KST = ZoneInfo("Asia/Seoul")
# 미국 거래일 경계. 정규장이 KST 자정을 넘으므로 KST 날짜로 판정하면 안 된다.
_ET = ZoneInfo("America/New_York")
# previousClose 판정에 필요한 최소 행 수는 2다(당일 + 직전 거래일). 여유 1행을
# 더 읽어 같은 날짜가 중복 저장된 경우에도 직전 거래일을 찾는다.
_CANDLE_LOOKBACK_ROWS = 3
# NH 공용 채널은 전 종목 공용으로 호출 간격 0.45초가 강제된다. 배치 전체가
# 그 속도에 묶이면 15초 폴링이 무의미해지므로, 예산을 넘긴 종목은 저장 일봉으로
# 강등한다. 토스가 살아 있으면 이 경로는 거의 쓰이지 않는다.
_NH_BATCH_BUDGET_SECONDS = 2.5


def normalize_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized in _KRX_MARKETS:
        return "KRX"
    if normalized in _US_MARKETS:
        return "US"
    raise MobileApiError(422, "VALIDATION_ERROR", "지원하지 않는 시장입니다.")


def normalize_symbols(symbols: str, *, market: str = "KRX") -> list[str]:
    """`symbols` 쿼리를 시장별 계약대로 정규화한다(1..50개, 중복 제거)."""

    wire_market = _wire_market(market)
    pattern = _US_SYMBOL_RE if wire_market == "US" else _KRX_SYMBOL_RE
    error_message = (
        "미국 종목코드만 조회할 수 있습니다."
        if wire_market == "US"
        else "KRX 6자리 종목코드만 조회할 수 있습니다."
    )
    normalized: list[str] = []
    for part in symbols.split(","):
        candidate = part.strip().upper()
        if not candidate:
            continue
        if pattern.fullmatch(candidate) is None:
            raise MobileApiError(422, "VALIDATION_ERROR", error_message)
        if candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise MobileApiError(
            422, "VALIDATION_ERROR", "조회할 종목 코드를 입력해 주세요."
        )
    if len(normalized) > MAX_BATCH_SYMBOLS:
        raise MobileApiError(
            422,
            "VALIDATION_ERROR",
            f"한 번에 최대 {MAX_BATCH_SYMBOLS}종목까지 조회할 수 있습니다.",
        )
    return normalized


def supports_market(market: str) -> bool:
    """이 모듈이 시세를 해석할 수 있는 시장인지 알려준다."""
    normalized = market.strip().upper()
    return normalized in _KRX_MARKETS or normalized in _US_MARKETS


def _wire_market(market: str) -> str:
    """응답 `market` 표기. 앱은 요청한 시장으로 행을 묶으므로 계약을 지킨다."""
    normalized = market.strip().upper()
    return "US" if normalized in _US_MARKETS else "KRX"


def _market_trading_date(market: str, value: datetime) -> date:
    """시장 기준 거래일. 미국은 ET, 국내는 KST 날짜다.

    토스는 미국 일봉을 ET 자정(= `13:00+09:00`)으로 라벨하므로 라벨의 KST
    날짜가 곧 미국 거래일이다. 반대로 미국 시세 시각을 KST 날짜로 읽으면
    정규장(22:30~05:00 KST)이 자정을 넘는 순간 하루 앞서간다.
    """
    zone = _ET if _wire_market(market) == "US" else _KST
    return value.astimezone(zone).date()


async def resolve_market_session_state(
    market: str, *, moment: datetime | None = None
) -> MarketSessionState | None:
    """Toss calendar의 현재 구간을 Android 와이어 상태로 바꾼다."""

    current = moment or datetime.now(UTC)
    if _wire_market(market) == "US":
        session = await get_us_toss_session_from_toss(current)
        return {
            "day": "DAY_MARKET",
            "pre": "PRE_MARKET",
            "regular": "REGULAR",
            "post": "AFTER_MARKET",
            "closed": "CLOSED",
            None: None,
        }[session]
    session = await get_kr_toss_session_from_toss(current)
    return {
        "nxt_premarket": "PRE_MARKET",
        "regular": "REGULAR",
        "nxt_after": "AFTER_MARKET",
        "closed": "CLOSED",
        None: None,
    }[session]


def _symbol_session_state(
    market: str,
    market_state: MarketSessionState | None,
    tradability: NxtTradability | None,
    *,
    moment: datetime,
) -> MarketSessionState | None:
    """KR NXT 전용 구간에서 종목별 참여 가능 여부를 반영한다."""

    if _wire_market(market) != "KRX" or market_state not in {
        "PRE_MARKET",
        "AFTER_MARKET",
    }:
        return market_state
    if tradability is None or tradability.is_stale(now=moment):
        return None
    return market_state if tradability.nxt_tradable else "CLOSED"


async def _quote_session_context(
    db: AsyncSession, *, market: str, symbols: Sequence[str]
) -> tuple[
    dict[str, MarketSessionState | None],
    TossSessionWindow | None,
]:
    moment = datetime.now(UTC)
    market_state = await resolve_market_session_state(market, moment=moment)
    tradability: dict[str, NxtTradability] = {}
    if _wire_market(market) == "KRX" and market_state in {
        "PRE_MARKET",
        "AFTER_MARKET",
    }:
        try:
            tradability = await get_kr_nxt_tradability(list(symbols), db=db)
        except Exception as exc:  # noqa: BLE001 — 종목별 세션은 모르면 null이다
            logger.warning(
                "kasset quote NXT tradability unavailable (%s): session omitted",
                type(exc).__name__,
            )
    states = {
        symbol: _symbol_session_state(
            market,
            market_state,
            tradability.get(symbol),
            moment=moment,
        )
        for symbol in symbols
    }
    regular_window = None
    if market_state is not None and market_state != "REGULAR":
        regular_window = await get_latest_completed_regular_window_from_toss(
            "us" if _wire_market(market) == "US" else "kr",
            moment,
        )
    return states, regular_window


async def quote_for_market(db: AsyncSession, *, market: str, symbol: str) -> Quote:
    """PAPER 시세의 단일 진입점.

    표시 경로(`/market/quote`)와 주문 기준가(`paper_orders`)가 서로 다른 시세를
    쓰면 화면 가격과 체결 기준가가 갈라진다. 실측 결함(2026-08-28): 표시는
    토스 실시간(TQQQ 73.06)이었는데 주문 미리보기는 Yahoo 하루 지연값
    (73.30000305175781)을 기준가로 썼고, 저장 일봉이 없는 KRX 종목은 주문
    미리보기가 `NOT_FOUND`로 실패했다. 두 경로가 이 함수만 쓰게 해서 계약을
    한곳에 고정한다.
    """
    if supports_market(market):
        return await resolve_quote(db, market=market, symbol=symbol)
    return await paper_account_adapter.quote(db, market=market, symbol=symbol)


async def resolve_quote(db: AsyncSession, *, market: str, symbol: str) -> Quote:
    """단일 시세. 토스 → NH 공용 → PAPER 저장 캔들 순서로 강등한다.

    미국 종목은 NH 경로가 없으므로 토스 실패 시 곧바로 PAPER로 내려간다.
    """
    normalized = symbol.strip().upper()
    wire_market = _wire_market(market)
    sessions, regular_window = await _quote_session_context(
        db, market=market, symbols=[normalized]
    )
    session = sessions.get(normalized)
    point = (await _toss_points(market, [normalized])).get(normalized)
    if point is not None:
        rows = (await _candle_rows(db, market, [normalized])).get(normalized, ())
        names = await _instrument_names(db, [normalized])
        regular_close = await _regular_closes(
            {normalized: point}, window=regular_window
        )
        known_previous_close = (
            regular_close if session in {"DAY_MARKET", "PRE_MARKET"} else {}
        )
        fallback = await _previous_close_fallback(
            market,
            {normalized: point},
            {normalized: rows},
            known=known_previous_close,
        )
        return _toss_quote(
            point,
            market=wire_market,
            name=names.get(normalized),
            rows=rows,
            previous_close_fallback=fallback.get(normalized),
            session=session,
            regular_close=regular_close.get(normalized),
        )
    if wire_market == "KRX":
        shared = await _nh_quote(market=market, symbol=normalized)
        if shared is not None:
            return shared.model_copy(update={"session": session})
    fallback_quote = await paper_account_adapter.quote(
        db, market=market, symbol=normalized
    )
    return fallback_quote.model_copy(update={"session": session})


async def resolve_quotes(
    db: AsyncSession, *, market: str, symbols: Sequence[str]
) -> list[Quote]:
    requested = list(symbols)
    wire_market = _wire_market(market)
    sessions, regular_window = await _quote_session_context(
        db, market=market, symbols=requested
    )
    points = await _toss_points(market, requested)
    candles = await _candle_rows(db, market, requested)
    names = await _instrument_names(db, requested)
    regular_closes = await _regular_closes(points, window=regular_window)
    known_previous_closes = {
        symbol: close
        for symbol, close in regular_closes.items()
        if sessions.get(symbol) in {"DAY_MARKET", "PRE_MARKET"}
    }
    fallback = await _previous_close_fallback(
        market,
        points,
        candles,
        known=known_previous_closes,
    )

    quotes: list[Quote] = []
    nh_deadline = time.monotonic() + _NH_BATCH_BUDGET_SECONDS
    for symbol in requested:
        name = names.get(symbol)
        rows = candles.get(symbol, ())
        point = points.get(symbol)
        session = sessions.get(symbol)
        if point is not None:
            quotes.append(
                _toss_quote(
                    point,
                    market=wire_market,
                    name=name,
                    rows=rows,
                    previous_close_fallback=fallback.get(symbol),
                    session=session,
                    regular_close=regular_closes.get(symbol),
                )
            )
            continue
        if wire_market == "KRX" and time.monotonic() < nh_deadline:
            shared = await _nh_quote(market=market, symbol=symbol)
            if shared is not None:
                quotes.append(shared.model_copy(update={"session": session}))
                continue
        candle_quote = _candle_quote(
            symbol,
            market=wire_market,
            name=name,
            rows=rows,
            session=session,
        )
        if candle_quote is not None:
            quotes.append(candle_quote)
    return quotes


async def _toss_points(
    market: str, symbols: Sequence[str]
) -> dict[str, TossQuotePoint]:
    pattern = _US_SYMBOL_RE if _wire_market(market) == "US" else _KRX_SYMBOL_RE
    accepted = [symbol for symbol in symbols if pattern.fullmatch(symbol) is not None]
    if not accepted:
        return {}
    return await toss_market_data.prices(accepted)


async def _previous_close_fallback(
    market: str,
    points: dict[str, TossQuotePoint],
    candles: dict[str, Sequence[DailyCandleRow]] | dict[str, list[DailyCandleRow]],
    *,
    known: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """저장 일봉으로 전일 종가를 못 구한 종목만 토스 일봉으로 메운다.

    저장 일봉 유니버스는 관심종목 전체를 담고 있지 않아, 새로 추가한 종목은
    등락률이 계속 `null`이었다. 저장 값이 있으면 그것을 그대로 쓰고 이 경로는
    호출하지 않는다.

    기준일은 **시장의 거래일**이다. 미국을 KST 날짜로 넘기면 정규장이 자정을
    넘는 순간 진행 중인 당일 봉이 전일 종가로 잡힌다.
    """
    resolved = dict(known or {})
    missing: dict[date, list[str]] = {}
    for symbol, point in points.items():
        if symbol in resolved:
            continue
        rows = candles.get(symbol) or ()
        if _previous_close(rows, market=market, before=point.as_of) is not None:
            continue
        boundary = _market_trading_date(market, point.as_of)
        missing.setdefault(boundary, []).append(symbol)
    for boundary, symbols in missing.items():
        resolved.update(
            await toss_market_data.previous_closes(symbols, boundary=boundary)
        )
    return resolved


async def _regular_closes(
    points: dict[str, TossQuotePoint], *, window: TossSessionWindow | None
) -> dict[str, Decimal]:
    if not points or window is None:
        return {}
    return await toss_market_data.regular_closes(list(points), window=window)


async def _nh_quote(*, market: str, symbol: str) -> Quote | None:
    # 시세는 계좌 연동과 무관한 공용 데이터다. 서버 공용 NH PLUG 채널이
    # 응답하면 브로커 표기만 PAPER로 바꿔 그대로 쓴다.
    try:
        shared = await nh_market_data.quote(market=market, symbol=symbol)
    except MobileApiError:
        return None
    return shared.model_copy(update={"broker": "PAPER"})


def _toss_quote(
    point: TossQuotePoint,
    *,
    market: str,
    name: str | None,
    rows: Sequence[DailyCandleRow],
    previous_close_fallback: Decimal | None = None,
    session: MarketSessionState | None,
    regular_close: Decimal | None,
) -> Quote:
    previous_close = _previous_close(rows, market=market, before=point.as_of)
    if previous_close is None:
        previous_close = previous_close_fallback
    # US 데이마켓은 ET 전날 저녁에 열리지만 다음 거래일 구간이다. 시세
    # timestamp의 ET 날짜로 일봉 경계를 잡으면 하루 전 종가를 하나 더
    # 건너뛰므로, calendar가 증명한 직전 정규장 종가를 previousClose로 쓴다.
    if session == "DAY_MARKET":
        previous_close = regular_close
    return build_quote(
        market=market,
        symbol=point.symbol,
        name=name,
        # 통화는 시장이 결정한다. 공급자 필드를 그대로 믿으면 수수료 자산군이
        # (`equity_kr` / `equity_us`) 잘못 키잉될 수 있다. `_candle_quote`와
        # 같은 규칙을 쓴다.
        currency="USD" if market == "US" else "KRW",
        price=point.price,
        previous_close=previous_close,
        session=session,
        regular_close=regular_close,
        as_of=point.as_of,
        source=TOSS_QUOTE_SOURCE,
    )


def _candle_quote(
    symbol: str,
    *,
    market: str,
    name: str | None,
    rows: Sequence[DailyCandleRow],
    session: MarketSessionState | None,
) -> Quote | None:
    if not rows:
        return None
    latest = rows[-1]
    price = _decimal(latest.close)
    if price is None:
        return None
    as_of = _aware(latest.time_utc)
    return build_quote(
        market=market,
        symbol=symbol,
        name=name,
        currency="USD" if market == "US" else "KRW",
        price=price,
        previous_close=_previous_close(rows, market=market, before=as_of),
        session=session,
        regular_close=None,
        as_of=as_of,
        source=CANDLE_QUOTE_SOURCE,
    )


def build_quote(
    *,
    market: str,
    symbol: str,
    name: str | None,
    currency: str,
    price: Decimal,
    previous_close: Decimal | None,
    as_of: datetime,
    source: str,
    session: MarketSessionState | None = None,
    regular_close: Decimal | None = None,
) -> Quote:
    """REST와 스트림이 공유하는 정규장/현재 세션 등락 생성점."""

    if session == "REGULAR":
        regular_close = None
    regular_basis = regular_close if regular_close is not None else price
    change_amount = (
        regular_basis - previous_close if previous_close is not None else None
    )
    rate = change_rate(change_amount, previous_close)
    session_change_amount = price - regular_close if regular_close is not None else None
    session_rate = change_rate(session_change_amount, regular_close)
    return Quote(
        broker="PAPER",
        market=market,
        symbol=symbol,
        name=name,
        currency=currency,
        price=decimal_text(price),
        previous_close=(
            decimal_text(previous_close) if previous_close is not None else None
        ),
        change_amount=(
            decimal_text(change_amount) if change_amount is not None else None
        ),
        change_rate=decimal_text(rate) if rate is not None else None,
        session=session,
        regular_close=(
            decimal_text(regular_close) if regular_close is not None else None
        ),
        session_change_amount=(
            decimal_text(session_change_amount)
            if session_change_amount is not None
            else None
        ),
        session_change_rate=(
            decimal_text(session_rate) if session_rate is not None else None
        ),
        as_of=iso_z(as_of),
        source=source,
    )


def change_rate(
    change_amount: Decimal | None, previous_close: Decimal | None
) -> Decimal | None:
    if change_amount is None or previous_close is None or previous_close == 0:
        return None
    rate = change_amount / previous_close * Decimal(100)
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _previous_close(
    rows: Sequence[DailyCandleRow], *, market: str, before: datetime
) -> Decimal | None:
    """`before` 거래일 직전 거래일의 저장 종가. 없으면 `None`.

    당일 종가만 저장된 종목은 직전 거래일 값이 없으므로 `None`을 준다. 당일
    종가를 previousClose로 재사용해 등락을 0으로 만들지 않는다.

    거래일 판정은 토스 폴백과 같은 시장 기준(미국 ET / 국내 KST)이다. 저장
    일봉도 미국 봉을 ET 자정으로 라벨하므로 두 경로의 경계가 일치한다.
    """
    boundary = _market_trading_date(market, before)
    for row in reversed(rows):
        if _market_trading_date(market, _aware(row.time_utc)) >= boundary:
            continue
        return _decimal(row.close)
    return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decimal(value: object) -> Decimal | None:
    """저장 일봉 값(float)을 시세 문자열로 쓸 수 있는 Decimal로 바꾼다.

    `float` 저장값은 `250000.0`처럼 소수점 꼬리를 남긴다. KRX 가격은 정수라
    정수값이면 정수 표기로 정규화해 `"250000"`으로 내려보낸다.
    """
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 — 저장 값 이상은 폴백 사유일 뿐이다
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    integral = parsed.to_integral_value()
    return integral if parsed == integral else parsed


async def _candle_rows(
    db: AsyncSession, market: str, symbols: Sequence[str]
) -> dict[str, list[DailyCandleRow]]:
    if not symbols:
        return {}
    try:
        candle_market, partition, _recommendation_market = _market_route(market)
    except ValueError:
        return {}
    try:
        return await DailyCandlesRepository(session=db).fetch_recent_batch(
            market=candle_market,
            symbols=list(symbols),
            partition=partition,
            count=_CANDLE_LOOKBACK_ROWS,
        )
    except Exception as exc:  # noqa: BLE001 — previousClose는 없으면 null이다
        logger.warning(
            "kasset quote candle read failed (%s): previousClose omitted",
            type(exc).__name__,
        )
        return {}


async def _instrument_names(db: AsyncSession, symbols: Sequence[str]) -> dict[str, str]:
    if not symbols:
        return {}
    try:
        result = await db.execute(
            select(Instrument.symbol, Instrument.name).where(
                Instrument.symbol.in_(set(symbols))
            )
        )
        return {symbol: name for symbol, name in result.all() if name}
    except Exception as exc:  # noqa: BLE001 — 종목명은 없으면 null이다
        logger.warning(
            "kasset krx quote name read failed (%s): name omitted",
            type(exc).__name__,
        )
        return {}
