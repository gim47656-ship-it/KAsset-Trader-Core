#!/usr/bin/env python3
"""KAsset PAPER readiness, backtest evidence, and promotion operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.extensions.kasset.automation.promotion_evidence import (  # noqa: E402
    FORWARD_PAPER_TRACK,
    HISTORICAL_PIT_TRACK,
    PromotionEvidenceBuildError,
    build_and_store_portfolio_evidence,
)
from app.extensions.kasset.automation.strategy_artifact import (  # noqa: E402
    current_strategy_artifact,
)
from app.extensions.kasset.automation.strategy_promotion import (  # noqa: E402
    PromotionState,
)
from app.extensions.kasset.automation.strategy_promotion_service import (  # noqa: E402
    PromotionCandidateTrustError,
    StrategyPromotionService,
)
from app.services.daily_candles.constants import (  # noqa: E402
    DAILY_CANDLE_BACKFILL_BARS_KR,
    DAILY_CANDLE_BACKFILL_BARS_US,
)
from app.services.daily_candles.readiness import (  # noqa: E402
    DailyCandlesReadiness,
    DailyCandlesReadinessService,
    MarketReadiness,
)

_BACKFILL_BARS = {
    "kr": DAILY_CANDLE_BACKFILL_BARS_KR,
    "us": DAILY_CANDLE_BACKFILL_BARS_US,
}

#: 각 blocker를 닫는 결정적 복구 동작. 값은 (동작 키, 사람이 읽는 이유)다.
#: 동작 키는 아래 ``_recovery_command``가 정확한 명령줄로 바꾼다.
_BLOCKER_RECOVERY: dict[str, tuple[str, str]] = {
    "cohort_not_found": ("cohort", "코호트가 없습니다."),
    "cohort_members_empty": ("cohort", "코호트에 active 멤버가 없습니다."),
    "cohort_member_count_mismatch": (
        "cohort",
        "코호트 멤버 수가 requested_size와 다릅니다.",
    ),
    "eligible_symbols_zero": ("backfill", "적격 종목이 0건입니다."),
    "insufficient_history": ("backfill", "252봉 미달 멤버가 있습니다."),
    "member_not_eligible": ("backfill", "품질 조건을 못 채운 멤버가 있습니다."),
    "stale_bar": ("backfill", "최신 완료 세션 일봉이 없는 멤버가 있습니다."),
    "ingest_lag_exceeded": ("backfill", "수집이 허용 지연 세션을 초과했습니다."),
    "expected_session_window_incomplete": (
        "backfill",
        "평가 창이 252세션에 미달합니다.",
    ),
    "missing_expected_trading_days": ("backfill", "창 안에 결측 거래일이 있습니다."),
    "adjustment_coverage_incomplete": (
        "backfill",
        "수정주가(분할/배당 반영) 근거가 부족합니다.",
    ),
    "benchmark_unavailable": ("benchmark", "벤치마크 일봉이 61봉 미달입니다."),
    "benchmark_source_missing": ("benchmark", "벤치마크 source 근거가 없습니다."),
    "benchmark_member_count_invalid": (
        "cohort",
        "코호트 benchmark 멤버가 정확히 1건이 아닙니다.",
    ),
    "calendar_unavailable": ("manual", "거래일 캘린더를 확인할 수 없습니다."),
    "future_bar": ("manual", "as_of 이후 타임스탬프 일봉이 저장돼 있습니다."),
    "duplicate_bar_timestamp": ("manual", "동일 타임스탬프 중복 일봉이 있습니다."),
    "invalid_ohlcv": ("manual", "OHLCV 무결성 위반 일봉이 있습니다."),
}


def _recovery_command(action: str, market: MarketReadiness) -> str:
    cohort_id = market.cohort.cohort_id if market.cohort is not None else None
    bars = _BACKFILL_BARS[market.market]
    if action == "cohort":
        source = "naver_finance" if market.market == "kr" else "yahoo"
        return (
            "uv run python scripts/build_kasset_research_cohort.py "
            f"--market {market.market} --valuation-source {source} "
            "--size 100 --commit"
        )
    if action == "backfill":
        if cohort_id is None:
            return _recovery_command("cohort", market)
        return (
            "uv run python scripts/backfill_daily_candles.py "
            f"--market {market.market} --cohort-id {cohort_id} "
            f"--horizon-bars {bars} --include-benchmark"
        )
    if action == "benchmark":
        return (
            "uv run python scripts/backfill_daily_candles.py "
            f"--market {market.market} --benchmark-only --horizon-bars {bars}"
        )
    return (
        f"{market.market} 일봉 저장소를 직접 점검해야 합니다: "
        f"public.{market.market}_candles_1d"
    )


def _recovery_plan(report: DailyCandlesReadiness) -> list[dict[str, object]]:
    """Map every daily-history blocker to one deterministic operator command."""

    plan: list[dict[str, object]] = []
    for market in report.markets:
        for blocker in market.daily_history_blockers:
            code = blocker.split(":", 1)[-1]
            action, why = _BLOCKER_RECOVERY.get(
                code, ("manual", "정의된 복구 동작이 없습니다.")
            )
            plan.append(
                {
                    "blocker": blocker,
                    "이유": why,
                    "명령": _recovery_command(action, market),
                }
            )
    return plan


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KAsset PAPER readiness/backtest/promotion operator CLI"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    readiness = commands.add_parser("readiness", help="DB 일봉 readiness를 조회합니다.")
    readiness.add_argument("--as-of", type=_aware_datetime)
    readiness.add_argument("--kr-cohort-id")
    readiness.add_argument("--us-cohort-id")

    backtest = commands.add_parser(
        "backtest-build",
        help="DB 일봉으로 diagnostics/walk-forward를 실행하고 registry에 저장합니다.",
    )
    backtest.add_argument("--as-of", type=_aware_datetime)
    backtest.add_argument(
        "--track",
        choices=[FORWARD_PAPER_TRACK, HISTORICAL_PIT_TRACK],
        default=FORWARD_PAPER_TRACK,
        help=(
            "근거 트랙. forward_paper는 수집 가능한 근거만 요구하고 미해결 "
            "historical 근거를 payload에 남깁니다. historical_pit은 "
            "point-in-time/상장폐지/KSD 원장까지 모두 증명돼야 통과합니다."
        ),
    )

    commands.add_parser("promotion-status", help="PAPER promotion 상태를 조회합니다.")

    draft = commands.add_parser(
        "promotion-draft", help="persisted candidate로 promotion draft를 생성합니다."
    )
    _candidate_reason_arguments(draft)

    approve = commands.add_parser(
        "promotion-approve", help="persisted candidate evidence를 승인합니다."
    )
    _candidate_reason_arguments(approve)

    for name, help_text in (
        ("promotion-suspend", "승인된 PAPER promotion을 중지합니다."),
        ("promotion-retire", "PAPER promotion을 폐기합니다."),
    ):
        lifecycle = commands.add_parser(name, help=help_text)
        lifecycle.add_argument("--strategy-key", required=True)
        lifecycle.add_argument("--version", required=True)
        lifecycle.add_argument("--reason", required=True)

    return parser.parse_args(argv)


def _candidate_reason_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-id", type=_positive_int, required=True)
    parser.add_argument("--reason", required=True)


async def run(args: argparse.Namespace) -> int:
    try:
        async with AsyncSessionLocal() as db:
            if args.command == "readiness":
                cohort_ids = {
                    market: cohort_id
                    for market, cohort_id in (
                        ("kr", args.kr_cohort_id),
                        ("us", args.us_cohort_id),
                    )
                    if cohort_id is not None
                }
                report = await DailyCandlesReadinessService(db).measure(
                    as_of=args.as_of,
                    cohort_ids=cohort_ids,
                )
                _print(
                    {
                        "명령": "readiness",
                        "cohortIds": {
                            item.market: (
                                item.cohort.cohort_id
                                if item.cohort is not None
                                else None
                            )
                            for item in report.markets
                        },
                        "dailyHistoryReady": report.daily_history_ready,
                        "promotionReady": report.promotion_ready,
                        "historicalEvidenceReady": (report.historical_evidence_ready),
                        "eligibleSymbolCount": report.eligible_symbol_count,
                        "dailyHistoryBlockers": list(report.daily_history_blockers),
                        "promotionBlockers": list(report.blockers),
                        "historicalEvidenceBlockers": list(
                            report.historical_evidence_blockers
                        ),
                        "unresolvedEvidence": list(report.unresolved_evidence),
                        "복구계획": _recovery_plan(report),
                        "reasons": list(report.reasons),
                        "evidence": asdict(report),
                    }
                )
                return 0 if report.promotion_ready else 2

            if args.command == "backtest-build":
                result = await build_and_store_portfolio_evidence(
                    db,
                    as_of=args.as_of,
                    track=args.track,
                )
                _print(
                    {
                        "명령": "backtest-build",
                        "track": result.raw_payload["promotionTrack"],
                        "experimentId": result.experiment.experiment_id,
                        "runId": result.run.id,
                        "promotionCandidateId": result.candidate.id,
                        "candidateStatus": result.candidate.status,
                        "candidateReasonCode": result.candidate.reason_code,
                        "unresolvedEvidence": list(
                            result.raw_payload["readiness"]["unresolvedEvidence"]
                        ),
                        "artifactFingerprint": result.raw_payload["strategy"][
                            "artifactFingerprint"
                        ],
                        "metrics": result.metrics.as_snapshot(),
                    }
                )
                return 0

            service = StrategyPromotionService(db)
            if args.command == "promotion-status":
                promotions = await service.list_status()
                _print(
                    {
                        "명령": "promotion-status",
                        "runtimeArtifactFingerprint": current_strategy_artifact().fingerprint,
                        "promotions": [item.as_evidence() for item in promotions],
                    }
                )
                return 0

            now = datetime.now(UTC)
            if args.command == "promotion-draft":
                promotion = await service.create_draft(
                    args.candidate_id,
                    at=now,
                    operator_reason=args.reason,
                )
            elif args.command == "promotion-approve":
                promotion = await service.approve_candidate(
                    args.candidate_id,
                    at=now,
                    operator_reason=args.reason,
                )
            elif args.command == "promotion-suspend":
                promotion = await service.transition(
                    args.strategy_key,
                    args.version,
                    PromotionState.PAPER_SUSPENDED,
                    at=now,
                    operator_reason=args.reason,
                )
            elif args.command == "promotion-retire":
                promotion = await service.transition(
                    args.strategy_key,
                    args.version,
                    PromotionState.RETIRED,
                    at=now,
                    operator_reason=args.reason,
                )
            else:  # pragma: no cover - argparse closes the command vocabulary
                raise ValueError(f"지원하지 않는 명령: {args.command}")
            _print({"명령": args.command, "promotion": promotion.as_evidence()})
            return 0
    except (
        PromotionCandidateTrustError,
        PromotionEvidenceBuildError,
        ValueError,
    ) as exc:
        _print({"성공": False, "명령": args.command, "오류": str(exc)})
        return 2


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of는 ISO-8601 이어야 합니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of에는 timezone이 필요합니다.")
    return parsed.astimezone(UTC)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("양의 정수가 필요합니다.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("양의 정수가 필요합니다.")
    return parsed


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def _print(value: object) -> None:
    print(json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True))


async def main(argv: list[str] | None = None) -> int:
    return await run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
