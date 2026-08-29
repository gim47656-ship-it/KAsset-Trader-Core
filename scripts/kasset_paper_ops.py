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
from app.services.daily_candles.readiness import (  # noqa: E402
    DailyCandlesReadinessService,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KAsset PAPER readiness/backtest/promotion operator CLI"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    readiness = commands.add_parser("readiness", help="DB 일봉 readiness를 조회합니다.")
    readiness.add_argument("--as-of", type=_aware_datetime)

    backtest = commands.add_parser(
        "backtest-build",
        help="DB 일봉으로 diagnostics/walk-forward를 실행하고 registry에 저장합니다.",
    )
    backtest.add_argument("--as-of", type=_aware_datetime)

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
                report = await DailyCandlesReadinessService(db).measure(
                    as_of=args.as_of
                )
                _print(
                    {
                        "명령": "readiness",
                        "promotionReady": report.promotion_ready,
                        "eligibleSymbolCount": report.eligible_symbol_count,
                        "blockers": list(report.blockers),
                        "reasons": list(report.reasons),
                        "evidence": asdict(report),
                    }
                )
                return 0 if report.promotion_ready else 2

            if args.command == "backtest-build":
                result = await build_and_store_portfolio_evidence(
                    db,
                    as_of=args.as_of,
                )
                _print(
                    {
                        "명령": "backtest-build",
                        "experimentId": result.experiment.experiment_id,
                        "runId": result.run.id,
                        "promotionCandidateId": result.candidate.id,
                        "candidateStatus": result.candidate.status,
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
