#!/usr/bin/env python3
"""KRX 스윙 SHADOW 관측·성과 CLI(주문 없음).

observe: 실제 현재 시각의 마지막 완료 KRX 세션을 판정해
``review.kasset_swing_shadow_runs``/``..._signals``에 기록한다. 다음 세션 장 시작
이후에는 기록하지 않고 rejected run만 남긴다. 과거 날짜 재생 옵션은 없다.

report: 저장된 신호의 1/3/5/10거래일 가상 성과(다음 세션 시가 가상 진입)를
읽기 전용으로 JSON 출력한다. 실제 체결·계좌 수익이 아니다.

Examples:
    python -m scripts.kasset_swing_shadow observe
    python -m scripts.kasset_swing_shadow report --since 2026-10-05 --signals
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, date, datetime

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.extensions.kasset.automation.swing_shadow_service import (
    build_swing_shadow_report,
    observe_swing_shadow,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KRX 스윙 SHADOW 관측·가상 성과 조회 (주문 없음)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "observe", help="마지막 완료 세션 신호를 기록한다(다음 세션 장 시작 전만)"
    )
    report = commands.add_parser("report", help="저장 신호의 가상 성과를 출력한다")
    report.add_argument(
        "--since", type=date.fromisoformat, required=True, help="신호 세션 시작일"
    )
    report.add_argument(
        "--until", type=date.fromisoformat, default=None, help="신호 세션 종료일"
    )
    report.add_argument(
        "--signals", action="store_true", help="신호별 상세 성과를 함께 출력"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="kasset-swing-shadow")
    args = parse_args(argv)
    now = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as session:
            if args.command == "observe":
                payload = await observe_swing_shadow(
                    session, now=now, trigger_source="cli"
                )
                exit_code = 0 if payload["status"] == "completed" else 2
            else:
                payload = await build_swing_shadow_report(
                    session,
                    now=now,
                    since=args.since,
                    until=args.until,
                    include_signals=args.signals,
                )
                exit_code = 0
    except Exception:
        logger.exception("kasset_swing_shadow %s crashed", args.command)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
