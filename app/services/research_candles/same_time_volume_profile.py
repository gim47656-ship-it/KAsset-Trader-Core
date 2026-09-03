from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, time, timedelta

from sqlalchemy import (
    Integer,
    Text,
    Time,
    cast,
    column,
    extract,
    func,
    literal_column,
    select,
    values,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.intraday_triggers import SameTimeVolumeBaseline
from app.models.kr_candles_1m_toss import KRTossMinuteCandle

_KRX_REGULAR = "KRX_REGULAR"
_KST_ZONE = literal_column("'Asia/Seoul'")
_FIVE_MINUTES = literal_column("INTERVAL '5 minutes'")


async def load_same_time_bucket_volumes(
    session: AsyncSession,
    *,
    requests: Mapping[str, Sequence[time]],
    before_session_date: date,
    lookback_days: int,
) -> dict[str, list[SameTimeVolumeBaseline]]:
    """최근 거래일의 동일 5분 bucket 거래량 합계를 종목별로 불러온다.

    종목마다 요청한 bucket만 합산한다. 해당 날짜와 bucket에 실거래 행이 하나도
    없고 모두 padding이면 표본에서 제외하지만, 실거래 행이 존재하는 거래량 0
    표본은 유지한다.
    """

    if not requests or lookback_days < 1:
        return {}

    requested_pairs = tuple(
        (symbol, bucket_start)
        for symbol, bucket_starts in requests.items()
        for bucket_start in dict.fromkeys(bucket_starts)
    )
    if not requested_pairs:
        return {}

    baselines = {symbol: [] for symbol in requests}
    requested_buckets = values(
        column("symbol", Text),
        column("bucket_start_kst", Time),
        name="requested_buckets",
    ).data(requested_pairs)

    kst_timestamp = KRTossMinuteCandle.time_utc.op("AT TIME ZONE")(_KST_ZONE)
    bucket_start_kst = cast(
        func.date_trunc("hour", kst_timestamp)
        + cast(func.floor(extract("minute", kst_timestamp) / 5), Integer)
        * _FIVE_MINUTES,
        Time,
    )

    # 거래일이 아닌 달력일 기준으로 먼저 물리 범위를 잘라 날짜 인덱스를 탄다.
    # 주말·휴일 여유를 위해 lookback_days * 2 + 15일을 조회한 뒤 종목별로 자른다.
    earliest_session_date = before_session_date - timedelta(days=lookback_days * 2 + 15)
    statement = (
        select(
            KRTossMinuteCandle.symbol,
            KRTossMinuteCandle.session_date_kst.label("session_date"),
            func.sum(KRTossMinuteCandle.volume).label("volume"),
        )
        .join(
            requested_buckets,
            (KRTossMinuteCandle.symbol == requested_buckets.c.symbol)
            & (bucket_start_kst == requested_buckets.c.bucket_start_kst),
        )
        .where(
            KRTossMinuteCandle.session_date_kst < before_session_date,
            KRTossMinuteCandle.session_date_kst >= earliest_session_date,
            KRTossMinuteCandle.session_segment == _KRX_REGULAR,
        )
        .group_by(
            KRTossMinuteCandle.symbol,
            KRTossMinuteCandle.session_date_kst,
        )
        .having(func.bool_or(KRTossMinuteCandle.is_padding.is_(False)))
        .order_by(
            KRTossMinuteCandle.symbol.asc(),
            KRTossMinuteCandle.session_date_kst.asc(),
        )
    )

    result = await session.execute(statement)
    for symbol, session_date, volume in result.all():
        baselines[symbol].append(
            SameTimeVolumeBaseline(session_date=session_date, volume=volume)
        )

    for symbol, symbol_baselines in baselines.items():
        symbol_baselines.sort(key=lambda baseline: baseline.session_date)
        baselines[symbol] = symbol_baselines[-lookback_days:]
    return baselines


__all__ = ["load_same_time_bucket_volumes"]
