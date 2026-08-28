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
from app.extensions.kasset.api.toss_market_data import (
    TOSS_QUOTE_SOURCE,
    TossQuotePoint,
    toss_market_data,
)
from app.models.trading import Instrument
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)

logger = logging.getLogger(__name__)

MAX_BATCH_SYMBOLS = 50
CANDLE_QUOTE_SOURCE = "PAPER_CANDLES"

_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")
_KRX_MARKETS = frozenset({"KRX", "KR"})
_KST = ZoneInfo("Asia/Seoul")
# previousClose 판정에 필요한 최소 행 수는 2다(당일 + 직전 거래일). 여유 1행을
# 더 읽어 같은 날짜가 중복 저장된 경우에도 직전 거래일을 찾는다.
_CANDLE_LOOKBACK_ROWS = 3
# NH 공용 채널은 전 종목 공용으로 호출 간격 0.45초가 강제된다. 배치 전체가
# 그 속도에 묶이면 15초 폴링이 무의미해지므로, 예산을 넘긴 종목은 저장 일봉으로
# 강등한다. 토스가 살아 있으면 이 경로는 거의 쓰이지 않는다.
_NH_BATCH_BUDGET_SECONDS = 2.5


def normalize_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized not in _KRX_MARKETS:
        raise MobileApiError(422, "VALIDATION_ERROR", "지원하지 않는 시장입니다.")
    return normalized


def normalize_symbols(symbols: str) -> list[str]:
    """`symbols` 쿼리를 계약대로 정규화한다(1..50개, 중복 제거, 6자리 KRX)."""
    normalized: list[str] = []
    for part in symbols.split(","):
        candidate = part.strip().upper()
        if not candidate:
            continue
        if _KRX_SYMBOL_RE.fullmatch(candidate) is None:
            raise MobileApiError(
                422,
                "VALIDATION_ERROR",
                "KRX 6자리 종목코드만 조회할 수 있습니다.",
            )
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


async def resolve_quote(db: AsyncSession, *, market: str, symbol: str) -> Quote:
    """KRX 단일 시세. 토스 → NH 공용 → PAPER 저장 캔들 순서로 강등한다."""
    normalized = symbol.strip().upper()
    point = (await _toss_points([normalized])).get(normalized)
    if point is not None:
        rows = (await _candle_rows(db, [normalized])).get(normalized, ())
        names = await _instrument_names(db, [normalized])
        return _toss_quote(point, name=names.get(normalized), rows=rows)
    shared = await _nh_quote(market=market, symbol=normalized)
    if shared is not None:
        return shared
    return await paper_account_adapter.quote(db, market=market, symbol=normalized)


async def resolve_quotes(
    db: AsyncSession, *, market: str, symbols: Sequence[str]
) -> list[Quote]:
    """KRX 배치 시세. 한 번의 토스 호출 + 한 번의 일봉 조회로 구성한다."""
    requested = list(symbols)
    points = await _toss_points(requested)
    candles = await _candle_rows(db, requested)
    names = await _instrument_names(db, requested)

    quotes: list[Quote] = []
    nh_deadline = time.monotonic() + _NH_BATCH_BUDGET_SECONDS
    for symbol in requested:
        name = names.get(symbol)
        rows = candles.get(symbol, ())
        point = points.get(symbol)
        if point is not None:
            quotes.append(_toss_quote(point, name=name, rows=rows))
            continue
        if time.monotonic() < nh_deadline:
            shared = await _nh_quote(market=market, symbol=symbol)
            if shared is not None:
                quotes.append(shared)
                continue
        candle_quote = _candle_quote(symbol, name=name, rows=rows)
        if candle_quote is not None:
            quotes.append(candle_quote)
    return quotes


async def _toss_points(symbols: Sequence[str]) -> dict[str, TossQuotePoint]:
    krx_symbols = [
        symbol for symbol in symbols if _KRX_SYMBOL_RE.fullmatch(symbol) is not None
    ]
    if not krx_symbols:
        return {}
    return await toss_market_data.prices(krx_symbols)


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
    name: str | None,
    rows: Sequence[DailyCandleRow],
) -> Quote:
    previous_close = _previous_close(rows, before=point.as_of)
    return _quote(
        symbol=point.symbol,
        name=name,
        currency=point.currency,
        price=point.price,
        previous_close=previous_close,
        as_of=point.as_of,
        source=TOSS_QUOTE_SOURCE,
    )


def _candle_quote(
    symbol: str,
    *,
    name: str | None,
    rows: Sequence[DailyCandleRow],
) -> Quote | None:
    if not rows:
        return None
    latest = rows[-1]
    price = _decimal(latest.close)
    if price is None:
        return None
    as_of = _aware(latest.time_utc)
    return _quote(
        symbol=symbol,
        name=name,
        currency="KRW",
        price=price,
        previous_close=_previous_close(rows, before=as_of),
        as_of=as_of,
        source=CANDLE_QUOTE_SOURCE,
    )


def _quote(
    *,
    symbol: str,
    name: str | None,
    currency: str,
    price: Decimal,
    previous_close: Decimal | None,
    as_of: datetime,
    source: str,
) -> Quote:
    change_amount = price - previous_close if previous_close is not None else None
    change_rate = _change_rate(change_amount, previous_close)
    return Quote(
        broker="PAPER",
        market="KRX",
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
        change_rate=decimal_text(change_rate) if change_rate is not None else None,
        as_of=iso_z(as_of),
        source=source,
    )


def _change_rate(
    change_amount: Decimal | None, previous_close: Decimal | None
) -> Decimal | None:
    if change_amount is None or previous_close is None or previous_close == 0:
        return None
    rate = change_amount / previous_close * Decimal(100)
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _previous_close(
    rows: Sequence[DailyCandleRow], *, before: datetime
) -> Decimal | None:
    """`before` 거래일 직전 거래일의 저장 종가. 없으면 `None`.

    당일 종가만 저장된 종목은 직전 거래일 값이 없으므로 `None`을 준다. 당일
    종가를 previousClose로 재사용해 등락을 0으로 만들지 않는다.
    """
    boundary = _trading_date(before)
    for row in reversed(rows):
        if _trading_date(_aware(row.time_utc)) >= boundary:
            continue
        return _decimal(row.close)
    return None


def _trading_date(value: datetime) -> date:
    return value.astimezone(_KST).date()


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
    db: AsyncSession, symbols: Sequence[str]
) -> dict[str, list[DailyCandleRow]]:
    if not symbols:
        return {}
    try:
        return await DailyCandlesRepository(session=db).fetch_recent_batch(
            market=MarketKey.KR,
            symbols=list(symbols),
            partition="KRX",
            count=_CANDLE_LOOKBACK_ROWS,
        )
    except Exception as exc:  # noqa: BLE001 — previousClose는 없으면 null이다
        logger.warning(
            "kasset krx quote candle read failed (%s): previousClose omitted",
            type(exc).__name__,
        )
        return {}


async def _instrument_names(
    db: AsyncSession, symbols: Sequence[str]
) -> dict[str, str]:
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
