"""일봉 동기화 오케스트레이터.

주식의 유일한 기본 provider는 Toss다. KR benchmark는 Toss market
indicator를 우선 사용하고 Naver로 보강하며, crypto는 Upbit를 사용한다.
Yahoo는 명시적 adjusted-close 보강 작업에만 남긴다.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.daily_candles.converters import frame_to_rows
from app.services.daily_candles.crypto_identity import upbit_daily_candle_partition
from app.services.daily_candles.read_service import drop_forming_daily_rows
from app.services.daily_candles.readiness import REQUIRED_HISTORY_BARS
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

logger = logging.getLogger(__name__)

# 관심종목 호환 경로는 거래소가 비어 있으면 NASD를 사용한다. 반면 대량 백필은
# DB에 명시된 유효 거래소만 받아 잘못된 파티션으로 쓰지 않는다.
_DEFAULT_US_PARTITION = "NASD"
_US_PARTITION_ALIASES = {
    "US": "NASD",
    "NASDAQ": "NASD",
    "NASDAQ_GS": "NASD",
    "NYQ": "NYSE",
    "NYSEMKT": "AMEX",
}
_VALID_US_PARTITIONS = frozenset({"NASD", "NYSE", "AMEX"})


def _us_partition(exchange: str | None) -> str:
    normalized = (exchange or "").strip().upper()
    if not normalized:
        return _DEFAULT_US_PARTITION
    return _US_PARTITION_ALIASES.get(normalized, normalized)


def _backfill_us_partition(exchange: str | None) -> str | None:
    normalized = str(exchange or "").strip().upper()
    if not normalized:
        return None
    partition = _US_PARTITION_ALIASES.get(normalized, normalized)
    return partition if partition in _VALID_US_PARTITIONS else None


def _closed_equity_frame(
    frame: pd.DataFrame, *, market: str, horizon_bars: int
) -> pd.DataFrame:
    """성공 응답의 형태를 검증하고 완료된 최신 ``horizon_bars``개만 남긴다."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{market} 일봉 fetcher는 DataFrame을 반환해야 합니다")
    if frame.empty:
        return frame
    if "close" not in frame.columns:
        raise ValueError(f"{market} 일봉 DataFrame에 close가 없습니다")
    date_column = "date" if "date" in frame.columns else "datetime"
    if date_column not in frame.columns:
        raise ValueError(f"{market} 일봉 DataFrame에 date/datetime이 없습니다")
    if pd.to_datetime(frame[date_column], errors="coerce").isna().any():
        raise ValueError(f"{market} 일봉 DataFrame에 잘못된 날짜가 있습니다")

    closed = drop_forming_daily_rows(frame, market=market)
    if closed.empty:
        return closed
    return (
        closed.assign(
            __daily_candle_sort_date=pd.to_datetime(closed[date_column], errors="raise")
        )
        .sort_values("__daily_candle_sort_date")
        .tail(horizon_bars)
        .drop(columns="__daily_candle_sort_date")
        .reset_index(drop=True)
    )


@dataclass(frozen=True, slots=True)
class SyncTarget:
    market: MarketKey
    symbol: str
    partition: str  # exchange / venue / market


@dataclass(frozen=True, slots=True)
class SyncOneResult:
    target: SyncTarget
    rows_upserted: int
    fallback_used: bool
    skipped_reason: str | None = None


YahooUsFetcher = Callable[..., Awaitable[list[YahooFallbackRow]]]
UpbitCryptoFetcher = Callable[..., Awaitable[pd.DataFrame]]
TossDailyFetcher = Callable[..., Awaitable[pd.DataFrame]]
TossKrBenchmarkFetcher = Callable[..., Awaitable[pd.DataFrame]]
NaverKrBenchmarkFetcher = Callable[..., Awaitable[pd.DataFrame]]


class DailyCandleSyncService:
    def __init__(
        self,
        *,
        repository: DailyCandlesRepository,
        toss_kr_fetcher: TossDailyFetcher,
        toss_us_fetcher: TossDailyFetcher,
        yahoo_us_fetcher: YahooUsFetcher,
        upbit_crypto_fetcher: UpbitCryptoFetcher,
        toss_kr_benchmark_fetcher: TossKrBenchmarkFetcher | None = None,
        naver_kr_benchmark_fetcher: NaverKrBenchmarkFetcher | None = None,
        close_callbacks: list[Callable[[], object]] | None = None,
    ) -> None:
        self._repository = repository
        self._toss_kr = toss_kr_fetcher
        self._toss_us = toss_us_fetcher
        self._yahoo_us = yahoo_us_fetcher
        self._upbit = upbit_crypto_fetcher
        self._toss_kr_benchmark = toss_kr_benchmark_fetcher
        self._naver_kr_benchmark = naver_kr_benchmark_fetcher
        self._close_callbacks = close_callbacks or []

    async def close(self) -> None:
        """기본 service factory가 소유한 자원을 해제한다."""
        for callback in self._close_callbacks:
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def sync_one(self, *, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        if horizon_bars <= 0:
            raise ValueError("horizon_bars는 양수여야 합니다")
        if target.market == MarketKey.KR:
            return await self._sync_kr(target, horizon_bars)
        if target.market == MarketKey.US:
            return await self._sync_us(target, horizon_bars)
        return await self._sync_crypto(target, horizon_bars)

    async def sync_us_adjusted_close(
        self, *, target: SyncTarget, horizon_bars: int
    ) -> int:
        """완전한 Yahoo adjusted-close backfill slice만 저장한다."""
        if target.market != MarketKey.US:
            raise ValueError("Yahoo adjusted-close 보강은 US 대상만 지원합니다")
        if target.partition not in _VALID_US_PARTITIONS:
            raise ValueError(f"지원하지 않는 미국 일봉 파티션: {target.partition!r}")
        if horizon_bars <= 0:
            raise ValueError("horizon_bars는 양수여야 합니다")

        yahoo_rows = await self._yahoo_us(symbol=target.symbol, n=horizon_bars)
        selected = yahoo_rows[-horizon_bars:]
        required_bars = min(horizon_bars, REQUIRED_HISTORY_BARS)
        required_slice = selected[-required_bars:]
        required_times = {row.time_utc for row in required_slice}
        if (
            len(required_slice) != required_bars
            or len(required_times) != required_bars
            or any(row.adj_close is None for row in required_slice)
        ):
            raise RuntimeError(
                "Yahoo adjusted-close 보강이 완전하지 않습니다: "
                f"symbol={target.symbol} requested={horizon_bars} "
                f"required={required_bars} received={len(selected)} "
                f"adjusted="
                f"{sum(row.adj_close is not None for row in required_slice)}"
            )

        unique_valid = {
            row.time_utc: row for row in selected if row.adj_close is not None
        }
        rows = [
            DailyCandleRow(
                time_utc=row.time_utc,
                symbol=target.symbol,
                partition=target.partition,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                adj_close=row.adj_close,
                volume=row.volume,
                value=row.value,
                source="yahoo_fallback",
            )
            for _time, row in sorted(unique_valid.items())
        ]
        upserted = await self._repository.upsert_us_adjusted_close(rows=rows)
        await self._commit_or_rollback()
        return upserted

    async def rollback(self) -> None:
        """다음 심볼을 시작하기 전에 실패한 심볼의 트랜잭션을 폐기한다."""

        await self._repository.session.rollback()

    async def sync_benchmark(
        self,
        *,
        market: MarketKey,
        horizon_bars: int,
        symbol: str | None = None,
    ) -> SyncOneResult:
        """후보 유니버스와 분리된 시장별 벤치마크를 동기화한다."""
        normalized_symbol = str(symbol or "").strip().upper()
        if market == MarketKey.US:
            if normalized_symbol and normalized_symbol != "SPY":
                raise ValueError(f"지원하지 않는 US 벤치마크입니다: {symbol!r}")
            return await self.sync_one(
                target=SyncTarget(
                    market=MarketKey.US,
                    symbol="SPY",
                    partition=_DEFAULT_US_PARTITION,
                ),
                horizon_bars=horizon_bars,
            )
        if market != MarketKey.KR:
            raise ValueError(f"일봉 벤치마크가 없는 시장입니다: {market}")
        if normalized_symbol not in {"", "KOSPI", "KOSDAQ"}:
            raise ValueError(f"지원하지 않는 KR 벤치마크입니다: {symbol!r}")
        if self._toss_kr_benchmark is None and self._naver_kr_benchmark is None:
            raise RuntimeError("KR 벤치마크 fetcher가 설정되지 않았습니다")

        target = SyncTarget(
            market=MarketKey.KR,
            symbol=normalized_symbol or "KOSPI",
            partition="KRX",
        )
        fallback_used = self._toss_kr_benchmark is None
        source = "toss_index"
        frame: pd.DataFrame | None = None
        if self._toss_kr_benchmark is not None:
            try:
                frame = await self._toss_kr_benchmark(
                    symbol=target.symbol,
                    n=horizon_bars,
                )
            except Exception as exc:
                if self._naver_kr_benchmark is None:
                    raise
                fallback_used = True
                logger.warning(
                    "Toss %s 일봉 조회 실패, Naver 대체 경로 사용: %s",
                    target.symbol,
                    exc,
                )
            else:
                frame = _closed_equity_frame(
                    frame,
                    market=MarketKey.KR.value,
                    horizon_bars=horizon_bars,
                )
                if len(frame) < horizon_bars:
                    if self._naver_kr_benchmark is None:
                        return SyncOneResult(
                            target=target,
                            rows_upserted=0,
                            fallback_used=False,
                            skipped_reason="toss_index_history_short",
                        )
                    fallback_used = True
                    frame = None
                    logger.warning(
                        "Toss %s 완료 일봉 수 부족, Naver 대체 경로 사용 requested=%d",
                        target.symbol,
                        horizon_bars,
                    )

        if frame is None:
            if self._naver_kr_benchmark is None:
                raise RuntimeError("Naver KR 벤치마크 fetcher가 설정되지 않았습니다")
            source = "naver"
            frame = await self._naver_kr_benchmark(
                symbol=target.symbol,
                n=horizon_bars,
            )
            frame = _closed_equity_frame(
                frame,
                market=MarketKey.KR.value,
                horizon_bars=horizon_bars,
            )
        rows = frame_to_rows(
            frame,
            symbol=target.symbol,
            partition=target.partition,
            source=source,
        )
        if len(rows) < horizon_bars:
            return SyncOneResult(
                target=target,
                rows_upserted=0,
                fallback_used=fallback_used,
                skipped_reason=f"{source}_history_short",
            )
        upserted = await self._repository.upsert_rows(market=target.market, rows=rows)
        await self._commit_or_rollback()
        return SyncOneResult(
            target=target,
            rows_upserted=upserted,
            fallback_used=fallback_used,
        )

    async def _sync_kr(self, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        frame = await self._toss_kr(symbol=target.symbol, n=horizon_bars + 1)
        frame = _closed_equity_frame(
            frame,
            market=MarketKey.KR.value,
            horizon_bars=horizon_bars,
        )
        rows = frame_to_rows(
            frame,
            symbol=target.symbol,
            partition=target.partition,
            source="toss",
        )
        if not rows:
            return SyncOneResult(
                target=target,
                rows_upserted=0,
                fallback_used=False,
                skipped_reason="toss_empty",
            )
        upserted = await self._repository.upsert_rows(market=target.market, rows=rows)
        await self._commit_or_rollback()
        return SyncOneResult(
            target=target,
            rows_upserted=upserted,
            fallback_used=False,
        )

    async def _sync_us(self, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        if target.partition not in _VALID_US_PARTITIONS:
            raise ValueError(f"지원하지 않는 미국 일봉 파티션: {target.partition!r}")
        frame = await self._toss_us(symbol=target.symbol, n=horizon_bars + 1)
        frame = _closed_equity_frame(
            frame,
            market=MarketKey.US.value,
            horizon_bars=horizon_bars,
        )
        rows = frame_to_rows(
            frame,
            symbol=target.symbol,
            partition=target.partition,
            source="toss",
        )
        if not rows:
            return SyncOneResult(
                target=target,
                rows_upserted=0,
                fallback_used=False,
                skipped_reason="toss_empty",
            )
        upserted = await self._repository.upsert_rows(
            market=target.market,
            rows=rows,
            update_adj_close=False,
        )
        await self._commit_or_rollback()
        return SyncOneResult(
            target=target,
            rows_upserted=upserted,
            fallback_used=False,
        )

    async def _sync_crypto(
        self, target: SyncTarget, horizon_bars: int
    ) -> SyncOneResult:
        canonical_partition = upbit_daily_candle_partition(target.symbol)
        if target.partition != canonical_partition:
            raise ValueError(
                "Crypto daily-candle target partition must match its canonical "
                f"Upbit symbol identity: symbol={target.symbol!r}, "
                f"partition={target.partition!r}, expected={canonical_partition!r}"
            )
        frame = await self._upbit(market=target.symbol, days=horizon_bars)
        rows = frame_to_rows(
            frame, symbol=target.symbol, partition=target.partition, source="upbit"
        )
        upserted = await self._repository.upsert_rows(market=target.market, rows=rows)
        await self._commit_or_rollback()
        return SyncOneResult(target=target, rows_upserted=upserted, fallback_used=False)

    async def resolve_backfill_targets(
        self,
        *,
        market: str,
        resume_after: str | None = None,
        limit: int | None = None,
    ) -> list[SyncTarget]:
        """대량 백필에 허용된 현재 보통주만 읽어 결정적 순서로 반환한다."""

        from sqlalchemy import text

        if limit is not None and limit <= 0:
            raise ValueError("limit은 양수여야 합니다")

        session = self._repository.session
        if market == MarketKey.KR.value:
            result = await session.execute(
                text(
                    """
                    SELECT symbol, exchange
                    FROM public.kr_symbol_universe
                    WHERE is_active IS TRUE
                      AND security_type = 'STOCK'
                      AND is_common_share IS TRUE
                      AND COALESCE(krx_trading_suspended, FALSE) = FALSE
                      AND delist_date IS NULL
                      AND LOWER(COALESCE(listing_status, ''))
                          NOT IN ('delisted', '상장폐지')
                    ORDER BY exchange, symbol
                    """
                )
            )
            targets = [
                SyncTarget(
                    market=MarketKey.KR,
                    symbol=str(row.symbol).strip().upper(),
                    partition="KRX",
                )
                for row in result
                if str(row.symbol).strip()
            ]
        elif market == MarketKey.US.value:
            result = await session.execute(
                text(
                    """
                    SELECT symbol, exchange
                    FROM public.us_symbol_universe
                    WHERE is_active IS TRUE
                      AND is_common_stock IS TRUE
                    ORDER BY exchange, symbol
                    """
                )
            )
            targets = []
            invalid_exchanges: list[tuple[str, object]] = []
            for row in result:
                symbol = str(row.symbol).strip().upper()
                partition = _backfill_us_partition(row.exchange)
                if not symbol:
                    continue
                if partition is None:
                    invalid_exchanges.append((symbol, row.exchange))
                    continue
                targets.append(
                    SyncTarget(
                        market=MarketKey.US,
                        symbol=symbol,
                        partition=partition,
                    )
                )
            if invalid_exchanges:
                logger.warning(
                    "유효하지 않은 exchange로 US 백필에서 제외 count=%d symbols=%s",
                    len(invalid_exchanges),
                    [symbol for symbol, _exchange in invalid_exchanges],
                )
        else:
            raise ValueError("--all은 kr과 us에서만 지원합니다")

        targets.sort(
            key=lambda target: (
                target.market.value,
                target.partition,
                target.symbol,
            )
        )
        if resume_after is not None:
            resume_symbol = str(resume_after).strip().upper()
            resume_index = next(
                (
                    index
                    for index, target in enumerate(targets)
                    if target.symbol == resume_symbol
                ),
                None,
            )
            if resume_index is None:
                raise ValueError(
                    f"--resume-after 심볼이 적격 유니버스에 없습니다: {resume_after!r}"
                )
            targets = targets[resume_index + 1 :]
        if limit is not None:
            targets = targets[:limit]
        return targets

    async def resolve_cohort_backfill_targets(
        self,
        *,
        market: str,
        cohort_id: str,
    ) -> list[SyncTarget]:
        """Resolve the exact active+forced membership of one research cohort."""

        from sqlalchemy import text

        if market not in {MarketKey.KR.value, MarketKey.US.value}:
            raise ValueError("--cohort-id는 kr과 us에서만 지원합니다")
        normalized_id = str(cohort_id).strip()
        if not normalized_id:
            raise ValueError("--cohort-id는 비어 있을 수 없습니다")

        session = self._repository.session
        cohort_result = await session.execute(
            text(
                """
                SELECT market
                FROM public.kasset_research_cohorts
                WHERE cohort_id = :cohort_id
                """
            ),
            {"cohort_id": normalized_id},
        )
        cohort_rows = list(cohort_result)
        if not cohort_rows:
            raise ValueError(f"코호트를 찾을 수 없습니다: {normalized_id!r}")
        cohort_market = str(cohort_rows[0].market).strip().lower()
        if cohort_market != market:
            raise ValueError(
                "코호트 market이 CLI market과 일치하지 않습니다: "
                f"cohort_id={normalized_id!r} cohort_market={cohort_market!r} "
                f"requested_market={market!r}"
            )

        universe_table = (
            "public.kr_symbol_universe"
            if market == MarketKey.KR.value
            else "public.us_symbol_universe"
        )
        member_result = await session.execute(
            text(
                f"""
                SELECT member.symbol, member.rank, member.member_kind,
                       universe.exchange
                FROM public.kasset_research_cohort_members AS member
                LEFT JOIN {universe_table} AS universe
                  ON universe.symbol = member.symbol
                WHERE member.cohort_id = :cohort_id
                  AND member.member_kind IN ('active', 'forced')
                ORDER BY member.rank, member.symbol
                """
            ),
            {"cohort_id": normalized_id},
        )
        market_key = MarketKey(market)
        targets: list[SyncTarget] = []
        for row in member_result:
            symbol = str(row.symbol).strip().upper()
            if not symbol:
                raise RuntimeError(
                    f"코호트에 빈 심볼이 있습니다: cohort_id={normalized_id!r}"
                )
            if market_key == MarketKey.KR:
                if row.exchange is None:
                    raise RuntimeError(
                        "코호트 심볼을 KR universe에서 찾을 수 없습니다: "
                        f"cohort_id={normalized_id!r} symbol={symbol!r}"
                    )
                partition = "KRX"
            else:
                partition = _backfill_us_partition(row.exchange)
                if partition is None:
                    raise RuntimeError(
                        "코호트 심볼의 US universe exchange가 유효하지 않습니다: "
                        f"cohort_id={normalized_id!r} symbol={symbol!r} "
                        f"exchange={row.exchange!r}"
                    )
            targets.append(
                SyncTarget(
                    market=market_key,
                    symbol=symbol,
                    partition=partition,
                )
            )
        if not targets:
            raise RuntimeError(
                f"코호트에 active/forced 멤버가 없습니다: {normalized_id!r}"
            )
        return targets

    async def resolve_top_market_cap_targets(
        self,
        *,
        market: str,
        count: int,
    ) -> list[SyncTarget]:
        """최신 시총 파티션에서 적격 보통주 상위 ``count``개를 결정한다."""

        from sqlalchemy import text

        if market not in {MarketKey.KR.value, MarketKey.US.value}:
            raise ValueError("--top-market-cap은 kr과 us에서만 지원합니다")
        if count <= 0:
            raise ValueError("count는 양수여야 합니다")
        market_key = MarketKey(market)

        if market == MarketKey.KR.value:
            query = text(
                """
                WITH latest_partition AS (
                    SELECT MAX(snapshot_date) AS snapshot_date
                    FROM public.market_valuation_snapshots
                    WHERE market = :market
                ),
                ranked AS (
                    SELECT valuation.symbol, MAX(valuation.market_cap) AS market_cap
                    FROM public.market_valuation_snapshots AS valuation
                    JOIN latest_partition AS latest
                      ON latest.snapshot_date = valuation.snapshot_date
                    JOIN public.kr_symbol_universe AS universe
                      ON universe.symbol = valuation.symbol
                    WHERE valuation.market = :market
                      AND valuation.market_cap > 0
                      AND universe.is_active IS TRUE
                      AND universe.security_type = 'STOCK'
                      AND universe.is_common_share IS TRUE
                      AND COALESCE(
                          universe.krx_trading_suspended, FALSE
                      ) = FALSE
                      AND universe.delist_date IS NULL
                      AND LOWER(COALESCE(universe.listing_status, ''))
                          NOT IN ('delisted', '상장폐지')
                    GROUP BY valuation.symbol
                )
                SELECT symbol, 'KRX' AS exchange
                FROM ranked
                ORDER BY market_cap DESC, symbol ASC
                LIMIT :count
                """
            )
        else:
            query = text(
                """
                WITH latest_partition AS (
                    SELECT MAX(snapshot_date) AS snapshot_date
                    FROM public.market_valuation_snapshots
                    WHERE market = :market
                ),
                ranked AS (
                    SELECT
                        valuation.symbol,
                        universe.exchange,
                        MAX(valuation.market_cap) AS market_cap
                    FROM public.market_valuation_snapshots AS valuation
                    JOIN latest_partition AS latest
                      ON latest.snapshot_date = valuation.snapshot_date
                    JOIN public.us_symbol_universe AS universe
                      ON universe.symbol = valuation.symbol
                    WHERE valuation.market = :market
                      AND valuation.market_cap > 0
                      AND universe.is_active IS TRUE
                      AND universe.is_common_stock IS TRUE
                      AND UPPER(TRIM(COALESCE(universe.exchange, ''))) IN (
                          'NASD', 'NASDAQ', 'NASDAQ_GS', 'US',
                          'NYSE', 'NYQ', 'AMEX', 'NYSEMKT'
                      )
                    GROUP BY valuation.symbol, universe.exchange
                )
                SELECT symbol, exchange
                FROM ranked
                ORDER BY market_cap DESC, symbol ASC
                LIMIT :count
                """
            )

        result = await self._repository.session.execute(
            query,
            {"market": market, "count": count},
        )
        rows = list(result)
        targets = [
            SyncTarget(
                market=market_key,
                symbol=str(row.symbol).strip().upper(),
                partition=(
                    "KRX"
                    if market == MarketKey.KR.value
                    else _backfill_us_partition(row.exchange) or ""
                ),
            )
            for row in rows
            if str(row.symbol).strip()
        ]
        allowed_partitions = _VALID_US_PARTITIONS | {"KRX"}
        if len(targets) != count or any(
            target.partition not in allowed_partitions for target in targets
        ):
            raise RuntimeError(
                "최신 시총 파티션이 적격 종목 요청 수를 충족하지 못했습니다: "
                f"market={market} requested={count} received={len(targets)}"
            )
        return targets

    async def sync_market_universe(
        self, *, market: str, horizon_bars: int
    ) -> dict[str, Any]:
        """Run sync_one for every active (symbol, partition) pair in the market.

        Target universe rules:
        - kr/us: active universe plus active watch items and research cohorts.
        - crypto: active KRW rows from upbit_symbol_universe.
        """
        targets = await self._resolve_universe(market=market)
        rows_total = 0
        fallback_count = 0
        skipped = 0
        for target in targets:
            result = await self.sync_one(target=target, horizon_bars=horizon_bars)
            rows_total += result.rows_upserted
            if result.fallback_used:
                fallback_count += 1
            if result.skipped_reason:
                skipped += 1
        return {
            "market": market,
            "targets_total": len(targets),
            "rows_upserted": rows_total,
            "fallback_count": fallback_count,
            "skipped": skipped,
        }

    _COHORT_MEMBER_SQL = """
        SELECT DISTINCT member.symbol AS symbol, universe.exchange AS exchange
        FROM public.kasset_research_cohort_members AS member
        JOIN public.kasset_research_cohorts AS cohort
          ON cohort.cohort_id = member.cohort_id
        LEFT JOIN public.{universe_table} AS universe
          ON universe.symbol = member.symbol
        WHERE cohort.market = :market
    """

    async def _resolve_universe(self, *, market: str) -> list[SyncTarget]:
        """Return list of (market, symbol, partition) targets to sync.

        Wires to the existing universe services. Implementation reads
        the per-market universe tables directly via the repository's
        session (we get it from the repository which already holds an
        AsyncSession).

        Active watchlist symbols are unioned in. Without them a symbol a user
        adds is never synced, so its stored daily candles stay empty and the
        app shows no previousClose and no chart for it (observed 2026-08-28:
        ``kr_symbol_universe`` was empty, so this job ran with zero targets
        every day while three manually seeded symbols were the only stored
        candles). Universe-table rows win on partition when a symbol appears
        in both sources.

        Research cohort members are unioned in for the same reason: readiness
        and PAPER promotion are measured against the immutable cohort, so a
        member that leaves the active universe (suspended, delisted, master
        refresh) must keep being synced or its cohort row goes stale forever
        and the promotion gate can never close again.
        """
        from sqlalchemy import text

        session = self._repository.session

        if market == "kr":
            targets: dict[str, str] = {}
            result = await session.execute(
                text(
                    "SELECT symbol FROM public.kr_symbol_universe"
                    " WHERE is_active = TRUE"
                )
            )
            for row in result:
                targets[row.symbol] = "KRX"
            result = await session.execute(
                text(
                    "SELECT DISTINCT i.symbol AS symbol"
                    " FROM public.user_watch_items w"
                    " JOIN public.instruments i ON i.id = w.instrument_id"
                    " WHERE w.is_active = TRUE AND i.type = 'equity_kr'"
                )
            )
            for row in result:
                targets.setdefault(row.symbol, "KRX")
            result = await session.execute(
                text(
                    self._COHORT_MEMBER_SQL.format(universe_table="kr_symbol_universe")
                ),
                {"market": market},
            )
            for row in result:
                symbol = str(row.symbol).strip().upper()
                if symbol:
                    targets.setdefault(symbol, "KRX")
            return [
                SyncTarget(market=MarketKey.KR, symbol=symbol, partition=partition)
                for symbol, partition in sorted(targets.items())
            ]
        if market == "us":
            targets = {}
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_symbol_universe"
                    " WHERE is_active = TRUE"
                )
            )
            for row in result:
                targets[row.symbol] = row.exchange
            result = await session.execute(
                text(
                    "SELECT DISTINCT i.symbol AS symbol, e.code AS exchange"
                    " FROM public.user_watch_items w"
                    " JOIN public.instruments i ON i.id = w.instrument_id"
                    " LEFT JOIN public.exchanges e ON e.id = i.exchange_id"
                    " WHERE w.is_active = TRUE AND i.type = 'equity_us'"
                )
            )
            for row in result:
                targets.setdefault(row.symbol, _us_partition(row.exchange))
            result = await session.execute(
                text(
                    self._COHORT_MEMBER_SQL.format(universe_table="us_symbol_universe")
                ),
                {"market": market},
            )
            unresolved: list[str] = []
            for row in result:
                symbol = str(row.symbol).strip().upper()
                if not symbol or symbol in targets:
                    continue
                # Never guess a partition for a cohort member: writing a symbol
                # into the wrong exchange partition corrupts its daily history.
                partition = _backfill_us_partition(row.exchange)
                if partition is None:
                    unresolved.append(symbol)
                    continue
                targets[symbol] = partition
            if unresolved:
                logger.warning(
                    "코호트 멤버의 US exchange를 확인할 수 없어 일봉 동기화에서 "
                    "제외 count=%d symbols=%s",
                    len(unresolved),
                    unresolved,
                )
            return [
                SyncTarget(market=MarketKey.US, symbol=symbol, partition=partition)
                for symbol, partition in sorted(targets.items())
            ]
        if market == "crypto":
            sql = text(
                "SELECT market FROM public.upbit_symbol_universe"
                " WHERE is_active = TRUE AND quote_currency = 'KRW'"
                " ORDER BY market"
            )
            result = await session.execute(sql)
            return [
                SyncTarget(
                    market=MarketKey.CRYPTO,
                    symbol=row.market,
                    partition=upbit_daily_candle_partition(row.market),
                )
                for row in result
            ]
        raise ValueError(f"Unknown market: {market}")

    async def _commit_or_rollback(self) -> None:
        """Commit the repository session after a successful upsert; rollback on error.

        The session is owned by the repository and exposed via its public
        ``session`` property.
        """
        session = self._repository.session
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _build_default_service() -> DailyCandleSyncService:
    """소유 자원을 포함한 Toss-primary 일봉 서비스를 만든다."""
    import app.services.brokers.upbit.client as upbit_service
    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.benchmark_fetcher import fetch_kr_benchmark_daily
    from app.services.daily_candles.toss_daily_fetcher import (
        fetch_daily_toss_unclamped,
    )
    from app.services.daily_candles.yahoo_us_fallback import (
        fetch_us_daily_yahoo_fallback,
    )
    from app.services.market_data.toss_ohlcv import (
        fetch_kr_index_daily_toss_frame,
    )

    session = AsyncSessionLocal()

    async def _yahoo(*, symbol: str, n: int) -> list[YahooFallbackRow]:
        return await fetch_us_daily_yahoo_fallback(symbol=symbol, n=n)

    async def _upbit(*, market: str, days: int) -> pd.DataFrame:
        return await upbit_service.fetch_ohlcv(
            market=market,
            days=days,
            period="day",
        )

    async def _toss(*, symbol: str, n: int) -> pd.DataFrame:
        return await fetch_daily_toss_unclamped(symbol=symbol, n=n)

    async def _toss_kr_benchmark(*, symbol: str, n: int) -> pd.DataFrame:
        return await fetch_kr_index_daily_toss_frame(
            symbol=symbol,
            count=n,
        )

    async def _naver_kr_benchmark(*, symbol: str, n: int) -> pd.DataFrame:
        return await fetch_kr_benchmark_daily(symbol=symbol, n=n)

    return DailyCandleSyncService(
        repository=DailyCandlesRepository(session=session),
        toss_kr_fetcher=_toss,
        toss_us_fetcher=_toss,
        yahoo_us_fetcher=_yahoo,
        upbit_crypto_fetcher=_upbit,
        toss_kr_benchmark_fetcher=_toss_kr_benchmark,
        naver_kr_benchmark_fetcher=_naver_kr_benchmark,
        close_callbacks=[session.close],
    )
