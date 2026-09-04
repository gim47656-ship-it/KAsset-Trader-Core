"""Read-only benchmark-relative-strength inputs for the candidate ranker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.kasset.automation.candidate_ranker import (
    BenchmarkReturn,
    CandidateKey,
)
from app.extensions.kasset.automation.contracts import PriceBar
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.daily_candles.constants import (
    US_BENCHMARK_PARTITION,
    US_BENCHMARK_SYMBOL,
)
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.market_data.constants import KR_BENCHMARK_SYMBOL_BY_EXCHANGE

BENCHMARK_SESSIONS: Final = 60
_BENCHMARK_BAR_COUNT: Final = BENCHMARK_SESSIONS + 1
_US_BENCHMARK: Final = US_BENCHMARK_SYMBOL


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("benchmark close must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("benchmark close must be finite and positive")
    return result


def benchmark_symbol_for_exchange(exchange: str | None) -> str | None:
    """Map a KR universe exchange label to its own broad-market benchmark."""

    normalized = str(exchange or "").strip().upper()
    return KR_BENCHMARK_SYMBOL_BY_EXCHANGE.get(normalized)


def compute_benchmark_return_60_from_bars(
    bars: Sequence[PriceBar],
    *,
    market: str,
    benchmark_symbol: str,
    as_of: datetime,
    maximum_age: timedelta,
) -> BenchmarkReturn | None:
    """Return a completed 60-session benchmark return, otherwise fail closed."""

    current = _aware_utc(as_of)
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")

    normalized: list[tuple[datetime, Decimal]] = []
    seen: set[datetime] = set()
    for bar in bars:
        try:
            timestamp = _aware_utc(bar.timestamp)
            close = _decimal(bar.close)
        except ValueError:
            return None
        if timestamp > current or timestamp in seen:
            return None
        seen.add(timestamp)
        normalized.append((timestamp, close))
    normalized.sort(key=lambda item: item[0])

    if len(normalized) < _BENCHMARK_BAR_COUNT:
        return None
    window = normalized[-_BENCHMARK_BAR_COUNT:]
    data_as_of = window[-1][0]
    if current - data_as_of >= maximum_age:
        return None
    return BenchmarkReturn(
        market=market,
        return_60=(window[-1][1] / window[0][1]) - Decimal("1"),
        data_as_of=data_as_of,
        benchmark_symbol=benchmark_symbol,
    )


def compute_benchmark_return_60(
    rows: Sequence[DailyCandleRow],
    *,
    market: str,
    benchmark_symbol: str,
    as_of: datetime,
    maximum_age: timedelta,
) -> BenchmarkReturn | None:
    """Adapt durable candle rows to the shared pure 60-session calculation."""

    bars: list[PriceBar] = []
    for row in rows:
        if str(row.symbol).strip().upper() != benchmark_symbol:
            return None
        try:
            close = _decimal(row.adj_close if row.adj_close is not None else row.close)
        except ValueError:
            return None
        bars.append(
            PriceBar(
                timestamp=row.time_utc,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("0"),
            )
        )
    return compute_benchmark_return_60_from_bars(
        bars,
        market=market,
        benchmark_symbol=benchmark_symbol,
        as_of=as_of,
        maximum_age=maximum_age,
    )


async def load_candidate_benchmark_returns(
    db: AsyncSession,
    candidates: Sequence[CandidateKey],
    *,
    as_of: datetime,
    maximum_age: timedelta,
) -> dict[CandidateKey, BenchmarkReturn]:
    """Load candidate-specific KOSPI/KOSDAQ/SPY returns without external I/O."""

    candidate_keys = tuple(dict.fromkeys(candidates))
    kr_symbols = sorted(
        str(symbol).strip().upper()
        for market, symbol in candidate_keys
        if market == "KR"
    )
    exchanges: Mapping[str, str] = {}
    if kr_symbols:
        exchanges = {
            str(symbol).strip().upper(): str(exchange).strip().upper()
            for symbol, exchange in (
                await db.execute(
                    select(KRSymbolUniverse.symbol, KRSymbolUniverse.exchange).where(
                        KRSymbolUniverse.symbol.in_(kr_symbols)
                    )
                )
            ).all()
        }

    benchmark_for_candidate: dict[CandidateKey, str] = {}
    for key in candidate_keys:
        market, symbol = key
        if market == "US":
            benchmark_for_candidate[key] = _US_BENCHMARK
            continue
        benchmark_symbol = benchmark_symbol_for_exchange(
            exchanges.get(str(symbol).strip().upper())
        )
        if benchmark_symbol is not None:
            benchmark_for_candidate[key] = benchmark_symbol

    repository = DailyCandlesRepository(session=db)
    rows_by_benchmark: dict[str, Sequence[DailyCandleRow]] = {}
    kr_benchmarks = sorted(
        {value for key, value in benchmark_for_candidate.items() if key[0] == "KR"}
    )
    if kr_benchmarks:
        rows_by_benchmark.update(
            await repository.fetch_recent_batch(
                market=MarketKey.KR,
                symbols=kr_benchmarks,
                partition="KRX",
                count=_BENCHMARK_BAR_COUNT,
            )
        )
    if any(key[0] == "US" for key in benchmark_for_candidate):
        rows_by_benchmark.update(
            await repository.fetch_recent_batch(
                market=MarketKey.US,
                symbols=[_US_BENCHMARK],
                # sync_benchmark가 쓰는 파티션만 읽는다. partition=None이면 유니버스
                # 경로가 남긴 다른 거래소 행이 같은 날짜로 겹쳐 fail-closed된다.
                partition=US_BENCHMARK_PARTITION,
                count=_BENCHMARK_BAR_COUNT,
            )
        )

    returns_by_symbol: dict[str, BenchmarkReturn] = {}
    for benchmark_symbol, rows in rows_by_benchmark.items():
        market = "US" if benchmark_symbol == _US_BENCHMARK else "KR"
        result = compute_benchmark_return_60(
            rows,
            market=market,
            benchmark_symbol=benchmark_symbol,
            as_of=as_of,
            maximum_age=maximum_age,
        )
        if result is not None:
            returns_by_symbol[benchmark_symbol] = result

    return {
        key: returns_by_symbol[benchmark_symbol]
        for key, benchmark_symbol in benchmark_for_candidate.items()
        if benchmark_symbol in returns_by_symbol
    }


__all__ = [
    "BENCHMARK_SESSIONS",
    "benchmark_symbol_for_exchange",
    "compute_benchmark_return_60",
    "compute_benchmark_return_60_from_bars",
    "load_candidate_benchmark_returns",
]
