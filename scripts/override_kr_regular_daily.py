#!/usr/bin/env python3
"""지정 거래일의 KR 저장 일봉을 Toss 1분봉 정규장 집계로 보정한다."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.services.daily_candles.kr_regular_daily import (
    latest_completed_kr_session_date,
)
from app.services.daily_candles.sync_service import _build_default_service
from app.services.market_events.session_calendar import is_trading_session
from app.tasks.kasset_market_events_tasks import _watchlist_kr_symbols

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value!r}"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=_iso_date,
        required=True,
        help="보정할 완료된 KST 거래일(YYYY-MM-DD)",
    )
    parser.add_argument(
        "--symbols",
        help="쉼표로 구분한 심볼. 생략하면 scheduled task와 같은 KR 관심종목을 사용합니다.",
    )
    return parser


def _explicit_symbols(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return sorted(
        {
            normalized
            for symbol in value.split(",")
            if (normalized := symbol.strip().upper())
        }
    )


def _is_completed_session_date(*, session_date: date, now: datetime) -> bool:
    """명시 날짜가 현재 시각 기준 완료된 KRX 거래일인지 확인한다."""

    latest_completed = latest_completed_kr_session_date(now)
    return (
        latest_completed is not None
        and session_date <= latest_completed
        and is_trading_session("kr", session_date)
    )


async def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging_and_sentry(service_name="kr-regular-daily-override")

    async def _job() -> int:
        reference_now = now or datetime.now(KST)
        if not _is_completed_session_date(
            session_date=args.date,
            now=reference_now,
        ):
            logger.error(
                "KR 정규장 일봉 override 거부: 완료되지 않은 거래일 date=%s",
                args.date,
            )
            return 1

        symbols = _explicit_symbols(args.symbols)
        if symbols is None:
            symbols = await _watchlist_kr_symbols()
        if not symbols:
            logger.error("KR 정규장 일봉 override 대상 심볼이 없습니다")
            return 1

        service = await _build_default_service()
        try:
            result = await service.override_kr_regular_daily(
                symbols=symbols,
                session_date=args.date,
            )
        finally:
            await service.close()

        summary = result.as_dict()
        if result.rows_upserted <= 0:
            logger.error("KR 정규장 일봉 override 미적용: %s", summary)
            return 1
        logger.info("KR 정규장 일봉 override 완료: %s", summary)
        return 0

    return await run_async_job(_job, process="override_kr_regular_daily")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
