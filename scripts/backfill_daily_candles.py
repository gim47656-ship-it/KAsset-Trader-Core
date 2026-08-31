"""KR/US/crypto 일봉 저장소의 운영자용 초기·재개 백필 CLI.

명시한 심볼은 기존 fail-fast 동작을 유지한다. ``--all``은 DB에서 현재 백필
가능한 보통주만 읽고 심볼 단위로 커밋하며, 한 심볼 실패가 다음 심볼을 막지 않는다.

Examples:
    uv run python scripts/backfill_daily_candles.py \
        --market us --symbols AAPL,MSFT,NVDA --horizon-bars 500

    uv run python scripts/backfill_daily_candles.py \
        --market kr --all --resume-after 005930 --limit 500 \
        --include-benchmark --horizon-bars 400

    uv run python scripts/backfill_daily_candles.py \
        --market us --top-market-cap 100 --horizon-bars 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.services.daily_candles.constants import (
    DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
    DAILY_CANDLE_BACKFILL_BARS_KR,
    DAILY_CANDLE_BACKFILL_BARS_US,
)
from app.services.daily_candles.crypto_identity import upbit_daily_candle_partition
from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    SyncTarget,
    _backfill_us_partition,
    _build_default_service,
)

logger = logging.getLogger(__name__)

_MARKET_DEFAULTS = {
    "kr": (MarketKey.KR, DAILY_CANDLE_BACKFILL_BARS_KR, "KRX"),
    "us": (MarketKey.US, DAILY_CANDLE_BACKFILL_BARS_US, "NASD"),
    "crypto": (MarketKey.CRYPTO, DAILY_CANDLE_BACKFILL_BARS_CRYPTO, None),
}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("양의 정수여야 합니다")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=list(_MARKET_DEFAULTS), required=True)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument(
        "--symbols",
        help="쉼표로 구분한 DB 정규 심볼(예: BRK-B가 아닌 BRK.B)",
    )
    targets.add_argument(
        "--all",
        action="store_true",
        help="DB에서 현재 활성 보통주 백필 대상을 조회",
    )
    targets.add_argument(
        "--top-market-cap",
        type=_positive_int,
        metavar="N",
        help="최신 시총 파티션에서 적격 보통주 상위 N개를 조회",
    )
    targets.add_argument(
        "--cohort-id",
        help="연구 코호트의 active+forced 멤버를 rank/symbol 순서로 조회",
    )
    parser.add_argument("--horizon-bars", type=_positive_int, default=None)
    parser.add_argument(
        "--partition",
        default=None,
        help=(
            "US 거래소 / KR 거래소 / 암호화폐 정규 파티션. "
            "기본값: NASD / KRX / 심볼에서 계산한 upbit_krw 또는 upbit_usdt. "
            "--all/--top-market-cap/--cohort-id에서는 사용할 수 없습니다."
        ),
    )
    parser.add_argument(
        "--resume-after",
        default=None,
        help="--all 결정 순서에서 이 심볼 다음부터 재개",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="--all에서 재개 필터 적용 후 처리할 최대 후보 수",
    )
    parser.add_argument("--include-benchmark", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _partition_for_symbol(
    *, market: MarketKey, symbol: str, requested_partition: str | None
) -> str:
    if market != MarketKey.CRYPTO:
        if not requested_partition:
            raise ValueError("암호화폐 외 백필에는 파티션이 필요합니다")
        if market == MarketKey.US:
            partition = _backfill_us_partition(requested_partition)
            if partition is None:
                raise ValueError(
                    f"지원하지 않는 미국 백필 파티션: {requested_partition!r}"
                )
            return partition
        return requested_partition

    canonical = upbit_daily_candle_partition(symbol)
    if requested_partition is not None and requested_partition != canonical:
        raise ValueError(
            "암호화폐 --partition은 심볼에서 계산한 정규 파티션과 일치해야 합니다: "
            f"symbol={symbol!r}, partition={requested_partition!r}, "
            f"expected={canonical!r}"
        )
    return canonical


def _explicit_targets(
    *,
    market: MarketKey,
    symbols_csv: str,
    requested_partition: str | None,
) -> list[SyncTarget]:
    symbols = [symbol.strip() for symbol in symbols_csv.split(",") if symbol.strip()]
    if not symbols:
        raise ValueError("--symbols에는 심볼이 하나 이상 있어야 합니다")
    return [
        SyncTarget(
            market=market,
            symbol=symbol,
            partition=_partition_for_symbol(
                market=market,
                symbol=symbol,
                requested_partition=requested_partition,
            ),
        )
        for symbol in symbols
    ]


def _benchmark_targets(market: MarketKey) -> tuple[SyncTarget, ...]:
    if market == MarketKey.KR:
        return (
            SyncTarget(market=market, symbol="KOSPI", partition="KRX"),
            SyncTarget(market=market, symbol="KOSDAQ", partition="KRX"),
        )
    if market == MarketKey.US:
        return (SyncTarget(market=market, symbol="SPY", partition="NASD"),)
    raise ValueError("--include-benchmark는 kr과 us에서만 지원합니다")


def _log_summary(
    *, targets_total: int, succeeded: int, fallback: int, failed_symbols: list[str]
) -> None:
    logger.info(
        "백필 요약 targets_total=%d succeeded=%d failed=%d "
        "fallback=%d failed_symbols=%s",
        targets_total,
        succeeded,
        len(failed_symbols),
        fallback,
        failed_symbols,
    )


async def _amain(args: argparse.Namespace) -> int:
    market_key, default_bars, default_partition = _MARKET_DEFAULTS[args.market]
    horizon = args.horizon_bars if args.horizon_bars is not None else default_bars

    bulk_mode = (
        args.all or args.top_market_cap is not None or args.cohort_id is not None
    )
    if bulk_mode:
        target_mode = (
            "--all"
            if args.all
            else "--top-market-cap"
            if args.top_market_cap is not None
            else "--cohort-id"
        )
        if args.market == "crypto":
            raise ValueError(f"{target_mode}은 kr과 us에서만 지원합니다")
        if args.partition is not None:
            raise ValueError(f"--partition은 {target_mode}과 함께 사용할 수 없습니다")
        if not args.all and (args.resume_after is not None or args.limit is not None):
            raise ValueError(
                f"--resume-after/--limit은 {target_mode}과 함께 사용할 수 없습니다"
            )
        explicit_targets: list[SyncTarget] = []
    else:
        if args.resume_after is not None or args.limit is not None:
            raise ValueError("--resume-after/--limit에는 --all이 필요합니다")
        requested_partition = args.partition or default_partition
        explicit_targets = _explicit_targets(
            market=market_key,
            symbols_csv=args.symbols,
            requested_partition=requested_partition,
        )

    benchmarks = _benchmark_targets(market_key) if args.include_benchmark else ()
    svc = await _build_default_service()
    try:
        if args.all:
            targets = await svc.resolve_backfill_targets(
                market=args.market,
                resume_after=args.resume_after,
                limit=args.limit,
            )
        elif args.top_market_cap is not None:
            targets = await svc.resolve_top_market_cap_targets(
                market=args.market,
                count=args.top_market_cap,
            )
        elif args.cohort_id is not None:
            targets = await svc.resolve_cohort_backfill_targets(
                market=args.market,
                cohort_id=args.cohort_id,
            )
        else:
            targets = explicit_targets
        for benchmark in benchmarks:
            if benchmark not in targets:
                targets.append(benchmark)

        if args.dry_run:
            for target in targets:
                logger.info("DRY RUN - 동기화 예정 %s", target)
            _log_summary(
                targets_total=len(targets),
                succeeded=0,
                fallback=0,
                failed_symbols=[],
            )
            return 0

        succeeded = 0
        fallback = 0
        failed_symbols: list[str] = []
        for target in targets:
            try:
                if target in benchmarks:
                    result = await svc.sync_benchmark(
                        market=market_key,
                        horizon_bars=horizon,
                        symbol=target.symbol,
                    )
                else:
                    result = await svc.sync_one(
                        target=target,
                        horizon_bars=horizon,
                    )
                if (
                    market_key == MarketKey.US
                    and getattr(result, "skipped_reason", None) is None
                ):
                    await svc.sync_us_adjusted_close(
                        target=target,
                        horizon_bars=horizon,
                    )
            except Exception as exc:
                failed_symbols.append(target.symbol)
                logger.exception(
                    "백필 실패 symbol=%s partition=%s error=%s",
                    target.symbol,
                    target.partition,
                    exc,
                )
                await svc.rollback()
                if not bulk_mode:
                    _log_summary(
                        targets_total=len(targets),
                        succeeded=succeeded,
                        fallback=fallback,
                        failed_symbols=failed_symbols,
                    )
                    raise
                continue

            if bool(getattr(result, "fallback_used", False)):
                fallback += 1
            skipped_reason = getattr(result, "skipped_reason", None)
            if skipped_reason:
                failed_symbols.append(target.symbol)
                logger.error(
                    "백필 결과 없음 symbol=%s partition=%s reason=%s",
                    target.symbol,
                    target.partition,
                    skipped_reason,
                )
                if not bulk_mode:
                    _log_summary(
                        targets_total=len(targets),
                        succeeded=succeeded,
                        fallback=fallback,
                        failed_symbols=failed_symbols,
                    )
                    raise RuntimeError(f"{target.symbol} 백필 실패: {skipped_reason}")
                continue

            succeeded += 1
            logger.info(
                "백필 완료 symbol=%s partition=%s upserted=%d fallback=%s",
                target.symbol,
                target.partition,
                result.rows_upserted,
                result.fallback_used,
            )

        _log_summary(
            targets_total=len(targets),
            succeeded=succeeded,
            fallback=fallback,
            failed_symbols=failed_symbols,
        )
        return 1 if failed_symbols else 0
    finally:
        await svc.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
