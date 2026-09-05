"""Android 종목 상세용 저장 스냅샷 projection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.api import krx_quotes
from app.extensions.kasset.api.errors import MobileApiError
from app.extensions.kasset.api.paper import decimal_text, iso_z
from app.extensions.kasset.api.schemas import (
    DailyCandlesResponse,
    FxRateResponse,
    InvestorFlowResponse,
    MarketSummaryResponse,
)
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.exchange_rate_service import get_usd_krw_rate_details
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)


def _one_symbol(market: str, symbol: str) -> tuple[str, str]:
    normalized_market = krx_quotes.normalize_market(market)
    normalized_symbol = krx_quotes.normalize_symbols(
        symbol,
        market=normalized_market,
    )
    if len(normalized_symbol) != 1:
        raise MobileApiError(
            422, "VALIDATION_ERROR", "종목 코드를 하나만 입력해 주세요."
        )
    return normalized_market, normalized_symbol[0]


def _wire_decimal(value: object | None) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return decimal_text(number)


def _candle_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def get_market_summary(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    intraday: DailyCandlesResponse,
) -> MarketSummaryResponse:
    """1분봉 당일 집계와 저장된 일봉·상세 스냅샷을 합친다."""

    normalized_market, normalized_symbol = _one_symbol(market, symbol)
    market_key = MarketKey.KR if normalized_market == "KRX" else MarketKey.US
    partition = "KRX" if normalized_market == "KRX" else "NASD"
    daily_rows = await DailyCandlesRepository(session=db).fetch_recent(
        market=market_key,
        symbol=normalized_symbol,
        partition=partition,
        count=3,
    )
    valuation_rows = await MarketValuationSnapshotsRepository(db).latest_for_symbols(
        market=market_key.value,
        symbols={normalized_symbol},
    )
    flow_rows = (
        await InvestorFlowSnapshotsRepository(db).latest_by_symbols(
            market="kr",
            symbols=[normalized_symbol],
        )
        if normalized_market == "KRX"
        else []
    )

    candles = sorted(intraday.candles, key=lambda candle: _candle_time(candle.time))
    current_date = (
        krx_quotes._market_trading_date(
            normalized_market,
            _candle_time(candles[-1].time),
        )
        if candles
        else None
    )
    previous_row = next(
        (
            row
            for row in reversed(daily_rows)
            if current_date is None
            or krx_quotes._market_trading_date(normalized_market, row.time_utc)
            < current_date
        ),
        None,
    )
    current_daily_row = next(
        (
            row
            for row in reversed(daily_rows)
            if current_date is not None
            and krx_quotes._market_trading_date(normalized_market, row.time_utc)
            == current_date
        ),
        None,
    )

    candle_open: str | None = None
    candle_high: str | None = None
    candle_low: str | None = None
    candle_volume: str | None = None
    volume_change_rate: str | None = None
    as_of: str | None = None
    source: str | None = None
    if candles:
        opens = [Decimal(candle.open) for candle in candles]
        highs = [Decimal(candle.high) for candle in candles]
        lows = [Decimal(candle.low) for candle in candles]
        volumes = [Decimal(candle.volume) for candle in candles]
        total_volume = sum(volumes, start=Decimal("0"))
        candle_open = decimal_text(opens[0])
        candle_high = decimal_text(max(highs))
        candle_low = decimal_text(min(lows))
        candle_volume = decimal_text(total_volume)
        as_of = iso_z(_candle_time(candles[-1].time))
        source = "TOSS_1M"
        if previous_row is not None:
            previous_volume = Decimal(str(previous_row.volume))
            if previous_volume > 0:
                change = (
                    (total_volume - previous_volume) / previous_volume * Decimal("100")
                )
                volume_change_rate = decimal_text(
                    change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                )
    elif daily_rows:
        as_of = iso_z(daily_rows[-1].time_utc)
        source = daily_rows[-1].source

    valuation = valuation_rows[0] if valuation_rows else None
    flow = flow_rows[0] if flow_rows else None
    trade_value = (
        _wire_decimal(current_daily_row.value)
        if current_daily_row is not None and Decimal(str(current_daily_row.value)) > 0
        else None
    )
    return MarketSummaryResponse(
        market=normalized_market,
        symbol=normalized_symbol,
        as_of=as_of,
        source=source,
        open=candle_open,
        high=candle_high,
        low=candle_low,
        prev_close=(
            _wire_decimal(previous_row.close) if previous_row is not None else None
        ),
        volume=candle_volume,
        trade_value=trade_value,
        volume_change_rate=volume_change_rate,
        high_52w=_wire_decimal(valuation.high_52w) if valuation else None,
        low_52w=_wire_decimal(valuation.low_52w) if valuation else None,
        market_cap=_wire_decimal(valuation.market_cap) if valuation else None,
        per=_wire_decimal(valuation.per) if valuation else None,
        pbr=_wire_decimal(valuation.pbr) if valuation else None,
        roe=_wire_decimal(valuation.roe) if valuation else None,
        dividend_yield=(_wire_decimal(valuation.dividend_yield) if valuation else None),
        foreign_holding_rate=(
            _wire_decimal(flow.foreign_holding_rate) if flow else None
        ),
    )


async def get_investor_flow(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
) -> InvestorFlowResponse:
    normalized_market, normalized_symbol = _one_symbol(market, symbol)
    row = None
    if normalized_market == "KRX":
        rows = await InvestorFlowSnapshotsRepository(db).latest_by_symbols(
            market="kr",
            symbols=[normalized_symbol],
        )
        row = rows[0] if rows else None
    return InvestorFlowResponse(
        symbol=normalized_symbol,
        as_of=row.snapshot_date.isoformat() if row else None,
        individual_net=_wire_decimal(row.individual_net) if row else None,
        foreign_net=_wire_decimal(row.foreign_net) if row else None,
        institution_net=_wire_decimal(row.institution_net) if row else None,
        unit="SHARES",
    )


async def get_fx_rate(*, pair: str, now: datetime | None = None) -> FxRateResponse:
    normalized_pair = pair.strip().upper()
    if normalized_pair != "USD-KRW":
        raise ValueError("unsupported pair")
    quote = await get_usd_krw_rate_details()
    moment = now or datetime.now(UTC)
    valid_until = quote.valid_until
    return FxRateResponse(
        pair="USD-KRW",
        rate=decimal_text(quote.default_rate_decimal),
        source=quote.source,
        as_of=iso_z(quote.valid_from) if quote.valid_from is not None else None,
        valid_until=iso_z(valid_until) if valid_until is not None else None,
        stale=valid_until is not None and valid_until <= moment,
    )
