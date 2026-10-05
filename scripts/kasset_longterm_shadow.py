#!/usr/bin/env python3
"""KRX 장기 추세·재무성장 SHADOW 관측·성과 CLI(주문 없음).

observe: 실제 현재 시각의 마지막 완료 KRX 세션이 calendar상 ISO 주의 마지막 거래일이면
두 후보(trend_momentum, quality_growth_trend)의 상위 종목을
``review.kasset_longterm_shadow_runs``/``..._signals``에 기록한다. 주 중간 세션은
``not_applicable`` run만 남기고, 다음 세션 장 시작 이후에는 기록하지 않고 rejected run만
남긴다. 과거 날짜 재생 옵션은 없다. exit 0=completed/not_applicable, 2=rejected/failed.

report: 저장된 코호트의 20/60/120거래일 가상 성과(다음 세션 시가 가상 진입)·벤치마크·
초과수익을 읽기 전용으로 JSON 출력한다. 실제 체결·계좌 수익이 아니다.

Examples:
    python -m scripts.kasset_longterm_shadow observe
    python -m scripts.kasset_longterm_shadow report --since 2026-10-02 --cohorts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, date, datetime

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.automation.longterm_shadow_service import (
    build_longterm_shadow_report,
    observe_longterm_shadow,
)

logger = logging.getLogger(__name__)

_OBSERVE_OK = ("completed", "not_applicable")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KRX 장기 추세·재무성장 SHADOW 관측·가상 성과 조회 (주문 없음)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "observe",
        help="마지막 완료 세션이 주 마지막 거래일이면 코호트를 기록한다(다음 세션 장 시작 전만)",
    )
    report = commands.add_parser("report", help="저장 코호트의 가상 성과를 출력한다")
    report.add_argument(
        "--since", type=date.fromisoformat, required=True, help="코호트 세션 시작일"
    )
    report.add_argument(
        "--until", type=date.fromisoformat, default=None, help="코호트 세션 종료일"
    )
    report.add_argument(
        "--signals", action="store_true", help="신호별 상세 성과를 함께 출력"
    )
    report.add_argument(
        "--cohorts",
        action="store_true",
        help="후보×세션×horizon 코호트·벤치마크·초과수익 상세를 함께 출력",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="kasset-longterm-shadow")
    args = parse_args(argv)
    now = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as session:
            if args.command == "observe":
                payload = await observe_longterm_shadow(
                    session, now=now, trigger_source="cli"
                )
                exit_code = 0 if payload["status"] in _OBSERVE_OK else 2
            else:
                payload = await build_longterm_shadow_report(
                    session,
                    now=now,
                    since=args.since,
                    until=args.until,
                    include_signals=args.signals,
                    include_cohorts=args.cohorts,
                )
                exit_code = 0
    except Exception:
        logger.exception("kasset_longterm_shadow %s crashed", args.command)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
